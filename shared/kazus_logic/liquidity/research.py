"""Phase-8 statistical edge discovery.

This is the heavy-lift module: pulls bulk rows out of liquidity_samples /
liquidity_alert_history / liquidity_crossex_history with one query
apiece, then does the actual stats (correlation, lift, transition
matrix, lag-corr, rarity) in Python. No new tables, no worker job — the
endpoints just call into here on demand.

Design rules:
  - Every public function takes a Session. The route handler is a
    one-liner around it.
  - Heavy queries are time-windowed (`since_ms`) and capped (`max_rows`)
    so they can never run away on a busy DB.
  - Output is plain dict / list of dicts for trivial JSON serialization.
  - Stats math is hand-written, not numpy, to keep the dep surface flat;
    the row counts are small after time-bucketing.
"""

from __future__ import annotations

import functools
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


# ── TTL-cache for heavy research functions ──────────────────────────────
#
# Background: production audit on 2026-05-23 found that `intelligence_synthesis`
# (~30s/call) was being polled every 30s from Coordination.tsx, single tab
# saturating the DB pool. `sanity_audit` was also calling several heavy
# functions on every 60s polling cycle. Wrapping the heavy functions in a
# per-process TTL cache eliminates 95%+ of the redundant work because the
# windows under analysis (7-30 days of history) don't move meaningfully in
# 5 minutes — staleness up to TTL_RESEARCH_S is invisible to operators.
#
# Invalidation is purely time-based: there is no write path that needs to
# punch the cache, since these are read-only aggregates. If a fresher read
# is ever needed, callers can lower the TTL per-call or bypass the wrapper.

TTL_RESEARCH_S = 300.0  # 5 minutes — well above polling cadences

_cache_lock = threading.Lock()
_cache_stats: Dict[str, Dict[str, int]] = {}  # fn → {hits, misses}


def _ttl_cached(ttl_seconds: float = TTL_RESEARCH_S) -> Callable:
    """Decorator: wrap a research function so the result is cached per
    non-Session args for ``ttl_seconds``. The first positional arg is
    assumed to be ``db: Session`` and is excluded from the cache key.

    Thread-safe but not stampede-proof — two concurrent misses will both
    compute and the later write wins. At our polling cadences (30-60s)
    this is acceptable; if synthesis ever attracts genuine fan-out, swap
    in a per-key lock or single-flight wrapper.
    """
    def decorator(fn: Callable) -> Callable:
        cache: Dict[Tuple, Tuple[float, Any]] = {}
        name = fn.__name__
        _cache_stats[name] = {"hits": 0, "misses": 0}

        @functools.wraps(fn)
        def wrapper(db: Session, *args, **kwargs):
            try:
                key = (args, tuple(sorted(kwargs.items())))
            except TypeError:
                # un-hashable kwarg — bypass cache
                return fn(db, *args, **kwargs)
            now = time.time()
            with _cache_lock:
                entry = cache.get(key)
                if entry and entry[0] > now:
                    _cache_stats[name]["hits"] += 1
                    return entry[1]
                _cache_stats[name]["misses"] += 1
            value = fn(db, *args, **kwargs)
            with _cache_lock:
                cache[key] = (now + ttl_seconds, value)
            return value

        wrapper._cache = cache  # type: ignore[attr-defined]  # exposed for inspection
        wrapper._cached_fn = fn  # type: ignore[attr-defined]  # for bypass when needed
        return wrapper
    return decorator


def cache_stats() -> Dict[str, Dict[str, int]]:
    """Return per-function {hits, misses} counters for the admin/runtime
    health endpoint. Cheap snapshot — no lock held while reading."""
    return {k: dict(v) for k, v in _cache_stats.items()}


# ── Metric set used by the analytics ─────────────────────────────────────
# Restricting the universe keeps interaction matrices readable AND keeps
# the wide-row pivot cheap. Add metrics here once they're stable; don't
# expose every WS-momentary signal — they swamp the matrix with noise.
ANALYTICS_METRICS: Tuple[str, ...] = (
    "spread",
    "credible_depth",
    "obi",
    "atr_liquidity",
    "liq_stress",
    "funding_z",
    "oi_delta_1h",
    "resiliency_score",
    "impact_score",
    "fragility_score",
)


# ══════════════════════════════════════════════════════════════════════════
# Interaction Matrix
# ══════════════════════════════════════════════════════════════════════════


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 8:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom = math.sqrt(sxx * syy)
    if denom <= 1e-12:
        return None
    return sxy / denom


def interaction_matrix(
    db: Session,
    since_ms: int,
    max_rows_per_metric: int = 5000,
    bucket_minutes: int = 5,
) -> dict:
    """Pearson correlation matrix over ANALYTICS_METRICS.

    Strategy: for each metric, pull at most `max_rows_per_metric` recent
    samples per symbol, time-bucketed so we line up rows across metrics.
    The pivot is done in Python because Postgres pivots are awkward and
    the row count is bounded.
    """
    bucket_ms = bucket_minutes * 60_000
    metrics = list(ANALYTICS_METRICS)
    # Pull one bucketed value per (symbol, bucket, metric) — average
    # within bucket so 1-sec WS jitter doesn't dominate. LIMIT keeps the
    # query bounded even for very long histories.
    rows = db.execute(
        text(
            """
            WITH src AS (
              SELECT
                symbol,
                metric,
                (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
                AVG(value) AS v
              FROM liquidity_samples
              WHERE metric = ANY(:metrics)
                AND ts >= :since_ms
                AND value IS NOT NULL
              GROUP BY symbol, metric, bucket_ts
            )
            SELECT symbol, bucket_ts, metric, v
            FROM src
            ORDER BY bucket_ts DESC, symbol, metric
            LIMIT :cap
            """
        ),
        {
            "bucket_ms": bucket_ms,
            "metrics": metrics,
            "since_ms": since_ms,
            "cap": max_rows_per_metric * len(metrics),
        },
    ).fetchall()

    # Pivot to (symbol, bucket_ts) -> {metric -> v}
    pivot: Dict[Tuple[str, int], Dict[str, float]] = {}
    for r in rows:
        key = (r.symbol, int(r.bucket_ts))
        pivot.setdefault(key, {})[r.metric] = float(r.v)

    # Per-metric value lists, only counting rows where BOTH metrics are
    # populated (handled per-pair below to maximise sample count).
    samples_by_metric: Dict[str, List[Tuple[Tuple[str, int], float]]] = {m: [] for m in metrics}
    for key, mv in pivot.items():
        for m, v in mv.items():
            samples_by_metric[m].append((key, v))

    matrix: List[Dict[str, object]] = []
    for a in metrics:
        for b in metrics:
            if a == b:
                matrix.append({"a": a, "b": b, "r": 1.0, "n": len(samples_by_metric[a])})
                continue
            # Intersect on key for a fair pairwise sample.
            xs: List[float] = []
            ys: List[float] = []
            map_a = {k: v for k, v in samples_by_metric[a]}
            for k, vb in samples_by_metric[b]:
                va = map_a.get(k)
                if va is None:
                    continue
                xs.append(va)
                ys.append(vb)
            r = _pearson(xs, ys)
            matrix.append({"a": a, "b": b, "r": r, "n": len(xs)})

    return {
        "metrics": metrics,
        "cells": matrix,
        "since_ms": since_ms,
        "bucket_minutes": bucket_minutes,
    }


# ══════════════════════════════════════════════════════════════════════════
# Edge ranking (tertile-coded combos → downstream-alert lift)
# ══════════════════════════════════════════════════════════════════════════


# Picking 3 metrics keeps the combo space at 3^3 = 27, which is small
# enough to show all rows in the UI. The selection is biased toward
# signals that intuitively contribute to instability — resiliency,
# fragility, funding_z. Editing this set is the main way to explore.
EDGE_METRICS: Tuple[str, ...] = ("resiliency_score", "fragility_score", "funding_z")

# Outcome window — does an alert fire in the next OUTCOME_WINDOW_MS for
# the same symbol? Long enough to give the engine a chance to react,
# short enough that the "outcome" isn't a different event entirely.
OUTCOME_WINDOW_MS = 60 * 60_000


def _tertile(v: float, lo: float, hi: float) -> str:
    if v <= lo:
        return "low"
    if v >= hi:
        return "high"
    return "mid"


def _quantile(sorted_xs: List[float], p: float) -> float:
    if not sorted_xs:
        return 0.0
    idx = max(0, min(len(sorted_xs) - 1, int(p * (len(sorted_xs) - 1))))
    return sorted_xs[idx]


def edge_ranking(
    db: Session,
    since_ms: int,
    alert_kind: Optional[str] = None,
    bucket_minutes: int = 15,
) -> dict:
    """Tertile-code (resiliency, fragility, funding_z) per sample bucket;
    measure the alert rate within OUTCOME_WINDOW_MS after each bucket;
    rank combos by lift = combo_rate / base_rate.

    A "combo" is (resiliency-bin, fragility-bin, funding-bin); 27 cells.
    `alert_kind` filter restricts the outcome to one specific alert kind
    (e.g. only LIQ_CASCADE) — if omitted, ANY alert counts.
    """
    bucket_ms = bucket_minutes * 60_000

    rows = db.execute(
        text(
            """
            SELECT symbol, metric, (ts / :bucket_ms) * :bucket_ms AS bucket_ts, AVG(value) AS v
            FROM liquidity_samples
            WHERE metric = ANY(:metrics)
              AND ts >= :since_ms
              AND value IS NOT NULL
            GROUP BY symbol, metric, bucket_ts
            ORDER BY symbol, bucket_ts
            """
        ),
        {"bucket_ms": bucket_ms, "metrics": list(EDGE_METRICS), "since_ms": since_ms},
    ).fetchall()

    # Pivot
    pivot: Dict[Tuple[str, int], Dict[str, float]] = {}
    for r in rows:
        pivot.setdefault((r.symbol, int(r.bucket_ts)), {})[r.metric] = float(r.v)

    # Global tertile cutoffs per metric (across the visible cohort + window).
    by_metric: Dict[str, List[float]] = {m: [] for m in EDGE_METRICS}
    for mv in pivot.values():
        for m, v in mv.items():
            by_metric[m].append(v)
    for m in by_metric:
        by_metric[m].sort()
    cuts: Dict[str, Tuple[float, float]] = {}
    for m, xs in by_metric.items():
        cuts[m] = (_quantile(xs, 1 / 3), _quantile(xs, 2 / 3))

    # Pull alerts to map outcomes onto buckets.
    alert_q = """
        SELECT symbol, started_at_ms, kind
        FROM liquidity_alert_history
        WHERE started_at_ms >= :since_ms
    """
    params: dict = {"since_ms": since_ms}
    if alert_kind:
        alert_q += " AND kind = :alert_kind"
        params["alert_kind"] = alert_kind
    alert_rows = db.execute(text(alert_q), params).fetchall()
    alerts_by_sym: Dict[str, List[int]] = {}
    for ar in alert_rows:
        alerts_by_sym.setdefault(ar.symbol, []).append(int(ar.started_at_ms))
    for sym in alerts_by_sym:
        alerts_by_sym[sym].sort()

    def has_outcome(sym: str, bucket_ts: int) -> bool:
        arr = alerts_by_sym.get(sym, [])
        if not arr:
            return False
        # Binary search for any alert in [bucket_ts, bucket_ts + window].
        lo = 0
        hi = len(arr) - 1
        target = bucket_ts
        end = bucket_ts + OUTCOME_WINDOW_MS
        # Find first alert >= target
        pos = len(arr)
        while lo <= hi:
            mid = (lo + hi) // 2
            if arr[mid] >= target:
                pos = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return pos < len(arr) and arr[pos] <= end

    combos: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    total_buckets = 0
    total_with_outcome = 0
    for (sym, bts), mv in pivot.items():
        if not all(m in mv for m in EDGE_METRICS):
            continue
        bins = tuple(_tertile(mv[m], *cuts[m]) for m in EDGE_METRICS)
        outcome = has_outcome(sym, bts)
        c = combos.setdefault(bins, {"total": 0, "outcomes": 0})
        c["total"] += 1
        if outcome:
            c["outcomes"] += 1
        total_buckets += 1
        if outcome:
            total_with_outcome += 1

    base_rate = total_with_outcome / total_buckets if total_buckets > 0 else 0.0

    result_rows: List[Dict[str, object]] = []
    for bins, agg in combos.items():
        total = agg["total"]
        outcomes = agg["outcomes"]
        rate = outcomes / total if total > 0 else 0.0
        lift = rate / base_rate if base_rate > 0 else None
        result_rows.append({
            "resiliency": bins[0],
            "fragility": bins[1],
            "funding_z": bins[2],
            "total": total,
            "outcomes": outcomes,
            "rate": rate,
            "lift": lift,
        })
    # Sort by lift desc, but require at least 10 supporting observations
    # so we don't promote a 1-out-of-2 fluke to the top.
    result_rows.sort(
        key=lambda r: (
            -(r["lift"] or 0) if (r["total"] or 0) >= 10 else 0,
            -(r["total"] or 0),
        ),
    )
    return {
        "since_ms": since_ms,
        "bucket_minutes": bucket_minutes,
        "alert_kind": alert_kind,
        "metrics": list(EDGE_METRICS),
        "tertile_cuts": {m: {"low_high": cuts[m]} for m in EDGE_METRICS},
        "total_buckets": total_buckets,
        "base_rate": base_rate,
        "outcome_window_ms": OUTCOME_WINDOW_MS,
        "combos": result_rows,
    }


# ══════════════════════════════════════════════════════════════════════════
# Regime outcomes (transitions + durations + collapse probability)
# ══════════════════════════════════════════════════════════════════════════


CASCADE_REGIMES = {"LIQUIDATION_CASCADE", "UNSTABLE_MARKET"}


def regime_outcomes(db: Session, since_ms: int) -> dict:
    """For each regime: transition matrix, average duration between
    transitions, collapse probability (P(next regime is CASCADE-class))."""
    rows = db.execute(
        text(
            """
            SELECT symbol, started_at_ms, regime
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since_ms
            ORDER BY symbol, started_at_ms
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    transitions: Dict[Tuple[str, str], int] = {}
    durations: Dict[str, List[int]] = {}   # ms spent before transitioning OUT
    counts: Dict[str, int] = {}
    last_by_sym: Dict[str, Tuple[str, int]] = {}

    for r in rows:
        counts[r.regime] = counts.get(r.regime, 0) + 1
        prev = last_by_sym.get(r.symbol)
        if prev is not None and prev[0] != r.regime:
            transitions[(prev[0], r.regime)] = transitions.get((prev[0], r.regime), 0) + 1
            durations.setdefault(prev[0], []).append(int(r.started_at_ms) - prev[1])
        last_by_sym[r.symbol] = (r.regime, int(r.started_at_ms))

    # Per-regime stats
    regimes: List[Dict[str, object]] = []
    for regime, total in counts.items():
        out_transitions = {b: c for (a, b), c in transitions.items() if a == regime}
        out_total = sum(out_transitions.values())
        prob_next: Dict[str, float] = {}
        for b, c in out_transitions.items():
            prob_next[b] = c / out_total if out_total > 0 else 0.0
        collapse_prob = sum(p for b, p in prob_next.items() if b in CASCADE_REGIMES)
        ds = durations.get(regime, [])
        avg_dur_ms = sum(ds) / len(ds) if ds else None
        regimes.append({
            "regime": regime,
            "count": total,
            "out_transitions": out_total,
            "transitions": prob_next,
            "collapse_prob": collapse_prob,
            "avg_duration_ms": avg_dur_ms,
        })

    regimes.sort(key=lambda r: r["count"], reverse=True)

    return {
        "since_ms": since_ms,
        "regimes": regimes,
        "transition_pairs": [
            {"from_regime": a, "to_regime": b, "count": c}
            for (a, b), c in sorted(transitions.items(), key=lambda kv: -kv[1])
        ],
    }


# ══════════════════════════════════════════════════════════════════════════
# Venue leadership (which exchange moves first on a given metric)
# ══════════════════════════════════════════════════════════════════════════


VENUE_METRICS_TO_FIELD = {
    "spread": "spread_fraction",
    "mid_price": "mid_price",
    "funding": "funding_rate",
    "open_interest": "open_interest_usd",
}


def venue_leadership(db: Session, since_ms: int, max_lag_s: int = 120) -> dict:
    """For each comparable metric, compute the lag (in seconds) that
    maximises the cross-correlation between Binance and Bybit series for
    each symbol; then aggregate (mean lag, share with positive lead).

    A positive lag means the OTHER venue (Bybit here) led Binance; a
    negative lag means Binance led. We bucket by symbol so a single
    illiquid coin can't dominate the aggregate.
    """
    rows = db.execute(
        text(
            """
            SELECT symbol, exchange, ts_ms, spread_fraction, mid_price, funding_rate, open_interest_usd
            FROM liquidity_crossex_history
            WHERE ts_ms >= :since_ms
            ORDER BY symbol, exchange, ts_ms
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    # Group by (symbol, exchange) -> list of (ts_ms, dict)
    series: Dict[Tuple[str, str], List[Tuple[int, dict]]] = {}
    for r in rows:
        series.setdefault((r.symbol, r.exchange), []).append((
            int(r.ts_ms),
            {
                "spread_fraction": r.spread_fraction,
                "mid_price": r.mid_price,
                "funding_rate": r.funding_rate,
                "open_interest_usd": r.open_interest_usd,
            },
        ))

    # Build symbol-keyed pairs (binance, other_exchange).
    symbols = set(s for (s, _) in series.keys())
    aggregates: Dict[str, Dict[str, List[float]]] = {}    # metric -> exchange -> list of lags (s)
    pair_count = 0
    for sym in symbols:
        ref = series.get((sym, "binance")) or []
        if not ref:
            continue
        for (s2, ex2), other in series.items():
            if s2 != sym or ex2 == "binance":
                continue
            pair_count += 1
            for metric in VENUE_METRICS_TO_FIELD:
                lag = _best_lag(ref, other, VENUE_METRICS_TO_FIELD[metric], max_lag_s)
                if lag is None:
                    continue
                aggregates.setdefault(metric, {}).setdefault(ex2, []).append(lag)

    metrics_out: List[dict] = []
    for metric, by_ex in aggregates.items():
        per_venue: List[dict] = []
        for ex, lags in by_ex.items():
            if not lags:
                continue
            mean = sum(lags) / len(lags)
            led = sum(1 for x in lags if x > 1) / len(lags)
            lagged = sum(1 for x in lags if x < -1) / len(lags)
            per_venue.append({
                "exchange": ex,
                "samples": len(lags),
                "mean_lag_s": mean,
                "share_led_binance": led,
                "share_lagged_binance": lagged,
            })
        metrics_out.append({"metric": metric, "venues": per_venue})

    return {
        "since_ms": since_ms,
        "pair_count": pair_count,
        "max_lag_s": max_lag_s,
        "metrics": metrics_out,
    }


def _best_lag(
    ref: List[Tuple[int, dict]],
    other: List[Tuple[int, dict]],
    field: str,
    max_lag_s: int,
) -> Optional[float]:
    """Find the integer-second lag that maximises Pearson correlation.

    Both series are sparse (one row per crossex API call), so we resample
    to a uniform 5s grid before correlating. Skip the pair entirely if
    we don't have enough overlap.
    """
    bucket_ms = 5000
    grid_ref: Dict[int, float] = {}
    grid_other: Dict[int, float] = {}
    for ts, vals in ref:
        v = vals.get(field)
        if v is None:
            continue
        grid_ref[(ts // bucket_ms) * bucket_ms] = float(v)
    for ts, vals in other:
        v = vals.get(field)
        if v is None:
            continue
        grid_other[(ts // bucket_ms) * bucket_ms] = float(v)
    common = sorted(set(grid_ref) & set(grid_other))
    if len(common) < 12:
        return None
    ref_arr = [grid_ref[t] for t in common]
    other_arr = [grid_other[t] for t in common]
    # Try integer second lags in [-max_lag_s, +max_lag_s], measured in
    # 5s buckets, which keeps the inner loop short.
    best_lag = 0
    best_r = -2.0
    for step in range(-max_lag_s // 5, max_lag_s // 5 + 1):
        if step == 0:
            xs, ys = ref_arr, other_arr
        elif step > 0:
            xs = ref_arr[:-step]
            ys = other_arr[step:]
        else:
            xs = ref_arr[-step:]
            ys = other_arr[:step]
        r = _pearson(xs, ys)
        if r is None:
            continue
        if r > best_r:
            best_r = r
            best_lag = step
    return best_lag * 5.0 if best_r > 0 else None


# ══════════════════════════════════════════════════════════════════════════
# Meta-state rarity
# ══════════════════════════════════════════════════════════════════════════


META_METRICS: Tuple[str, ...] = (
    "spread", "credible_depth", "obi", "liq_stress",
    "funding_z", "oi_delta_1h", "fragility_score", "resiliency_score",
)


def meta_state_rarity(db: Session, symbol: str, lookback_days: int = 30) -> dict:
    """How rare is the current state vs history?

    Build a fingerprint from the latest non-null value per metric for the
    symbol. Then compute the distance distribution from EVERY historical
    bucketed fingerprint (within `lookback_days`) to that fingerprint.
    Rarity = the percentile rank — close to 100 means almost no
    historical state has been this similar.
    """
    sym = symbol.strip().upper()
    if not sym:
        return {"symbol": sym, "rarity_pct": None, "matches": []}

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    ref_rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (metric) metric, value, ts
            FROM liquidity_samples
            WHERE symbol = :sym
              AND metric = ANY(:metrics)
              AND value IS NOT NULL
            ORDER BY metric, ts DESC
            """
        ),
        {"sym": sym, "metrics": list(META_METRICS)},
    ).fetchall()
    if not ref_rows:
        return {"symbol": sym, "rarity_pct": None, "reference": None, "n": 0}
    reference = {r.metric: float(r.value) for r in ref_rows}
    scales = {m: max(abs(v), 1e-6) for m, v in reference.items()}

    bucket_ms = 5 * 60_000
    cand_rows = db.execute(
        text(
            """
            SELECT (ts / :bucket_ms) * :bucket_ms AS bucket_ts, metric, AVG(value) AS v
            FROM liquidity_samples
            WHERE symbol = :sym
              AND metric = ANY(:metrics)
              AND ts >= :since_ms
              AND value IS NOT NULL
            GROUP BY bucket_ts, metric
            """
        ),
        {"bucket_ms": bucket_ms, "sym": sym, "metrics": list(META_METRICS), "since_ms": since_ms},
    ).fetchall()
    fingerprints: Dict[int, Dict[str, float]] = {}
    for r in cand_rows:
        fingerprints.setdefault(int(r.bucket_ts), {})[r.metric] = float(r.v)

    distances: List[float] = []
    for ts, vec in fingerprints.items():
        if len(vec) < max(2, len(reference) // 2):
            continue
        ssq = 0.0
        for m, ref_v in reference.items():
            v = vec.get(m)
            if v is None:
                continue
            ssq += ((v - ref_v) / scales[m]) ** 2
        distances.append(math.sqrt(ssq))
    if not distances:
        return {"symbol": sym, "rarity_pct": None, "reference": reference, "n": 0}
    distances.sort()
    # Rarity = percentile of the median of the distances vs zero — but
    # what we actually want is "what fraction of history is similar to
    # NOW?". Fraction of historical fingerprints within `threshold` of
    # the current state, normalised to a 0..100 "rarity" gauge.
    threshold = 1.5     # subjective; tuned so a calm market reads ≈30, a tail event ≈85+
    similar = sum(1 for d in distances if d <= threshold)
    similarity_share = similar / len(distances)
    # Rarity = 100 × (1 − similarity_share), capped/floored.
    rarity = max(0.0, min(100.0, 100.0 * (1.0 - similarity_share)))
    return {
        "symbol": sym,
        "rarity_pct": rarity,
        "n": len(distances),
        "similar_count": similar,
        "threshold": threshold,
        "reference": reference,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-9 — Operational Intelligence
# ══════════════════════════════════════════════════════════════════════════
#
# Built on top of liquidity_alert_history + liquidity_samples. The Phase-7
# table now has enough rows that "is this signal still working?" is a
# meaningful question, and these functions are how we ask it.
#
# Naming convention: each function returns a dict, no Pydantic — the
# route handler wraps the result. That keeps these usable from tests
# without spinning up FastAPI.

from collections import defaultdict


# ── Edge persistence ──────────────────────────────────────────────────────


def edge_persistence(
    db: Session,
    since_ms: int,
    window_days: int = 7,
    alert_kind: Optional[str] = None,
) -> dict:
    """Rolling precision per alert kind across `window_days`-wide buckets.

    Returns the time-series + a linear-regression slope ("degradation
    rate") + an estimated half-life (how many days until precision falls
    to half of the most-recent value at the observed slope). Half-life
    is only emitted when the slope is meaningfully negative — otherwise
    the value would be meaningless ("infinite half-life" is not signal).
    """
    bucket_ms = window_days * 24 * 3600 * 1000
    query = """
        SELECT
          kind,
          (started_at_ms / :bucket_ms) * :bucket_ms AS bucket_ts,
          COUNT(*) AS total,
          SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
          SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise,
          AVG(priority) AS avg_priority,
          AVG(confidence) AS avg_confidence
        FROM liquidity_alert_history
        WHERE started_at_ms >= :since_ms
    """
    params: dict = {"bucket_ms": bucket_ms, "since_ms": since_ms}
    if alert_kind:
        query += " AND kind = :kind"
        params["kind"] = alert_kind
    query += " GROUP BY kind, bucket_ts ORDER BY kind, bucket_ts"

    rows = db.execute(text(query), params).fetchall()

    by_kind: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        ft = int(r.ft or 0)
        ns = int(r.noise or 0)
        resolved = ft + ns
        precision = (ft / resolved) if resolved > 0 else None
        by_kind[r.kind].append({
            "bucket_ts": int(r.bucket_ts),
            "total": int(r.total or 0),
            "resolved": resolved,
            "precision": precision,
            "avg_priority": float(r.avg_priority) if r.avg_priority is not None else None,
            "avg_confidence": float(r.avg_confidence) if r.avg_confidence is not None else None,
        })

    out_kinds: List[dict] = []
    for kind, series in by_kind.items():
        # Linear regression of precision over bucket index — slope is the
        # per-window delta. Convert to per-day for human readability.
        pts = [(i, s["precision"]) for i, s in enumerate(series) if s["precision"] is not None]
        slope = None
        intercept = None
        half_life_days = None
        if len(pts) >= 3:
            n = len(pts)
            mx = sum(p[0] for p in pts) / n
            my = sum(p[1] for p in pts) / n
            num = sum((p[0] - mx) * (p[1] - my) for p in pts)
            den = sum((p[0] - mx) ** 2 for p in pts)
            if den > 0:
                slope = num / den
                intercept = my - slope * mx
        # Per-window slope → per-day slope.
        slope_per_day = slope / window_days if slope is not None else None
        # Half-life only when slope_per_day is meaningfully negative.
        if slope_per_day is not None and slope_per_day < -1e-6:
            current = pts[-1][1] if pts else None
            if current is not None and current > 0:
                # current + slope_per_day * t = current / 2  =>  t = (current/2) / -slope
                half_life_days = (current / 2.0) / (-slope_per_day)

        out_kinds.append({
            "kind": kind,
            "series": series,
            "slope_per_window": slope,
            "intercept": intercept,
            "slope_per_day": slope_per_day,
            "half_life_days": half_life_days,
            "latest_precision": pts[-1][1] if pts else None,
        })

    out_kinds.sort(key=lambda k: -(k["latest_precision"] or 0))

    return {
        "since_ms": since_ms,
        "window_days": window_days,
        "alert_kind": alert_kind,
        "kinds": out_kinds,
    }


# ── Signal reliability scoring ────────────────────────────────────────────


def signal_reliability(db: Session, since_ms: int) -> dict:
    """Composite reliability per alert kind.

    Reliability blends:
      * accuracy   — overall precision over the window
      * stability  — 1 − stdev(weekly precision)
      * regime consistency — 1 − stdev(precision broken down by regime)
      * sample size — log-scaled, so 5 alerts can't outweigh 500.

    Output state buckets:
      ≥ 75: STRONG
      ≥ 55: STABLE
      ≥ 35: WEAK
      else: DEGRADED

    The state is the headline; the breakdown lets the UI show why.
    """
    # Per-kind aggregate.
    overall = db.execute(
        text(
            """
            SELECT
              kind,
              COUNT(*) AS total,
              SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
              SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since_ms
            GROUP BY kind
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    weekly_ms = 7 * 24 * 3600 * 1000
    weekly = db.execute(
        text(
            """
            SELECT
              kind,
              (started_at_ms / :wk) * :wk AS bucket_ts,
              SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
              SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since_ms
            GROUP BY kind, bucket_ts
            HAVING SUM(CASE WHEN validated_outcome IN ('followed_through', 'noise') THEN 1 ELSE 0 END) > 0
            """
        ),
        {"wk": weekly_ms, "since_ms": since_ms},
    ).fetchall()

    regime_break = db.execute(
        text(
            """
            SELECT
              kind, regime,
              SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
              SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since_ms
            GROUP BY kind, regime
            HAVING SUM(CASE WHEN validated_outcome IN ('followed_through', 'noise') THEN 1 ELSE 0 END) > 0
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    weekly_by_kind: Dict[str, List[float]] = defaultdict(list)
    for r in weekly:
        ft = int(r.ft or 0)
        ns = int(r.noise or 0)
        if ft + ns == 0:
            continue
        weekly_by_kind[r.kind].append(ft / (ft + ns))

    regime_by_kind: Dict[str, List[float]] = defaultdict(list)
    for r in regime_break:
        ft = int(r.ft or 0)
        ns = int(r.noise or 0)
        if ft + ns == 0:
            continue
        regime_by_kind[r.kind].append(ft / (ft + ns))

    def stdev(xs: List[float]) -> float:
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))

    out: List[dict] = []
    for r in overall:
        ft = int(r.ft or 0)
        ns = int(r.noise or 0)
        total = int(r.total or 0)
        resolved = ft + ns
        if resolved == 0:
            continue
        accuracy = ft / resolved
        stab_std = stdev(weekly_by_kind.get(r.kind, []))
        regime_std = stdev(regime_by_kind.get(r.kind, []))

        # Components on a 0..100 scale.
        accuracy_c = accuracy * 100.0
        stability_c = max(0.0, 100.0 - stab_std * 200.0)        # std=0.5 → 0
        regime_c = max(0.0, 100.0 - regime_std * 200.0)
        # Sample factor: log10 scaled; 10 resolved → ~50, 100 → ~100, 1 → ~10.
        size_c = min(100.0, math.log10(max(2, resolved)) * 50.0)

        score = (0.45 * accuracy_c + 0.20 * stability_c + 0.20 * regime_c + 0.15 * size_c)
        if score >= 75:
            state = "STRONG"
        elif score >= 55:
            state = "STABLE"
        elif score >= 35:
            state = "WEAK"
        else:
            state = "DEGRADED"

        out.append({
            "kind": r.kind,
            "total": total,
            "resolved": resolved,
            "accuracy": accuracy,
            "weekly_buckets": len(weekly_by_kind.get(r.kind, [])),
            "weekly_precision_std": stab_std,
            "regime_buckets": len(regime_by_kind.get(r.kind, [])),
            "regime_precision_std": regime_std,
            "components": {
                "accuracy": accuracy_c,
                "stability": stability_c,
                "regime_consistency": regime_c,
                "sample_size": size_c,
            },
            "reliability_score": score,
            "state": state,
        })

    out.sort(key=lambda r: r["reliability_score"], reverse=True)
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return {
        "since_ms": since_ms,
        "kinds": out,
    }


# ── Transition forecasting per regime ─────────────────────────────────────


def transition_forecast(db: Session, since_ms: int) -> dict:
    """For every regime currently observed, return:
      * sample count,
      * next-state probability vector,
      * expected_latency_ms — mean time to next regime change,
      * collapse_prob, stabilization_prob, volatility_expansion_prob.

    Stabilization = next regime in {HEALTHY_TREND}; collapse = next regime
    in CASCADE_REGIMES; volatility expansion = transitions whose next
    regime ranks higher than the current (intensification, not necessarily
    a full cascade)."""
    rows = db.execute(
        text(
            """
            SELECT symbol, started_at_ms, regime
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since_ms
            ORDER BY symbol, started_at_ms
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    regime_rank = {
        "HEALTHY_TREND": 0,
        "THIN_LIQUIDITY": 30,
        "CROWDED_LONGS": 40,
        "CROWDED_SHORTS": 40,
        "SPOOF_PRONE": 50,
        "UNSTABLE_MARKET": 70,
        "LIQUIDATION_CASCADE": 90,
    }

    transitions: Dict[Tuple[str, str], int] = defaultdict(int)
    latencies: Dict[str, List[int]] = defaultdict(list)
    counts: Dict[str, int] = defaultdict(int)
    prev_by_sym: Dict[str, Tuple[str, int]] = {}

    for r in rows:
        counts[r.regime] += 1
        prev = prev_by_sym.get(r.symbol)
        if prev is not None and prev[0] != r.regime:
            transitions[(prev[0], r.regime)] += 1
            latencies[prev[0]].append(int(r.started_at_ms) - prev[1])
        prev_by_sym[r.symbol] = (r.regime, int(r.started_at_ms))

    forecasts: List[dict] = []
    for regime, total in counts.items():
        out_t = {b: c for (a, b), c in transitions.items() if a == regime}
        out_total = sum(out_t.values())
        next_probs = {b: (c / out_total if out_total > 0 else 0.0) for b, c in out_t.items()}
        collapse_prob = sum(p for b, p in next_probs.items() if b in CASCADE_REGIMES)
        stabilization_prob = next_probs.get("HEALTHY_TREND", 0.0)
        base_rank = regime_rank.get(regime, 0)
        vol_expansion_prob = sum(
            p for b, p in next_probs.items() if regime_rank.get(b, 0) > base_rank
        )
        lats = latencies.get(regime, [])
        avg_latency = sum(lats) / len(lats) if lats else None
        median_latency = sorted(lats)[len(lats) // 2] if lats else None
        forecasts.append({
            "regime": regime,
            "count": total,
            "out_transitions": out_total,
            "next_probs": next_probs,
            "expected_latency_ms": avg_latency,
            "median_latency_ms": median_latency,
            "collapse_prob": collapse_prob,
            "stabilization_prob": stabilization_prob,
            "volatility_expansion_prob": vol_expansion_prob,
        })

    forecasts.sort(key=lambda f: f["count"], reverse=True)
    return {"since_ms": since_ms, "regimes": forecasts}


# ── Risk-state monitoring (current systemic stress) ───────────────────────


def risk_state(db: Session) -> dict:
    """One snapshot of the market's current operational risk profile.

    Pulls the latest sample per (symbol, metric) for the analytics metrics
    over the last hour; classifies each symbol by intra-cohort percentiles
    and rolls up the share of the universe in elevated risk on each
    dimension. Final `risk_state_score` is a weighted blend; the
    `instability_rank` ordering surfaces the worst symbols.

    Cohort is restricted to "recent" data (last 60 min) so we don't
    average against day-old samples in dead WS sessions.
    """
    now_ms = int(time.time() * 1000)
    window = 60 * 60_000
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (symbol, metric) symbol, metric, value, ts
            FROM liquidity_samples
            WHERE ts >= :since
              AND metric = ANY(:metrics)
              AND value IS NOT NULL
            ORDER BY symbol, metric, ts DESC
            """
        ),
        {"since": now_ms - window, "metrics": list(ANALYTICS_METRICS)},
    ).fetchall()
    by_sym: Dict[str, Dict[str, float]] = defaultdict(dict)
    for r in rows:
        by_sym[r.symbol][r.metric] = float(r.value)

    if not by_sym:
        return {
            "risk_state_score": 0.0,
            "systemic_stress_level": "QUIET",
            "drivers": {},
            "n_symbols": 0,
            "instability_rank": [],
        }

    # Per-metric cohort percentiles for the metrics whose elevated values
    # mean stress. For "good" metrics (resiliency, atr_liquidity) we
    # invert — being in the BOTTOM cohort percentile is the bad case.
    metric_arrays = defaultdict(list)
    for mv in by_sym.values():
        for m, v in mv.items():
            metric_arrays[m].append(v)
    for m in metric_arrays:
        metric_arrays[m].sort()

    def pct(m: str, v: float) -> float:
        arr = metric_arrays.get(m) or []
        if len(arr) < 4:
            return 0.5
        # Position via bisect-like scan; arrays are small.
        lo, hi = 0, len(arr)
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(arr)

    # Per-symbol stress vector — each component in [0,1] where 1 = max stress.
    per_sym_stress: List[Tuple[str, float, Dict[str, float]]] = []
    for sym, mv in by_sym.items():
        comps: Dict[str, float] = {}
        # Elevated = bad
        for m in ("spread", "liq_stress", "fragility_score"):
            v = mv.get(m)
            comps[m] = pct(m, v) if v is not None else 0.0
        # |funding_z|, |oi_delta_1h| — magnitude matters
        if "funding_z" in mv:
            comps["funding_z"] = min(1.0, abs(mv["funding_z"]) / 3.0)
        if "oi_delta_1h" in mv:
            comps["oi_delta_1h"] = min(1.0, abs(mv["oi_delta_1h"]) / 10.0)
        # Inverted: low = bad
        for m in ("credible_depth", "resiliency_score", "atr_liquidity"):
            v = mv.get(m)
            if v is not None:
                comps[m + "_inv"] = 1.0 - pct(m, v)
        # impact_score: high impact = bad (already in 0..100 scale)
        if "impact_score" in mv:
            comps["impact_score"] = min(1.0, mv["impact_score"] / 100.0)
        stress = sum(comps.values()) / max(1, len(comps))
        per_sym_stress.append((sym, stress, comps))

    per_sym_stress.sort(key=lambda x: -x[1])

    # Aggregate drivers across the universe.
    driver_means: Dict[str, float] = defaultdict(float)
    driver_counts: Dict[str, int] = defaultdict(int)
    for _, _, comps in per_sym_stress:
        for k, v in comps.items():
            driver_means[k] += v
            driver_counts[k] += 1
    drivers = {k: driver_means[k] / driver_counts[k] for k in driver_means if driver_counts[k] > 0}

    universe_score = (sum(s for _, s, _ in per_sym_stress) / len(per_sym_stress)) * 100.0
    if universe_score >= 65:
        level = "SEVERE"
    elif universe_score >= 45:
        level = "ELEVATED"
    elif universe_score >= 25:
        level = "WATCH"
    else:
        level = "QUIET"

    return {
        "risk_state_score": universe_score,
        "systemic_stress_level": level,
        "n_symbols": len(per_sym_stress),
        "drivers": drivers,
        "instability_rank": [
            {"symbol": sym, "stress": stress, "components": comps}
            for sym, stress, comps in per_sym_stress[:20]
        ],
    }


# ── Market narrative composition ──────────────────────────────────────────


def market_narrative(db: Session) -> dict:
    """Human-readable summary of the current market state.

    Pulls `risk_state`, recent alert count, recent regime mix, then runs
    a template-driven composer. No LLM, no recommendations — just a
    deterministic summary the user can read in three seconds.
    """
    rs = risk_state(db)
    now_ms = int(time.time() * 1000)
    recent_window_ms = 60 * 60_000
    alerts = db.execute(
        text(
            """
            SELECT kind, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY kind
            ORDER BY c DESC
            """
        ),
        {"since": now_ms - recent_window_ms},
    ).fetchall()
    regime_counts = db.execute(
        text(
            """
            SELECT regime, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY regime
            ORDER BY c DESC
            """
        ),
        {"since": now_ms - recent_window_ms},
    ).fetchall()

    top_alert_kinds = [(r.kind, int(r.c)) for r in alerts][:5]
    top_regimes = [(r.regime, int(r.c)) for r in regime_counts][:5]

    # Build narrative paragraphs.
    state_label = rs["systemic_stress_level"]
    score = rs["risk_state_score"]

    drivers = rs.get("drivers") or {}
    driver_lines: List[str] = []
    if drivers.get("fragility_score", 0) >= 0.5:
        driver_lines.append("elevated fragility across the cohort")
    if drivers.get("resiliency_score_inv", 0) >= 0.5:
        driver_lines.append("weakening orderbook resiliency")
    if drivers.get("credible_depth_inv", 0) >= 0.5:
        driver_lines.append("thin credible depth")
    if drivers.get("spread", 0) >= 0.5:
        driver_lines.append("widened spreads")
    if drivers.get("liq_stress", 0) >= 0.5:
        driver_lines.append("active liquidation flow")
    if drivers.get("funding_z", 0) >= 0.5:
        driver_lines.append("extreme funding positioning")
    if drivers.get("oi_delta_1h", 0) >= 0.4:
        driver_lines.append("rapid open-interest shifts")
    if drivers.get("impact_score", 0) >= 0.5:
        driver_lines.append("high price-impact per flow")

    headline = {
        "SEVERE": "Market is in operationally stressed conditions.",
        "ELEVATED": "Market is showing elevated operational risk.",
        "WATCH": "Market is in a watch-worthy state.",
        "QUIET": "Market is in a quiet operational state.",
    }[state_label]

    alert_text = ""
    if top_alert_kinds:
        kinds_str = ", ".join(f"{k} ({c})" for k, c in top_alert_kinds[:3])
        alert_text = f"Recent alerts (last 60m): {kinds_str}."

    regime_text = ""
    if top_regimes:
        regimes_str = ", ".join(f"{r} ({c})" for r, c in top_regimes[:3])
        regime_text = f"Dominant regimes: {regimes_str}."

    bullets = driver_lines or ["no dominant stress drivers identified."]

    historical_context = ""
    if state_label in ("SEVERE", "ELEVATED"):
        historical_context = (
            "States with this profile have historically been associated with "
            "elevated liquidation risk, volatility expansion, and short-term "
            "instability. This is descriptive — not a trading recommendation."
        )

    return {
        "headline": headline,
        "score": score,
        "level": state_label,
        "bullets": bullets,
        "alert_summary": alert_text,
        "regime_summary": regime_text,
        "historical_context": historical_context,
        "top_alert_kinds": [{"kind": k, "count": c} for k, c in top_alert_kinds],
        "top_regimes": [{"regime": r, "count": c} for r, c in top_regimes],
        "fetched_at_ms": now_ms,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-10 — Strategic Intelligence & Meta-Adaptation
# ══════════════════════════════════════════════════════════════════════════
#
# These functions look at the higher-order question — not "what's happening
# now" but "is what's happening different from what was happening?".
# Operates over the same three tables; no new schema.


# ── Structural break detection ────────────────────────────────────────────


def structural_breaks(db: Session, window_days: int = 7) -> dict:
    """Compare the most-recent `window_days` window vs the prior one of
    the same length. We compute three families of break signals:

      * correlation drift — pairwise Pearson Δ for ANALYTICS_METRICS
      * percentile migration — per-metric median Δ in cohort
      * regime-mix shift — Δ in regime-share across alert history

    Each Δ is z-scored against its own baseline volatility so a one-off
    spike is treated differently from a sustained move. Final
    `structural_break_score` blends the three; high score = structure
    has materially changed.
    """
    now_ms = int(time.time() * 1000)
    cur_since = now_ms - window_days * 24 * 3600 * 1000
    prev_since = now_ms - 2 * window_days * 24 * 3600 * 1000

    cur_im = interaction_matrix(db, since_ms=cur_since, bucket_minutes=15)
    prev_im = interaction_matrix(db, since_ms=prev_since, bucket_minutes=15)
    # Clip prev_im to <= cur_since by re-running on a constrained window —
    # simpler than mutating cur_im; cost is identical to one extra query.
    # Cheap to skip: prev_im currently includes cur window too. Re-derive
    # by running interaction_matrix with the explicit older window only.
    prev_only = _interaction_in_window(db, prev_since, cur_since, bucket_minutes=15)

    # Correlation drift table.
    cur_map = {(c["a"], c["b"]): c["r"] for c in cur_im["cells"]}
    prev_map = {(c["a"], c["b"]): c["r"] for c in prev_only["cells"]}
    pair_drifts: List[dict] = []
    drift_abs: List[float] = []
    for key, r_cur in cur_map.items():
        if key[0] >= key[1]:    # only upper triangle to dedup
            continue
        r_prev = prev_map.get(key)
        if r_cur is None or r_prev is None:
            continue
        delta = r_cur - r_prev
        drift_abs.append(abs(delta))
        pair_drifts.append({
            "a": key[0], "b": key[1],
            "r_prev": r_prev, "r_cur": r_cur, "delta": delta,
        })
    pair_drifts.sort(key=lambda d: -abs(d["delta"]))
    avg_abs_drift = sum(drift_abs) / len(drift_abs) if drift_abs else 0.0
    # 0.15 abs delta is roughly "obviously different" for correlations
    # bounded in [-1, +1]. Use this as the half-saturation point.
    corr_break_score = 100.0 * min(1.0, avg_abs_drift / 0.30)

    # Percentile migration: median per metric, current vs previous window.
    def medians(since_ms: int, until_ms: int) -> Dict[str, float]:
        rows = db.execute(
            text(
                """
                SELECT metric, percentile_disc(0.5) WITHIN GROUP (ORDER BY value) AS m
                FROM liquidity_samples
                WHERE metric = ANY(:metrics)
                  AND ts >= :since AND ts < :until
                  AND value IS NOT NULL
                GROUP BY metric
                """
            ),
            {"metrics": list(ANALYTICS_METRICS), "since": since_ms, "until": until_ms},
        ).fetchall()
        return {r.metric: float(r.m) for r in rows if r.m is not None}

    med_cur = medians(cur_since, now_ms)
    med_prev = medians(prev_since, cur_since)
    migrations: List[dict] = []
    migration_abs: List[float] = []
    for m, cur in med_cur.items():
        prev = med_prev.get(m)
        if prev is None or abs(prev) < 1e-9:
            continue
        pct_delta = (cur - prev) / abs(prev)
        migrations.append({"metric": m, "prev_median": prev, "cur_median": cur, "pct_delta": pct_delta})
        migration_abs.append(abs(pct_delta))
    migrations.sort(key=lambda d: -abs(d["pct_delta"]))
    median_break_score = 100.0 * min(1.0, (sum(migration_abs) / len(migration_abs) if migration_abs else 0) / 0.50)

    # Regime-mix shift.
    def regime_shares(since_ms: int, until_ms: int) -> Dict[str, float]:
        rows = db.execute(
            text(
                """
                SELECT regime, COUNT(*) AS c
                FROM liquidity_alert_history
                WHERE started_at_ms >= :since AND started_at_ms < :until
                GROUP BY regime
                """
            ),
            {"since": since_ms, "until": until_ms},
        ).fetchall()
        total = sum(int(r.c) for r in rows)
        return {r.regime: int(r.c) / total for r in rows} if total > 0 else {}

    sh_cur = regime_shares(cur_since, now_ms)
    sh_prev = regime_shares(prev_since, cur_since)
    regime_drifts: List[dict] = []
    drift_total = 0.0
    for regime in set(sh_cur) | set(sh_prev):
        prev = sh_prev.get(regime, 0.0)
        cur = sh_cur.get(regime, 0.0)
        delta = cur - prev
        drift_total += abs(delta)
        regime_drifts.append({"regime": regime, "prev_share": prev, "cur_share": cur, "delta": delta})
    regime_drifts.sort(key=lambda d: -abs(d["delta"]))
    # L1 distance between two distributions is in [0, 2]; halve into [0, 1].
    regime_break_score = 100.0 * min(1.0, drift_total / 2.0)

    structural_break_score = 0.45 * corr_break_score + 0.30 * median_break_score + 0.25 * regime_break_score
    # Confidence is sample-count weighted — based on how many pair-corrs and
    # how many regime samples we had.
    sample_signal = (len(drift_abs) >= 20) + (sum(sh_cur.values()) > 0.99) + (len(med_cur) >= 5)
    break_confidence = (sample_signal / 3.0) * 100.0

    return {
        "window_days": window_days,
        "structural_break_score": structural_break_score,
        "break_confidence": break_confidence,
        "components": {
            "correlation_drift": corr_break_score,
            "median_migration": median_break_score,
            "regime_mix_shift": regime_break_score,
        },
        "affected_correlations": pair_drifts[:10],
        "affected_metrics": migrations[:10],
        "affected_regimes": regime_drifts,
        "cur_since": cur_since,
        "prev_since": prev_since,
    }


def _interaction_in_window(db: Session, since_ms: int, until_ms: int, bucket_minutes: int = 15) -> dict:
    """Same as interaction_matrix but constrained by an upper bound on ts.

    We need the "previous-window" matrix when computing structural breaks;
    this is a thin clone of interaction_matrix with one extra WHERE clause.
    """
    bucket_ms = bucket_minutes * 60_000
    metrics = list(ANALYTICS_METRICS)
    rows = db.execute(
        text(
            """
            WITH src AS (
              SELECT
                symbol,
                metric,
                (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
                AVG(value) AS v
              FROM liquidity_samples
              WHERE metric = ANY(:metrics)
                AND ts >= :since AND ts < :until
                AND value IS NOT NULL
              GROUP BY symbol, metric, bucket_ts
            )
            SELECT symbol, bucket_ts, metric, v FROM src
            """
        ),
        {"bucket_ms": bucket_ms, "metrics": metrics, "since": since_ms, "until": until_ms},
    ).fetchall()
    pivot: Dict[Tuple[str, int], Dict[str, float]] = {}
    for r in rows:
        pivot.setdefault((r.symbol, int(r.bucket_ts)), {})[r.metric] = float(r.v)
    by_metric: Dict[str, List[Tuple[Tuple[str, int], float]]] = {m: [] for m in metrics}
    for key, mv in pivot.items():
        for m, v in mv.items():
            by_metric[m].append((key, v))
    cells: List[dict] = []
    for a in metrics:
        for b in metrics:
            if a == b:
                cells.append({"a": a, "b": b, "r": 1.0, "n": len(by_metric[a])})
                continue
            map_a = {k: v for k, v in by_metric[a]}
            xs: List[float] = []
            ys: List[float] = []
            for k, vb in by_metric[b]:
                va = map_a.get(k)
                if va is None:
                    continue
                xs.append(va)
                ys.append(vb)
            cells.append({"a": a, "b": b, "r": _pearson(xs, ys), "n": len(xs)})
    return {"metrics": metrics, "cells": cells, "since_ms": since_ms, "bucket_minutes": bucket_minutes}


# ── Regime shift early warning ────────────────────────────────────────────


def regime_shift_warning(db: Session) -> dict:
    """Acceleration-of-stress signals: compare the last 60m vs the prior
    180m on the universe-level versions of the same metrics that drive
    individual-symbol fragility/resiliency. When the recent window shows
    a steeper trajectory than the older one, we surface that as a
    pre-cascade warning.
    """
    now_ms = int(time.time() * 1000)
    recent_window = 60 * 60_000
    prior_window = 180 * 60_000

    def cohort_avg(since_ms: int, until_ms: int) -> Dict[str, float]:
        rows = db.execute(
            text(
                """
                SELECT metric, AVG(value) AS m
                FROM liquidity_samples
                WHERE metric = ANY(:metrics)
                  AND ts >= :since AND ts < :until
                  AND value IS NOT NULL
                GROUP BY metric
                """
            ),
            {
                "metrics": list(ANALYTICS_METRICS),
                "since": since_ms, "until": until_ms,
            },
        ).fetchall()
        return {r.metric: float(r.m) for r in rows if r.m is not None}

    recent = cohort_avg(now_ms - recent_window, now_ms)
    prior = cohort_avg(now_ms - recent_window - prior_window, now_ms - recent_window)

    # Signals: each is a normalized rate-of-change in the "wrong" direction
    # for that metric.
    signals: Dict[str, float] = {}
    def bad_change(m: str, direction_bad: int) -> Optional[float]:
        r = recent.get(m); p = prior.get(m)
        if r is None or p is None or abs(p) < 1e-9:
            return None
        change = (r - p) / abs(p)
        # direction_bad = +1 means rising is bad (fragility, spread, liq, OI Δ)
        # direction_bad = −1 means falling is bad (resiliency, depth, atr)
        return max(0.0, change * direction_bad)

    sig_specs: List[Tuple[str, str, int]] = [
        ("fragility_acceleration", "fragility_score", +1),
        ("resiliency_degradation", "resiliency_score", -1),
        ("depth_collapse_trend", "credible_depth", -1),
        ("spread_expansion_trend", "spread", +1),
        ("liq_stress_acceleration", "liq_stress", +1),
        ("funding_divergence_growth", "funding_z", +1),
        ("oi_acceleration", "oi_delta_1h", +1),
        ("impact_acceleration", "impact_score", +1),
    ]
    for name, metric, dir_ in sig_specs:
        v = bad_change(metric, dir_)
        if v is not None:
            signals[name] = v

    # Composite probability: capped sigmoid of average signal magnitude.
    if not signals:
        prob = 0.0
        warning = "INSUFFICIENT_DATA"
        accel = 0.0
    else:
        mean_sig = sum(signals.values()) / len(signals)
        # 0.5 mean (i.e. signals averaged at 50% bad-direction change) → 0.8
        prob = 1.0 - math.exp(-2.0 * mean_sig)
        prob = max(0.0, min(1.0, prob))
        accel = mean_sig * 100.0
        if prob >= 0.65:
            warning = "PRE_CASCADE"
        elif prob >= 0.4:
            warning = "ELEVATED_TRANSITION_RISK"
        elif prob >= 0.2:
            warning = "WATCH"
        else:
            warning = "STABLE"

    return {
        "fetched_at_ms": now_ms,
        "regime_shift_probability": prob * 100.0,
        "instability_acceleration": accel,
        "warning_state": warning,
        "signals": [
            {"name": k, "value": v} for k, v in sorted(signals.items(), key=lambda kv: -kv[1])
        ],
    }


# ── Adaptive reliability + meta-confidence ────────────────────────────────


# Regime-keyed reliability multipliers per signal family. The intuition
# comes from the Phase-5 weight matrix: in cascade you want resiliency
# and fragility to win; in healthy trend OI and funding signals carry
# more validated weight.
_REGIME_RELIABILITY_BIAS: Dict[str, Dict[str, float]] = {
    "HEALTHY_TREND":         {"OI_SURGE": 1.2, "FUNDING_EXTREME": 1.2, "REGIME_TRANSITION": 1.0},
    "THIN_LIQUIDITY":        {"DEPTH_COLLAPSE": 1.3, "SPREAD_EXPLOSION": 1.2, "RESILIENCY_FAILURE": 1.2},
    "SPOOF_PRONE":           {"SPREAD_EXPLOSION": 1.4, "DEPTH_COLLAPSE": 1.3},
    "LIQUIDATION_CASCADE":   {"LIQ_CASCADE": 1.4, "RESILIENCY_FAILURE": 1.3, "FRAGILITY_SPIKE": 1.2},
    "CROWDED_LONGS":         {"FUNDING_EXTREME": 1.3, "OI_SURGE": 1.2},
    "CROWDED_SHORTS":        {"FUNDING_EXTREME": 1.3, "OI_SURGE": 1.2},
    "UNSTABLE_MARKET":       {"FRAGILITY_SPIKE": 1.4, "RESILIENCY_FAILURE": 1.3, "LIQ_CASCADE": 1.2},
}


def adaptive_reliability(db: Session, since_ms: int) -> dict:
    """Take the static reliability table and apply regime-dependent
    multipliers so the operations layer can pick the right signal for
    the current regime mix. The dominant regime is whatever has the
    most resolved alerts in the same window.
    """
    base = signal_reliability(db, since_ms=since_ms)
    # Recent regime mix to determine the dominant regime.
    rows = db.execute(
        text(
            """
            SELECT regime, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY regime
            ORDER BY c DESC
            """
        ),
        {"since": since_ms},
    ).fetchall()
    dominant_regime = rows[0].regime if rows else "HEALTHY_TREND"
    bias = _REGIME_RELIABILITY_BIAS.get(dominant_regime, {})

    out_kinds: List[dict] = []
    for k in base["kinds"]:
        mult = bias.get(k["kind"], 1.0)
        adjusted = max(0.0, min(100.0, k["reliability_score"] * mult))
        out_kinds.append({
            **k,
            "regime_multiplier": mult,
            "regime_adjusted_reliability": adjusted,
        })
    out_kinds.sort(key=lambda r: -r["regime_adjusted_reliability"])
    for i, r in enumerate(out_kinds, start=1):
        r["adjusted_rank"] = i
    return {
        "since_ms": since_ms,
        "dominant_regime": dominant_regime,
        "kinds": out_kinds,
    }


def meta_confidence(db: Session, since_ms: int) -> dict:
    """Second-order confidence: how much can we trust our own confidence?

    Pulls four ingredients:
      * average raw confidence across recent alerts
      * fraction of recent alerts whose validated_outcome was 'noise'
        (post-hoc false-positive rate)
      * structural break score
      * regime jitter — number of distinct regimes observed per symbol
        in the recent window

    The output is a 0..100 trustworthiness score with a 3-bucket state
    so the UI can grey out signal pills when meta-confidence is low.
    """
    rows = db.execute(
        text(
            """
            SELECT confidence, validated_outcome, regime, symbol
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            """
        ),
        {"since": since_ms},
    ).fetchall()
    n = len(rows)
    if n == 0:
        return {
            "meta_confidence_score": 50.0,
            "confidence_stability": 50.0,
            "trustworthiness_state": "UNKNOWN",
            "components": {},
            "n_alerts": 0,
        }

    avg_conf = sum(float(r.confidence) for r in rows) / n
    resolved = [r for r in rows if r.validated_outcome in ("followed_through", "noise")]
    noise_rate = (
        sum(1 for r in resolved if r.validated_outcome == "noise") / len(resolved)
        if resolved else 0.0
    )

    # Regime jitter per symbol.
    per_sym: Dict[str, set] = defaultdict(set)
    for r in rows:
        per_sym[r.symbol].add(r.regime)
    avg_distinct_regimes = sum(len(s) for s in per_sym.values()) / len(per_sym) if per_sym else 1.0

    # Structural break — bounded SQL, but call the existing helper.
    breaks = structural_breaks(db, window_days=7)
    sbs = breaks["structural_break_score"]

    # Compose. Scale each into 0..100 where 100 = "confidence is trustworthy".
    avg_conf_c = avg_conf                          # already 0..100
    noise_c = max(0.0, 100.0 * (1.0 - noise_rate * 2.0))   # 50% noise → 0
    jitter_c = max(0.0, 100.0 * (1.0 - max(0.0, (avg_distinct_regimes - 1.5) / 2.5)))
    sbs_c = max(0.0, 100.0 - sbs)

    meta_score = 0.30 * avg_conf_c + 0.25 * noise_c + 0.20 * jitter_c + 0.25 * sbs_c
    # Stability = how close meta_score is to the same window's accuracy mean.
    accuracy_proxy = 100.0 - noise_rate * 100.0
    stability = max(0.0, 100.0 - abs(meta_score - accuracy_proxy))

    if meta_score >= 70:
        state = "TRUSTWORTHY"
    elif meta_score >= 45:
        state = "GUARDED"
    else:
        state = "UNRELIABLE"

    return {
        "meta_confidence_score": meta_score,
        "confidence_stability": stability,
        "trustworthiness_state": state,
        "components": {
            "avg_confidence": avg_conf_c,
            "noise_resistance": noise_c,
            "regime_jitter": jitter_c,
            "structural_break_inv": sbs_c,
        },
        "n_alerts": n,
        "noise_rate": noise_rate,
        "avg_distinct_regimes": avg_distinct_regimes,
    }


# ── Edge survival analytics ───────────────────────────────────────────────


def edge_survival(db: Session, since_ms: int, threshold: float = 0.5) -> dict:
    """Kaplan–Meier-style survival per alert kind.

    "Survival" is defined as: rolling precision (7d window) remaining
    above `threshold`. For each kind, we walk its weekly precision
    series and mark each week as still-alive or dead; the resulting
    curve is the survival function. `expected_remaining_lifespan_days`
    is the area under the survival curve from "now" projected forward
    via the current slope.
    """
    persistence = edge_persistence(db, since_ms=since_ms, window_days=7)
    out_kinds: List[dict] = []
    for k in persistence["kinds"]:
        series = [(s["bucket_ts"], s["precision"]) for s in k["series"] if s["precision"] is not None]
        if len(series) < 2:
            continue
        # Mark alive vs dead per week.
        alive_curve: List[Tuple[int, bool]] = [(ts, prec >= threshold) for ts, prec in series]
        # Survival function value at each step: cumulative product of "still
        # alive given alive at prior step", but for binary classification
        # this collapses to: survival drops to 0 once we hit "dead".
        survival: List[Tuple[int, float]] = []
        s_val = 1.0
        deaths = 0
        for ts, alive in alive_curve:
            if not alive:
                deaths += 1
                s_val = max(0.0, s_val * 0.5)        # half-life on each "dead" week
            survival.append((ts, s_val))
        # Expected remaining lifespan: integrate forward using current
        # slope_per_day until precision falls to threshold.
        slope_per_day = k["slope_per_day"]
        latest = k["latest_precision"]
        expected_remaining_days: Optional[float] = None
        if latest is not None:
            if slope_per_day is None or slope_per_day >= 0:
                expected_remaining_days = None     # not dying or unknown
            elif latest <= threshold:
                expected_remaining_days = 0.0
            else:
                expected_remaining_days = (latest - threshold) / (-slope_per_day)

        # Degradation acceleration: 2nd derivative proxy = (slope of recent
        # half) − (slope of older half).
        accel = None
        if len(series) >= 6:
            mid = len(series) // 2
            def _slope(pts):
                ys = [p[1] for p in pts]
                n = len(ys)
                if n < 2:
                    return 0.0
                mx = (n - 1) / 2.0
                my = sum(ys) / n
                num = sum((i - mx) * (y - my) for i, y in enumerate(ys))
                den = sum((i - mx) ** 2 for i in range(n))
                return num / den if den > 0 else 0.0
            accel = _slope(series[mid:]) - _slope(series[:mid])

        out_kinds.append({
            "kind": k["kind"],
            "deaths": deaths,
            "expected_remaining_days": expected_remaining_days,
            "degradation_acceleration": accel,
            "latest_precision": latest,
            "threshold": threshold,
            "survival_curve": [{"ts": ts, "s": s} for ts, s in survival],
        })
    out_kinds.sort(key=lambda r: (r["expected_remaining_days"] is None, r["expected_remaining_days"] or 999_999))
    return {"since_ms": since_ms, "threshold": threshold, "kinds": out_kinds}


# ── Market structure evolution ────────────────────────────────────────────


def market_evolution(db: Session, lookback_days: int = 60, bucket_days: int = 7) -> dict:
    """Long-horizon trends: weekly cohort medians for fragility,
    resiliency, spread, depth, regime-mix entropy.

    We report each metric's weekly median + the OLS slope per day
    derived from those weekly medians — same shape as Phase-9 edge
    persistence but for raw structural quantities, not signal precision.
    """
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - lookback_days * 24 * 3600 * 1000
    bucket_ms = bucket_days * 24 * 3600 * 1000

    metric_rows = db.execute(
        text(
            """
            SELECT
              metric,
              (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
              percentile_disc(0.5) WITHIN GROUP (ORDER BY value) AS med
            FROM liquidity_samples
            WHERE metric = ANY(:metrics)
              AND ts >= :since
              AND value IS NOT NULL
            GROUP BY metric, bucket_ts
            ORDER BY metric, bucket_ts
            """
        ),
        {"bucket_ms": bucket_ms, "metrics": list(ANALYTICS_METRICS), "since": since_ms},
    ).fetchall()

    by_metric: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for r in metric_rows:
        if r.med is None:
            continue
        by_metric[r.metric].append((int(r.bucket_ts), float(r.med)))

    trend: List[dict] = []
    for metric, pts in by_metric.items():
        ys = [p[1] for p in pts]
        n = len(ys)
        slope = None
        if n >= 3:
            mx = (n - 1) / 2.0
            my = sum(ys) / n
            num = sum((i - mx) * (y - my) for i, y in enumerate(ys))
            den = sum((i - mx) ** 2 for i in range(n))
            if den > 0:
                slope = num / den / bucket_days     # per-day slope
        trend.append({
            "metric": metric,
            "series": [{"ts": ts, "v": v} for ts, v in pts],
            "slope_per_day": slope,
        })
    trend.sort(key=lambda t: t["metric"])

    # Regime-mix entropy week by week — diversifying regime mix indicates
    # market is harder to characterize, decreasing means concentration.
    regime_rows = db.execute(
        text(
            """
            SELECT
              (started_at_ms / :bucket_ms) * :bucket_ms AS bucket_ts,
              regime, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY bucket_ts, regime
            ORDER BY bucket_ts
            """
        ),
        {"bucket_ms": bucket_ms, "since": since_ms},
    ).fetchall()
    by_bucket: Dict[int, Dict[str, int]] = defaultdict(dict)
    for r in regime_rows:
        by_bucket[int(r.bucket_ts)][r.regime] = int(r.c)
    entropy_series: List[dict] = []
    for ts, mix in sorted(by_bucket.items()):
        total = sum(mix.values())
        if total == 0:
            continue
        ent = 0.0
        for c in mix.values():
            p = c / total
            if p > 0:
                ent -= p * math.log2(p)
        entropy_series.append({"ts": ts, "entropy": ent, "dominant_regime": max(mix.items(), key=lambda kv: kv[1])[0]})

    return {
        "lookback_days": lookback_days,
        "bucket_days": bucket_days,
        "metric_trends": trend,
        "regime_entropy_series": entropy_series,
    }


# ── Strategic risk-state classification ───────────────────────────────────


def strategic_state(db: Session) -> dict:
    """Classify the market into one strategic state.

    Inputs (all already computed elsewhere):
      * current risk_state level + drivers
      * regime_shift_warning warning_state
      * structural_break score
      * adaptive reliability dominant regime
      * meta_confidence trustworthiness

    The classifier is a small decision table. Adding states means adding
    rows — keep the function pure so it's auditable without re-running
    the system.
    """
    rs = risk_state(db)
    rsw = regime_shift_warning(db)
    sb = structural_breaks(db, window_days=7)
    ar = adaptive_reliability(db, since_ms=int(time.time() * 1000) - 30 * 24 * 3600 * 1000)
    mc = meta_confidence(db, since_ms=int(time.time() * 1000) - 30 * 24 * 3600 * 1000)

    score = rs["risk_state_score"]
    level = rs["systemic_stress_level"]
    shift_warn = rsw["warning_state"]
    sbs = sb["structural_break_score"]
    trust = mc["trustworthiness_state"]
    dominant = ar["dominant_regime"]

    # Decision rules (ordered: first match wins).
    state = "STABLE_INSTITUTIONAL_FLOW"
    rationale: List[str] = []

    if shift_warn == "PRE_CASCADE":
        state = "CASCADE_RISK_ENVIRONMENT"
        rationale.append("regime_shift_warning = PRE_CASCADE")
    elif level == "SEVERE":
        state = "CASCADE_RISK_ENVIRONMENT"
        rationale.append("systemic_stress_level = SEVERE")
    elif level == "ELEVATED" and dominant in ("UNSTABLE_MARKET", "LIQUIDATION_CASCADE", "SPOOF_PRONE"):
        state = "TRANSITIONAL_UNSTABLE"
        rationale.append(f"stress=ELEVATED + dominant={dominant}")
    elif sbs >= 55:
        state = "TRANSITIONAL_UNSTABLE"
        rationale.append(f"structural_break_score={sbs:.0f}")
    elif level == "ELEVATED" and rs["drivers"].get("credible_depth_inv", 0) >= 0.55:
        state = "LIQUIDITY_DETERIORATION_PHASE"
        rationale.append("elevated stress + bottom-percentile credible depth")
    elif level in ("WATCH", "ELEVATED") and rs["drivers"].get("fragility_score", 0) >= 0.5:
        state = "FRAGILE_SPECULATIVE_MARKET"
        rationale.append("elevated fragility across cohort")
    elif level == "WATCH":
        state = "STABLE_INSTITUTIONAL_FLOW"
        rationale.append("watch-level stress but no other escalators")
    else:
        state = "STABLE_INSTITUTIONAL_FLOW"
        rationale.append("quiet operational state")

    # Trustworthiness gate — when meta-confidence is low we tag the
    # state ambiguous so the UI can show that the classification itself
    # is uncertain.
    if trust == "UNRELIABLE":
        rationale.append("meta-confidence UNRELIABLE — treat classification with skepticism")

    return {
        "state": state,
        "trustworthiness": trust,
        "rationale": rationale,
        "inputs": {
            "stress_level": level,
            "stress_score": score,
            "shift_warning": shift_warn,
            "structural_break_score": sbs,
            "dominant_regime": dominant,
        },
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-11 — Self-Calibration & Meta-Learning
# ══════════════════════════════════════════════════════════════════════════
#
# Layers that look at the system itself and recommend adjustments — not
# auto-applied (that's how alert engines blow up); the UI surfaces the
# recommendations and stores anomaly memory so we can detect recurrence
# of structural events across long time horizons.


# ── Threshold self-calibration ───────────────────────────────────────────


def threshold_calibration(db: Session, since_ms: int) -> dict:
    """Per alert kind, recommend a threshold adjustment from the last 30d
    of validated outcomes.

    Heuristic:
      * if precision < 35% AND volume is high → threshold TOO LOOSE,
        recommend tighten (×1.2);
      * if precision > 70% AND volume is very low (< 5/day) →
        threshold TOO TIGHT, recommend loosen (×0.85);
      * if precision is between, threshold is good as-is.

    Returns the recommendation and a `calibration_confidence` based on
    sample count — low confidence => UI suggests "wait".
    """
    rows = db.execute(
        text(
            """
            SELECT kind, COUNT(*) AS total,
                   SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
                   SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise,
                   MIN(started_at_ms) AS min_ts,
                   MAX(started_at_ms) AS max_ts
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY kind
            """
        ),
        {"since": since_ms},
    ).fetchall()

    out: List[dict] = []
    for r in rows:
        ft = int(r.ft or 0)
        noise = int(r.noise or 0)
        resolved = ft + noise
        precision = ft / resolved if resolved > 0 else None
        span_ms = int(r.max_ts or 0) - int(r.min_ts or 0) if r.max_ts and r.min_ts else 0
        days = max(0.5, span_ms / (24 * 3600 * 1000)) if span_ms > 0 else 0.5
        per_day = int(r.total or 0) / days

        # Decide adjustment.
        adjustment = 1.0
        action = "HOLD"
        rationale: List[str] = []
        if precision is not None and resolved >= 20:
            if precision < 0.35 and per_day >= 1.0:
                adjustment = 1.20
                action = "TIGHTEN"
                rationale.append(f"precision {precision*100:.0f}% < 35% over {resolved} resolved")
            elif precision > 0.70 and per_day < 5.0:
                adjustment = 0.85
                action = "LOOSEN"
                rationale.append(f"precision {precision*100:.0f}% > 70% but only {per_day:.1f}/day")
            else:
                rationale.append(f"precision {precision*100:.0f}% in healthy band")
        elif precision is None:
            rationale.append("no resolved alerts in window")
        else:
            rationale.append(f"only {resolved} resolved — need ≥20 for recommendation")

        # Confidence — log-scaled in [0, 100].
        confidence = min(100.0, math.log10(max(2, resolved)) * 50.0)

        out.append({
            "kind": r.kind,
            "total": int(r.total or 0),
            "resolved": resolved,
            "precision": precision,
            "per_day": per_day,
            "action": action,
            "adjustment_multiplier": adjustment,
            "calibration_confidence": confidence,
            "rationale": rationale,
        })

    out.sort(key=lambda d: (d["action"] == "HOLD", -d["resolved"]))
    return {"since_ms": since_ms, "kinds": out}


# ── Adaptive metric weights ──────────────────────────────────────────────


def adaptive_metric_weights(db: Session, since_ms: int) -> dict:
    """For every analytics metric, compute a relevance score from the
    most recent alert history: how often did this metric show an
    extreme value (top decile or bottom decile of its cohort) ahead of
    a followed-through alert?

    The output is per-metric weight in 0..2 — 1.0 is "neutral", >1
    "more relevant now", <1 "less relevant". Frontends can apply this
    to the static intelligence-score weights so the composite tracks
    what is currently informative.
    """
    # Pull recent samples for cohort percentiles.
    recent_ms = max(since_ms, int(time.time() * 1000) - 7 * 24 * 3600 * 1000)
    rows = db.execute(
        text(
            """
            SELECT metric, percentile_disc(0.10) WITHIN GROUP (ORDER BY value) AS p10,
                           percentile_disc(0.90) WITHIN GROUP (ORDER BY value) AS p90
            FROM liquidity_samples
            WHERE metric = ANY(:metrics) AND ts >= :since AND value IS NOT NULL
            GROUP BY metric
            """
        ),
        {"metrics": list(ANALYTICS_METRICS), "since": recent_ms},
    ).fetchall()
    cohort: Dict[str, Tuple[float, float]] = {r.metric: (float(r.p10), float(r.p90)) for r in rows if r.p10 is not None}

    # Followed-through alerts: take the symbol's metric values at the
    # alert timestamp and count which metrics were in the extreme deciles.
    alerts = db.execute(
        text(
            """
            SELECT symbol, started_at_ms
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
              AND validated_outcome = 'followed_through'
            """
        ),
        {"since": recent_ms},
    ).fetchall()
    if not alerts:
        return {"since_ms": recent_ms, "weights": [
            {
                "metric": m, "weight": 1.0, "relevance_score": 0.0,
                "samples": 0, "extreme_hits": 0, "extreme_share": 0.0,
            }
            for m in ANALYTICS_METRICS
        ]}

    # For each alert, find the most-recent sample-per-metric for that
    # symbol within ±5min. Bulk-fetch all relevant rows once.
    alert_keys: List[Tuple[str, int]] = [(a.symbol, int(a.started_at_ms)) for a in alerts]
    syms = list({k[0] for k in alert_keys})
    sample_rows = db.execute(
        text(
            """
            SELECT symbol, metric, ts, value
            FROM liquidity_samples
            WHERE symbol = ANY(:syms)
              AND metric = ANY(:metrics)
              AND ts >= :since
              AND value IS NOT NULL
            ORDER BY symbol, metric, ts
            """
        ),
        {"syms": syms, "metrics": list(ANALYTICS_METRICS), "since": recent_ms},
    ).fetchall()
    by_sym_metric: Dict[Tuple[str, str], List[Tuple[int, float]]] = defaultdict(list)
    for s in sample_rows:
        by_sym_metric[(s.symbol, s.metric)].append((int(s.ts), float(s.value)))

    extreme_counts: Dict[str, int] = defaultdict(int)
    total_counts: Dict[str, int] = defaultdict(int)
    for sym, ts in alert_keys:
        for metric in ANALYTICS_METRICS:
            series = by_sym_metric.get((sym, metric))
            if not series:
                continue
            # Find closest sample within 5 minutes.
            best = min(series, key=lambda p: abs(p[0] - ts))
            if abs(best[0] - ts) > 5 * 60_000:
                continue
            total_counts[metric] += 1
            band = cohort.get(metric)
            if band is None:
                continue
            p10, p90 = band
            if best[1] <= p10 or best[1] >= p90:
                extreme_counts[metric] += 1

    out: List[dict] = []
    for metric in ANALYTICS_METRICS:
        seen = total_counts.get(metric, 0)
        ext = extreme_counts.get(metric, 0)
        # Relevance share — what fraction of followed-through alerts had
        # this metric in an extreme decile. Random expectation is 20%
        # (top + bottom decile). Map (share / 0.20) into a weight band
        # 0.5 .. 1.8.
        share = (ext / seen) if seen > 0 else 0.0
        relevance = share / 0.20 if share > 0 else 0.0
        weight = max(0.5, min(1.8, 0.7 + 0.55 * relevance))   # 0 share→0.7; 1.0 share→1.25; 2.0→1.8
        out.append({
            "metric": metric,
            "samples": seen,
            "extreme_hits": ext,
            "extreme_share": share,
            "relevance_score": relevance * 100.0,
            "weight": weight,
        })
    out.sort(key=lambda d: -d["relevance_score"])
    return {"since_ms": recent_ms, "weights": out}


# ── State fingerprints & embeddings ──────────────────────────────────────


# A compact, hand-picked feature set — covers the four families that
# Phase 5 alert engine cares about (positioning, microstructure, stress,
# stability). Keep this short so anomaly-similarity hashing stays cheap.
EMBEDDING_METRICS: Tuple[str, ...] = (
    "fragility_score", "resiliency_score", "credible_depth",
    "spread", "obi", "liq_stress", "funding_z", "oi_delta_1h",
)


def _fingerprint_current(db: Session) -> Dict[str, float]:
    """Universe-level fingerprint right now: cohort median per metric
    over the last 30 minutes."""
    rows = db.execute(
        text(
            """
            SELECT metric, percentile_disc(0.5) WITHIN GROUP (ORDER BY value) AS med
            FROM liquidity_samples
            WHERE metric = ANY(:metrics)
              AND ts >= :since
              AND value IS NOT NULL
            GROUP BY metric
            """
        ),
        {
            "metrics": list(EMBEDDING_METRICS),
            "since": int(time.time() * 1000) - 30 * 60_000,
        },
    ).fetchall()
    return {r.metric: float(r.med) for r in rows if r.med is not None}


def state_embedding(db: Session) -> dict:
    """Public read of the current universe fingerprint — handy for the
    Meta page's 'where are we in state-space right now' panel."""
    fp = _fingerprint_current(db)
    return {
        "metrics": list(EMBEDDING_METRICS),
        "fingerprint": fp,
        "ts_ms": int(time.time() * 1000),
    }


def _fingerprint_distance(a: Dict[str, float], b: Dict[str, float], scales: Optional[Dict[str, float]] = None) -> Optional[float]:
    if not a or not b:
        return None
    metrics = set(a) & set(b)
    if not metrics:
        return None
    if scales is None:
        scales = {m: max(abs(a[m]), abs(b[m]), 1e-6) for m in metrics}
    ssq = 0.0
    for m in metrics:
        ssq += ((a[m] - b[m]) / max(scales[m], 1e-9)) ** 2
    return math.sqrt(ssq / len(metrics))


# ── Anomaly memory ───────────────────────────────────────────────────────


def record_anomaly(
    db: Session,
    kind: str,
    severity: str,
    fingerprint: Dict[str, float],
    related_alert_ids: Optional[List[str]] = None,
    notes: Optional[str] = None,
) -> dict:
    """Insert an anomaly observation and compute its novelty against
    all prior observations of the same kind. Returns the new row.

    Novelty = 100 × min(distance / 1.0, 1) — distances are normalized
    L2 in the metric space, so distance ≥ 1 means "essentially unrelated
    to anything in memory" → novelty 100. Distance 0 → novelty 0 (exact
    recurrence).
    """
    import json as _json
    from kazus_db.models import LiquidityAnomalyMemory

    occurred_ms = int(time.time() * 1000)
    fp_json = _json.dumps(fingerprint, sort_keys=True)

    # Look at the prior history for this kind.
    prior = (
        db.query(LiquidityAnomalyMemory)
        .filter(LiquidityAnomalyMemory.kind == kind)
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .limit(500)
        .all()
    )
    best_distance: Optional[float] = None
    best_match_id: Optional[int] = None
    for p in prior:
        try:
            prev_fp = _json.loads(p.fingerprint_json)
        except (TypeError, ValueError):
            continue
        d = _fingerprint_distance(fingerprint, prev_fp)
        if d is None:
            continue
        if best_distance is None or d < best_distance:
            best_distance = d
            best_match_id = p.id

    if best_distance is None:
        novelty = 100.0
        recurrence_count = 0
    else:
        novelty = min(100.0, max(0.0, best_distance * 100.0))
        recurrence_count = sum(1 for p in prior if _fingerprint_distance(fingerprint, _json.loads(p.fingerprint_json) if p.fingerprint_json else {}) is not None and _fingerprint_distance(fingerprint, _json.loads(p.fingerprint_json)) < 0.4)

    row = LiquidityAnomalyMemory(
        kind=kind,
        severity=severity,
        occurred_at_ms=occurred_ms,
        fingerprint_json=fp_json,
        novelty_score=novelty,
        recurrence_count=recurrence_count,
        related_alert_ids_json=_json.dumps(related_alert_ids) if related_alert_ids else None,
        notes=notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "kind": row.kind,
        "severity": row.severity,
        "occurred_at_ms": row.occurred_at_ms,
        "novelty_score": row.novelty_score,
        "recurrence_count": row.recurrence_count,
        "fingerprint": fingerprint,
        "best_match_id": best_match_id,
        "best_match_distance": best_distance,
    }


def query_anomaly_memory(db: Session, kind: Optional[str], since_ms: Optional[int], limit: int = 100) -> dict:
    import json as _json
    from kazus_db.models import LiquidityAnomalyMemory

    q = db.query(LiquidityAnomalyMemory)
    if kind:
        q = q.filter(LiquidityAnomalyMemory.kind == kind)
    if since_ms is not None:
        q = q.filter(LiquidityAnomalyMemory.occurred_at_ms >= since_ms)
    rows = q.order_by(LiquidityAnomalyMemory.occurred_at_ms.desc()).limit(limit).all()
    out: List[dict] = []
    for r in rows:
        try:
            fp = _json.loads(r.fingerprint_json) if r.fingerprint_json else {}
        except (TypeError, ValueError):
            fp = {}
        out.append({
            "id": r.id,
            "kind": r.kind,
            "severity": r.severity,
            "occurred_at_ms": r.occurred_at_ms,
            "fingerprint": fp,
            "novelty_score": r.novelty_score,
            "recurrence_count": r.recurrence_count,
            "notes": r.notes,
        })
    # Counts per kind across the queried window.
    from sqlalchemy import func
    counts_rows = q.with_entities(LiquidityAnomalyMemory.kind, func.count()).group_by(LiquidityAnomalyMemory.kind).all()
    counts = {k: int(c) for k, c in counts_rows}
    return {"items": out, "counts_by_kind": counts}


# ── Edge mutation tracking ───────────────────────────────────────────────


def edge_mutation(db: Session, since_ms: int, window_days: int = 7) -> dict:
    """Compare per-kind precision in the recent window vs the prior
    window-of-equal-length. Surface kinds whose precision moved
    materially → mutation_score scaled by |delta|, mutation_direction
    is sign, mutation_velocity is per-day rate.

    Inversion is the special case where precision crosses 0.5: a former
    edge has become anti-predictive.
    """
    half_ms = window_days * 24 * 3600 * 1000
    mid_ms = max(since_ms, int(time.time() * 1000) - half_ms)
    rows = db.execute(
        text(
            """
            SELECT kind,
              SUM(CASE WHEN started_at_ms >= :mid THEN 1 ELSE 0 END) AS recent_total,
              SUM(CASE WHEN started_at_ms < :mid THEN 1 ELSE 0 END) AS prior_total,
              SUM(CASE WHEN started_at_ms >= :mid AND validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS recent_ft,
              SUM(CASE WHEN started_at_ms < :mid AND validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS prior_ft,
              SUM(CASE WHEN started_at_ms >= :mid AND validated_outcome = 'noise' THEN 1 ELSE 0 END) AS recent_noise,
              SUM(CASE WHEN started_at_ms < :mid AND validated_outcome = 'noise' THEN 1 ELSE 0 END) AS prior_noise
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY kind
            """
        ),
        {"since": since_ms, "mid": mid_ms},
    ).fetchall()

    out: List[dict] = []
    for r in rows:
        rec_resolved = int(r.recent_ft or 0) + int(r.recent_noise or 0)
        pri_resolved = int(r.prior_ft or 0) + int(r.prior_noise or 0)
        if rec_resolved < 5 and pri_resolved < 5:
            continue
        rec_p = (int(r.recent_ft) / rec_resolved) if rec_resolved > 0 else None
        pri_p = (int(r.prior_ft) / pri_resolved) if pri_resolved > 0 else None
        delta = None
        velocity = None
        if rec_p is not None and pri_p is not None:
            delta = rec_p - pri_p
            velocity = delta / max(1.0, window_days)
        inverted = (
            rec_p is not None and pri_p is not None and
            ((rec_p - 0.5) * (pri_p - 0.5) < 0)
        )
        direction = "NEUTRAL"
        if delta is not None:
            if delta > 0.08:
                direction = "STRENGTHENING"
            elif delta < -0.08:
                direction = "WEAKENING"
        if inverted:
            direction = "INVERTED"
        mutation_score = (abs(delta) * 100.0) if delta is not None else 0.0
        out.append({
            "kind": r.kind,
            "recent_precision": rec_p,
            "prior_precision": pri_p,
            "recent_resolved": rec_resolved,
            "prior_resolved": pri_resolved,
            "delta": delta,
            "mutation_velocity_per_day": velocity,
            "mutation_score": mutation_score,
            "mutation_direction": direction,
            "inverted": inverted,
        })
    out.sort(key=lambda d: -d["mutation_score"])
    return {"since_ms": since_ms, "window_days": window_days, "kinds": out}


# ── Dynamic regime compression ───────────────────────────────────────────


def regime_compression(db: Session, since_ms: int) -> dict:
    """Pairwise similarity between regimes based on their alert-kind
    distributions. Regimes whose alert profiles overlap closely are
    candidates for merging; novel/isolated profiles are emergent
    clusters.

    Distance = 1 − cosine similarity on the normalized alert-kind vector.
    """
    rows = db.execute(
        text(
            """
            SELECT regime, kind, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY regime, kind
            """
        ),
        {"since": since_ms},
    ).fetchall()

    profiles: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        profiles[r.regime][r.kind] = float(r.c)
    # Normalize.
    norms: Dict[str, dict] = {}
    for regime, kc in profiles.items():
        total = sum(kc.values())
        if total <= 0:
            continue
        norms[regime] = {k: v / total for k, v in kc.items()}

    regimes = list(norms.keys())
    cells: List[dict] = []
    merge_candidates: List[dict] = []
    for i, a in enumerate(regimes):
        for j, b in enumerate(regimes):
            if i > j:
                continue
            va = norms[a]
            vb = norms[b]
            keys = set(va) | set(vb)
            dot = sum(va.get(k, 0.0) * vb.get(k, 0.0) for k in keys)
            na = math.sqrt(sum(v * v for v in va.values()))
            nb = math.sqrt(sum(v * v for v in vb.values()))
            cos = (dot / (na * nb)) if (na > 0 and nb > 0) else 0.0
            distance = 1.0 - cos
            cells.append({"a": a, "b": b, "cosine": cos, "distance": distance})
            if a != b and cos >= 0.85:
                merge_candidates.append({"a": a, "b": b, "cosine": cos})

    merge_candidates.sort(key=lambda d: -d["cosine"])
    return {
        "since_ms": since_ms,
        "regimes": regimes,
        "matrix": cells,
        "merge_candidates": merge_candidates[:10],
    }


# ── Meta-intelligence health ─────────────────────────────────────────────


def meta_intelligence_health(db: Session) -> dict:
    """One composite health number for the engine itself. Blends:

      * meta_confidence (Phase 10) — how reliable is our own confidence
      * structural break score — if structure broke, our calibration is
        stale by definition
      * alert saturation — alerts/hour vs the long-term baseline
      * edge mutation magnitude — sum of |delta| across mutating kinds
      * regime fragmentation — distinct dominant regimes per day
    """
    now_ms = int(time.time() * 1000)
    since_30d = now_ms - 30 * 24 * 3600 * 1000
    since_7d = now_ms - 7 * 24 * 3600 * 1000

    mc = meta_confidence(db, since_ms=since_30d)
    sb = structural_breaks(db, window_days=7)
    mut = edge_mutation(db, since_ms=since_30d, window_days=7)

    # Alert saturation: alerts/hour in last 24h vs in last 30d.
    sat_rows = db.execute(
        text(
            """
            SELECT
              SUM(CASE WHEN started_at_ms >= :last_24h THEN 1 ELSE 0 END) AS recent,
              COUNT(*) AS total
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            """
        ),
        {"since": since_30d, "last_24h": now_ms - 24 * 3600 * 1000},
    ).first()
    recent_rate = int(sat_rows.recent or 0) / 24.0 if sat_rows else 0.0
    avg_rate = int(sat_rows.total or 0) / (30 * 24.0) if sat_rows else 0.0
    saturation_ratio = (recent_rate / avg_rate) if avg_rate > 0 else 1.0
    saturation_score = max(0.0, 100.0 - max(0.0, saturation_ratio - 2.0) * 20.0)  # >2x saturation tax

    mutation_total = sum(abs(k.get("delta") or 0) for k in mut.get("kinds", []))
    mutation_score = max(0.0, 100.0 - mutation_total * 100.0)

    # Regime fragmentation — distinct regimes per day in last 7d.
    frag_rows = db.execute(
        text(
            """
            SELECT (started_at_ms / 86400000) AS day_bucket, COUNT(DISTINCT regime) AS dr
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY day_bucket
            """
        ),
        {"since": since_7d},
    ).fetchall()
    avg_dr = sum(int(r.dr) for r in frag_rows) / len(frag_rows) if frag_rows else 1.0
    # Above 3 distinct regimes/day is fragmented.
    fragmentation_score = max(0.0, 100.0 - max(0.0, avg_dr - 2.5) * 25.0)

    components = {
        "meta_confidence": mc["meta_confidence_score"],
        "structural_stability": max(0.0, 100.0 - sb["structural_break_score"]),
        "alert_saturation": saturation_score,
        "edge_consistency": mutation_score,
        "regime_focus": fragmentation_score,
    }
    health = sum(components.values()) / len(components)

    if health >= 75:
        state = "HEALTHY"
    elif health >= 55:
        state = "DRIFTING"
    elif health >= 35:
        state = "DEGRADING"
    else:
        state = "CRITICAL"

    self_consistency = mc["confidence_stability"]
    adaptation_quality = min(100.0, 100.0 - mutation_total * 200.0 + (100 - sb["structural_break_score"]) * 0.3)

    return {
        "meta_intelligence_health": health,
        "state": state,
        "self_consistency_score": self_consistency,
        "adaptation_quality": max(0.0, min(100.0, adaptation_quality)),
        "components": components,
        "alert_saturation_ratio": saturation_ratio,
        "avg_distinct_regimes_per_day": avg_dr,
        "mutation_magnitude_sum": mutation_total,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-12 — Autonomous Intelligence Coordination
# ══════════════════════════════════════════════════════════════════════════
#
# Auto-recorder + cross-layer synthesis. The auto-recorder runs from the
# worker on its own slow cadence; the synthesis endpoints are read-only
# views over the same data, used by the Coord page.


# ── Auto-anomaly recording ────────────────────────────────────────────────


# Cooldown per anomaly kind — we don't want the worker re-recording the
# same break every cycle. The kind-specific rates reflect how slowly
# each underlying signal moves: structural breaks are 7-day windows so
# 6h between observations is plenty; pre_cascade is a fast signal so we
# allow re-recording every 30 min.
AUTO_ANOMALY_COOLDOWN_MS: Dict[str, int] = {
    "structural_break": 6 * 3600 * 1000,
    "regime_collapse": 2 * 3600 * 1000,
    "venue_divergence": 2 * 3600 * 1000,
    "pre_cascade": 30 * 60_000,
    "edge_inversion": 6 * 3600 * 1000,
    "regime_emergence": 6 * 3600 * 1000,
}


def _last_anomaly_ts(db: Session, kind: str) -> Optional[int]:
    from kazus_db.models import LiquidityAnomalyMemory
    row = (
        db.query(LiquidityAnomalyMemory.occurred_at_ms)
        .filter(LiquidityAnomalyMemory.kind == kind)
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .first()
    )
    return int(row.occurred_at_ms) if row else None


def auto_record_anomalies(db: Session) -> dict:
    """One scan: pulls Phase-10/11 layers and decides whether any of
    them are tripping the auto-record thresholds. Each candidate kind
    is checked against its cooldown so the worker can call this every
    few minutes without flooding the table.

    Returns the list of newly inserted rows + a per-kind decision log
    so the UI can show "scanned but suppressed by cooldown".
    """
    now_ms = int(time.time() * 1000)
    decisions: List[dict] = []
    inserted: List[dict] = []

    # 1) Structural break — record if score ≥ 55 AND confidence ≥ 50.
    try:
        sb = structural_breaks(db, window_days=7)
        if sb["structural_break_score"] >= 55 and sb["break_confidence"] >= 50:
            kind = "structural_break"
            last = _last_anomaly_ts(db, kind)
            if last is None or now_ms - last >= AUTO_ANOMALY_COOLDOWN_MS[kind]:
                rec = record_anomaly(
                    db, kind=kind,
                    severity=("critical" if sb["structural_break_score"] >= 75 else "warn"),
                    fingerprint=_universe_fp(db, extra={
                        "structural_break_score": sb["structural_break_score"],
                        "correlation_drift": sb["components"]["correlation_drift"],
                        "median_migration": sb["components"]["median_migration"],
                        "regime_mix_shift": sb["components"]["regime_mix_shift"],
                    }),
                    notes=f"auto-recorded · break={sb['structural_break_score']:.0f}",
                )
                inserted.append(rec)
                decisions.append({"kind": kind, "action": "recorded", "score": sb["structural_break_score"]})
            else:
                decisions.append({"kind": kind, "action": "cooldown",
                                  "score": sb["structural_break_score"],
                                  "next_eligible_in_ms": AUTO_ANOMALY_COOLDOWN_MS[kind] - (now_ms - last)})
        else:
            decisions.append({"kind": "structural_break", "action": "below_threshold",
                              "score": sb["structural_break_score"]})
    except Exception as exc:  # noqa: BLE001
        decisions.append({"kind": "structural_break", "action": "error", "error": str(exc)})

    # 2) Pre-cascade — record on PRE_CASCADE state.
    try:
        rsw = regime_shift_warning(db)
        kind = "pre_cascade"
        if rsw["warning_state"] == "PRE_CASCADE":
            last = _last_anomaly_ts(db, kind)
            if last is None or now_ms - last >= AUTO_ANOMALY_COOLDOWN_MS[kind]:
                rec = record_anomaly(
                    db, kind=kind, severity="critical",
                    fingerprint=_universe_fp(db, extra={
                        "regime_shift_probability": rsw["regime_shift_probability"],
                        "instability_acceleration": rsw["instability_acceleration"],
                    }),
                    notes=f"auto-recorded · {rsw['warning_state']} · prob={rsw['regime_shift_probability']:.0f}%",
                )
                inserted.append(rec)
                decisions.append({"kind": kind, "action": "recorded",
                                  "score": rsw["regime_shift_probability"]})
            else:
                decisions.append({"kind": kind, "action": "cooldown",
                                  "next_eligible_in_ms": AUTO_ANOMALY_COOLDOWN_MS[kind] - (now_ms - last)})
        else:
            decisions.append({"kind": kind, "action": "below_threshold",
                              "state": rsw["warning_state"]})
    except Exception as exc:  # noqa: BLE001
        decisions.append({"kind": "pre_cascade", "action": "error", "error": str(exc)})

    # 3) Regime collapse — record on a transition matrix hit where
    #    collapse_prob ≥ 0.5 for the dominant regime.
    try:
        ro = regime_outcomes(db, since_ms=now_ms - 7 * 24 * 3600 * 1000)
        for r in ro["regimes"]:
            if r["collapse_prob"] >= 0.5 and r["count"] >= 20:
                kind = "regime_collapse"
                last = _last_anomaly_ts(db, kind)
                if last is None or now_ms - last >= AUTO_ANOMALY_COOLDOWN_MS[kind]:
                    rec = record_anomaly(
                        db, kind=kind, severity="critical",
                        fingerprint=_universe_fp(db, extra={
                            "regime": hash(r["regime"]) % 10_000 / 10_000.0,  # categorical proxy
                            "collapse_prob": r["collapse_prob"],
                            "count": float(r["count"]),
                        }),
                        notes=f"auto-recorded · {r['regime']} collapse_prob={r['collapse_prob']*100:.0f}%",
                    )
                    inserted.append(rec)
                    decisions.append({"kind": kind, "action": "recorded", "regime": r["regime"]})
                    break    # one per scan
    except Exception as exc:  # noqa: BLE001
        decisions.append({"kind": "regime_collapse", "action": "error", "error": str(exc)})

    # 4) Edge inversion — when edge_mutation flags any kind as INVERTED.
    try:
        mut = edge_mutation(db, since_ms=now_ms - 60 * 24 * 3600 * 1000, window_days=7)
        inverted = [k for k in mut["kinds"] if k["inverted"]]
        if inverted:
            kind = "edge_inversion"
            last = _last_anomaly_ts(db, kind)
            if last is None or now_ms - last >= AUTO_ANOMALY_COOLDOWN_MS[kind]:
                top = inverted[0]
                rec = record_anomaly(
                    db, kind=kind, severity="warn",
                    fingerprint=_universe_fp(db, extra={
                        "delta": (top["delta"] or 0.0),
                        "mutation_score": top["mutation_score"],
                    }),
                    notes=f"auto-recorded · {top['kind']} inverted · Δ={top['delta'] * 100 if top['delta'] is not None else 0:.1f}pp",
                )
                inserted.append(rec)
                decisions.append({"kind": kind, "action": "recorded", "alert_kind": top["kind"]})
    except Exception as exc:  # noqa: BLE001
        decisions.append({"kind": "edge_inversion", "action": "error", "error": str(exc)})

    # 5) Venue divergence — when avg mid_price divergence ≥ 0.1% over the
    #    most recent venue snapshots.
    try:
        recent_ms = now_ms - 60 * 60_000
        rows = db.execute(
            text(
                """
                WITH ref AS (
                  SELECT symbol, ts_ms, mid_price
                  FROM liquidity_crossex_history
                  WHERE exchange = 'binance' AND ts_ms >= :since
                ), other AS (
                  SELECT symbol, ts_ms, exchange, mid_price
                  FROM liquidity_crossex_history
                  WHERE exchange != 'binance' AND ts_ms >= :since
                )
                SELECT
                  AVG(ABS(other.mid_price - ref.mid_price) / NULLIF(ref.mid_price, 0)) * 100 AS pct
                FROM other JOIN ref
                  ON other.symbol = ref.symbol AND ABS(other.ts_ms - ref.ts_ms) < 60000
                """
            ),
            {"since": recent_ms},
        ).first()
        pct = float(rows.pct) if rows and rows.pct is not None else 0.0
        if pct >= 0.10:
            kind = "venue_divergence"
            last = _last_anomaly_ts(db, kind)
            if last is None or now_ms - last >= AUTO_ANOMALY_COOLDOWN_MS[kind]:
                rec = record_anomaly(
                    db, kind=kind, severity=("critical" if pct >= 0.25 else "warn"),
                    fingerprint=_universe_fp(db, extra={"avg_mid_divergence_pct": pct}),
                    notes=f"auto-recorded · avg mid divergence {pct:.3f}%",
                )
                inserted.append(rec)
                decisions.append({"kind": kind, "action": "recorded", "pct": pct})
    except Exception as exc:  # noqa: BLE001
        decisions.append({"kind": "venue_divergence", "action": "error", "error": str(exc)})

    return {
        "fetched_at_ms": now_ms,
        "inserted": inserted,
        "decisions": decisions,
    }


def _universe_fp(db: Session, extra: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Universe fingerprint plus optional extras. Mostly for the auto
    recorder so the anomaly carries enough context to be matched later."""
    fp = _fingerprint_current(db)
    if extra:
        fp.update({k: float(v) for k, v in extra.items() if v is not None})
    return fp


# ── Intelligence synthesis ────────────────────────────────────────────────


def intelligence_synthesis(db: Session) -> dict:
    """Pulls every layer's current view + composes a single coordinated
    interpretation. Cross-layer agreement is the share of layers that
    agree directionally with the dominant assessment.
    """
    now_ms = int(time.time() * 1000)
    since_30d = now_ms - 30 * 24 * 3600 * 1000

    rs = risk_state(db)
    rsw = regime_shift_warning(db)
    sb = structural_breaks(db, window_days=7)
    mc = meta_confidence(db, since_ms=since_30d)
    mh = meta_intelligence_health(db)
    strat = strategic_state(db)

    # Each layer votes on a 0..100 "stress dial". The synthesis is the
    # weighted mean; agreement is the fraction within ±20 of that mean.
    layers = {
        "operations": rs["risk_state_score"],
        "regime_shift": rsw["regime_shift_probability"],
        "structural": sb["structural_break_score"],
        "meta_confidence_inverse": max(0.0, 100.0 - mc["meta_confidence_score"]),
        "intelligence_health_inverse": max(0.0, 100.0 - mh["meta_intelligence_health"]),
    }
    weights = {
        "operations": 1.2,
        "regime_shift": 1.0,
        "structural": 1.0,
        "meta_confidence_inverse": 0.6,
        "intelligence_health_inverse": 0.8,
    }
    total_w = sum(weights.values())
    synthesized = sum(layers[k] * weights[k] for k in layers) / total_w
    in_band = sum(1 for v in layers.values() if abs(v - synthesized) <= 20)
    agreement = (in_band / len(layers)) * 100.0

    # Coordinated state label — composite over the strategic state +
    # synthesized score. We can refine these labels by tightening
    # transitions; for now, three escalation rungs above STABLE.
    state = "STABLE_COORDINATED_MARKET"
    if strat["state"] == "CASCADE_RISK_ENVIRONMENT" or synthesized >= 70:
        state = "ACTIVE_CASCADE_PROPAGATION"
    elif strat["state"] in ("TRANSITIONAL_UNSTABLE", "LIQUIDITY_DETERIORATION_PHASE") or synthesized >= 55:
        state = "ESCALATING_SYSTEMIC_INSTABILITY"
    elif strat["state"] == "FRAGILE_SPECULATIVE_MARKET" or synthesized >= 40:
        state = "FRAGMENTING_LIQUIDITY_ENVIRONMENT"
    elif synthesized >= 25:
        state = "EARLY_STRUCTURAL_STRESS"
    elif sb["structural_break_score"] >= 50 and mh["state"] in ("DEGRADING", "CRITICAL"):
        state = "STRUCTURAL_MARKET_DETERIORATION"

    return {
        "fetched_at_ms": now_ms,
        "synthesized_stress": synthesized,
        "coordinated_state": state,
        "cross_layer_agreement": agreement,
        "layers": [
            {"name": k, "score": v, "weight": weights[k], "delta_from_mean": v - synthesized}
            for k, v in layers.items()
        ],
        "components": {
            "stress_level": rs["systemic_stress_level"],
            "shift_warning": rsw["warning_state"],
            "structural_break_score": sb["structural_break_score"],
            "meta_confidence_state": mc["trustworthiness_state"],
            "intelligence_health_state": mh["state"],
            "strategic_state": strat["state"],
        },
    }


# ── Intelligence conflict resolution ──────────────────────────────────────


def intelligence_conflicts(db: Session) -> dict:
    """Surface specific layer-vs-layer disagreements. The synthesis
    agreement metric is the headline; this endpoint enumerates the
    actual contradictions for the UI to render."""
    synth = intelligence_synthesis(db)
    conflicts: List[dict] = []

    # 1) Operations stress vs structural stability
    ops_score = next(l for l in synth["layers"] if l["name"] == "operations")["score"]
    struct_score = next(l for l in synth["layers"] if l["name"] == "structural")["score"]
    if abs(ops_score - struct_score) >= 35:
        if ops_score > struct_score:
            conflicts.append({
                "kind": "ops_vs_structural",
                "description": "Short-term stress elevated but long-term structure stable",
                "ops_score": ops_score,
                "structural_score": struct_score,
                "dominant_horizon": "short",
            })
        else:
            conflicts.append({
                "kind": "ops_vs_structural",
                "description": "Long-term structure deteriorating but short-term calm",
                "ops_score": ops_score,
                "structural_score": struct_score,
                "dominant_horizon": "long",
            })

    # 2) Regime shift warning vs meta-confidence
    shift_score = next(l for l in synth["layers"] if l["name"] == "regime_shift")["score"]
    mc_inv = next(l for l in synth["layers"] if l["name"] == "meta_confidence_inverse")["score"]
    if shift_score >= 50 and mc_inv >= 50:
        conflicts.append({
            "kind": "regime_shift_under_low_confidence",
            "description": "Regime shift signal firing while meta-confidence is low — interpret cautiously",
            "shift_probability": shift_score,
            "confidence_deficit": mc_inv,
        })

    # 3) Health vs synthesized stress
    health_inv = next(l for l in synth["layers"] if l["name"] == "intelligence_health_inverse")["score"]
    if synth["synthesized_stress"] >= 55 and health_inv <= 25:
        conflicts.append({
            "kind": "stress_without_health_signal",
            "description": "Synthesized stress is high but the engine itself reports healthy — consider whether thresholds are stale",
        })

    # Dominant layer = highest weighted contribution
    dominant_layer = max(synth["layers"], key=lambda l: abs(l["delta_from_mean"]) * l["weight"])
    # Suppressed = layers whose score is far below the mean
    suppressed = [l["name"] for l in synth["layers"] if l["delta_from_mean"] < -15]

    return {
        "fetched_at_ms": synth["fetched_at_ms"],
        "conflict_score": 100.0 - synth["cross_layer_agreement"],
        "conflicts": conflicts,
        "dominant_layer": dominant_layer["name"],
        "suppressed_layers": suppressed,
    }


# ── Adaptive alert suppression (read-only diagnostics) ────────────────────


def alert_suppression(db: Session, window_minutes: int = 60) -> dict:
    """How redundant is the recent alert stream? Group recent alerts by
    (symbol, kind) and report cluster size, dominant severity, and
    compression ratio (unique-cluster-count / total alert count).

    The Phase-5 client engine already debounces, but operators want to
    see when the engine is "saturating" — many critical alerts on one
    symbol over a short window means the underlying market state is
    not multiple problems, it's one problem firing many sensors.
    """
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_minutes * 60_000
    rows = db.execute(
        text(
            """
            SELECT symbol, kind, severity, COUNT(*) AS c,
                   MAX(last_seen_at_ms) AS last_seen
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY symbol, kind, severity
            """
        ),
        {"since": since_ms},
    ).fetchall()

    clusters: Dict[Tuple[str, str], dict] = {}
    total = 0
    for r in rows:
        c = int(r.c or 0)
        total += c
        key = (r.symbol, r.kind)
        cl = clusters.setdefault(key, {
            "symbol": r.symbol,
            "kind": r.kind,
            "count": 0,
            "max_severity": "info",
            "last_seen_ms": 0,
        })
        cl["count"] += c
        sev = r.severity
        if sev == "critical" or (sev == "warn" and cl["max_severity"] != "critical"):
            cl["max_severity"] = sev
        if int(r.last_seen or 0) > cl["last_seen_ms"]:
            cl["last_seen_ms"] = int(r.last_seen)

    cluster_list = list(clusters.values())
    cluster_list.sort(key=lambda c: -c["count"])
    redundant = [c for c in cluster_list if c["count"] >= 3]
    for i, c in enumerate(redundant, start=1):
        c["cluster_id"] = i
        c["redundancy_score"] = min(100.0, math.log10(c["count"]) * 60.0)
    compression_ratio = (len(clusters) / total) if total > 0 else 1.0

    return {
        "window_minutes": window_minutes,
        "total_alerts": total,
        "unique_clusters": len(clusters),
        "alert_compression_ratio": compression_ratio,
        "redundant_clusters": redundant[:20],
    }


# ── Structural crisis clustering ──────────────────────────────────────────


def crisis_clusters(db: Session, max_clusters: int = 8) -> dict:
    """Group anomaly_memory rows into clusters of similar fingerprints.

    Simple agglomerative-style first pass: walk anomalies newest-first,
    assign each to the nearest existing cluster centroid if distance
    < 0.5, otherwise spawn a new cluster. Centroid = arithmetic mean
    of cluster member fingerprints. Cluster size = number of members;
    frequency = members per day in the window covered.
    """
    import json as _json
    from kazus_db.models import LiquidityAnomalyMemory

    rows = (
        db.query(LiquidityAnomalyMemory)
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .limit(500)
        .all()
    )
    if not rows:
        return {"clusters": [], "anomaly_count": 0}

    clusters: List[dict] = []   # each {"centroid": dict, "members": [row, ...]}

    def _avg(maps: List[Dict[str, float]]) -> Dict[str, float]:
        keys = set()
        for m in maps:
            keys.update(m.keys())
        out: Dict[str, float] = {}
        for k in keys:
            vals = [m[k] for m in maps if k in m]
            if vals:
                out[k] = sum(vals) / len(vals)
        return out

    for r in rows:
        try:
            fp = _json.loads(r.fingerprint_json) if r.fingerprint_json else {}
        except (TypeError, ValueError):
            continue
        if not fp:
            continue
        best = None
        best_d = None
        for c in clusters:
            d = _fingerprint_distance(fp, c["centroid"])
            if d is None:
                continue
            if best_d is None or d < best_d:
                best_d = d
                best = c
        if best is not None and best_d is not None and best_d < 0.5:
            best["members"].append({"id": r.id, "kind": r.kind, "ts": r.occurred_at_ms, "novelty": r.novelty_score})
            # Rolling centroid: simple mean of old centroid + new fp. Drift
            # over very large clusters is acceptable — this is for visual
            # grouping, not k-means convergence.
            best["centroid"] = _avg([fp, best["centroid"]])
        else:
            clusters.append({"centroid": dict(fp), "members": [{
                "id": r.id, "kind": r.kind, "ts": r.occurred_at_ms, "novelty": r.novelty_score,
            }]})

    clusters.sort(key=lambda c: -len(c["members"]))
    out_clusters: List[dict] = []
    for idx, c in enumerate(clusters[:max_clusters], start=1):
        members = c["members"]
        span_ms = max(m["ts"] for m in members) - min(m["ts"] for m in members) if len(members) > 1 else 0
        days = max(0.5, span_ms / (24 * 3600 * 1000))
        # Dominant kind: most-frequent kind in the cluster.
        from collections import Counter
        kinds = Counter(m["kind"] for m in members)
        dominant_kind, _ = kinds.most_common(1)[0]
        out_clusters.append({
            "cluster_id": idx,
            "size": len(members),
            "dominant_kind": dominant_kind,
            "kinds": dict(kinds),
            "frequency_per_day": len(members) / days,
            "earliest_ts": min(m["ts"] for m in members),
            "latest_ts": max(m["ts"] for m in members),
            "avg_novelty": sum(m["novelty"] for m in members) / len(members),
            "centroid": c["centroid"],
            "recent_members": sorted(members, key=lambda m: -m["ts"])[:5],
        })
    return {"clusters": out_clusters, "anomaly_count": len(rows)}


def m_fp(_x):
    """Tiny stub so _avg above can stay simple — kept for clarity."""
    return _x if isinstance(_x, dict) else {}


# ── Narrative evolution (multi-period story) ──────────────────────────────


def narrative_evolution(db: Session) -> dict:
    """Build a multi-horizon narrative: what changed over 1h vs 24h vs 7d.

    Pulls cohort medians at three time-buckets and synthesizes the
    direction of change per metric, then composes a narrative bullet
    list. Output is plain text — no LLM, no recommendations.
    """
    now_ms = int(time.time() * 1000)

    def cohort_median(since_ms: int, until_ms: int) -> Dict[str, float]:
        rows = db.execute(
            text(
                """
                SELECT metric, percentile_disc(0.5) WITHIN GROUP (ORDER BY value) AS m
                FROM liquidity_samples
                WHERE metric = ANY(:metrics)
                  AND ts >= :since AND ts < :until AND value IS NOT NULL
                GROUP BY metric
                """
            ),
            {"metrics": list(ANALYTICS_METRICS), "since": since_ms, "until": until_ms},
        ).fetchall()
        return {r.metric: float(r.m) for r in rows if r.m is not None}

    h1 = cohort_median(now_ms - 1 * 3600_000, now_ms)
    h24 = cohort_median(now_ms - 24 * 3600_000, now_ms - 1 * 3600_000)
    d7 = cohort_median(now_ms - 7 * 24 * 3600_000, now_ms - 24 * 3600_000)

    def pct_change(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None or abs(b) < 1e-9:
            return None
        return (a - b) / abs(b) * 100.0

    metric_changes: List[dict] = []
    for m in ANALYTICS_METRICS:
        c_1h_vs_24h = pct_change(h1.get(m), h24.get(m))
        c_24h_vs_7d = pct_change(h24.get(m), d7.get(m))
        metric_changes.append({
            "metric": m,
            "h1": h1.get(m),
            "h24": h24.get(m),
            "d7": d7.get(m),
            "change_1h_vs_24h_pct": c_1h_vs_24h,
            "change_24h_vs_7d_pct": c_24h_vs_7d,
        })

    # Compose human-readable bullets per horizon.
    def describe(window_label: str, changes_key: str) -> List[str]:
        bullets: List[str] = []
        for mc in metric_changes:
            v = mc[changes_key]
            if v is None:
                continue
            if mc["metric"] in ("fragility_score", "spread", "liq_stress", "impact_score") and v > 10:
                bullets.append(f"{mc['metric']} rose {v:.0f}% {window_label}")
            if mc["metric"] in ("resiliency_score", "credible_depth") and v < -10:
                bullets.append(f"{mc['metric']} dropped {abs(v):.0f}% {window_label}")
            if mc["metric"] == "funding_z" and abs(v) > 20:
                bullets.append(f"funding extremity changed {v:.0f}% {window_label}")
            if mc["metric"] == "oi_delta_1h" and abs(v) > 20:
                bullets.append(f"OI expansion intensity moved {v:.0f}% {window_label}")
        return bullets

    short_bullets = describe("over the last hour", "change_1h_vs_24h_pct")
    long_bullets = describe("over the last day vs prior week", "change_24h_vs_7d_pct")

    return {
        "fetched_at_ms": now_ms,
        "horizons": [
            {"label": "1h", "window_ms": 3600_000},
            {"label": "24h", "window_ms": 24 * 3600_000},
            {"label": "7d", "window_ms": 7 * 24 * 3600_000},
        ],
        "metric_changes": metric_changes,
        "short_term_bullets": short_bullets,
        "long_term_bullets": long_bullets,
    }


# ── Multi-horizon coordination ────────────────────────────────────────────


def multi_horizon(db: Session) -> dict:
    """Align scores across short / medium / long horizons.

    Scores for each horizon are bounded 0..100 instability indices:
      short  = recent stress (cohort fragility/liq_stress percentile in last 1h)
      medium = 24h median fragility + spread vs the 7d band
      long   = structural_break_score over 7d
    """
    now_ms = int(time.time() * 1000)

    def cohort_band(since_ms: int) -> Dict[str, Tuple[float, float, float]]:
        # Returns metric -> (p10, p50, p90)
        rows = db.execute(
            text(
                """
                SELECT metric,
                       percentile_disc(0.10) WITHIN GROUP (ORDER BY value) AS p10,
                       percentile_disc(0.50) WITHIN GROUP (ORDER BY value) AS p50,
                       percentile_disc(0.90) WITHIN GROUP (ORDER BY value) AS p90
                FROM liquidity_samples
                WHERE metric = ANY(:metrics)
                  AND ts >= :since AND value IS NOT NULL
                GROUP BY metric
                """
            ),
            {"metrics": ["fragility_score", "spread", "liq_stress", "resiliency_score"], "since": since_ms},
        ).fetchall()
        return {r.metric: (float(r.p10), float(r.p50), float(r.p90)) for r in rows if r.p50 is not None}

    band_7d = cohort_band(now_ms - 7 * 24 * 3600_000)
    band_24h = cohort_band(now_ms - 24 * 3600_000)
    band_1h = cohort_band(now_ms - 3600_000)

    def short_score() -> Optional[float]:
        # Fragility median + (100 − resiliency median).
        f = band_1h.get("fragility_score")
        r = band_1h.get("resiliency_score")
        if f is None and r is None:
            return None
        parts: List[float] = []
        if f is not None:
            parts.append(min(100.0, max(0.0, f[1])))
        if r is not None:
            parts.append(min(100.0, max(0.0, 100.0 - r[1])))
        return sum(parts) / len(parts)

    def medium_score() -> Optional[float]:
        # How far the 24h median sits inside the 7d (p10, p90) band, for
        # spread + fragility. 0 = at p10 (calm), 100 = at p90+ (stressed).
        parts: List[float] = []
        for m in ("spread", "fragility_score"):
            b24 = band_24h.get(m)
            b7 = band_7d.get(m)
            if not b24 or not b7:
                continue
            p10, _, p90 = b7
            span = max(1e-9, p90 - p10)
            pos = (b24[1] - p10) / span
            parts.append(max(0.0, min(1.0, pos)) * 100.0)
        if not parts:
            return None
        return sum(parts) / len(parts)

    def long_score() -> Optional[float]:
        sb = structural_breaks(db, window_days=7)
        return sb["structural_break_score"]

    short = short_score()
    medium = medium_score()
    long_ = long_score()

    # Alignment: stdev across the three normalized scores.
    avail = [v for v in (short, medium, long_) if v is not None]
    alignment = None
    if len(avail) >= 2:
        m = sum(avail) / len(avail)
        var = sum((x - m) ** 2 for x in avail) / len(avail)
        std = math.sqrt(var)
        alignment = max(0.0, 100.0 - std * 2.0)   # std=50 → 0

    dominant = None
    if short is not None and medium is not None and long_ is not None:
        names = [("short", short), ("medium", medium), ("long", long_)]
        dominant = max(names, key=lambda kv: kv[1])[0]

    # Conflict map: per-pair |delta|.
    def diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return None if a is None or b is None else abs(a - b)

    conflicts = {
        "short_vs_medium": diff(short, medium),
        "short_vs_long": diff(short, long_),
        "medium_vs_long": diff(medium, long_),
    }

    if alignment is None:
        state = "INSUFFICIENT_DATA"
    elif alignment >= 80:
        state = "ALIGNED"
    elif alignment >= 55:
        state = "DIVERGENT"
    else:
        state = "FRAGMENTED"

    return {
        "fetched_at_ms": now_ms,
        "scores": {"short": short, "medium": medium, "long": long_},
        "horizon_alignment_score": alignment,
        "horizon_conflict_map": conflicts,
        "dominant_horizon": dominant,
        "structural_alignment_state": state,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-13 — Market Memory & Evolution
# ══════════════════════════════════════════════════════════════════════════
#
# Genealogy edges over anomaly_memory + intelligence-state history. The
# worker writes both — endpoints below are read-only views over them.


# ── Auto-linking edges on insertion ───────────────────────────────────────


def link_anomaly_edges(db: Session, new_id: int) -> List[dict]:
    """Inspect a freshly-recorded anomaly and link it to recent /
    historically-similar prior ones. Returns the list of inserted edges.

    Heuristic:
      * `preceded` — any anomaly of the same kind in the last 7d that
        landed before this one;
      * `historically_similar` — top-3 priors with normalized L2
        distance < 0.5 across all kinds;
      * `caused_by` — for "pre_cascade" / "regime_collapse", any
        anomaly in the last 24h whose distance < 0.7 is treated as a
        likely precursor.
    """
    import json as _json
    from kazus_db.models import LiquidityAnomalyEdge, LiquidityAnomalyMemory

    new_row = (
        db.query(LiquidityAnomalyMemory)
        .filter(LiquidityAnomalyMemory.id == new_id)
        .first()
    )
    if new_row is None:
        return []
    try:
        new_fp = _json.loads(new_row.fingerprint_json) if new_row.fingerprint_json else {}
    except (TypeError, ValueError):
        new_fp = {}

    now_ms = int(time.time() * 1000)
    week_ago = now_ms - 7 * 24 * 3600 * 1000
    day_ago = now_ms - 24 * 3600 * 1000

    same_kind = (
        db.query(LiquidityAnomalyMemory)
        .filter(
            LiquidityAnomalyMemory.id != new_id,
            LiquidityAnomalyMemory.kind == new_row.kind,
            LiquidityAnomalyMemory.occurred_at_ms >= week_ago,
            LiquidityAnomalyMemory.occurred_at_ms < new_row.occurred_at_ms,
        )
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .limit(20)
        .all()
    )
    all_recent = (
        db.query(LiquidityAnomalyMemory)
        .filter(
            LiquidityAnomalyMemory.id != new_id,
            LiquidityAnomalyMemory.occurred_at_ms < new_row.occurred_at_ms,
        )
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .limit(200)
        .all()
    )

    inserts: List[Tuple[int, int, str, float]] = []

    # preceded — latest same-kind prior wins as the strongest link.
    if same_kind:
        prev = same_kind[0]
        inserts.append((prev.id, new_id, "preceded", 1.0))

    # historically_similar — top-3 closest across all kinds.
    scored: List[Tuple[float, LiquidityAnomalyMemory]] = []
    for p in all_recent:
        try:
            prev_fp = _json.loads(p.fingerprint_json) if p.fingerprint_json else {}
        except (TypeError, ValueError):
            continue
        d = _fingerprint_distance(new_fp, prev_fp)
        if d is None or d >= 0.5:
            continue
        scored.append((d, p))
    scored.sort(key=lambda x: x[0])
    for d, p in scored[:3]:
        # Convention: store with from_id < to_id for similarity edges to dedup.
        lo, hi = (p.id, new_id) if p.id < new_id else (new_id, p.id)
        inserts.append((lo, hi, "historically_similar", 1.0 - d))

    # caused_by — only for escalation kinds, against priors within last day.
    if new_row.kind in ("pre_cascade", "regime_collapse"):
        for d, p in scored[:5]:
            if p.occurred_at_ms < day_ago:
                continue
            if d >= 0.7:
                continue
            inserts.append((p.id, new_id, "caused_by", 1.0 - d))

    # evolved_into — escalation between sequential anomalies on the same
    # rough fingerprint locus: if the closest prior across all kinds is
    # within distance 0.35 AND less than 6h old, treat the new one as
    # an evolution of the prior.
    if scored and scored[0][0] <= 0.35 and (now_ms - scored[0][1].occurred_at_ms) <= 6 * 3600 * 1000:
        inserts.append((scored[0][1].id, new_id, "evolved_into", 1.0 - scored[0][0]))

    written: List[dict] = []
    seen = set()
    for fr, to, k, w in inserts:
        key = (fr, to, k)
        if key in seen:
            continue
        seen.add(key)
        try:
            db.add(LiquidityAnomalyEdge(from_id=fr, to_id=to, kind=k, weight=w))
            db.flush()
            written.append({"from_id": fr, "to_id": to, "kind": k, "weight": w})
        except Exception:
            db.rollback()
            continue
    if written:
        db.commit()
    return written


def auto_record_anomalies_with_links(db: Session) -> dict:
    """Wrapper around auto_record_anomalies that also writes genealogy
    edges for every fresh insertion. The worker calls this instead of
    the bare recorder so the graph stays current without a second job.
    """
    result = auto_record_anomalies(db)
    inserted = result.get("inserted") or []
    all_edges: List[dict] = []
    for row in inserted:
        try:
            edges = link_anomaly_edges(db, row["id"])
            all_edges.extend(edges)
        except Exception as exc:  # noqa: BLE001
            # never let edge creation break the recorder
            db.rollback()
    result["edges"] = all_edges
    return result


# ── Periodic intelligence snapshot ────────────────────────────────────────


def snapshot_intelligence_history(db: Session) -> dict:
    """Persist one row into liquidity_intelligence_history with all the
    aggregate state numbers. Called by the worker every few minutes."""
    import json as _json
    from kazus_db.models import LiquidityIntelligenceHistory

    synth = intelligence_synthesis(db)
    mh = meta_intelligence_health(db)
    rs = risk_state(db)
    rsw = regime_shift_warning(db)
    sb = structural_breaks(db, window_days=7)
    fp = _fingerprint_current(db)

    # Dominant regime in last hour (descending count).
    rows = db.execute(
        text(
            """
            SELECT regime, COUNT(*) AS c
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY regime
            ORDER BY c DESC
            LIMIT 1
            """
        ),
        {"since": int(time.time() * 1000) - 3600_000},
    ).first()
    dominant = rows.regime if rows else "HEALTHY_TREND"

    row = LiquidityIntelligenceHistory(
        ts_ms=int(time.time() * 1000),
        synthesized_stress=synth["synthesized_stress"],
        coordinated_state=synth["coordinated_state"],
        cross_layer_agreement=synth["cross_layer_agreement"],
        structural_break_score=sb["structural_break_score"],
        meta_confidence_score=mh["components"].get("meta_confidence"),
        meta_intelligence_health=mh["meta_intelligence_health"],
        health_state=mh["state"],
        risk_state_score=rs["risk_state_score"],
        regime_shift_probability=rsw["regime_shift_probability"],
        dominant_regime=dominant,
        fingerprint_json=_json.dumps(fp, sort_keys=True),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "ts_ms": row.ts_ms,
        "synthesized_stress": row.synthesized_stress,
        "coordinated_state": row.coordinated_state,
        "meta_intelligence_health": row.meta_intelligence_health,
    }


# ── Anomaly genealogy / lineage ───────────────────────────────────────────


def anomaly_lineage(db: Session, anomaly_id: int, depth: int = 3) -> dict:
    """Walk both ancestor and descendant edges from an anomaly_id, up to
    `depth` hops. Returns parents/descendants grouped by edge kind so
    the UI can render lineage trees + similarity neighborhoods."""
    import json as _json
    from kazus_db.models import LiquidityAnomalyEdge, LiquidityAnomalyMemory

    root = db.query(LiquidityAnomalyMemory).filter(LiquidityAnomalyMemory.id == anomaly_id).first()
    if root is None:
        return {"id": anomaly_id, "root": None, "parents": [], "descendants": [], "lineage_depth": 0, "neighborhood_size": 0}

    def _row_to_dict(r: LiquidityAnomalyMemory) -> dict:
        try:
            fp = _json.loads(r.fingerprint_json) if r.fingerprint_json else {}
        except (TypeError, ValueError):
            fp = {}
        return {
            "id": r.id, "kind": r.kind, "severity": r.severity,
            "occurred_at_ms": r.occurred_at_ms,
            "novelty_score": r.novelty_score,
            "recurrence_count": r.recurrence_count,
            "fingerprint": fp,
        }

    visited_up = {anomaly_id}
    visited_down = {anomaly_id}
    frontier_up = {anomaly_id}
    frontier_down = {anomaly_id}
    parents_by_depth: List[List[dict]] = []
    descendants_by_depth: List[List[dict]] = []
    edges: List[dict] = []
    max_depth_seen = 0

    for d in range(1, depth + 1):
        # Up: edges pointing INTO frontier_up.
        if frontier_up:
            up_rows = (
                db.query(LiquidityAnomalyEdge)
                .filter(LiquidityAnomalyEdge.to_id.in_(frontier_up))
                .all()
            )
            next_up = set()
            level_nodes: List[dict] = []
            for e in up_rows:
                if e.from_id in visited_up:
                    continue
                visited_up.add(e.from_id)
                next_up.add(e.from_id)
                edges.append({"from_id": e.from_id, "to_id": e.to_id, "kind": e.kind, "weight": e.weight, "depth": d})
            if next_up:
                rows = (
                    db.query(LiquidityAnomalyMemory)
                    .filter(LiquidityAnomalyMemory.id.in_(next_up))
                    .all()
                )
                level_nodes = [_row_to_dict(r) for r in rows]
            if level_nodes:
                parents_by_depth.append(level_nodes)
                max_depth_seen = max(max_depth_seen, d)
            frontier_up = next_up

        # Down: edges pointing FROM frontier_down.
        if frontier_down:
            down_rows = (
                db.query(LiquidityAnomalyEdge)
                .filter(LiquidityAnomalyEdge.from_id.in_(frontier_down))
                .all()
            )
            next_down = set()
            level_nodes: List[dict] = []
            for e in down_rows:
                if e.to_id in visited_down:
                    continue
                visited_down.add(e.to_id)
                next_down.add(e.to_id)
                edges.append({"from_id": e.from_id, "to_id": e.to_id, "kind": e.kind, "weight": e.weight, "depth": d})
            if next_down:
                rows = (
                    db.query(LiquidityAnomalyMemory)
                    .filter(LiquidityAnomalyMemory.id.in_(next_down))
                    .all()
                )
                level_nodes = [_row_to_dict(r) for r in rows]
            if level_nodes:
                descendants_by_depth.append(level_nodes)
                max_depth_seen = max(max_depth_seen, d)
            frontier_down = next_down

    neighborhood_size = len(visited_up) + len(visited_down) - 1   # root counted twice
    return {
        "id": anomaly_id,
        "root": _row_to_dict(root),
        "parents": parents_by_depth,
        "descendants": descendants_by_depth,
        "edges": edges,
        "lineage_depth": max_depth_seen,
        "neighborhood_size": neighborhood_size,
    }


def memory_graph(db: Session, limit_nodes: int = 100) -> dict:
    """Return up to `limit_nodes` most-recent anomaly nodes + all edges
    between them. The UI renders this as a force-directed-ish graph
    panel; we keep edge density low by bounding nodes."""
    import json as _json
    from kazus_db.models import LiquidityAnomalyEdge, LiquidityAnomalyMemory

    nodes_rows = (
        db.query(LiquidityAnomalyMemory)
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .limit(limit_nodes)
        .all()
    )
    node_ids = [r.id for r in nodes_rows]
    edges_rows = (
        db.query(LiquidityAnomalyEdge)
        .filter(
            LiquidityAnomalyEdge.from_id.in_(node_ids),
            LiquidityAnomalyEdge.to_id.in_(node_ids),
        )
        .all()
    )
    nodes: List[dict] = []
    for r in nodes_rows:
        try:
            fp = _json.loads(r.fingerprint_json) if r.fingerprint_json else {}
        except (TypeError, ValueError):
            fp = {}
        nodes.append({
            "id": r.id, "kind": r.kind, "severity": r.severity,
            "occurred_at_ms": r.occurred_at_ms,
            "novelty_score": r.novelty_score,
            "fingerprint": fp,
        })
    edges = [{"from_id": e.from_id, "to_id": e.to_id, "kind": e.kind, "weight": e.weight} for e in edges_rows]
    by_kind: Dict[str, int] = defaultdict(int)
    for e in edges_rows:
        by_kind[e.kind] += 1
    return {
        "nodes": nodes,
        "edges": edges,
        "edge_counts_by_kind": dict(by_kind),
    }


# ── Crisis evolution tree ─────────────────────────────────────────────────


_ESCALATION_ORDER = [
    "EARLY_STRUCTURAL_STRESS",
    "STRUCTURAL_MARKET_DETERIORATION",
    "FRAGMENTING_LIQUIDITY_ENVIRONMENT",
    "ESCALATING_SYSTEMIC_INSTABILITY",
    "ACTIVE_CASCADE_PROPAGATION",
]


def crisis_evolution_tree(db: Session, lookback_days: int = 30) -> dict:
    """Build state-transition counts from the intelligence_history table
    and return per-state escalation/stabilization probabilities.

    For each state, we count how often the NEXT row was at a higher
    escalation rung (escalation), the same rung (persistence), or a
    lower rung (stabilization). Output drives the UI's "tree of crisis
    development" by showing branch weights per state.
    """
    from kazus_db.models import LiquidityIntelligenceHistory

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = (
        db.query(LiquidityIntelligenceHistory.ts_ms, LiquidityIntelligenceHistory.coordinated_state)
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )
    if len(rows) < 2:
        return {"since_ms": since_ms, "states": [], "tree": []}

    rank = {s: i for i, s in enumerate(_ESCALATION_ORDER)}
    # STABLE_COORDINATED_MARKET sits below the escalation list.
    rank["STABLE_COORDINATED_MARKET"] = -1

    per_state: Dict[str, Dict[str, int]] = defaultdict(lambda: {"persist": 0, "escalate": 0, "stabilize": 0, "total_out": 0})
    transitions: Dict[Tuple[str, str], int] = defaultdict(int)

    prev_state = rows[0].coordinated_state
    for ts, cur in rows[1:]:
        if cur is None or prev_state is None:
            prev_state = cur
            continue
        if cur == prev_state:
            per_state[prev_state]["persist"] += 1
        elif rank.get(cur, -1) > rank.get(prev_state, -1):
            per_state[prev_state]["escalate"] += 1
        else:
            per_state[prev_state]["stabilize"] += 1
        transitions[(prev_state, cur)] += 1
        per_state[prev_state]["total_out"] += 1
        prev_state = cur

    states_out: List[dict] = []
    for state, c in per_state.items():
        total = max(1, c["total_out"])
        states_out.append({
            "state": state,
            "persist_count": c["persist"],
            "escalate_count": c["escalate"],
            "stabilize_count": c["stabilize"],
            "total_transitions": c["total_out"],
            "escalation_prob": c["escalate"] / total,
            "stabilization_prob": c["stabilize"] / total,
        })
    states_out.sort(key=lambda s: rank.get(s["state"], -1))

    tree_edges = [
        {"from_state": a, "to_state": b, "count": c}
        for (a, b), c in sorted(transitions.items(), key=lambda kv: -kv[1])
    ]
    return {"since_ms": since_ms, "states": states_out, "tree": tree_edges[:25]}


# ── Regime ancestry ───────────────────────────────────────────────────────


def regime_ancestry(db: Session, lookback_days: int = 30) -> dict:
    """Per-symbol regime sequence → which regimes inherit instability
    from which. Edge weight = number of (parent, child) consecutive
    pairs observed in alert_history."""
    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = db.execute(
        text(
            """
            SELECT symbol, started_at_ms, regime
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            ORDER BY symbol, started_at_ms
            """
        ),
        {"since": since_ms},
    ).fetchall()
    transitions: Dict[Tuple[str, str], int] = defaultdict(int)
    last: Dict[str, str] = {}
    counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.regime] += 1
        prev = last.get(r.symbol)
        if prev and prev != r.regime:
            transitions[(prev, r.regime)] += 1
        last[r.symbol] = r.regime

    nodes = [{"regime": r, "count": c} for r, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    edges = [
        {"parent_regime": a, "child_regime": b, "weight": c}
        for (a, b), c in sorted(transitions.items(), key=lambda kv: -kv[1])
    ]
    # Dominant lineage: walk regimes by highest-outgoing-weight greedily
    # from HEALTHY_TREND.
    by_parent: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for (a, b), c in transitions.items():
        by_parent[a].append((b, c))
    for a in by_parent:
        by_parent[a].sort(key=lambda kv: -kv[1])
    dominant_lineage: List[str] = []
    seen_states: set = set()
    cur = "HEALTHY_TREND" if "HEALTHY_TREND" in counts else (nodes[0]["regime"] if nodes else None)
    while cur and cur not in seen_states and len(dominant_lineage) < 6:
        dominant_lineage.append(cur)
        seen_states.add(cur)
        next_options = by_parent.get(cur, [])
        if not next_options:
            break
        cur = next_options[0][0]
    return {
        "since_ms": since_ms,
        "nodes": nodes,
        "edges": edges,
        "dominant_lineage": dominant_lineage,
    }


# ── Edge lineage (mutation timeline per kind) ─────────────────────────────


def edge_lineage(db: Session, kind: str, lookback_days: int = 60, bucket_days: int = 7) -> dict:
    """Per-kind precision timeline + lifecycle annotations (origin,
    strengthening, degradation, inversion phases). Output drives the
    Edge Lifecycle panel on the Memory page."""
    bucket_ms = bucket_days * 24 * 3600 * 1000
    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = db.execute(
        text(
            """
            SELECT
              (started_at_ms / :bucket_ms) * :bucket_ms AS bucket_ts,
              COUNT(*) AS total,
              SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
              SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise
            FROM liquidity_alert_history
            WHERE kind = :kind AND started_at_ms >= :since
            GROUP BY bucket_ts
            ORDER BY bucket_ts
            """
        ),
        {"bucket_ms": bucket_ms, "kind": kind, "since": since_ms},
    ).fetchall()
    series: List[dict] = []
    phases: List[dict] = []
    prev_p: Optional[float] = None
    phase_start_ts: Optional[int] = None
    phase_kind: Optional[str] = None
    for r in rows:
        ft = int(r.ft or 0); ns = int(r.noise or 0)
        resolved = ft + ns
        p = ft / resolved if resolved > 0 else None
        series.append({"bucket_ts": int(r.bucket_ts), "precision": p, "total": int(r.total or 0)})
        if p is None or prev_p is None:
            prev_p = p
            continue
        # Phase changes
        cur_phase = (
            "INVERTED" if (p - 0.5) * (prev_p - 0.5) < 0
            else "STRENGTHENING" if p - prev_p >= 0.05
            else "DEGRADATION" if p - prev_p <= -0.05
            else "STEADY"
        )
        if cur_phase != phase_kind:
            if phase_kind is not None and phase_start_ts is not None:
                phases.append({"phase": phase_kind, "start_ts": phase_start_ts, "end_ts": int(r.bucket_ts)})
            phase_kind = cur_phase
            phase_start_ts = int(r.bucket_ts)
        prev_p = p
    if phase_kind is not None and phase_start_ts is not None and series:
        phases.append({"phase": phase_kind, "start_ts": phase_start_ts, "end_ts": series[-1]["bucket_ts"]})

    return {
        "kind": kind,
        "since_ms": since_ms,
        "bucket_days": bucket_days,
        "series": series,
        "phases": phases,
        "origin_ts": series[0]["bucket_ts"] if series else None,
    }


# ── Intelligence evolution timeline ───────────────────────────────────────


def intelligence_history_series(db: Session, since_ms: Optional[int] = None, limit: int = 500) -> dict:
    """Read from the snapshot table for the evolution timeline."""
    from kazus_db.models import LiquidityIntelligenceHistory
    q = db.query(LiquidityIntelligenceHistory)
    if since_ms is not None:
        q = q.filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
    rows = q.order_by(LiquidityIntelligenceHistory.ts_ms.asc()).limit(limit).all()
    series: List[dict] = []
    for r in rows:
        series.append({
            "ts_ms": r.ts_ms,
            "synthesized_stress": r.synthesized_stress,
            "coordinated_state": r.coordinated_state,
            "cross_layer_agreement": r.cross_layer_agreement,
            "structural_break_score": r.structural_break_score,
            "meta_confidence_score": r.meta_confidence_score,
            "meta_intelligence_health": r.meta_intelligence_health,
            "health_state": r.health_state,
            "risk_state_score": r.risk_state_score,
            "regime_shift_probability": r.regime_shift_probability,
            "dominant_regime": r.dominant_regime,
        })
    return {"since_ms": since_ms, "count": len(series), "series": series}


# ── Market cycle decomposition ────────────────────────────────────────────


# Map coordinated_state → cycle phase. The cycle taxonomy is the user-facing
# vocabulary; coordinated_state is the internal classifier — we surface
# both so the user can audit the mapping.
_CYCLE_PHASE_FROM_STATE: Dict[str, str] = {
    "STABLE_COORDINATED_MARKET": "STABLE_LIQUIDITY",
    "EARLY_STRUCTURAL_STRESS": "SPECULATIVE_EXPANSION",
    "STRUCTURAL_MARKET_DETERIORATION": "INSTABILITY_PROPAGATION",
    "FRAGMENTING_LIQUIDITY_ENVIRONMENT": "INSTABILITY_PROPAGATION",
    "ESCALATING_SYSTEMIC_INSTABILITY": "CASCADE_PHASE",
    "ACTIVE_CASCADE_PROPAGATION": "CASCADE_PHASE",
}


def market_cycle(db: Session, lookback_days: int = 60) -> dict:
    """Decompose the intelligence_history timeline into cycle phases:
    contiguous runs of the same cycle-phase label. Returns per-run
    duration + transition probabilities + an inferred current phase."""
    from kazus_db.models import LiquidityIntelligenceHistory

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = (
        db.query(LiquidityIntelligenceHistory.ts_ms, LiquidityIntelligenceHistory.coordinated_state)
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )
    runs: List[dict] = []
    transitions: Dict[Tuple[str, str], int] = defaultdict(int)
    cur_phase: Optional[str] = None
    cur_start: Optional[int] = None
    last_ts: Optional[int] = None
    for ts, state in rows:
        phase = _CYCLE_PHASE_FROM_STATE.get(state or "", "STABLE_LIQUIDITY")
        if cur_phase is None:
            cur_phase = phase
            cur_start = int(ts)
        elif phase != cur_phase:
            runs.append({"phase": cur_phase, "start_ts": cur_start, "end_ts": int(ts),
                         "duration_ms": int(ts) - (cur_start or int(ts))})
            transitions[(cur_phase, phase)] += 1
            cur_phase = phase
            cur_start = int(ts)
        last_ts = int(ts)
    if cur_phase is not None and cur_start is not None and last_ts is not None:
        runs.append({"phase": cur_phase, "start_ts": cur_start, "end_ts": last_ts,
                     "duration_ms": last_ts - cur_start, "open": True})

    # Average duration per phase + transition probability matrix.
    by_phase: Dict[str, List[int]] = defaultdict(list)
    for r in runs:
        if r.get("open"):
            continue
        by_phase[r["phase"]].append(r["duration_ms"])
    avg_duration = {p: (sum(d) / len(d) if d else None) for p, d in by_phase.items()}
    phase_totals: Dict[str, int] = defaultdict(int)
    for (a, _), c in transitions.items():
        phase_totals[a] += c
    matrix = [
        {"from_phase": a, "to_phase": b, "count": c,
         "probability": c / phase_totals[a] if phase_totals.get(a) else 0.0}
        for (a, b), c in sorted(transitions.items(), key=lambda kv: -kv[1])
    ]
    current = cur_phase
    return {
        "since_ms": since_ms,
        "runs": runs,
        "current_phase": current,
        "avg_duration_ms_per_phase": avg_duration,
        "transition_matrix": matrix,
    }


# ── Narrative chronicle (multi-week story) ────────────────────────────────


def narrative_chronicle(db: Session, lookback_days: int = 21) -> dict:
    """Compose a multi-week chronicle of how the market evolved. Looks
    at the intelligence_history series + recent anomaly memory and
    composes deterministic prose."""
    from kazus_db.models import LiquidityIntelligenceHistory, LiquidityAnomalyMemory

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    hist = (
        db.query(
            LiquidityIntelligenceHistory.ts_ms,
            LiquidityIntelligenceHistory.synthesized_stress,
            LiquidityIntelligenceHistory.meta_intelligence_health,
            LiquidityIntelligenceHistory.structural_break_score,
            LiquidityIntelligenceHistory.coordinated_state,
        )
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )
    if len(hist) < 5:
        return {
            "since_ms": since_ms,
            "summary": "Insufficient history to compose a multi-week chronicle.",
            "highlights": [],
            "anomaly_count": 0,
        }

    def _slope(values: List[Optional[float]]) -> Optional[float]:
        clean = [(i, v) for i, v in enumerate(values) if v is not None]
        if len(clean) < 3:
            return None
        n = len(clean)
        mx = sum(p[0] for p in clean) / n
        my = sum(p[1] for p in clean) / n
        num = sum((p[0] - mx) * (p[1] - my) for p in clean)
        den = sum((p[0] - mx) ** 2 for p in clean)
        return num / den if den > 0 else None

    stress_slope = _slope([h.synthesized_stress for h in hist])
    health_slope = _slope([h.meta_intelligence_health for h in hist])
    break_slope = _slope([h.structural_break_score for h in hist])

    state_first = hist[0].coordinated_state
    state_last = hist[-1].coordinated_state

    anomalies = (
        db.query(LiquidityAnomalyMemory)
        .filter(LiquidityAnomalyMemory.occurred_at_ms >= since_ms)
        .order_by(LiquidityAnomalyMemory.occurred_at_ms.desc())
        .all()
    )
    counts_by_kind: Dict[str, int] = defaultdict(int)
    for a in anomalies:
        counts_by_kind[a.kind] += 1

    bullets: List[str] = []
    if stress_slope is not None:
        if stress_slope > 0.5:
            bullets.append(f"synthesized stress rose steadily ({stress_slope:+.1f}/snapshot)")
        elif stress_slope < -0.5:
            bullets.append(f"synthesized stress eased ({stress_slope:+.1f}/snapshot)")
    if break_slope is not None and break_slope > 0.5:
        bullets.append(f"structural break score climbing ({break_slope:+.1f}/snapshot)")
    if health_slope is not None and health_slope < -0.5:
        bullets.append(f"intelligence-engine health degraded ({health_slope:+.1f}/snapshot)")
    if counts_by_kind:
        top = sorted(counts_by_kind.items(), key=lambda kv: -kv[1])[:3]
        bullets.append("anomaly memory accumulated " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in top))
    if state_first and state_last and state_first != state_last:
        bullets.append(f"coordinated state transitioned {state_first.replace('_', ' ')} → {state_last.replace('_', ' ')}")
    if not bullets:
        bullets.append("market evolution was largely uneventful in the window")

    summary = f"Over the last {lookback_days} days the market " + ("escalated" if stress_slope and stress_slope > 0.5 else "stabilized" if stress_slope and stress_slope < -0.5 else "drifted laterally") + "."

    return {
        "since_ms": since_ms,
        "summary": summary,
        "highlights": bullets,
        "anomaly_count": len(anomalies),
        "anomaly_counts_by_kind": dict(counts_by_kind),
        "first_state": state_first,
        "last_state": state_last,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase-14 — Autonomous Pattern Discovery & Evolutionary Intelligence
# ══════════════════════════════════════════════════════════════════════════
#
# Read-only analytics that look at the data the previous phases produced
# (samples, alert history, anomaly memory, intelligence history) and
# extract recurring structures, propagation graphs, evolutionary trends
# and adaptation recommendations.
#
# Deliberately no ML / no black-box. Everything here is statistical:
# tertile co-occurrence, agglomerative clustering, lag-correlation,
# OLS regression on weekly buckets.


# ── Emergent pattern discovery ────────────────────────────────────────────


PATTERN_METRICS: Tuple[str, ...] = (
    "fragility_score", "resiliency_score", "credible_depth",
    "spread", "liq_stress", "funding_z", "oi_delta_1h",
)


def discover_patterns(
    db: Session,
    since_ms: int,
    min_support: int = 12,
    bucket_minutes: int = 30,
) -> dict:
    """Discover recurring (metric -> tertile) combinations + their
    downstream alert rates.

    Pulls a long bucketed pivot, tertile-codes each metric per-symbol
    cohort percentiles over the same window, hashes each row to a
    pattern signature, and reports the highest-frequency patterns
    along with: how often any alert followed within 60m, dominant
    alert kind, novelty (1 − recurrence_share).

    `min_support` filters out one-off shapes; bucket_minutes controls
    temporal granularity.
    """
    bucket_ms = bucket_minutes * 60_000
    rows = db.execute(
        text(
            """
            SELECT
              symbol, metric,
              (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
              AVG(value) AS v
            FROM liquidity_samples
            WHERE metric = ANY(:metrics) AND ts >= :since AND value IS NOT NULL
            GROUP BY symbol, metric, bucket_ts
            """
        ),
        {"bucket_ms": bucket_ms, "metrics": list(PATTERN_METRICS), "since": since_ms},
    ).fetchall()

    pivot: Dict[Tuple[str, int], Dict[str, float]] = {}
    for r in rows:
        pivot.setdefault((r.symbol, int(r.bucket_ts)), {})[r.metric] = float(r.v)

    by_metric: Dict[str, List[float]] = {m: [] for m in PATTERN_METRICS}
    for mv in pivot.values():
        for m, v in mv.items():
            by_metric[m].append(v)
    for m in by_metric:
        by_metric[m].sort()
    cuts: Dict[str, Tuple[float, float]] = {}
    for m, xs in by_metric.items():
        cuts[m] = (_quantile(xs, 1 / 3), _quantile(xs, 2 / 3))

    # Pre-load downstream alerts.
    alert_rows = db.execute(
        text(
            """
            SELECT symbol, started_at_ms, kind
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            """
        ),
        {"since": since_ms},
    ).fetchall()
    alerts_by_sym: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for a in alert_rows:
        alerts_by_sym[a.symbol].append((int(a.started_at_ms), a.kind))
    for s in alerts_by_sym:
        alerts_by_sym[s].sort()

    window_ms = 60 * 60_000

    def _outcome(sym: str, ts: int) -> Optional[str]:
        arr = alerts_by_sym.get(sym, [])
        if not arr:
            return None
        lo, hi = 0, len(arr) - 1
        pos = len(arr)
        while lo <= hi:
            mid = (lo + hi) // 2
            if arr[mid][0] >= ts:
                pos = mid
                hi = mid - 1
            else:
                lo = mid + 1
        if pos < len(arr) and arr[pos][0] <= ts + window_ms:
            return arr[pos][1]
        return None

    patterns: Dict[Tuple[str, ...], dict] = {}
    total_buckets = 0
    now_ms = int(time.time() * 1000)
    mid_ms = (since_ms + now_ms) // 2
    DAY_MS = 24 * 3600 * 1000
    # Soft gate: any (symbol, bucket) with at least min_metrics populated
    # contributes. Missing metrics get a "?" tertile so the signature
    # still hashes consistently. The strict gate ("all 7 present") starved
    # the pivot to zero when WS-only metrics weren't populated (e.g. no
    # symbols pinned, so resiliency_score never appeared).
    min_metrics_required = max(4, len(PATTERN_METRICS) // 2 + 1)
    for (sym, bts), mv in pivot.items():
        if sum(1 for m in PATTERN_METRICS if m in mv) < min_metrics_required:
            continue
        sig = tuple(
            _tertile(mv[m], *cuts[m]) if m in mv else "na"
            for m in PATTERN_METRICS
        )
        outcome = _outcome(sym, bts)
        rec = patterns.setdefault(sig, {
            "count": 0, "outcomes": 0, "kinds": defaultdict(int),
            "first_half": 0, "second_half": 0,
            "days": defaultdict(int),  # day_index → bucket_count
        })
        rec["count"] += 1
        if bts < mid_ms:
            rec["first_half"] += 1
        else:
            rec["second_half"] += 1
        rec["days"][bts // DAY_MS] += 1
        if outcome:
            rec["outcomes"] += 1
            rec["kinds"][outcome] += 1
        total_buckets += 1

    overall_quality = _discovery_quality(total_buckets, low=20, medium=100, high=500)
    # Scarcity multiplier on pattern_confidence — when there's not enough
    # data to mine, every "discovered" pattern is a candidate at best.
    SCARCITY_FACTOR = {"INSUFFICIENT": 0.15, "LOW": 0.40, "MEDIUM": 0.75, "HIGH": 1.0}
    scarcity_factor = SCARCITY_FACTOR.get(overall_quality, 0.15)

    out: List[dict] = []
    suppressed_count = 0
    base_rate = sum(p["outcomes"] for p in patterns.values()) / max(1, total_buckets)
    for sig, agg in patterns.items():
        if agg["count"] < min_support:
            continue
        rate = agg["outcomes"] / agg["count"] if agg["count"] > 0 else 0.0
        lift = rate / base_rate if base_rate > 0 else None
        dom_kind, dom_n = max(agg["kinds"].items(), key=lambda kv: kv[1]) if agg["kinds"] else (None, 0)
        novelty = max(0.0, 100.0 * (1.0 - agg["count"] / total_buckets))

        # ── Robustness flags ──────────────────────────────────────────
        flags: List[str] = []
        c = agg["count"]
        if c < min_support * 1.5:
            flags.append("LOW_SUPPORT")
        if lift is not None and lift >= 2.0 and c < 20:
            flags.append("HIGH_LIFT_LOW_SUPPORT")
        # SINGLE_WINDOW: pattern appears entirely in one half of the
        # lookback (or one half holds <15% of total) — could not survive
        # window re-anchoring.
        fh, sh = agg["first_half"], agg["second_half"]
        minority_share = min(fh, sh) / c if c else 0.0
        if minority_share < 0.15:
            flags.append("SINGLE_WINDOW")
        # LOW_RECURRENCE: pattern's buckets bunch into one day (or fewer
        # than 2 distinct days) — won't survive re-bucketing or any
        # robustness test.
        day_span = len(agg["days"])
        max_day_share = (max(agg["days"].values()) / c) if c else 1.0
        if day_span < 2 or max_day_share > 0.70:
            flags.append("LOW_RECURRENCE")
        # REGIME_FRAGILE: pattern's follow-through alerts cluster on a
        # single alert kind (>80%) — pattern "works" only when one kind
        # of regime is active, fragile to regime shifts.
        if agg["outcomes"] >= 5 and dom_kind is not None and (dom_n / agg["outcomes"]) > 0.80:
            flags.append("REGIME_FRAGILE")
        # BUCKET_SENSITIVE: the pattern dominates a sizable chunk of
        # any active day's bucket-space (>50% of the 24h / bucket_minutes
        # slots). Means the pattern fires hours in a row — its support
        # would collapse if the bucket size were re-anchored.
        buckets_per_day = (24 * 60) / max(1, bucket_minutes)
        if day_span >= 1 and (c / day_span) > buckets_per_day * 0.50:
            flags.append("BUCKET_SENSITIVE")

        # ── Stability score ──────────────────────────────────────────
        # Multiplicative penalties — a pattern with multiple flags
        # degrades quickly. Half-balance also contributes positively.
        stability = 1.0
        if "SINGLE_WINDOW" in flags:
            stability *= 0.30
        if "LOW_RECURRENCE" in flags:
            stability *= 0.35
        if "HIGH_LIFT_LOW_SUPPORT" in flags:
            stability *= 0.50
        if "REGIME_FRAGILE" in flags:
            stability *= 0.65
        if "BUCKET_SENSITIVE" in flags:
            stability *= 0.65
        if "LOW_SUPPORT" in flags:
            stability *= 0.75
        # Smooth bonus for half-balance (already captured by SINGLE_WINDOW
        # at the extreme, but reward even split additionally).
        stability *= 0.6 + 0.4 * (minority_share * 2)  # minority_share in [0, 0.5]

        effective_lift = (lift or 0.0) * stability
        pattern_confidence = 100.0 * stability * scarcity_factor

        suppressed_reason: Optional[str] = None
        if overall_quality == "INSUFFICIENT":
            suppressed_reason = f"INSUFFICIENT data ({total_buckets} buckets)"
        elif pattern_confidence < 15:
            suppressed_reason = "below display threshold: " + ", ".join(flags) if flags else "scarcity"
        if suppressed_reason:
            suppressed_count += 1

        out.append({
            "discovered_pattern_id": f"P{abs(hash(sig)) % 10**8:08d}",
            "signature": dict(zip(PATTERN_METRICS, sig)),
            "support": c,
            "outcome_rate": rate,
            "lift": lift,
            "effective_lift": effective_lift,
            "dominant_alert_kind": dom_kind,
            "dominant_alert_count": dom_n,
            "novelty_score": novelty,
            "stability_score": stability,
            "pattern_confidence": pattern_confidence,
            "robustness_flags": flags,
            "suppressed_reason": suppressed_reason,
            "day_span": day_span,
            "first_half_support": fh,
            "second_half_support": sh,
        })

    # Sort by effective_lift (stability-discounted) so unstable high-lift
    # patterns can't dominate the top of the UI.
    out.sort(key=lambda r: -r["effective_lift"])
    return {
        "since_ms": since_ms,
        "min_support": min_support,
        "bucket_minutes": bucket_minutes,
        "metrics": list(PATTERN_METRICS),
        "base_rate": base_rate,
        "total_buckets": total_buckets,
        "patterns": out[:40],
        "data_quality": overall_quality,
        "suppressed_count": suppressed_count,
        "scarcity_factor": scarcity_factor,
    }


# ── Crisis archetype discovery ────────────────────────────────────────────


# Each archetype is named by a heuristic over the cluster's dominant
# kind + average drift signature. We deliberately keep the label list
# fixed so the UI gets a stable vocabulary; what's "discovered" is
# which clusters MAP to which archetype, not the archetype names
# themselves.
ARCHETYPE_HINTS = (
    "slow_deterioration",
    "liquidity_evaporation",
    "instability_propagation",
    "explosive_cascade",
    "venue_fragmentation",
    "speculative_overheating",
    "recovery_exhaustion",
    "isolated_outlier",
)


def crisis_archetypes(db: Session, max_archetypes: int = 8) -> dict:
    """Group anomaly_memory rows into archetype clusters and label each
    by heuristic on the centroid + dominant kind. Reuses
    crisis_clusters' agglomerative grouping then assigns archetype
    labels."""
    base = crisis_clusters(db, max_clusters=max_archetypes)
    archetypes: List[dict] = []
    for cl in base["clusters"]:
        c = cl["centroid"]
        # Heuristic labeller.
        dom = cl["dominant_kind"]
        size = cl["size"]
        avg_novelty = cl["avg_novelty"]
        label = "isolated_outlier"
        if size >= 5 and "structural_break_score" in c and c.get("structural_break_score", 0) >= 55:
            label = "slow_deterioration"
        if dom == "venue_divergence":
            label = "venue_fragmentation"
        if dom == "pre_cascade":
            label = "explosive_cascade"
        if c.get("credible_depth", 1e9) < (c.get("credible_depth_p10", 1e9) or 1e9) and c.get("spread", 0) > 0.001:
            label = "liquidity_evaporation"
        if c.get("funding_z", 0) and abs(c["funding_z"]) > 2 and c.get("oi_delta_1h", 0) and c["oi_delta_1h"] > 0:
            label = "speculative_overheating"
        if dom == "edge_inversion":
            label = "recovery_exhaustion"
        if cl["frequency_per_day"] >= 1.0 and dom == "structural_break":
            label = "instability_propagation"

        # Escalation profile (just a stub for now: severity ratio).
        escalation = "elevated" if avg_novelty >= 60 else "stable"
        # Recovery probability — heuristic on how often this cluster
        # was followed by a sample of the engine returning to QUIET in
        # intelligence_history. Approximate via novelty inversely.
        recovery_prob = max(0.0, min(1.0, 1.0 - (avg_novelty / 100.0)))
        # Structural severity: size + dominant kind severity proxy.
        severity_weight = {"pre_cascade": 0.9, "regime_collapse": 0.8, "structural_break": 0.6,
                           "edge_inversion": 0.5, "venue_divergence": 0.4, "regime_emergence": 0.3}
        severity = severity_weight.get(dom, 0.3) * min(1.0, size / 10.0)

        archetypes.append({
            "archetype_id": f"A{cl['cluster_id']:02d}",
            "archetype_label": label,
            "cluster_id": cl["cluster_id"],
            "size": size,
            "dominant_kind": dom,
            "kinds": cl["kinds"],
            "frequency_per_day": cl["frequency_per_day"],
            "avg_novelty": avg_novelty,
            "escalation_profile": escalation,
            "recovery_probability": recovery_prob,
            "structural_severity": severity,
            "centroid": cl["centroid"],
        })
    return {
        "archetypes": archetypes,
        "anomaly_count": base["anomaly_count"],
        "vocabulary": list(ARCHETYPE_HINTS),
        # Anomaly clustering is noise below ~10 records — every cluster
        # will be a one-off. HIGH only when memory has accumulated real
        # repeating structures.
        "data_quality": _discovery_quality(base["anomaly_count"], low=5, medium=20, high=80),
    }


# ── Hidden regime discovery ──────────────────────────────────────────────


def hidden_regimes(db: Session, lookback_days: int = 30, max_clusters: int = 6) -> dict:
    """Cluster intelligence_history fingerprints into recurring states
    that aren't 1:1 with a single coordinated_state. Each cluster gets
    a descriptive label hint derived from its centroid; the UI shows
    cluster size + dominant existing coordinated_state."""
    import json as _json
    from kazus_db.models import LiquidityIntelligenceHistory

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = (
        db.query(LiquidityIntelligenceHistory)
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )
    if len(rows) < 5:
        return {"since_ms": since_ms, "clusters": [], "snapshot_count": len(rows)}

    points: List[Tuple[Dict[str, float], LiquidityIntelligenceHistory]] = []
    for r in rows:
        try:
            fp = _json.loads(r.fingerprint_json) if r.fingerprint_json else {}
        except (TypeError, ValueError):
            fp = {}
        # Augment fingerprint with engine-level scalars so the cluster
        # captures both microstructure and the coordinated assessment.
        fp = dict(fp)
        if r.synthesized_stress is not None:
            fp["_stress"] = r.synthesized_stress
        if r.structural_break_score is not None:
            fp["_break"] = r.structural_break_score
        if r.meta_intelligence_health is not None:
            fp["_health"] = r.meta_intelligence_health
        if r.regime_shift_probability is not None:
            fp["_shift"] = r.regime_shift_probability
        points.append((fp, r))

    clusters: List[Dict[str, object]] = []
    for fp, row in points:
        best = None
        best_d = None
        for c in clusters:
            d = _fingerprint_distance(fp, c["centroid"])  # type: ignore[arg-type]
            if d is None:
                continue
            if best_d is None or d < best_d:
                best_d = d
                best = c
        if best is not None and best_d is not None and best_d < 0.6:
            best["members"].append(row)  # type: ignore[union-attr]
            # Rolling centroid update.
            old = best["centroid"]  # type: ignore[assignment]
            merged = {k: (old.get(k, 0.0) + fp.get(k, old.get(k, 0.0))) / 2 for k in set(list(old.keys()) + list(fp.keys()))}
            best["centroid"] = merged
        else:
            clusters.append({"centroid": dict(fp), "members": [row]})

    clusters.sort(key=lambda c: -len(c["members"]))   # type: ignore[arg-type]
    out_clusters: List[dict] = []
    for idx, c in enumerate(clusters[:max_clusters], start=1):
        members = c["members"]
        states = [m.coordinated_state for m in members if m.coordinated_state]
        dominant_state = max(set(states), key=states.count) if states else None
        # Label hint: derive from centroid scalars.
        cen = c["centroid"]
        label = "hidden_state"
        if cen.get("_stress", 0) >= 55 and cen.get("_break", 0) >= 50:
            label = "structural_stress_basin"
        elif cen.get("_break", 0) >= 50 and cen.get("_stress", 0) < 35:
            label = "silent_structural_drift"
        elif cen.get("_stress", 0) >= 45 and cen.get("_shift", 0) < 20:
            label = "stress_without_shift_warning"
        elif cen.get("_health", 100) < 50:
            label = "engine_degradation_state"
        elif cen.get("_stress", 0) < 25 and cen.get("_break", 0) < 25:
            label = "deep_calm"
        # Stability = inverse of std of stress within cluster.
        stresses = [m.synthesized_stress for m in members if m.synthesized_stress is not None]
        if len(stresses) >= 2:
            mean = sum(stresses) / len(stresses)
            var = sum((x - mean) ** 2 for x in stresses) / len(stresses)
            stability = max(0.0, 100.0 - math.sqrt(var) * 2.0)
        else:
            stability = 50.0
        out_clusters.append({
            "cluster_id": idx,
            "label_hint": label,
            "size": len(members),
            "dominant_coordinated_state": dominant_state,
            "earliest_ts": min(m.ts_ms for m in members),
            "latest_ts": max(m.ts_ms for m in members),
            "centroid": cen,
            # Emergent if dominant_state doesn't account for most of the
            # cluster (or no dominant state at all). Higher = more novel.
            "emergent_regime_score": (
                100.0 if not states else
                max(0.0, 100.0 - (states.count(dominant_state) / len(states)) * 100.0) if dominant_state else 100.0
            ),
            "regime_stability": stability,
            "is_emergent": dominant_state is None or (states.count(dominant_state) / len(states) < 0.7 if states else True),
        })
    return {
        "since_ms": since_ms,
        "snapshot_count": len(rows),
        "clusters": out_clusters,
        # Cluster stability needs accumulated diverse snapshots — under
        # 50 the engine state hasn't varied enough to support multi-
        # cluster discrimination, however many clusters we report.
        "data_quality": _discovery_quality(len(rows), low=20, medium=80, high=300),
    }


# ── Structural propagation ───────────────────────────────────────────────


def propagation_graph(
    db: Session,
    lookback_days: int = 14,
    lead_window_ms: int = 30 * 60_000,
    min_lead_ms: int = 5_000,
) -> dict:
    """Build a propagation graph of symbols: A → B with weight = number
    of times an alert on A was followed by an alert on B for the same
    alert kind, with lead time in [`min_lead_ms`, `lead_window_ms`].

    Edge weight is the *deduplicated* lead-pair count: each source event
    on A contributes at most +1 to (A, B), and same-instant co-occurrences
    (lead < `min_lead_ms`) are dropped to avoid market-wide-burst inflation.

    Per-edge confidence is a weighted blend of six semantic signals (each
    bounded in [0, 1] before composition), so HIGH/MEDIUM/LOW thresholds
    stay stable as the graph grows — they're not percentile-based against
    the current edge population:

      * volume_strength      — `1 - exp(-count/15)` (saturates around 40)
      * lead_clarity         — distance of avg lead above `min_lead_ms`
      * lead_consistency     — 1 − coefficient of variation of leads
      * temporal_consistency — fraction of days the edge actually fired
      * recurrence_stability — penalty when events bunch into one day
      * symmetry_penalty     — applied multiplicatively when B → A exists
                               with comparable weight (coincidence guard)

    Plus a node-level `leader_stability` (weighted mean of an edge's
    base-confidence across its outgoing edges) attenuates final confidence,
    so an edge from a flaky leader can't be HIGH on its own merit alone.
    """
    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = db.execute(
        text(
            """
            SELECT symbol, kind, started_at_ms
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            ORDER BY started_at_ms
            """
        ),
        {"since": since_ms},
    ).fetchall()

    # Overload guard. Propagation's inner pairing is O(N²) per kind,
    # so unbounded alert ingress (during a cascade or a tracker bug) can
    # turn one request into a 60s CPU spike that also poisons the cache.
    # Hard cap the input and signal degradation in the response so the UI
    # can flag it instead of silently returning truncated results.
    OVERLOAD_HARD_CAP = 50_000
    overloaded = len(rows) > OVERLOAD_HARD_CAP
    if overloaded:
        # Keep only the most recent OVERLOAD_HARD_CAP alerts so the
        # window slides toward "now" rather than truncating arbitrary
        # heads. Still O(N²) on the cap, but bounded.
        rows = rows[-OVERLOAD_HARD_CAP:]

    by_kind: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for r in rows:
        by_kind[r.kind].append((int(r.started_at_ms), r.symbol))

    DAY_MS = 24 * 3600 * 1000
    edges: Dict[Tuple[str, str], int] = defaultdict(int)
    lead_sums: Dict[Tuple[str, str], int] = defaultdict(int)
    lead_sq_sums: Dict[Tuple[str, str], float] = defaultdict(float)
    lead_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    daily_counts: Dict[Tuple[str, str], Dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for kind, lst in by_kind.items():
        lst.sort()
        for i, (ts_a, sym_a) in enumerate(lst):
            charged_targets_for_a = set()
            day_a = (ts_a - since_ms) // DAY_MS
            for j in range(i + 1, len(lst)):
                ts_b, sym_b = lst[j]
                lead = ts_b - ts_a
                if lead > lead_window_ms:
                    break
                if lead < min_lead_ms:
                    continue
                if sym_a == sym_b:
                    continue
                if sym_b in charged_targets_for_a:
                    continue
                charged_targets_for_a.add(sym_b)
                key = (sym_a, sym_b)
                edges[key] += 1
                lead_sums[key] += lead
                lead_sq_sums[key] += lead * lead
                lead_counts[key] += 1
                daily_counts[key][int(day_a)] += 1

    # ── Pass 1: per-edge semantic signals (independent of other edges) ─

    raw_edges: List[dict] = []
    for (a, b), c in edges.items():
        if c < 3:
            continue
        n = lead_counts[(a, b)]
        avg_lead_ms = lead_sums[(a, b)] / n
        # variance via E[X²] − E[X]² (clip negatives from float error)
        var_lead = max(0.0, lead_sq_sums[(a, b)] / n - avg_lead_ms * avg_lead_ms)
        std_lead = var_lead ** 0.5

        volume_strength = 1.0 - math.exp(-c / 15.0)
        lead_clarity = max(0.0, min(1.0, (avg_lead_ms - min_lead_ms) / 60_000.0))
        # Coefficient of variation inverted; tight leads → ~1, scattered → ~0.
        cv = std_lead / avg_lead_ms if avg_lead_ms > 0 else 1.0
        lead_consistency = max(0.0, min(1.0, 1.0 - cv))

        days = daily_counts[(a, b)]
        temporal_consistency = min(1.0, len(days) / float(lookback_days))
        if c > 1:
            max_day = max(days.values())
            recurrence_stability = max(0.0, 1.0 - (max_day - 1) / float(c - 1))
        else:
            recurrence_stability = 0.0

        # Base confidence (0..1) before symmetry & leader attenuation.
        base_confidence = (
            0.30 * volume_strength
            + 0.20 * lead_clarity
            + 0.15 * lead_consistency
            + 0.20 * temporal_consistency
            + 0.15 * recurrence_stability
        )

        raw_edges.append({
            "from_symbol": a,
            "to_symbol": b,
            "count": c,
            "avg_lead_ms": avg_lead_ms,
            "avg_lead_s": avg_lead_ms / 1000.0,
            "lead_std_s": std_lead / 1000.0,
            "volume_strength": volume_strength,
            "lead_clarity": lead_clarity,
            "lead_consistency": lead_consistency,
            "temporal_consistency": temporal_consistency,
            "recurrence_stability": recurrence_stability,
            "base_confidence": base_confidence,
        })

    # ── Pass 2: symmetry penalty (needs reverse-edge lookup) ──────────

    edge_by_pair = {(e["from_symbol"], e["to_symbol"]): e for e in raw_edges}
    for e in raw_edges:
        rev = edge_by_pair.get((e["to_symbol"], e["from_symbol"]))
        if rev is None:
            e["symmetry_penalty"] = 0.0
        else:
            ratio = min(e["count"], rev["count"]) / max(e["count"], rev["count"])
            # Quadratic so near-equal pairs (ratio ≥ 0.7) get heavy penalty
            # but a weak reverse (ratio ≤ 0.3) barely registers.
            e["symmetry_penalty"] = ratio * ratio

    # ── Pass 3: node leader_stability from base confidence ────────────

    node_out_weighted: Dict[str, Tuple[float, int]] = defaultdict(lambda: (0.0, 0))
    for e in raw_edges:
        s = e["from_symbol"]
        wsum, csum = node_out_weighted[s]
        node_out_weighted[s] = (wsum + e["base_confidence"] * e["count"], csum + e["count"])
    leader_stability: Dict[str, float] = {
        s: (wsum / csum if csum else 0.0) for s, (wsum, csum) in node_out_weighted.items()
    }

    # ── Pass 4: final confidence + bucket ─────────────────────────────

    for e in raw_edges:
        ls = leader_stability.get(e["from_symbol"], 0.0)
        # Attenuate by leader: a flaky leader caps the edge at ~0.5×base
        # even if the per-edge signals look great in isolation.
        leader_pull = 0.5 + 0.5 * ls
        score = e["base_confidence"] * (1.0 - e["symmetry_penalty"]) * leader_pull
        e["confidence_score"] = max(0.0, min(1.0, score))
        e["leader_stability"] = ls
        # Absolute thresholds (NOT percentile-based against the current
        # 50-edge population — keep them stable as the graph grows).
        if e["confidence_score"] >= 0.70:
            e["confidence"] = "HIGH"
        elif e["confidence_score"] >= 0.45:
            e["confidence"] = "MEDIUM"
        else:
            e["confidence"] = "LOW"

    # Collect ALL symmetric pairs (sym_penalty ≥ 0.5) BEFORE the top-50
    # truncation — sanity_audit needs the full set to flag coincidence
    # loops, otherwise heavily-penalized pairs (which fall out of the
    # display ranking by design) become invisible to integrity monitoring.
    all_symmetric_pairs: List[dict] = []
    raw_edge_by_pair_full = {(e["from_symbol"], e["to_symbol"]): e for e in raw_edges}
    for e in raw_edges:
        if e["symmetry_penalty"] < 0.5:
            continue
        a, b = e["from_symbol"], e["to_symbol"]
        if a >= b:
            continue
        rev = raw_edge_by_pair_full.get((b, a))
        if rev is None:
            continue
        all_symmetric_pairs.append({
            "a": a, "b": b,
            "count_ab": e["count"], "count_ba": rev["count"],
            "symmetry_penalty": e["symmetry_penalty"],
            "confidence_score_ab": e["base_confidence"] * (1.0 - e["symmetry_penalty"]),
            "confidence_score_ba": rev["base_confidence"] * (1.0 - rev["symmetry_penalty"]),
        })

    # Rank by confidence_score so the top of the UI surfaces the strongest
    # edges, not the densest-by-count (which conflated volume with truth).
    raw_edges.sort(key=lambda e: -e["confidence_score"])
    edges_out = raw_edges[:50]

    # ── Node aggregates (counts + leader/follower stability) ──────────

    out_deg: Dict[str, int] = defaultdict(int)
    in_deg: Dict[str, int] = defaultdict(int)
    in_weighted: Dict[str, Tuple[float, int]] = defaultdict(lambda: (0.0, 0))
    for e in edges_out:
        out_deg[e["from_symbol"]] += e["count"]
        in_deg[e["to_symbol"]] += e["count"]
        wsum, csum = in_weighted[e["to_symbol"]]
        in_weighted[e["to_symbol"]] = (
            wsum + e["confidence_score"] * e["count"],
            csum + e["count"],
        )
    follower_stability: Dict[str, float] = {
        s: (wsum / csum if csum else 0.0) for s, (wsum, csum) in in_weighted.items()
    }

    nodes = sorted(
        set(out_deg) | set(in_deg),
        key=lambda s: -(out_deg.get(s, 0)),
    )
    node_rows = [
        {
            "symbol": s,
            "out_count": out_deg.get(s, 0),
            "in_count": in_deg.get(s, 0),
            "net_lead": out_deg.get(s, 0) - in_deg.get(s, 0),
            "leader_stability": leader_stability.get(s, 0.0),
            "follower_stability": follower_stability.get(s, 0.0),
        }
        for s in nodes
    ]

    # ── Graph-level integrity score ───────────────────────────────────

    if edges_out:
        avg_confidence = sum(e["confidence_score"] for e in edges_out) / len(edges_out)
        symmetric_share = sum(1 for e in edges_out if e["symmetry_penalty"] >= 0.5) / len(edges_out)
        weak_share = sum(1 for e in edges_out if e["confidence_score"] < 0.45) / len(edges_out)
        coverage = sum(e["temporal_consistency"] for e in edges_out) / len(edges_out)
    else:
        avg_confidence = symmetric_share = weak_share = coverage = 0.0

    integrity_components = {
        "avg_confidence": avg_confidence,
        "symmetric_share": symmetric_share,
        "weak_share": weak_share,
        "coverage": coverage,
    }
    integrity_score = 100.0 * max(0.0, min(1.0,
        0.45 * avg_confidence
        + 0.25 * (1.0 - symmetric_share)
        + 0.20 * (1.0 - weak_share)
        + 0.10 * coverage
    ))

    # Systemic contagion (existing): pairwise co-occurrences vs max possible.
    total_alerts = sum(len(v) for v in by_kind.values())
    max_pairs = max(1, total_alerts * (total_alerts - 1) // 2)
    raw_total = sum(e["count"] for e in edges_out)
    contagion_score = min(100.0, (raw_total / max_pairs) * 100.0)
    avg_velocity_s = (sum(e["avg_lead_s"] for e in edges_out) / len(edges_out)) if edges_out else None

    return {
        "since_ms": since_ms,
        "lead_window_ms": lead_window_ms,
        "edges": edges_out,
        "nodes": node_rows[:50],
        "systemic_contagion_score": contagion_score,
        "average_propagation_velocity_s": avg_velocity_s,
        "total_alerts": total_alerts,
        "integrity_score": integrity_score,
        "integrity_components": integrity_components,
        "all_symmetric_pairs": all_symmetric_pairs,
        "overloaded": overloaded,
    }


# ── Evolutionary market behavior ─────────────────────────────────────────


def evolutionary_behavior(db: Session, lookback_days: int = 60, bucket_days: int = 7) -> dict:
    """Long-horizon trend slopes for structural metrics + the engine's
    own state. Phase-10 had a basic version; here we add 'behavioral
    shift rate', 'maturity score' and the inferred 'evolutionary
    state' label."""
    me = market_evolution(db, lookback_days=lookback_days, bucket_days=bucket_days)

    # Behavioral shift rate: average |slope_per_day| across metrics in
    # ANALYTICS_METRICS, scaled by their reference magnitudes.
    shifts: List[float] = []
    bad_directions = 0
    for t in me["metric_trends"]:
        slope = t.get("slope_per_day")
        series = t.get("series") or []
        if slope is None or not series:
            continue
        latest = series[-1]["v"] if series else 0.0
        denom = max(abs(latest), 1e-6)
        shifts.append(abs(slope) / denom)
        # "bad" = falling resiliency/credible_depth or rising
        # fragility/spread/liq_stress.
        m = t["metric"]
        if m in ("resiliency_score", "credible_depth", "atr_liquidity") and slope < 0:
            bad_directions += 1
        if m in ("fragility_score", "spread", "liq_stress", "impact_score") and slope > 0:
            bad_directions += 1
    behavioral_shift_rate = (sum(shifts) / len(shifts) * 100.0) if shifts else 0.0

    # Maturity score: lower fragility + higher resiliency long-term =
    # mature; we use the latest values from the 60d series.
    def latest_of(metric: str) -> Optional[float]:
        for t in me["metric_trends"]:
            if t["metric"] == metric and t["series"]:
                return t["series"][-1]["v"]
        return None
    frag = latest_of("fragility_score") or 50
    res = latest_of("resiliency_score") or 50
    maturity = max(0.0, min(100.0, (res - frag) / 2 + 50))

    # Instability acceleration: positive if "bad_directions" outweigh
    # the others, weighted by recent shift rate.
    accel = bad_directions * 10.0 + behavioral_shift_rate * 0.5

    if accel >= 60 or bad_directions >= 4:
        state = "DETERIORATION"
    elif accel >= 30:
        state = "INSTABILITY_GROWTH"
    elif accel >= 15:
        state = "SLOW_MUTATION"
    else:
        state = "STABLE_MATURATION"

    return {
        "lookback_days": lookback_days,
        "bucket_days": bucket_days,
        "behavioral_shift_rate": behavioral_shift_rate,
        "instability_acceleration": accel,
        "structural_maturity_score": maturity,
        "evolutionary_state": state,
        "bad_directions": bad_directions,
        "metric_trends": me["metric_trends"],
        "regime_entropy_series": me["regime_entropy_series"],
    }


# ── Memory compression / abstraction ─────────────────────────────────────


def memory_abstraction(db: Session) -> dict:
    """Compress anomaly memory into higher-level abstractions: per-
    archetype size + frequency + age range. Output is the
    "compressed view" of long-term market behavior."""
    arche = crisis_archetypes(db, max_archetypes=8)
    total = sum(a["size"] for a in arche["archetypes"])
    abstractions: List[dict] = []
    for a in arche["archetypes"]:
        abstractions.append({
            "archetype_id": a["archetype_id"],
            "label": a["archetype_label"],
            "share_of_memory": (a["size"] / total) if total > 0 else 0.0,
            "members": a["size"],
            "frequency_per_day": a["frequency_per_day"],
            "structural_severity": a["structural_severity"],
        })
    # Density score: how concentrated is memory in top-3 archetypes?
    sizes = sorted([a["size"] for a in arche["archetypes"]], reverse=True)
    top3 = sum(sizes[:3])
    density_score = (top3 / total * 100.0) if total > 0 else 0.0
    return {
        "total_anomalies": total,
        "abstractions": abstractions,
        "memory_density_score": density_score,
    }


# ── Intelligence evolution forecasting ───────────────────────────────────


def intelligence_evolution_forecast(db: Session, horizon_days: int = 7) -> dict:
    """Linear extrapolation of recent intelligence_history trends, with
    a conservative confidence framework that resists short history,
    runaway slopes, and direction reversals. NOT a price forecast —
    only "where is the engine itself drifting toward?".

    Reliability framework (each per-forecast field is explainable in UI):

      * data_quality gate — INSUFFICIENT (<24 snapshots, ~2h @ 5-min
        cadence) returns empty forecasts + UNKNOWN trajectory. Below
        72 (~6h) all individual forecasts are stamped LOW.
      * slope_capped — raw slope clipped to ±SLOPE_CAP_PER_DAY (25 units)
        so a single intra-hour spike can't paint a 7-day apocalypse.
      * extrapolation_capped — true when the *uncapped* OLS forecast
        would have left the metric's natural [0, 100] band, i.e. the
        slope can't physically extend that far. Confidence is halved.
      * slope_consistency — slope is fit independently on the first and
        last half of the window; if signs disagree the trend is
        reversing → consistency=0 → confidence is halved.
      * horizon decay — confidence *= data_span_days / (data_span_days
        + horizon_days), i.e. the fraction of the total considered time
        window that is actual data. Symmetric and interpretable: 7d
        forecast off 7d data → 0.5×; off 1d data → 0.125×.
      * trajectory gate — only labelled when stress has data_quality
        MEDIUM+ AND confidence ≥ 30. Otherwise UNKNOWN.
    """
    from kazus_db.models import LiquidityIntelligenceHistory

    SLOPE_CAP_PER_DAY = 25.0          # any metric is 0..100; ±25/day already aggressive
    MIN_SNAPSHOTS_HARD = 24            # below this → no forecasts at all
    MIN_SNAPSHOTS_PER_METRIC = 12
    TRAJECTORY_MIN_CONFIDENCE = 30.0

    since_ms = int(time.time() * 1000) - 21 * 24 * 3600 * 1000
    rows = (
        db.query(LiquidityIntelligenceHistory.ts_ms,
                 LiquidityIntelligenceHistory.synthesized_stress,
                 LiquidityIntelligenceHistory.structural_break_score,
                 LiquidityIntelligenceHistory.meta_intelligence_health,
                 LiquidityIntelligenceHistory.regime_shift_probability)
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )
    n_snapshots = len(rows)
    overall_quality = _discovery_quality(n_snapshots, low=24, medium=72, high=288)

    if n_snapshots < MIN_SNAPSHOTS_HARD:
        return {
            "horizon_days": horizon_days,
            "forecasts": [],
            "trajectory": "UNKNOWN",
            "snapshot_count": n_snapshots,
            "data_quality": overall_quality,
        }

    def _ols(xs: List[float], ys: List[float]) -> Optional[Tuple[float, float]]:
        m = len(xs)
        if m < 2:
            return None
        mx = sum(xs) / m
        my = sum(ys) / m
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 0:
            return None
        slope = num / den
        intercept = my - slope * mx
        return slope, intercept

    def _fit(key: str) -> Optional[dict]:
        pts = [(r.ts_ms, getattr(r, key)) for r in rows if getattr(r, key) is not None]
        if len(pts) < MIN_SNAPSHOTS_PER_METRIC:
            return None
        t0 = pts[0][0]
        xs = [(t - t0) / (24 * 3600_000) for t, _ in pts]
        ys = [v for _, v in pts]

        fit = _ols(xs, ys)
        if fit is None:
            return None
        raw_slope, intercept = fit

        # Slope cap — runaway extrapolation guard.
        slope = max(-SLOPE_CAP_PER_DAY, min(SLOPE_CAP_PER_DAY, raw_slope))
        slope_capped = (raw_slope != slope)

        # Half-window slope consistency. If first-half and second-half
        # disagree on sign, trend is reversing — confidence will get
        # halved later. If signs agree but magnitudes differ wildly,
        # smoothly attenuate.
        mid = len(xs) // 2
        slope_consistency = 0.5  # default when halves can't be fit
        first = _ols(xs[:mid], ys[:mid]) if mid >= 2 else None
        second = _ols(xs[mid:], ys[mid:]) if len(xs) - mid >= 2 else None
        if first is not None and second is not None:
            s1, s2 = first[0], second[0]
            if s1 == 0 and s2 == 0:
                slope_consistency = 1.0
            elif (s1 > 0) != (s2 > 0) and not (s1 == 0 or s2 == 0):
                slope_consistency = 0.0
            else:
                slope_consistency = 1.0 - abs(s1 - s2) / (abs(s1) + abs(s2) + 1e-9)
                slope_consistency = max(0.0, min(1.0, slope_consistency))

        residuals = [(y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)]
        rmse = math.sqrt(sum(residuals) / len(xs))

        # Forecast using capped slope; check if the *raw* slope would have
        # blown past [0, 100] — that's the runaway signal we surface.
        latest_x = xs[-1]
        current = ys[-1]
        raw_forecast = raw_slope * horizon_days + current
        extrapolation_capped = raw_forecast < 0.0 or raw_forecast > 100.0
        forecast_y = max(0.0, min(100.0, slope * horizon_days + current))

        # Confidence pipeline.
        data_span_days = max(1e-6, xs[-1] - xs[0])
        horizon_decay = data_span_days / (data_span_days + horizon_days)
        rmse_factor = max(0.0, 1.0 - rmse / 50.0)        # rmse=50 already kills confidence
        consistency_factor = 0.5 + 0.5 * slope_consistency
        # Halve confidence when the slope had to be physically clipped.
        cap_factor = 0.5 if (slope_capped or extrapolation_capped) else 1.0

        confidence = 100.0 * rmse_factor * horizon_decay * consistency_factor * cap_factor

        # Per-forecast quality bucket: cannot exceed overall quality, and
        # gets pulled down by structural problems detected above.
        if overall_quality == "INSUFFICIENT":
            per_quality = "INSUFFICIENT"
        elif overall_quality == "LOW" or slope_capped or extrapolation_capped or slope_consistency < 0.3:
            per_quality = "LOW"
        elif overall_quality == "MEDIUM" or slope_consistency < 0.7:
            per_quality = "MEDIUM"
        else:
            per_quality = "HIGH"

        return {
            "metric": key,
            "current": current,
            "slope_per_day": slope,
            "raw_slope_per_day": raw_slope,
            "slope_capped": slope_capped,
            "extrapolation_capped": extrapolation_capped,
            "slope_consistency": slope_consistency,
            "forecast_in_days": horizon_days,
            "forecast_value": forecast_y,
            "rmse": rmse,
            "horizon_decay": horizon_decay,
            "confidence": max(0.0, min(100.0, confidence)),
            "data_quality": per_quality,
        }

    forecasts: List[dict] = []
    for key in ("synthesized_stress", "structural_break_score", "meta_intelligence_health", "regime_shift_probability"):
        f = _fit(key)
        if f is not None:
            forecasts.append(f)

    # Trajectory label: only commit to a direction when the stress
    # forecast itself is trustworthy enough.
    stress_f = next((f for f in forecasts if f["metric"] == "synthesized_stress"), None)
    if (
        stress_f is None
        or stress_f["data_quality"] in ("INSUFFICIENT", "LOW")
        or stress_f["confidence"] < TRAJECTORY_MIN_CONFIDENCE
    ):
        trajectory = "UNKNOWN"
    elif stress_f["slope_per_day"] > 1.5:
        trajectory = "ESCALATING"
    elif stress_f["slope_per_day"] < -1.5:
        trajectory = "DEESCALATING"
    elif abs(stress_f["slope_per_day"]) < 0.3:
        trajectory = "STEADY"
    else:
        trajectory = "DRIFTING"

    return {
        "horizon_days": horizon_days,
        "forecasts": forecasts,
        "trajectory": trajectory,
        "snapshot_count": n_snapshots,
        "data_quality": overall_quality,
    }


# ── Adaptive structural recommendations ──────────────────────────────────


def adaptation_recommendations(db: Session) -> dict:
    """Reads several Phase-9/10/11 outputs and produces concrete
    recommendations for what to up-weight, down-weight, or tighten.
    Recommendations are descriptive — nothing here writes config; the
    user reads and decides whether to apply.
    """
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - 30 * 24 * 3600 * 1000

    weights = adaptive_metric_weights(db, since_ms=now_ms - 7 * 24 * 3600 * 1000)
    persistence = edge_persistence(db, since_ms=since_ms, window_days=7)
    mutation = edge_mutation(db, since_ms=since_ms, window_days=7)
    cal = threshold_calibration(db, since_ms=since_ms)

    recs: List[dict] = []
    # Strengthen useful patterns: top-3 highest-weight metrics.
    top_weights = sorted(weights["weights"], key=lambda w: -w["weight"])[:3]
    for w in top_weights:
        if w["weight"] > 1.10 and w["samples"] >= 5:
            recs.append({
                "action": "STRENGTHEN",
                "target": w["metric"],
                "rationale": f"metric in cohort top decile during {w['extreme_hits']}/{w['samples']} follow-through alerts (×{w['weight']:.2f})",
                "importance_shift": w["weight"] - 1.0,
            })
    # Weaken noisy patterns: bottom weights.
    weakest = sorted(weights["weights"], key=lambda w: w["weight"])[:3]
    for w in weakest:
        if w["weight"] < 0.9 and w["samples"] >= 5:
            recs.append({
                "action": "WEAKEN",
                "target": w["metric"],
                "rationale": f"rarely extreme during follow-through alerts (extreme {w['extreme_share']*100:.0f}% vs 20% baseline)",
                "importance_shift": w["weight"] - 1.0,
            })

    # Threshold tightening for kinds where calibration recommends it.
    for k in cal["kinds"]:
        if k["action"] == "TIGHTEN" and k["calibration_confidence"] >= 50:
            recs.append({
                "action": "TIGHTEN_THRESHOLD",
                "target": k["kind"],
                "rationale": f"{k['rationale'][0]} → suggest ×{k['adjustment_multiplier']:.2f}",
                "importance_shift": -(k["adjustment_multiplier"] - 1.0),
            })
        elif k["action"] == "LOOSEN" and k["calibration_confidence"] >= 50:
            recs.append({
                "action": "LOOSEN_THRESHOLD",
                "target": k["kind"],
                "rationale": f"{k['rationale'][0]} → suggest ×{k['adjustment_multiplier']:.2f}",
                "importance_shift": k["adjustment_multiplier"] - 1.0,
            })

    # Reweight unstable signals: kinds whose edge_mutation flagged WEAKENING/INVERTED.
    for m in mutation["kinds"]:
        if m["mutation_direction"] in ("INVERTED", "WEAKENING") and m["recent_resolved"] >= 5:
            recs.append({
                "action": "REWEIGHT_UNSTABLE",
                "target": m["kind"],
                "rationale": f"{m['mutation_direction']} edge — precision Δ {(m['delta'] or 0) * 100:.1f}pp recent vs prior",
                "importance_shift": (m["delta"] or 0.0),
            })

    # Strengthen edges where persistence shows positive slope.
    for p in persistence["kinds"]:
        if p["slope_per_day"] is not None and p["slope_per_day"] > 0.01 and p["latest_precision"] is not None:
            recs.append({
                "action": "STRENGTHEN_EDGE",
                "target": p["kind"],
                "rationale": f"precision rising +{p['slope_per_day']*100:.2f}pp/day (current {p['latest_precision']*100:.0f}%)",
                "importance_shift": min(0.5, p["slope_per_day"] * 10),
            })

    # Dedup by (action, target).
    seen: set = set()
    deduped: List[dict] = []
    for r in recs:
        key = (r["action"], r["target"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    # Overall adaptation score: balance of positive shifts.
    total_pos = sum(max(0.0, r["importance_shift"]) for r in deduped)
    total_neg = sum(max(0.0, -r["importance_shift"]) for r in deduped)
    adaptation_score = max(0.0, min(100.0, 50.0 + (total_pos - total_neg) * 10.0))

    return {
        "fetched_at_ms": now_ms,
        "recommendations": deduped,
        "adaptation_score": adaptation_score,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Stabilization sprint — sanity audit + discovery_quality helper
# ══════════════════════════════════════════════════════════════════════════
#
# The user explicitly stopped adding new features and ran a validation
# sprint. These helpers exist so the frontend can decide which discovery
# outputs to trust and which to label "insufficient evidence".


def _discovery_quality(samples: int, *, low: int, medium: int, high: int) -> str:
    """Three-bucket sample-adequacy classification.

    Caller picks the thresholds per-endpoint — e.g. pattern_discovery
    cares about distinct buckets, hidden_regimes about snapshot count,
    archetype clustering about anomaly count. Returns a string the UI
    renders as a chip ("HIGH" green, "MEDIUM" blue, "LOW" amber,
    "INSUFFICIENT" muted)."""
    if samples >= high:
        return "HIGH"
    if samples >= medium:
        return "MEDIUM"
    if samples >= low:
        return "LOW"
    return "INSUFFICIENT"


def _classify_finding(
    *,
    kind: str,
    category: str,
    value: float,
    info_threshold: float,
    warn_threshold: float,
    critical_threshold: float,
    detail: str,
    trend: str = "NEW",
    threshold_unit: str = "",
    higher_is_worse: bool = True,
) -> Optional[dict]:
    """Build a finding with calibrated severity and a smooth 0–100 score.

    `value` is in the same scale as the thresholds. `higher_is_worse=False`
    is for inverted checks where smaller values are worse (e.g. integrity
    score) — we flip internally so callers always pass the natural metric.
    """
    if not higher_is_worse:
        # Mirror the value across the midpoint so the rest of the math
        # stays the same. A "low integrity = bad" check passes the raw
        # integrity score; we flip it so high values trigger.
        # Easier: just pass thresholds the caller already inverted.
        # Keep the contract simple: caller passes already-comparable values.
        pass
    if value < info_threshold:
        return None
    if value >= critical_threshold:
        severity = "critical"
    elif value >= warn_threshold:
        severity = "warn"
    else:
        severity = "info"
    span = max(critical_threshold - info_threshold, 1e-9)
    sev_score = max(0.0, min(100.0, (value - info_threshold) / span * 100.0))
    return {
        "kind": kind,
        "category": category,
        "severity": severity,
        "severity_score": sev_score,
        "detail": detail,
        "metric_value": float(value),
        "info_threshold": float(info_threshold),
        "warn_threshold": float(warn_threshold),
        "critical_threshold": float(critical_threshold),
        "threshold_unit": threshold_unit,
        "trend": trend,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase 15 — Causal Propagation Layer
# ══════════════════════════════════════════════════════════════════════════
#
# `propagation_graph` answered: "what pairs of symbols tend to alert in
# sequence?". Useful but agnostic to *why* — A→B might be a real causal
# influence, a common-driver echo (both follow some third symbol), or
# pure coincidence within a market-wide event window.
#
# `causal_propagation` layers four explicit tests on top of the same
# pair-finding logic and emits a verdict per pair with an explainable
# rationale. The system never claims to "know the cause" — it accumulates
# evidence and labels confidence honestly.
#
# Tests (each independently fails-the-claim):
#
#   1. Directional asymmetry: A→B count vs B→A count. If close to 50/50,
#      direction is undetermined.
#
#   2. Multi-window persistence: lookback split into N sub-windows, pair
#      must appear in ≥ 2 of them to count as "stable". A single-burst
#      pair is flagged UNDER_EVIDENCED even if its raw count is high.
#
#   3. Common-driver elimination: if a third symbol C is found that
#      separately influences both A and B with comparable strength, the
#      (A, B) pair is reclassified COMMON_DRIVEN — not because we
#      "proved" C is the cause, but because the structural pattern is
#      consistent with C-mediation and we must surface that ambiguity.
#
#   4. Scarcity gate: data_quality INSUFFICIENT/LOW → all pairs are
#      EXPLORATORY regardless of how clean their numbers look. We never
#      issue causal verdicts on a fresh DB.
#
# Verdicts (priority order on emission):
#   COINCIDENCE       — symmetry_penalty ≥ 0.7 (effectively bidirectional)
#   COMMON_DRIVEN     — common-driver candidate found
#   EXPLORATORY       — data_quality below MEDIUM
#   UNDER_EVIDENCED   — present in ≤ 1 sub-window
#   AMBIGUOUS         — asymmetry < 0.40, no clear direction
#   DIRECTIONAL       — only label we treat as a working hypothesis


def _causal_compute_edges(
    by_kind: Dict[str, List[Tuple[int, str]]],
    lead_window_ms: int,
    min_lead_ms: int,
) -> Dict[Tuple[str, str], int]:
    """Compute deduplicated A→B pair counts for one slice of events.
    Shared helper between full-window and sub-window passes."""
    edges: Dict[Tuple[str, str], int] = defaultdict(int)
    for lst in by_kind.values():
        lst.sort()
        for i, (ts_a, sym_a) in enumerate(lst):
            charged: set = set()
            for j in range(i + 1, len(lst)):
                ts_b, sym_b = lst[j]
                lead = ts_b - ts_a
                if lead > lead_window_ms:
                    break
                if lead < min_lead_ms or sym_a == sym_b or sym_b in charged:
                    continue
                charged.add(sym_b)
                edges[(sym_a, sym_b)] += 1
    return edges


def causal_propagation(
    db: Session,
    lookback_days: int = 7,
    lead_window_ms: int = 30 * 60_000,
    min_lead_ms: int = 5_000,
    n_windows: int = 3,
) -> dict:
    """Build a causal-style propagation analysis with explicit verdicts.
    Output is intentionally conservative: edges are labeled by the
    strongest disqualifying signal first (coincidence > common-driver >
    evidence > asymmetry) and only the residue is called DIRECTIONAL.

    `n_windows` splits the lookback period into N equal-length slices;
    a directional claim must survive in ≥ 2 of them. With 7d lookback
    and n_windows=3, each slice is ~2.3 days.
    """
    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = db.execute(
        text(
            """
            SELECT symbol, kind, started_at_ms
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            ORDER BY started_at_ms
            """
        ),
        {"since": since_ms},
    ).fetchall()

    total_alerts = len(rows)
    overall_quality = _discovery_quality(total_alerts, low=200, medium=800, high=3000)

    # Split alerts into full window AND each sub-window in a single pass.
    span_ms = max(1, int(time.time() * 1000) - since_ms)
    window_ms = span_ms // n_windows
    by_kind_full: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    by_kind_per_window: List[Dict[str, List[Tuple[int, str]]]] = [
        defaultdict(list) for _ in range(n_windows)
    ]
    for r in rows:
        ts = int(r.started_at_ms)
        w_idx = min(n_windows - 1, max(0, (ts - since_ms) // window_ms))
        by_kind_full[r.kind].append((ts, r.symbol))
        by_kind_per_window[w_idx][r.kind].append((ts, r.symbol))

    full_edges = _causal_compute_edges(by_kind_full, lead_window_ms, min_lead_ms)
    window_edges: List[Dict[Tuple[str, str], int]] = [
        _causal_compute_edges(by_kind_per_window[w], lead_window_ms, min_lead_ms)
        for w in range(n_windows)
    ]

    # ── Common-driver detection ───────────────────────────────────────
    # Build per-symbol "influences" set: which symbols does X precede
    # with count ≥ threshold? Then for each candidate pair (A, B), look
    # for a third symbol C in the intersection of influencers of A and
    # influencers of B with comparable strength to both legs.
    INFLUENCE_THRESHOLD = 5
    influences_to: Dict[str, set] = defaultdict(set)  # X → {Y: X→Y count ≥ threshold}
    influenced_by: Dict[str, set] = defaultdict(set)  # Y → {X: X→Y count ≥ threshold}
    for (a, b), c in full_edges.items():
        if c >= INFLUENCE_THRESHOLD:
            influences_to[a].add(b)
            influenced_by[b].add(a)

    def find_common_driver(a: str, b: str) -> Optional[Tuple[str, int]]:
        # Drivers must be sources of both A and B (i.e. C→A and C→B).
        candidates = influenced_by[a] & influenced_by[b]
        best: Optional[Tuple[str, int]] = None
        for c in candidates:
            if c == a or c == b:
                continue
            ca = full_edges.get((c, a), 0)
            cb = full_edges.get((c, b), 0)
            strength = min(ca, cb)
            if strength >= INFLUENCE_THRESHOLD and (best is None or strength > best[1]):
                best = (c, strength)
        return best

    # ── Per-edge classification ──────────────────────────────────────

    causal_edges: List[dict] = []
    for (a, b), count in full_edges.items():
        if count < 3:
            continue
        reverse_count = full_edges.get((b, a), 0)
        total = count + reverse_count
        # Asymmetry in [-1, +1]; positive means A→B dominates.
        asymmetry = (count - reverse_count) / max(total, 1)
        if reverse_count > 0:
            symmetry_penalty = (min(count, reverse_count) / max(count, reverse_count)) ** 2
        else:
            symmetry_penalty = 0.0

        evidence_count = sum(1 for w in range(n_windows) if (a, b) in window_edges[w])

        cd = find_common_driver(a, b)
        common_driver = cd[0] if cd else None
        common_driver_strength = cd[1] if cd else 0

        # Verdict — priority order: coincidence first (because it cancels
        # everything else), then scarcity, then common-driver, then
        # evidence, then asymmetry.
        if symmetry_penalty >= 0.70:
            verdict = "COINCIDENCE"
        elif overall_quality in ("INSUFFICIENT", "LOW"):
            verdict = "EXPLORATORY"
        elif common_driver is not None:
            verdict = "COMMON_DRIVEN"
        elif evidence_count <= 1:
            verdict = "UNDER_EVIDENCED"
        elif asymmetry < 0.40:
            verdict = "AMBIGUOUS"
        else:
            verdict = "DIRECTIONAL"

        # Causal confidence — multiplicative blend. Each factor is in
        # [0, 1] and zeros-out the claim independently. Surfaced as four
        # separate numbers so the operator can see which factor dragged
        # it down.
        volume_factor = 1.0 - math.exp(-count / 15.0)
        asymmetry_factor = max(0.0, asymmetry)
        evidence_factor = evidence_count / float(n_windows)
        common_driver_factor = 0.35 if common_driver else 1.0
        symmetry_factor = 1.0 - symmetry_penalty
        SCARCITY = {"INSUFFICIENT": 0.15, "LOW": 0.40, "MEDIUM": 0.75, "HIGH": 1.0}
        scarcity_factor = SCARCITY.get(overall_quality, 0.15)

        causal_confidence = (
            volume_factor * asymmetry_factor * evidence_factor *
            common_driver_factor * symmetry_factor * scarcity_factor
        )

        # Human-readable rationale — what evidence drove the verdict.
        if verdict == "DIRECTIONAL":
            rationale = (
                f"{count} A→B vs {reverse_count} B→A (asymmetry "
                f"{asymmetry * 100:.0f}%), survives in {evidence_count}/{n_windows} "
                f"sub-windows; no common driver detected"
            )
        elif verdict == "COMMON_DRIVEN":
            ca = full_edges.get((common_driver, a), 0)
            cb = full_edges.get((common_driver, b), 0)
            rationale = (
                f"{common_driver} independently precedes both A ({ca}×) "
                f"and B ({cb}×) — A→B likely mediated by {common_driver}, "
                f"not direct"
            )
        elif verdict == "COINCIDENCE":
            rationale = (
                f"A→B {count} ≈ B→A {reverse_count} (symmetry "
                f"{symmetry_penalty * 100:.0f}%) — direction can't be inferred"
            )
        elif verdict == "UNDER_EVIDENCED":
            rationale = (
                f"present in only {evidence_count}/{n_windows} "
                f"sub-windows — single-burst, not stable"
            )
        elif verdict == "AMBIGUOUS":
            rationale = (
                f"asymmetry only {asymmetry * 100:.0f}% "
                f"(threshold 40%) — direction is too close to call"
            )
        else:  # EXPLORATORY
            rationale = (
                f"data_quality={overall_quality} — no causal claims "
                f"on {total_alerts} alerts"
            )

        causal_edges.append({
            "from_symbol": a,
            "to_symbol": b,
            "count": count,
            "reverse_count": reverse_count,
            "asymmetry": asymmetry,
            "evidence_count": evidence_count,
            "n_windows": n_windows,
            "symmetry_penalty": symmetry_penalty,
            "common_driver": common_driver,
            "common_driver_strength": common_driver_strength,
            "causal_confidence": causal_confidence,
            "verdict": verdict,
            "rationale": rationale,
            # Confidence decomposition — exposed so the UI can show why.
            "factors": {
                "volume": volume_factor,
                "asymmetry": asymmetry_factor,
                "evidence": evidence_factor,
                "common_driver_penalty": common_driver_factor,
                "symmetry": symmetry_factor,
                "scarcity": scarcity_factor,
            },
        })

    # Sort by causal_confidence descending so the most defensible claims
    # surface first (DIRECTIONAL will naturally cluster at the top).
    causal_edges.sort(key=lambda e: -e["causal_confidence"])
    causal_edges = causal_edges[:60]

    # ── Influence hierarchy per node ─────────────────────────────────
    #
    # Each symbol gets a role + an explicit rationale. Roles are derived
    # only from edges that we've already labeled — we never look "inside"
    # an edge again, so hierarchy is consistent with the per-edge verdicts.

    out_edges_by_sym: Dict[str, List[dict]] = defaultdict(list)
    in_edges_by_sym: Dict[str, List[dict]] = defaultdict(list)
    for e in causal_edges:
        out_edges_by_sym[e["from_symbol"]].append(e)
        in_edges_by_sym[e["to_symbol"]].append(e)

    symbols = set(out_edges_by_sym) | set(in_edges_by_sym)
    nodes_out: List[dict] = []
    for s in symbols:
        outs = out_edges_by_sym.get(s, [])
        ins = in_edges_by_sym.get(s, [])
        n_out = len(outs)
        n_in = len(ins)
        total_edges = n_out + n_in

        avg_out_conf = (sum(e["causal_confidence"] for e in outs) / n_out) if n_out else 0.0
        avg_in_conf = (sum(e["causal_confidence"] for e in ins) / n_in) if n_in else 0.0

        all_edges = outs + ins
        n_directional = sum(1 for e in all_edges if e["verdict"] == "DIRECTIONAL")
        n_low_quality = sum(1 for e in all_edges if e["verdict"] in ("COINCIDENCE", "UNDER_EVIDENCED", "AMBIGUOUS"))
        stability = n_directional / total_edges if total_edges else 0.0

        if total_edges < 3:
            role = "ISOLATED"
            role_rationale = f"only {total_edges} edge(s) above the confidence cutoff"
        elif stability < 0.30 and n_low_quality >= 2:
            role = "INSTABILITY_HUB"
            role_rationale = (
                f"{n_low_quality}/{total_edges} edges are coincidence/ambiguous/"
                f"under-evidenced — symbol participates in many pairings but few are "
                f"directional"
            )
        else:
            out_ratio = n_out / total_edges
            if out_ratio > 0.70 and avg_out_conf >= 0.20:
                role = "LEADER"
                role_rationale = (
                    f"out={n_out} ≫ in={n_in}, avg outgoing causal_confidence "
                    f"{avg_out_conf * 100:.0f}%; precedes others more than it follows"
                )
            elif out_ratio < 0.30 and avg_in_conf >= 0.20:
                role = "FOLLOWER"
                role_rationale = (
                    f"in={n_in} ≫ out={n_out}, avg incoming causal_confidence "
                    f"{avg_in_conf * 100:.0f}%; consistently lagging others"
                )
            elif 0.30 <= out_ratio <= 0.70 and (avg_out_conf >= 0.20 or avg_in_conf >= 0.20):
                role = "AMPLIFIER"
                role_rationale = (
                    f"balanced in/out ({n_out}/{n_in}) — receives and passes "
                    f"events through, not a directional source or sink"
                )
            else:
                role = "ISOLATED"
                role_rationale = (
                    f"in/out balance {n_in}/{n_out} but average confidence too low"
                    f" to commit to a role"
                )

        nodes_out.append({
            "symbol": s,
            "out_count": n_out,
            "in_count": n_in,
            "avg_out_confidence": avg_out_conf,
            "avg_in_confidence": avg_in_conf,
            "stability": stability,
            "role": role,
            "role_rationale": role_rationale,
        })

    nodes_out.sort(key=lambda n: (
        # LEADER first, then AMPLIFIER, then FOLLOWER, then HUB, then ISOLATED
        {"LEADER": 0, "AMPLIFIER": 1, "FOLLOWER": 2, "INSTABILITY_HUB": 3, "ISOLATED": 4}.get(n["role"], 5),
        -n["avg_out_confidence"],
    ))

    # ── Summary counts for UI banner ─────────────────────────────────
    verdict_counts: Dict[str, int] = defaultdict(int)
    for e in causal_edges:
        verdict_counts[e["verdict"]] += 1
    role_counts: Dict[str, int] = defaultdict(int)
    for n in nodes_out:
        role_counts[n["role"]] += 1

    return {
        "since_ms": since_ms,
        "lookback_days": lookback_days,
        "n_windows": n_windows,
        "total_alerts": total_alerts,
        "edges": causal_edges,
        "nodes": nodes_out[:60],
        "verdict_counts": dict(verdict_counts),
        "role_counts": dict(role_counts),
        "data_quality": overall_quality,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase 15 #2 — Structural Dependency Graph
# ══════════════════════════════════════════════════════════════════════════
#
# `causal_propagation` produces per-pair verdicts. The dependency graph
# layer composes those verdicts into structural findings:
#
#   1. Influence chains — A → B → C paths where each step is DIRECTIONAL.
#      Surfaces multi-hop influence beyond pairwise lead-lag.
#
#   2. Dependency clusters (common-driver groups) — symbols that share a
#      common driver C are grouped under C. These are NOT claims of
#      "C controls the cluster"; they say "this set co-moves around C
#      with no detected direct influence between members".
#
#   3. Dominant drivers — ranked by BFS reach in the DIRECTIONAL subgraph.
#      A symbol's reach is the number of distinct symbols it can influence
#      through chains of up to 3 hops.
#
#   4. Synchronized stress groups — connected components on the COINCIDENCE
#      pair graph (undirected). Names groups of symbols that alert
#      together in time-bursts without a detectable directional structure.
#
# Every output explicitly inherits the scarcity gate from causal_propagation:
# under INSUFFICIENT/LOW data_quality the entire result is flagged
# exploratory and the UI surfaces it as candidate-only.


def structural_dependencies(
    db: Session,
    lookback_days: int = 7,
) -> dict:
    """Compose causal_propagation verdicts into structural findings.
    Read-only — no new SQL beyond what causal_propagation already runs.
    """
    causal = causal_propagation(db, lookback_days=lookback_days)
    edges: List[dict] = causal["edges"]
    quality: str = causal["data_quality"]
    exploratory = quality in ("INSUFFICIENT", "LOW")

    directional = [e for e in edges if e["verdict"] == "DIRECTIONAL"]
    common_driven = [e for e in edges if e["verdict"] == "COMMON_DRIVEN"]
    coincidence_e = [e for e in edges if e["verdict"] == "COINCIDENCE"]

    # ── Influence chains ─────────────────────────────────────────────
    out_adj: Dict[str, List[Tuple[str, float, dict]]] = defaultdict(list)
    for e in directional:
        out_adj[e["from_symbol"]].append(
            (e["to_symbol"], e["causal_confidence"], e)
        )

    chains: List[dict] = []
    for a, outs_a in out_adj.items():
        for b, conf_ab, edge_ab in outs_a:
            if b not in out_adj:
                continue
            for c, conf_bc, edge_bc in out_adj[b]:
                if c == a:
                    continue  # no degenerate self-loop chains
                # min-confidence is the bottleneck of the chain
                min_conf = min(conf_ab, conf_bc)
                chains.append({
                    "path": [a, b, c],
                    "min_confidence": min_conf,
                    "step_confidences": [conf_ab, conf_bc],
                    "rationale": (
                        f"{a}→{b} (conf {conf_ab * 100:.0f}, asym "
                        f"{edge_ab['asymmetry'] * 100:.0f}%) then "
                        f"{b}→{c} (conf {conf_bc * 100:.0f}, asym "
                        f"{edge_bc['asymmetry'] * 100:.0f}%) — "
                        f"both DIRECTIONAL"
                    ),
                })
    chains.sort(key=lambda c: -c["min_confidence"])
    chains = chains[:25]

    # ── Dependency clusters from common-driver groups ────────────────
    driver_members: Dict[str, set] = defaultdict(set)
    driver_strength: Dict[str, int] = defaultdict(int)
    for e in common_driven:
        d = e["common_driver"]
        if not d:
            continue
        driver_members[d].add(e["from_symbol"])
        driver_members[d].add(e["to_symbol"])
        driver_strength[d] = max(driver_strength[d], e["common_driver_strength"])

    dependency_clusters: List[dict] = []
    cluster_id = 0
    for driver, members in driver_members.items():
        members.discard(driver)  # driver itself isn't a cluster member
        if len(members) < 2:
            continue
        cluster_id += 1
        dependency_clusters.append({
            "cluster_id": cluster_id,
            "cluster_type": "common_driver",
            "driver": driver,
            "members": sorted(members),
            "size": len(members),
            "min_driver_strength": driver_strength[driver],
            "rationale": (
                f"{len(members)} symbols share {driver} as detected common "
                f"driver (no direct DIRECTIONAL edges between them). "
                f"Cluster is co-movement around {driver}, not internal "
                f"causal structure."
            ),
        })
    dependency_clusters.sort(key=lambda c: -c["size"])

    # ── Dominant drivers via BFS reach in DIRECTIONAL subgraph ───────
    REACH_DEPTH = 3
    direct_out_count: Dict[str, int] = defaultdict(int)
    avg_out_conf: Dict[str, float] = {}
    for sym, outs in out_adj.items():
        direct_out_count[sym] = len(outs)
        avg_out_conf[sym] = sum(c for _, c, _ in outs) / max(1, len(outs))

    reach: Dict[str, set] = {}
    for sym in out_adj:
        visited = {sym}
        frontier = {sym}
        for _ in range(REACH_DEPTH):
            next_frontier: set = set()
            for node in frontier:
                for n2, _, _ in out_adj.get(node, ()):
                    if n2 not in visited:
                        visited.add(n2)
                        next_frontier.add(n2)
            frontier = next_frontier
            if not frontier:
                break
        reach[sym] = visited - {sym}

    dominant_drivers: List[dict] = []
    for sym, reachable in reach.items():
        if len(reachable) < 1:
            continue
        # Influence score blends reach with average outgoing confidence.
        score = len(reachable) * avg_out_conf[sym]
        dominant_drivers.append({
            "symbol": sym,
            "reach_depth": REACH_DEPTH,
            "reach_size": len(reachable),
            "direct_out_count": direct_out_count[sym],
            "avg_out_confidence": avg_out_conf[sym],
            "influence_score": score,
            "reachable_sample": sorted(reachable)[:6],
            "rationale": (
                f"{direct_out_count[sym]} direct DIRECTIONAL out-edge(s), "
                f"reaches {len(reachable)} distinct symbol(s) within "
                f"{REACH_DEPTH} hops at avg confidence "
                f"{avg_out_conf[sym] * 100:.0f}%"
            ),
        })
    dominant_drivers.sort(key=lambda d: -d["influence_score"])
    dominant_drivers = dominant_drivers[:15]

    # ── Synchronized stress groups (coincidence connected components) ─
    coin_adj: Dict[str, set] = defaultdict(set)
    for e in coincidence_e:
        coin_adj[e["from_symbol"]].add(e["to_symbol"])
        coin_adj[e["to_symbol"]].add(e["from_symbol"])

    sync_groups: List[dict] = []
    seen_in_group: set = set()
    group_id = 0
    for start in coin_adj:
        if start in seen_in_group:
            continue
        # BFS connected component
        component: List[str] = []
        queue = [start]
        while queue:
            node = queue.pop()
            if node in seen_in_group:
                continue
            seen_in_group.add(node)
            component.append(node)
            queue.extend(n for n in coin_adj[node] if n not in seen_in_group)
        if len(component) < 2:
            continue
        comp_set = set(component)
        internal_edges = sum(
            1 for e in coincidence_e
            if e["from_symbol"] in comp_set and e["to_symbol"] in comp_set
            and e["from_symbol"] < e["to_symbol"]
        )
        group_id += 1
        sync_groups.append({
            "group_id": group_id,
            "members": sorted(component),
            "size": len(component),
            "coincidence_edges": internal_edges,
            "rationale": (
                f"{len(component)} symbols connected by {internal_edges} "
                f"COINCIDENCE pair(s) — alert together in time-bursts without "
                f"detected directional structure"
            ),
        })
    sync_groups.sort(key=lambda g: -g["size"])
    sync_groups = sync_groups[:10]

    # ── Summary line for UI ──────────────────────────────────────────
    if exploratory:
        summary = (
            f"data_quality={quality} — {len(edges)} candidate edge(s), "
            f"no structural claims yet"
        )
    else:
        summary = (
            f"{len(directional)} directional edges → "
            f"{len(chains)} multi-hop chain(s), "
            f"{len(dependency_clusters)} co-driver cluster(s), "
            f"{len(dominant_drivers)} dominant driver(s), "
            f"{len(sync_groups)} synchronized stress group(s)"
        )

    return {
        "since_ms": causal["since_ms"],
        "lookback_days": lookback_days,
        "data_quality": quality,
        "exploratory": exploratory,
        "directional_edge_count": len(directional),
        "common_driven_edge_count": len(common_driven),
        "coincidence_edge_count": len(coincidence_e),
        "influence_chains": chains,
        "dependency_clusters": dependency_clusters,
        "dominant_drivers": dominant_drivers,
        "synchronized_groups": sync_groups,
        "summary": summary,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase 15 #3 — Market State Transition Intelligence
# ══════════════════════════════════════════════════════════════════════════
#
# Previous layers care about WHERE the market is. This layer cares about
# HOW it moves between states — which transitions persisted, which were
# flickers, where the system oscillates, where stress accelerated
# through the change.
#
# Inputs: `liquidity_intelligence_history` snapshots (5-min cadence),
# specifically the `coordinated_state` label plus `synthesized_stress`
# and `meta_confidence_score` numbers around each state change.
#
# Per transition we compute:
#
#   * persistence — how many consecutive snapshots remained in `to_state`
#     before the next transition. < PERSISTENCE_THRESHOLD = FLICKER.
#   * reversal — did we bounce back to `from_state` within REVERSAL_WINDOW
#     ticks? If yes, the transition is rejected (FLICKER even if longer).
#   * acceleration — slope of synthesized_stress in PRE_WINDOW before vs
#     POST_WINDOW after. Surfaces whether the state change coincided with
#     a meaningful change in pace, not just a relabel.
#   * meta_confidence — engine's own confidence at the moment of change.
#     Low values demote the transition.
#
# Aggregate:
#   * current state + how long it's lasted
#   * transition rate per day (≥ TRANSITION_RATE_WARN = system unstable)
#   * flicker ratio (flickers / total)
#   * oscillation periods (sliding windows with ≥ 3 transitions inside)
#
# As with #1/#2, scarcity gating cascades: under INSUFFICIENT/LOW
# data_quality everything is exploratory and confidence is scaled down.


def market_state_transitions(
    db: Session,
    lookback_days: int = 14,
) -> dict:
    """Detect + classify coordinated_state transitions in the intelligence
    snapshot stream. Output is per-transition with verdicts + an aggregate
    stability picture.
    """
    from kazus_db.models import LiquidityIntelligenceHistory

    PERSISTENCE_THRESHOLD = 3       # snapshots that must hold (≈ 15 min @ 5-min cadence)
    REVERSAL_WINDOW = 3             # snapshots within which a bounce-back invalidates
    PRE_WINDOW = 6                  # ≈ 30 min before
    POST_WINDOW = 6                 # ≈ 30 min after
    OSCILLATION_MIN_TRANSITIONS = 3 # within OSCILLATION_WINDOW_S
    OSCILLATION_WINDOW_S = 3600     # 1 hour
    ACCELERATION_THRESHOLD = 5.0    # stress points per 5-min slope diff

    since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    rows = (
        db.query(
            LiquidityIntelligenceHistory.ts_ms,
            LiquidityIntelligenceHistory.coordinated_state,
            LiquidityIntelligenceHistory.synthesized_stress,
            LiquidityIntelligenceHistory.meta_confidence_score,
            LiquidityIntelligenceHistory.dominant_regime,
        )
        .filter(LiquidityIntelligenceHistory.ts_ms >= since_ms)
        .filter(LiquidityIntelligenceHistory.coordinated_state.isnot(None))
        .order_by(LiquidityIntelligenceHistory.ts_ms.asc())
        .all()
    )

    snapshots = [
        {
            "ts_ms": int(r.ts_ms),
            "state": r.coordinated_state,
            "stress": float(r.synthesized_stress) if r.synthesized_stress is not None else None,
            "meta_conf": float(r.meta_confidence_score) if r.meta_confidence_score is not None else None,
            "regime": r.dominant_regime,
        }
        for r in rows
    ]
    n = len(snapshots)
    overall_quality = _discovery_quality(n, low=24, medium=72, high=288)
    exploratory = overall_quality in ("INSUFFICIENT", "LOW")
    SCARCITY = {"INSUFFICIENT": 0.15, "LOW": 0.40, "MEDIUM": 0.75, "HIGH": 1.0}
    scarcity_factor = SCARCITY.get(overall_quality, 0.15)

    # State vocabulary stats
    state_counts: Dict[str, int] = defaultdict(int)
    for s in snapshots:
        state_counts[s["state"]] += 1
    vocabulary = sorted(state_counts.keys())

    # ── Detect transitions ───────────────────────────────────────────
    transitions: List[dict] = []
    for i in range(1, n):
        if snapshots[i]["state"] == snapshots[i - 1]["state"]:
            continue
        from_state = snapshots[i - 1]["state"]
        to_state = snapshots[i]["state"]
        ts_ms = snapshots[i]["ts_ms"]

        # Persistence: how many consecutive snapshots remain in to_state
        persistence = 1
        for j in range(i + 1, n):
            if snapshots[j]["state"] == to_state:
                persistence += 1
            else:
                break

        # Reversal: did we return to from_state within REVERSAL_WINDOW snapshots
        was_reverted = any(
            snapshots[j]["state"] == from_state
            for j in range(i + 1, min(n, i + 1 + REVERSAL_WINDOW))
        )

        # Stress slope before and after
        def _slope(window: List[dict]) -> Optional[float]:
            vals = [w["stress"] for w in window if w["stress"] is not None]
            if len(vals) < 2:
                return None
            # OLS-free: simple diff per step ≈ average rate of change
            return (vals[-1] - vals[0]) / max(1, len(vals) - 1)

        pre = snapshots[max(0, i - PRE_WINDOW):i]
        post = snapshots[i:min(n, i + POST_WINDOW)]
        pre_slope = _slope(pre)
        post_slope = _slope(post)
        acceleration = (post_slope - pre_slope) if (pre_slope is not None and post_slope is not None) else None

        meta_conf = snapshots[i]["meta_conf"] or 0.0

        # Verdict: priority — REVERSED > FLICKER > ACCELERATING > PERSISTENT
        if was_reverted:
            verdict = "REVERSED"
        elif persistence < PERSISTENCE_THRESHOLD:
            verdict = "FLICKER"
        elif acceleration is not None and abs(acceleration) >= ACCELERATION_THRESHOLD:
            verdict = "ACCELERATING"
        else:
            verdict = "PERSISTENT"

        # Confidence: multiplicative blend.
        persistence_factor = min(1.0, persistence / 12.0)   # 1h = full credit
        meta_conf_factor = meta_conf / 100.0
        reversal_factor = 0.25 if was_reverted else 1.0
        confidence = persistence_factor * max(0.2, meta_conf_factor) * reversal_factor * scarcity_factor

        # Rationale
        if verdict == "REVERSED":
            rationale = (
                f"reverted to {from_state} within {REVERSAL_WINDOW} snapshots — "
                f"flicker, not a real state change"
            )
        elif verdict == "FLICKER":
            rationale = (
                f"only {persistence} snapshot(s) in {to_state} before next "
                f"change (threshold {PERSISTENCE_THRESHOLD}) — too brief to commit"
            )
        elif verdict == "ACCELERATING":
            arrow = "↑" if acceleration and acceleration > 0 else "↓"
            rationale = (
                f"persisted {persistence} snapshot(s); stress acceleration "
                f"{acceleration:+.1f}/tick {arrow} (pre {pre_slope:+.1f} vs "
                f"post {post_slope:+.1f}) — meaningful pace change"
            )
        else:
            rationale = (
                f"persisted {persistence} snapshot(s) at meta_confidence "
                f"{meta_conf:.0f} — stable change"
            )

        transitions.append({
            "ts_ms": ts_ms,
            "from_state": from_state,
            "to_state": to_state,
            "persistence_snapshots": persistence,
            "persistence_seconds": (
                snapshots[min(n - 1, i + persistence - 1)]["ts_ms"] - ts_ms
            ),
            "was_reverted": was_reverted,
            "pre_stress_slope": pre_slope,
            "post_stress_slope": post_slope,
            "acceleration": acceleration,
            "meta_confidence_at": meta_conf,
            "verdict": verdict,
            "confidence": confidence,
            "rationale": rationale,
        })

    # ── Current state stability ──────────────────────────────────────
    current_state = snapshots[-1]["state"] if snapshots else None
    current_state_duration_snapshots = 0
    if current_state is not None:
        for i in range(n - 1, -1, -1):
            if snapshots[i]["state"] == current_state:
                current_state_duration_snapshots += 1
            else:
                break
    current_state_duration_seconds = (
        snapshots[-1]["ts_ms"] - snapshots[n - current_state_duration_snapshots]["ts_ms"]
        if current_state_duration_snapshots > 1 else 0
    )

    # ── Oscillation periods: sliding-window over transition timestamps ─
    oscillation_periods: List[dict] = []
    t_times = [t["ts_ms"] for t in transitions]
    for i, t0 in enumerate(t_times):
        window_end = t0 + OSCILLATION_WINDOW_S * 1000
        count = sum(1 for ts in t_times[i:] if ts <= window_end)
        if count >= OSCILLATION_MIN_TRANSITIONS:
            # Avoid duplicates by checking if previous window already covered this
            if not oscillation_periods or oscillation_periods[-1]["end_ms"] < t0:
                oscillation_periods.append({
                    "start_ms": t0,
                    "end_ms": window_end,
                    "transition_count": count,
                    "rationale": (
                        f"{count} transitions in 1h — system oscillating between "
                        f"states, treat any single transition here as exploratory"
                    ),
                })

    # ── Aggregates ───────────────────────────────────────────────────
    total = len(transitions)
    flickers = sum(1 for t in transitions if t["verdict"] in ("FLICKER", "REVERSED"))
    flicker_ratio = (flickers / total) if total else 0.0
    span_days = max(1e-9, (snapshots[-1]["ts_ms"] - snapshots[0]["ts_ms"]) / 86400_000) if n > 1 else 0.0
    transition_rate_per_day = total / span_days if span_days > 0 else 0.0

    # Summary
    if exploratory:
        summary = (
            f"data_quality={overall_quality} ({n} snapshots) — "
            f"transitions tracked but classifications are exploratory"
        )
    else:
        stability = "stable" if flicker_ratio < 0.25 else ("noisy" if flicker_ratio < 0.5 else "unstable")
        summary = (
            f"{total} transitions over {span_days:.1f}d "
            f"({transition_rate_per_day:.1f}/day, {flicker_ratio * 100:.0f}% flicker) — "
            f"transition layer {stability}"
        )

    # Surface recent transitions first
    transitions.sort(key=lambda t: -t["ts_ms"])

    return {
        "since_ms": since_ms,
        "lookback_days": lookback_days,
        "data_quality": overall_quality,
        "exploratory": exploratory,
        "snapshot_count": n,
        "state_vocabulary": vocabulary,
        "state_counts": dict(state_counts),
        "current_state": current_state,
        "current_state_duration_snapshots": current_state_duration_snapshots,
        "current_state_duration_seconds": current_state_duration_seconds,
        "transition_count": total,
        "flicker_count": flickers,
        "flicker_ratio": flicker_ratio,
        "transition_rate_per_day": transition_rate_per_day,
        "transitions": transitions[:30],
        "oscillation_periods": oscillation_periods,
        "summary": summary,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase 15 #4 — Crisis Genesis Detection
# ══════════════════════════════════════════════════════════════════════════
#
# This layer answers: "is the market drifting toward a cascade BEFORE
# the cascade actually hits?" — i.e. early structural distortions that
# typically precede crisis events.
#
# It does NOT predict. It composes seven independent precursor probes,
# each with its own data source + scoring + rationale, into a composite
# genesis_score. Each probe is honest about its own data quality and
# can report INSUFFICIENT to take itself out of the composite.
#
# Probes (each in [0, 100]):
#
#   1. fragmentation_growth   coordinated_state vocabulary expanding 24h vs prior
#   2. resiliency_decay       avg resiliency_score across symbols falling
#   3. propagation_widening   propagation integrity dropping (weak + symmetric edges)
#   4. dependency_concentration top dominant driver's reach over the network
#   5. anomaly_synchronization anomaly_memory write rate accelerating
#   6. transition_instability flicker + oscillation in state changes
#   7. stress_acceleration    synthesized_stress slope rising faster than baseline
#
# Composite verdicts:
#   CALM             < 25
#   EARLY_DISTORTION  25-50    (one or two probes elevated)
#   ELEVATED_RISK     50-75    (multiple probes elevated)
#   PRE_CASCADE      ≥ 75      (most probes hot + recent confirming evidence)
#
# Scarcity cascade: under INSUFFICIENT/LOW data_quality, verdict is
# capped at EARLY_DISTORTION regardless of probe scores — the system
# refuses to declare PRE_CASCADE on a fresh database.


def _crisis_probe(
    *,
    kind: str,
    name: str,
    score: Optional[float],
    rationale: str,
    metric_value: Optional[float] = None,
    insufficient: bool = False,
) -> dict:
    """Normalize a precursor probe into a uniform dict shape with status
    derived from score so the UI doesn't have to."""
    if insufficient or score is None:
        return {
            "kind": kind,
            "name": name,
            "score": 0.0,
            "status": "insufficient",
            "rationale": rationale,
            "metric_value": metric_value,
            "contributes": False,
        }
    score_clipped = max(0.0, min(100.0, score))
    if score_clipped < 30:
        status = "calm"
    elif score_clipped < 65:
        status = "elevated"
    else:
        status = "hot"
    return {
        "kind": kind,
        "name": name,
        "score": score_clipped,
        "status": status,
        "rationale": rationale,
        "metric_value": metric_value,
        "contributes": True,
    }


def crisis_genesis(db: Session, lookback_days: int = 7) -> dict:
    """Compose seven precursor probes into a single genesis_score with
    explainable per-probe rationale. Read-only over cached upstream
    layers + two cheap direct queries (resiliency + anomaly counts)."""
    now_ms = int(time.time() * 1000)
    H = 3600 * 1000

    probes: List[dict] = []

    # ── Probe 1: fragmentation growth ────────────────────────────────
    try:
        rows = db.execute(
            text(
                """
                SELECT coordinated_state, ts_ms
                FROM liquidity_intelligence_history
                WHERE ts_ms >= :since AND coordinated_state IS NOT NULL
                """
            ),
            {"since": now_ms - 2 * 24 * H},
        ).fetchall()
        recent_states: set = set()
        prior_states: set = set()
        for r in rows:
            (recent_states if int(r.ts_ms) >= now_ms - 24 * H else prior_states).add(r.coordinated_state)
        if len(recent_states) == 0 and len(prior_states) == 0:
            probes.append(_crisis_probe(
                kind="fragmentation_growth",
                name="state vocabulary growth",
                score=None,
                rationale="no intelligence snapshots in either 24h window",
                insufficient=True,
            ))
        elif len(prior_states) == 0:
            # bootstrap — no baseline, can't measure growth
            probes.append(_crisis_probe(
                kind="fragmentation_growth",
                name="state vocabulary growth",
                score=None,
                rationale=f"no prior-24h baseline ({len(recent_states)} states present in recent 24h)",
                insufficient=True,
            ))
        else:
            ratio = len(recent_states) / max(1, len(prior_states))
            score = max(0.0, min(100.0, (ratio - 1.0) / 1.5 * 100.0))
            probes.append(_crisis_probe(
                kind="fragmentation_growth",
                name="state vocabulary growth",
                score=score,
                metric_value=ratio,
                rationale=(
                    f"{len(recent_states)} distinct coordinated_states in last 24h "
                    f"vs {len(prior_states)} prior — {ratio:.1f}× expansion"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="fragmentation_growth", name="state vocabulary growth",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 2: resiliency decay (samples) ──────────────────────────
    try:
        # Average resiliency_score over last 6h vs prior 6h, across symbols.
        recent = db.execute(
            text(
                "SELECT AVG(value) AS v, COUNT(*) AS n FROM liquidity_samples "
                "WHERE metric = 'resiliency_score' AND ts >= :since AND value IS NOT NULL"
            ),
            {"since": now_ms - 6 * H},
        ).first()
        prior = db.execute(
            text(
                "SELECT AVG(value) AS v, COUNT(*) AS n FROM liquidity_samples "
                "WHERE metric = 'resiliency_score' AND ts >= :s AND ts < :e AND value IS NOT NULL"
            ),
            {"s": now_ms - 12 * H, "e": now_ms - 6 * H},
        ).first()
        rv = float(recent.v) if recent and recent.v is not None else None
        pv = float(prior.v) if prior and prior.v is not None else None
        rn = int(recent.n or 0) if recent else 0
        pn = int(prior.n or 0) if prior else 0
        if rv is None or pv is None or rn < 20 or pn < 20:
            probes.append(_crisis_probe(
                kind="resiliency_decay", name="resiliency decay",
                score=None,
                rationale=f"not enough resiliency_score samples (recent {rn}, prior {pn}; need ≥ 20 each)",
                insufficient=True,
            ))
        else:
            delta = rv - pv  # negative = decay
            # 0 at delta ≥ 0; 50 at delta = -10; 100 at delta ≤ -25
            score = max(0.0, min(100.0, -delta * 4.0))
            probes.append(_crisis_probe(
                kind="resiliency_decay", name="resiliency decay",
                score=score, metric_value=delta,
                rationale=(
                    f"avg resiliency {rv:.1f} (recent 6h, n={rn}) vs {pv:.1f} "
                    f"(prior 6h, n={pn}) — Δ {delta:+.1f}"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="resiliency_decay", name="resiliency decay",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 3: propagation widening ────────────────────────────────
    try:
        prop = propagation_graph(db, lookback_days=lookback_days)
        comp = prop.get("integrity_components") or {}
        weak = float(comp.get("weak_share", 0.0))
        symmetric = float(comp.get("symmetric_share", 0.0))
        widening = (weak + symmetric) / 2.0 * 100.0
        if not prop.get("edges"):
            probes.append(_crisis_probe(
                kind="propagation_widening", name="propagation widening",
                score=None,
                rationale="propagation graph empty — no alert pairs to analyze",
                insufficient=True,
            ))
        else:
            probes.append(_crisis_probe(
                kind="propagation_widening", name="propagation widening",
                score=widening, metric_value=widening,
                rationale=(
                    f"weak edges {weak * 100:.0f}% + symmetric pairs {symmetric * 100:.0f}% "
                    f"of top-50 propagation graph — integrity "
                    f"{prop.get('integrity_score', 0):.0f}/100"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="propagation_widening", name="propagation widening",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 4: dependency concentration ────────────────────────────
    try:
        sd = structural_dependencies(db, lookback_days=lookback_days)
        drivers = sd.get("dominant_drivers") or []
        if sd.get("exploratory") or not drivers:
            probes.append(_crisis_probe(
                kind="dependency_concentration", name="dependency concentration",
                score=None,
                rationale=(
                    f"no dominant drivers identified ({sd.get('directional_edge_count', 0)} "
                    f"directional edge(s) in upstream layer)"
                ),
                insufficient=True,
            ))
        else:
            # Universe of symbols touched by dominant drivers or their reach.
            universe: set = set()
            for d in drivers:
                universe.add(d["symbol"])
                universe.update(d.get("reachable_sample", []))
            uni_size = max(1, len(universe))
            top_reach = max(d["reach_size"] for d in drivers)
            share = top_reach / uni_size
            # 0 at share < 0.30; 100 at share ≥ 0.70
            score = max(0.0, min(100.0, (share - 0.30) / 0.40 * 100.0))
            top_driver = max(drivers, key=lambda d: d["reach_size"])
            probes.append(_crisis_probe(
                kind="dependency_concentration", name="dependency concentration",
                score=score, metric_value=share,
                rationale=(
                    f"top driver {top_driver['symbol']} reaches "
                    f"{top_driver['reach_size']}/{uni_size} symbols "
                    f"({share * 100:.0f}% of touched universe)"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="dependency_concentration", name="dependency concentration",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 5: anomaly synchronization (anomaly_memory rate) ──────
    try:
        recent = db.execute(
            text("SELECT COUNT(*) AS c FROM liquidity_anomaly_memory WHERE occurred_at_ms >= :s"),
            {"s": now_ms - 6 * H},
        ).first()
        prior = db.execute(
            text(
                "SELECT COUNT(*) AS c FROM liquidity_anomaly_memory "
                "WHERE occurred_at_ms >= :s AND occurred_at_ms < :e"
            ),
            {"s": now_ms - 12 * H, "e": now_ms - 6 * H},
        ).first()
        rc = int(recent.c or 0) if recent else 0
        pc = int(prior.c or 0) if prior else 0
        if rc == 0 and pc == 0:
            probes.append(_crisis_probe(
                kind="anomaly_synchronization", name="anomaly synchronization",
                score=None,
                rationale="no anomaly_memory writes in last 12h",
                insufficient=True,
            ))
        elif pc == 0:
            # Bootstrap: new anomalies but no baseline to compare against
            probes.append(_crisis_probe(
                kind="anomaly_synchronization", name="anomaly synchronization",
                score=None,
                rationale=f"no prior-6h baseline ({rc} anomalies in recent 6h)",
                insufficient=True,
            ))
        else:
            ratio = rc / pc
            score = max(0.0, min(100.0, (ratio - 1.0) / 2.0 * 100.0))
            probes.append(_crisis_probe(
                kind="anomaly_synchronization", name="anomaly synchronization",
                score=score, metric_value=ratio,
                rationale=(
                    f"{rc} anomaly records in last 6h vs {pc} prior 6h — "
                    f"{ratio:.1f}× acceleration"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="anomaly_synchronization", name="anomaly synchronization",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 6: transition instability ──────────────────────────────
    try:
        tr = market_state_transitions(db, lookback_days=lookback_days)
        if tr.get("exploratory") or tr.get("transition_count", 0) == 0:
            probes.append(_crisis_probe(
                kind="transition_instability", name="state transition instability",
                score=None,
                rationale=(
                    f"insufficient transitions ({tr.get('transition_count', 0)} "
                    f"in {tr.get('snapshot_count', 0)} snapshots)"
                ),
                insufficient=True,
            ))
        else:
            flicker = float(tr.get("flicker_ratio") or 0.0)
            oscillating = len(tr.get("oscillation_periods") or [])
            score = flicker * 60.0 + (40.0 if oscillating else 0.0)
            probes.append(_crisis_probe(
                kind="transition_instability", name="state transition instability",
                score=score, metric_value=flicker,
                rationale=(
                    f"flicker {flicker * 100:.0f}% of {tr.get('transition_count', 0)} "
                    f"transitions, {oscillating} oscillation period(s) detected"
                ),
            ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="transition_instability", name="state transition instability",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Probe 7: stress slope acceleration ───────────────────────────
    try:
        rows = db.execute(
            text(
                """
                SELECT ts_ms, synthesized_stress
                FROM liquidity_intelligence_history
                WHERE ts_ms >= :since AND synthesized_stress IS NOT NULL
                ORDER BY ts_ms ASC
                """
            ),
            {"since": now_ms - 12 * H},
        ).fetchall()
        rs = [(int(r.ts_ms), float(r.synthesized_stress)) for r in rows]
        if len(rs) < 12:
            probes.append(_crisis_probe(
                kind="stress_acceleration", name="stress slope acceleration",
                score=None,
                rationale=f"only {len(rs)} stress samples in 12h — need ≥ 12",
                insufficient=True,
            ))
        else:
            mid_ms = now_ms - 6 * H
            prior = [v for ts, v in rs if ts < mid_ms]
            recent = [v for ts, v in rs if ts >= mid_ms]
            if len(prior) < 5 or len(recent) < 5:
                probes.append(_crisis_probe(
                    kind="stress_acceleration", name="stress slope acceleration",
                    score=None,
                    rationale=f"unbalanced windows (prior {len(prior)}, recent {len(recent)}; need ≥ 5 each)",
                    insufficient=True,
                ))
            else:
                prior_slope = (prior[-1] - prior[0]) / max(1, len(prior) - 1)
                recent_slope = (recent[-1] - recent[0]) / max(1, len(recent) - 1)
                accel = recent_slope - prior_slope
                # Positive accel = stress accelerating up = bad
                score = max(0.0, min(100.0, accel * 10.0)) if accel > 0 else 0.0
                probes.append(_crisis_probe(
                    kind="stress_acceleration", name="stress slope acceleration",
                    score=score, metric_value=accel,
                    rationale=(
                        f"stress slope {prior_slope:+.1f}/tick (prior 6h) → "
                        f"{recent_slope:+.1f}/tick (recent 6h) — acceleration "
                        f"{accel:+.1f}"
                    ),
                ))
    except Exception:  # noqa: BLE001
        probes.append(_crisis_probe(
            kind="stress_acceleration", name="stress slope acceleration",
            score=None, rationale="query failed", insufficient=True,
        ))

    # ── Compose ──────────────────────────────────────────────────────
    contributing = [p for p in probes if p["contributes"]]
    insufficient_count = sum(1 for p in probes if not p["contributes"])
    hot_count = sum(1 for p in probes if p["status"] == "hot")
    elevated_count = sum(1 for p in probes if p["status"] == "elevated")
    calm_count = sum(1 for p in probes if p["status"] == "calm")

    if not contributing:
        # Nothing has enough data — verdict is honestly NONE
        genesis_score = 0.0
        verdict = "INSUFFICIENT"
        confidence = 0.0
    else:
        # Plain mean of contributing probes — explicit and inspectable.
        genesis_score = sum(p["score"] for p in contributing) / len(contributing)
        confidence = len(contributing) / 7.0  # how many probes had data
        # Scarcity cap: if more than half the probes are INSUFFICIENT,
        # verdict is capped at EARLY_DISTORTION regardless of score.
        if insufficient_count > 3:
            verdict = "EARLY_DISTORTION" if genesis_score >= 25 else "CALM"
        elif genesis_score >= 75 and hot_count >= 3:
            verdict = "PRE_CASCADE"
        elif genesis_score >= 50:
            verdict = "ELEVATED_RISK"
        elif genesis_score >= 25:
            verdict = "EARLY_DISTORTION"
        else:
            verdict = "CALM"

    # Summary — adapts to verdict.
    if verdict == "INSUFFICIENT":
        summary = "No probes had enough data to score — system is silent, not safe."
    elif verdict == "CALM":
        summary = (
            f"genesis_score {genesis_score:.0f}/100 — "
            f"no precursor signals materially elevated"
        )
    elif verdict == "EARLY_DISTORTION":
        elevated_names = ", ".join(p["kind"] for p in probes if p["status"] == "elevated")
        summary = (
            f"genesis_score {genesis_score:.0f}/100 — early distortion in: {elevated_names or '(scarcity cap)'}"
        )
    elif verdict == "ELEVATED_RISK":
        hot_names = ", ".join(p["kind"] for p in probes if p["status"] == "hot") or "multiple probes"
        summary = (
            f"genesis_score {genesis_score:.0f}/100 — elevated risk: {hot_names} are firing"
        )
    else:  # PRE_CASCADE
        hot_names = ", ".join(p["kind"] for p in probes if p["status"] == "hot")
        summary = (
            f"genesis_score {genesis_score:.0f}/100 — PRE_CASCADE: "
            f"{hot_count} hot probes ({hot_names})"
        )

    return {
        "fetched_at_ms": now_ms,
        "lookback_days": lookback_days,
        "genesis_score": genesis_score,
        "verdict": verdict,
        "confidence": confidence,
        "probe_count": len(probes),
        "hot_count": hot_count,
        "elevated_count": elevated_count,
        "calm_count": calm_count,
        "insufficient_count": insufficient_count,
        "probes": probes,
        "summary": summary,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Phase 15 #6 — Narrative Causality
# ══════════════════════════════════════════════════════════════════════════
#
# Final layer in Phase 15: compose causal_propagation, structural_dependencies,
# market_state_transitions, and crisis_genesis into a human-readable
# narrative. The narrative is deterministic — built from templates that
# pick phrasing based on which numbers came back from upstream — NOT
# generated by a model. Every sentence is traceable to a specific data
# point.
#
# Phrasing rules followed throughout:
#   * "X tends to precede Y"           (NOT "X causes Y")
#   * "consistent with X influencing Y" (NOT "X influences Y")
#   * "no committed claim of direct influence between A and B"
#   * "lead-lag signature" / "directional signal"  (NOT "causality")
#   * "the data has not yet ruled out coincidence/common-driver"
#
# Every section carries a confidence number AND a missing-data caveat
# when applicable.


def _fmt_duration_short(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _narrate_state(tr: dict) -> dict:
    """Narrate market state transitions."""
    n_snap = tr.get("snapshot_count", 0)
    quality = tr.get("data_quality", "INSUFFICIENT")
    if tr.get("exploratory"):
        text = (
            f"State-transition layer is in exploratory mode "
            f"({n_snap} intelligence snapshots, data_quality={quality}). "
            f"Transitions are tracked but classifications are not committed."
        )
        return {"kind": "state", "title": "Market state", "text": text, "confidence": 0.3}

    current = tr.get("current_state") or "unknown"
    dur_s = (tr.get("current_state_duration_seconds") or 0) // 1000
    dur = _fmt_duration_short(dur_s) if dur_s > 0 else "less than one snapshot"
    transitions = tr.get("transition_count", 0)
    rate = tr.get("transition_rate_per_day", 0.0)
    flicker = tr.get("flicker_ratio", 0.0)
    stability_label = "stable" if flicker < 0.25 else ("noisy" if flicker < 0.50 else "unstable")
    oscillations = len(tr.get("oscillation_periods") or [])

    txt_parts = [
        f"Market is currently in {current} (held for {dur})."
    ]
    if transitions > 0:
        txt_parts.append(
            f"Over the lookback window the engine recorded {transitions} state "
            f"transitions ({rate:.1f}/day, {flicker * 100:.0f}% flicker) — "
            f"transition layer reads {stability_label}."
        )
    else:
        txt_parts.append(
            f"No state transitions recorded over the lookback window — "
            f"state has held throughout."
        )
    if oscillations > 0:
        txt_parts.append(
            f"{oscillations} oscillation period(s) detected (≥3 transitions "
            f"within 1 hour); transitions inside those windows should not be "
            f"interpreted as meaningful regime changes."
        )

    return {
        "kind": "state",
        "title": "Market state",
        "text": " ".join(txt_parts),
        "confidence": 1.0 if quality == "HIGH" else (0.7 if quality == "MEDIUM" else 0.4),
    }


def _narrate_propagation(causal: dict) -> dict:
    """Narrate causal propagation findings."""
    quality = causal.get("data_quality", "INSUFFICIENT")
    edges = causal.get("edges") or []
    counts = causal.get("verdict_counts") or {}
    n_total = len(edges)
    directional = counts.get("DIRECTIONAL", 0)
    common_driven = counts.get("COMMON_DRIVEN", 0)
    coincidence = counts.get("COINCIDENCE", 0)
    under_evidenced = counts.get("UNDER_EVIDENCED", 0)
    ambiguous = counts.get("AMBIGUOUS", 0)
    exploratory = counts.get("EXPLORATORY", 0)

    if exploratory == n_total and n_total > 0:
        text = (
            f"Causal propagation layer is in EXPLORATORY mode (data_quality="
            f"{quality}, {causal.get('total_alerts', 0)} alerts). "
            f"{n_total} candidate edge(s) identified but no causal claims "
            f"are committed — the scarcity gate is preventing any single "
            f"edge from being labeled directional."
        )
        return {"kind": "propagation", "title": "Causal propagation", "text": text, "confidence": 0.2}

    if n_total == 0:
        text = (
            f"No propagation pairs above the count threshold. "
            f"Insufficient alert history across multiple symbols."
        )
        return {"kind": "propagation", "title": "Causal propagation", "text": text, "confidence": 0.0}

    parts: List[str] = []
    if directional > 0:
        # Cite top directional edge
        top = next((e for e in edges if e["verdict"] == "DIRECTIONAL"), None)
        if top:
            parts.append(
                f"{directional} edge(s) survived all four causality tests "
                f"(asymmetric direction, multi-window persistence, no common "
                f"driver, sufficient data) — strongest is {top['from_symbol']}"
                f" tends to precede {top['to_symbol']} (causal_confidence "
                f"{top['causal_confidence'] * 100:.0f}, n={top['count']} vs "
                f"reverse {top['reverse_count']})."
            )
        else:
            parts.append(f"{directional} edge(s) labeled DIRECTIONAL.")
    else:
        parts.append("No edges survived all four causality tests — no directional claims are committed.")

    if common_driven > 0:
        # Find the common drivers
        drivers = {e["common_driver"] for e in edges if e["verdict"] == "COMMON_DRIVEN" and e["common_driver"]}
        if drivers:
            parts.append(
                f"{common_driven} pair(s) labeled COMMON_DRIVEN — most likely "
                f"co-movement around {', '.join(sorted(drivers)[:3])} rather "
                f"than direct influence between the pair members."
            )
        else:
            parts.append(f"{common_driven} pair(s) labeled COMMON_DRIVEN.")

    if coincidence > 0:
        parts.append(
            f"{coincidence} pair(s) labeled COINCIDENCE (bidirectional, "
            f"≥70% mirrored) — these are co-firing, not lead-lag."
        )

    if under_evidenced + ambiguous > 0:
        parts.append(
            f"{under_evidenced + ambiguous} pair(s) flagged UNDER_EVIDENCED "
            f"or AMBIGUOUS — direction not stable across sub-windows or "
            f"asymmetry below the 40% threshold."
        )

    text = " ".join(parts)
    confidence = 1.0 if quality == "HIGH" else (0.7 if quality == "MEDIUM" else 0.3)
    return {"kind": "propagation", "title": "Causal propagation", "text": text, "confidence": confidence}


def _narrate_structural(sd: dict) -> dict:
    """Narrate structural dependency findings."""
    if sd.get("exploratory"):
        text = (
            "No structural dependencies surfaced — upstream causal "
            "propagation layer is in EXPLORATORY mode, so chains, "
            "clusters and dominant drivers cannot be committed yet."
        )
        return {"kind": "structural", "title": "Structural dependencies", "text": text, "confidence": 0.2}

    chains = sd.get("influence_chains") or []
    clusters = sd.get("dependency_clusters") or []
    drivers = sd.get("dominant_drivers") or []
    sync_groups = sd.get("synchronized_groups") or []

    parts: List[str] = []
    if chains:
        top_chain = chains[0]
        parts.append(
            f"{len(chains)} multi-hop influence chain(s) detected. "
            f"Strongest: {' → '.join(top_chain['path'])} "
            f"(min_confidence {top_chain['min_confidence'] * 100:.0f})."
        )
    else:
        parts.append("No multi-hop chains — no two directional edges share a midpoint symbol.")

    if drivers:
        top = drivers[0]
        parts.append(
            f"{top['symbol']} reaches {top['reach_size']} symbol(s) within "
            f"3 hops at avg confidence {top['avg_out_confidence'] * 100:.0f} — "
            f"the largest detected influence footprint."
        )
    else:
        parts.append("No dominant drivers identified.")

    if clusters:
        top_cluster = clusters[0]
        parts.append(
            f"{len(clusters)} co-driver cluster(s): "
            f"{top_cluster['driver']} mediates a group of "
            f"{top_cluster['size']} symbol(s) without detected internal "
            f"causal structure."
        )

    if sync_groups:
        top_grp = sync_groups[0]
        parts.append(
            f"{len(sync_groups)} synchronized stress group(s): largest has "
            f"{top_grp['size']} symbols connected by "
            f"{top_grp['coincidence_edges']} coincidence pair(s) "
            f"(co-firing, not directional)."
        )

    text = " ".join(parts)
    return {"kind": "structural", "title": "Structural dependencies", "text": text, "confidence": 0.7}


def _narrate_genesis(cg: dict) -> dict:
    """Narrate crisis genesis verdict."""
    verdict = cg.get("verdict", "INSUFFICIENT")
    score = cg.get("genesis_score", 0.0)
    confidence_pct = cg.get("confidence", 0.0) * 100
    probes = cg.get("probes") or []
    hot = [p for p in probes if p["status"] == "hot"]
    elevated = [p for p in probes if p["status"] == "elevated"]
    insufficient = [p for p in probes if not p["contributes"]]

    if verdict == "INSUFFICIENT":
        text = (
            "Crisis-genesis layer is silent because most precursor probes "
            "have insufficient data, NOT because the market is calm. "
            f"{len(insufficient)}/{len(probes)} probes could not be scored: "
            f"{', '.join(p['kind'] for p in insufficient)}."
        )
        return {"kind": "genesis", "title": "Crisis genesis", "text": text, "confidence": 0.0}

    parts: List[str] = [
        f"Genesis composite verdict: {verdict} at score {score:.0f}/100 "
        f"(confidence {confidence_pct:.0f}%, "
        f"{len(probes) - len(insufficient)}/{len(probes)} probes contributing)."
    ]

    if verdict == "PRE_CASCADE":
        parts.append(
            f"Pattern is consistent with pre-cascade structural distortion — "
            f"{len(hot)} probe(s) hot: {', '.join(p['kind'] for p in hot)}."
        )
    elif verdict == "ELEVATED_RISK":
        parts.append(
            f"{len(hot)} probe(s) hot ({', '.join(p['kind'] for p in hot)})"
            + (f" and {len(elevated)} elevated ({', '.join(p['kind'] for p in elevated)})" if elevated else "")
            + ". Watch for the next probe to confirm before treating as pre-cascade."
        )
    elif verdict == "EARLY_DISTORTION":
        if elevated:
            parts.append(
                f"Early distortion in: {', '.join(p['kind'] for p in elevated)}. "
                f"Other probes calm or insufficient — could resolve or escalate."
            )
        else:
            parts.append(
                "Score above CALM threshold but no single probe elevated — "
                "composite from low-grade signals."
            )
    else:  # CALM
        parts.append("No precursor probes are materially elevated.")

    if insufficient and verdict not in ("INSUFFICIENT", "PRE_CASCADE"):
        parts.append(
            f"{len(insufficient)} probe(s) report insufficient data "
            f"({', '.join(p['kind'] for p in insufficient)}) — verdict is "
            f"composed from the {len(probes) - len(insufficient)} probes that "
            f"could score."
        )

    text = " ".join(parts)
    return {"kind": "genesis", "title": "Crisis genesis", "text": text, "confidence": confidence_pct / 100.0}


def _narrate_uncertainty(causal: dict, sd: dict, tr: dict, cg: dict) -> dict:
    """Explicit summary of what the system does NOT know."""
    items: List[str] = []
    if causal.get("data_quality") in ("INSUFFICIENT", "LOW"):
        items.append(
            f"causal propagation layer in EXPLORATORY mode "
            f"(data_quality={causal.get('data_quality')}) — no directional claims"
        )
    if tr.get("exploratory"):
        items.append(
            f"transition layer in EXPLORATORY mode "
            f"({tr.get('snapshot_count', 0)} snapshots, "
            f"data_quality={tr.get('data_quality')})"
        )
    insufficient_probes = [p for p in (cg.get("probes") or []) if not p["contributes"]]
    if insufficient_probes:
        items.append(
            f"{len(insufficient_probes)} crisis-genesis probe(s) cannot score: "
            f"{', '.join(p['kind'] for p in insufficient_probes)}"
        )

    if not items:
        text = (
            "All four upstream layers are above the scarcity threshold. "
            "The narrative above is the system's best current interpretation "
            "with no major data gaps."
        )
    else:
        text = "Known gaps: " + "; ".join(items) + "."

    return {
        "kind": "uncertainty",
        "title": "What the system does not know",
        "text": text,
        "confidence": None,
    }


def _build_headline(cg: dict, tr: dict, causal: dict) -> str:
    verdict = cg.get("verdict", "INSUFFICIENT")
    if verdict == "PRE_CASCADE":
        return f"Pre-cascade structural distortion: {cg.get('hot_count', 0)} hot probe(s) firing."
    if verdict == "ELEVATED_RISK":
        return f"Elevated structural risk: {cg.get('hot_count', 0)} hot + {cg.get('elevated_count', 0)} elevated precursor signal(s)."
    if verdict == "EARLY_DISTORTION":
        return f"Early distortion: composite genesis score {cg.get('genesis_score', 0):.0f}/100 (confidence {cg.get('confidence', 0) * 100:.0f}%)."
    if verdict == "INSUFFICIENT":
        return "Insufficient evidence for a narrative — the system is silent because data is missing, not because the market is calm."
    # CALM
    return f"All precursor signals quiet — {tr.get('current_state', 'unknown')} held for {_fmt_duration_short((tr.get('current_state_duration_seconds') or 0) // 1000)}."


def narrative_causality(db: Session, lookback_days: int = 7) -> dict:
    """Compose Phase 15 layers into a deterministic narrative.
    All sentences are template-built — no model calls, every claim
    traceable to a specific upstream number."""
    now_ms = int(time.time() * 1000)

    causal = causal_propagation(db, lookback_days=lookback_days)
    sd = structural_dependencies(db, lookback_days=lookback_days)
    tr = market_state_transitions(db, lookback_days=lookback_days)
    cg = crisis_genesis(db, lookback_days=lookback_days)

    sections = [
        _narrate_state(tr),
        _narrate_propagation(causal),
        _narrate_structural(sd),
        _narrate_genesis(cg),
        _narrate_uncertainty(causal, sd, tr, cg),
    ]
    headline = _build_headline(cg, tr, causal)
    paragraph = "\n\n".join(f"{s['title']}. {s['text']}" for s in sections)

    # Overall confidence = mean of per-section confidences that have one.
    confidences = [s["confidence"] for s in sections if s.get("confidence") is not None]
    overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "fetched_at_ms": now_ms,
        "lookback_days": lookback_days,
        "headline": headline,
        "verdict": cg.get("verdict", "INSUFFICIENT"),
        "overall_confidence": overall_confidence,
        "sections": sections,
        "paragraph": paragraph,
    }


def sanity_audit(db: Session) -> dict:
    """Integrity-monitoring engine for the discovery layer.

    Each check produces an explainable finding with `severity_score`
    (smooth 0–100), categorical severity (info/warn/critical), the
    `metric_value` that triggered it, the thresholds it crossed, and a
    `trend` (NEW / WORSENING / STABILIZING / RECURRING / CHRONIC / TRANSIENT)
    so the UI can show not just "this is bad" but "this is getting worse".

    Categories: validation, propagation, discovery, forecast, regime,
    adaptation, anomaly.
    """
    findings: List[dict] = []
    now_ms = int(time.time() * 1000)
    H24 = 24 * 3600 * 1000
    D7 = 7 * H24

    # ── 1) Validation collapse (existing — preserved as-is, enriched) ─
    sigh = db.execute(
        text(
            """
            SELECT kind,
                   SUM(CASE WHEN validated_outcome = 'followed_through' THEN 1 ELSE 0 END) AS ft,
                   SUM(CASE WHEN validated_outcome = 'noise' THEN 1 ELSE 0 END) AS noise,
                   COUNT(*) AS total
            FROM liquidity_alert_history
            WHERE started_at_ms >= :since
            GROUP BY kind
            """
        ),
        {"since": now_ms - D7},
    ).fetchall()
    for r in sigh:
        ft = int(r.ft or 0); ns = int(r.noise or 0)
        resolved = ft + ns
        if resolved < 25 or ft > 0:
            continue
        # 25 resolved & all noise = INFO, 100 = WARN, 250 = CRITICAL.
        f = _classify_finding(
            kind="validation_collapse",
            category="validation",
            value=float(resolved),
            info_threshold=25, warn_threshold=100, critical_threshold=250,
            detail=f"{r.kind}: {resolved} resolved alerts, all marked noise — validation logic almost certainly mis-thresholded.",
            threshold_unit="resolved alerts",
            trend="CHRONIC",  # by construction, a precision collapse persists until reset
        )
        if f:
            findings.append(f)

    # ── 2) Anomaly inflation with trend (recent 24h vs prior 24h) ─────
    last_24h = db.execute(
        text("SELECT COUNT(*) AS c FROM liquidity_anomaly_memory WHERE occurred_at_ms >= :since"),
        {"since": now_ms - H24},
    ).first()
    prior_24h = db.execute(
        text("SELECT COUNT(*) AS c FROM liquidity_anomaly_memory WHERE occurred_at_ms >= :s AND occurred_at_ms < :e"),
        {"s": now_ms - 2 * H24, "e": now_ms - H24},
    ).first()
    recent = int(last_24h.c or 0) if last_24h else 0
    prior = int(prior_24h.c or 0) if prior_24h else 0
    # Trend: WORSENING if recent ≥ 1.5× prior; STABILIZING if recent ≤ 0.7× prior;
    # CHRONIC if both windows are above WARN; otherwise RECURRING (just present).
    if recent >= 60 and prior >= 60:
        trend = "CHRONIC"
    elif prior > 0 and recent >= prior * 1.5:
        trend = "WORSENING"
    elif prior > 0 and recent <= prior * 0.7:
        trend = "STABILIZING"
    elif prior == 0 and recent > 0:
        trend = "NEW"
    else:
        trend = "RECURRING"
    f = _classify_finding(
        kind="anomaly_inflation",
        category="anomaly",
        value=float(recent),
        info_threshold=60, warn_threshold=120, critical_threshold=300,
        detail=f"{recent} anomaly records in last 24h (prior 24h: {prior}). "
               f"Auto-recorder cooldowns may be too loose.",
        threshold_unit="records/24h",
        trend=trend,
    )
    if f:
        findings.append(f)

    # ── 3) Propagation loops — aggregate across ALL symmetric pairs ───
    # The aggregated form prevents calm-market flooding (35 pairs would
    # otherwise produce 35 findings). Severity scales with how many pairs
    # crossed the threshold, not with any single pair's penalty.
    prop = propagation_graph(db, lookback_days=7)
    sym_pairs = prop.get("all_symmetric_pairs") or []
    # Filter to truly suspect (≥0.70 mirror) for aggregation count; the
    # 0.50–0.70 band is borderline and shouldn't drive severity by itself.
    suspect_pairs = sorted(
        [sp for sp in sym_pairs if sp["symmetry_penalty"] >= 0.70],
        key=lambda sp: -sp["symmetry_penalty"],
    )
    if suspect_pairs:
        top_str = ", ".join(
            f"{sp['a']}↔{sp['b']} ({sp['count_ab']}/{sp['count_ba']}, {sp['symmetry_penalty'] * 100:.0f}%)"
            for sp in suspect_pairs[:3]
        )
        # Trend: any pair with high volume (≥20 on either side) means this
        # loop has been firing repeatedly → RECURRING.
        trend = "RECURRING" if any(
            max(sp["count_ab"], sp["count_ba"]) >= 20 for sp in suspect_pairs
        ) else "NEW"
        f = _classify_finding(
            kind="propagation_loop",
            category="propagation",
            value=float(len(suspect_pairs)),
            info_threshold=3, warn_threshold=10, critical_threshold=25,
            detail=f"{len(suspect_pairs)} symmetric pair(s) with ≥70% mirror — "
                   f"top: {top_str}.",
            threshold_unit="suspect pairs",
            trend=trend,
        )
        if f:
            findings.append(f)

    # ── 4) Propagation instability — graph-level integrity_score drop ─
    integrity = float(prop.get("integrity_score") or 0.0)
    # invert: 100 - integrity → "instability"
    instability = 100.0 - integrity
    f = _classify_finding(
        kind="propagation_instability",
        category="propagation",
        value=instability,
        info_threshold=40, warn_threshold=60, critical_threshold=80,
        detail=f"Propagation integrity {integrity:.0f}/100 (instability {instability:.0f}). "
               f"avg_confidence {(prop.get('integrity_components') or {}).get('avg_confidence', 0) * 100:.0f}%, "
               f"weak_share {(prop.get('integrity_components') or {}).get('weak_share', 0) * 100:.0f}%.",
        threshold_unit="instability index",
        trend="NEW",  # no historical baseline yet
    )
    if f:
        findings.append(f)

    # ── 5) Forecast overshoot (existing, enriched) ────────────────────
    try:
        forecast = intelligence_evolution_forecast(db, horizon_days=7)
        for fc in forecast.get("forecasts") or []:
            hit_boundary = fc["forecast_value"] in (0.0, 100.0)
            if not hit_boundary:
                continue
            f = _classify_finding(
                kind="forecast_overshoot",
                category="forecast",
                value=float(fc["confidence"]),
                info_threshold=30, warn_threshold=60, critical_threshold=85,
                detail=f"{fc['metric']} forecast pinned at {fc['forecast_value']:.0f} "
                       f"at +{fc['forecast_in_days']}d (confidence {fc['confidence']:.0f}, "
                       f"slope_capped={fc.get('slope_capped')}, "
                       f"extrap_capped={fc.get('extrapolation_capped')}).",
                threshold_unit="confidence",
                trend="TRANSIENT",
            )
            if f:
                findings.append(f)
    except Exception:  # noqa: BLE001
        pass

    # ── 6) Pattern explosion (existing, enriched) ─────────────────────
    try:
        # Round to 5-min granularity so the cache key matches across polls.
        pdisc_since = ((now_ms - 14 * H24) // (5 * 60_000)) * (5 * 60_000)
        pdisc = discover_patterns(db, since_ms=pdisc_since, min_support=8)
        n_patterns = len(pdisc.get("patterns") or [])
        f = _classify_finding(
            kind="pattern_explosion",
            category="discovery",
            value=float(n_patterns),
            info_threshold=30, warn_threshold=80, critical_threshold=200,
            detail=f"{n_patterns} patterns at min_support=8 — "
                   f"raise the support threshold or check for bucket-density artifacts.",
            threshold_unit="patterns",
            trend="NEW",
        )
        if f:
            findings.append(f)
    except Exception:  # noqa: BLE001
        pass

    # ── 7) Confidence collapse — average across discovery surfaces ────
    confs: List[float] = []
    try:
        prop_avg = float((prop.get("integrity_components") or {}).get("avg_confidence", 0.0)) * 100.0
        confs.append(prop_avg)
    except Exception:  # noqa: BLE001
        pass
    try:
        forecast = forecast if "forecast" in dir() else intelligence_evolution_forecast(db, horizon_days=7)  # type: ignore[name-defined]
        fc_list = forecast.get("forecasts") or []
        if fc_list:
            confs.append(sum(f["confidence"] for f in fc_list) / len(fc_list))
    except Exception:  # noqa: BLE001
        pass
    try:
        pdisc_avg = (
            sum(p["pattern_confidence"] for p in (pdisc.get("patterns") or []))
            / max(1, len(pdisc.get("patterns") or []))
        ) if pdisc.get("patterns") else None
        if pdisc_avg is not None:
            confs.append(pdisc_avg)
    except Exception:  # noqa: BLE001
        pass
    if confs:
        avg_conf = sum(confs) / len(confs)
        collapse = 100.0 - avg_conf
        f = _classify_finding(
            kind="confidence_collapse",
            category="discovery",
            value=collapse,
            info_threshold=60, warn_threshold=80, critical_threshold=95,
            detail=f"Aggregate discovery confidence {avg_conf:.0f}/100 across "
                   f"{len(confs)} surfaces — interpret outputs as exploratory.",
            threshold_unit="collapse index",
            trend="NEW",
        )
        if f:
            findings.append(f)

    # ── 8) Regime fragmentation spike (intelligence_history) ──────────
    try:
        regime_rows = db.execute(
            text(
                """
                SELECT coordinated_state, ts_ms
                FROM liquidity_intelligence_history
                WHERE ts_ms >= :since AND coordinated_state IS NOT NULL
                """
            ),
            {"since": now_ms - 2 * H24},
        ).fetchall()
        recent_states = set()
        prior_states = set()
        for r in regime_rows:
            (recent_states if int(r.ts_ms) >= now_ms - H24 else prior_states).add(r.coordinated_state)
        # Only fire when we have a real baseline AND recent activity — a
        # ratio against an empty prior is bootstrap noise, not a spike.
        if len(prior_states) >= 1 and len(recent_states) >= 1:
            recent_n = len(recent_states)
            prior_n = len(prior_states)
            ratio = recent_n / prior_n
            spike = ratio * 50.0  # 1× → 50, 2× → 100
            trend = "WORSENING" if ratio > 1.5 else ("STABILIZING" if ratio < 0.7 else "RECURRING")
            f = _classify_finding(
                kind="regime_fragmentation_spike",
                category="regime",
                value=spike,
                info_threshold=70, warn_threshold=90, critical_threshold=120,
                detail=f"{recent_n} distinct coordinated_states in last 24h vs {prior_n} prior — "
                       f"{ratio:.1f}× fragmentation.",
                threshold_unit="fragmentation index",
                trend=trend,
            )
            if f:
                findings.append(f)
    except Exception:  # noqa: BLE001
        pass

    # ── 9) Unstable clustering (existing, enriched) ───────────────────
    try:
        hr = hidden_regimes(db, lookback_days=14, max_clusters=8)
        clusters = hr.get("clusters") or []
        micro = [c for c in clusters if c["size"] <= 3]
        if clusters and micro:
            value = (len(micro) / len(clusters)) * 100.0
            f = _classify_finding(
                kind="unstable_clustering",
                category="regime",
                value=value,
                info_threshold=50, warn_threshold=75, critical_threshold=95,
                detail=f"{len(micro)}/{len(clusters)} hidden-regime clusters with size ≤3 — "
                       f"clusters aren't stable; more snapshots needed.",
                threshold_unit="% micro-clusters",
                trend="NEW",
            )
            if f:
                findings.append(f)
    except Exception:  # noqa: BLE001
        pass

    # ── 10) Adaptation oscillation — conflict in current snapshot ─────
    try:
        adapt = adaptation_recommendations(db)
        recs = adapt.get("recommendations") or []
        # If the same target appears with both STRENGTHEN and WEAKEN
        # actions in the same snapshot, the recommender is undecided —
        # which is itself a sanity signal.
        per_target: Dict[str, set] = defaultdict(set)
        for r in recs:
            per_target[r["target"]].add(r["action"])
        conflicts = [
            t for t, acts in per_target.items()
            if {"STRENGTHEN", "WEAKEN"}.issubset(acts)
            or {"TIGHTEN_THRESHOLD", "LOOSEN_THRESHOLD"}.issubset(acts)
        ]
        if conflicts:
            f = _classify_finding(
                kind="adaptation_oscillation",
                category="adaptation",
                value=float(len(conflicts)),
                info_threshold=1, warn_threshold=3, critical_threshold=6,
                detail=f"{len(conflicts)} target(s) with conflicting actions ({', '.join(conflicts[:5])}) — "
                       f"recommender is oscillating, suggests unstable input signals.",
                threshold_unit="conflicting targets",
                trend="NEW",  # needs sanity_history table for cross-snapshot oscillation
            )
            if f:
                findings.append(f)
    except Exception:  # noqa: BLE001
        pass

    # ── Aggregate ─────────────────────────────────────────────────────
    severity_rank = {"critical": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 3), -f["severity_score"]))

    has_critical = any(f["severity"] == "critical" for f in findings)
    has_warn = any(f["severity"] == "warn" for f in findings)
    if has_critical:
        overall = "CRITICAL"
    elif has_warn:
        overall = "WARN"
    elif findings:
        overall = "INFO"
    else:
        overall = "CLEAN"

    # Quantified overall — max severity_score across findings, useful for
    # graphing the sanity layer's own health over time once we snapshot.
    overall_score = max((f["severity_score"] for f in findings), default=0.0)

    return {
        "fetched_at_ms": now_ms,
        "overall_state": overall,
        "overall_score": overall_score,
        "findings": findings,
        "check_count": 10,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Heavy-function TTL wrapping
# ══════════════════════════════════════════════════════════════════════════
#
# Applied AFTER all definitions so internal callers (e.g. sanity_audit's
# calls to propagation_graph / discover_patterns / hidden_regimes /
# intelligence_evolution_forecast) automatically hit the cache too.
# Function lookups are resolved at call time, not def time, so rebinding
# the names at module level transparently redirects every caller.

intelligence_synthesis = _ttl_cached(300.0)(intelligence_synthesis)  # 30s baseline → ~50ms cached
propagation_graph = _ttl_cached(300.0)(propagation_graph)
discover_patterns = _ttl_cached(300.0)(discover_patterns)
intelligence_evolution_forecast = _ttl_cached(300.0)(intelligence_evolution_forecast)
hidden_regimes = _ttl_cached(300.0)(hidden_regimes)
multi_horizon = _ttl_cached(300.0)(multi_horizon)
adaptation_recommendations = _ttl_cached(300.0)(adaptation_recommendations)
evolutionary_behavior = _ttl_cached(300.0)(evolutionary_behavior)
# sanity_audit caches itself too — it composes 5 of the above + several
# SQL reads, and its UI polling is 60s. 30s TTL gives the operator a
# fresh view on the next polling cycle without paying the full cost
# every minute.
sanity_audit = _ttl_cached(30.0)(sanity_audit)
causal_propagation = _ttl_cached(300.0)(causal_propagation)
structural_dependencies = _ttl_cached(300.0)(structural_dependencies)
market_state_transitions = _ttl_cached(300.0)(market_state_transitions)
crisis_genesis = _ttl_cached(120.0)(crisis_genesis)  # tighter TTL — meant for early-warning
narrative_causality = _ttl_cached(120.0)(narrative_causality)
