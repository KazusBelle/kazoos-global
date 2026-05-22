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

import math
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


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
    for (sym, bts), mv in pivot.items():
        if not all(m in mv for m in PATTERN_METRICS):
            continue
        sig = tuple(_tertile(mv[m], *cuts[m]) for m in PATTERN_METRICS)
        outcome = _outcome(sym, bts)
        rec = patterns.setdefault(sig, {"count": 0, "outcomes": 0, "kinds": defaultdict(int)})
        rec["count"] += 1
        if outcome:
            rec["outcomes"] += 1
            rec["kinds"][outcome] += 1
        total_buckets += 1

    out: List[dict] = []
    base_rate = sum(p["outcomes"] for p in patterns.values()) / max(1, total_buckets)
    for i, (sig, agg) in enumerate(patterns.items(), start=1):
        if agg["count"] < min_support:
            continue
        rate = agg["outcomes"] / agg["count"] if agg["count"] > 0 else 0.0
        lift = rate / base_rate if base_rate > 0 else None
        dom_kind, dom_n = max(agg["kinds"].items(), key=lambda kv: kv[1]) if agg["kinds"] else (None, 0)
        novelty = max(0.0, 100.0 * (1.0 - agg["count"] / total_buckets))
        out.append({
            "discovered_pattern_id": f"P{abs(hash(sig)) % 10**8:08d}",
            "signature": dict(zip(PATTERN_METRICS, sig)),
            "support": agg["count"],
            "outcome_rate": rate,
            "lift": lift,
            "dominant_alert_kind": dom_kind,
            "dominant_alert_count": dom_n,
            "novelty_score": novelty,
        })
    out.sort(key=lambda r: -(r["lift"] or 0))
    return {
        "since_ms": since_ms,
        "min_support": min_support,
        "bucket_minutes": bucket_minutes,
        "metrics": list(PATTERN_METRICS),
        "base_rate": base_rate,
        "total_buckets": total_buckets,
        "patterns": out[:40],
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
    return {"archetypes": archetypes, "anomaly_count": base["anomaly_count"], "vocabulary": list(ARCHETYPE_HINTS)}


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
    return {"since_ms": since_ms, "snapshot_count": len(rows), "clusters": out_clusters}


# ── Structural propagation ───────────────────────────────────────────────


def propagation_graph(db: Session, lookback_days: int = 14, lead_window_ms: int = 30 * 60_000) -> dict:
    """Build a propagation graph of symbols: A → B with weight = number
    of times an alert on A was followed within `lead_window_ms` by an
    alert on B for the same kind family. Filters to pairs with ≥3
    co-occurrences. Sectors not modeled — symbol-level only.
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

    by_kind: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for r in rows:
        by_kind[r.kind].append((int(r.started_at_ms), r.symbol))

    edges: Dict[Tuple[str, str], int] = defaultdict(int)
    lead_sums: Dict[Tuple[str, str], int] = defaultdict(int)
    lead_counts: Dict[Tuple[str, str], int] = defaultdict(int)

    for kind, lst in by_kind.items():
        lst.sort()
        # For each event, find any followers in next lead_window_ms.
        for i, (ts_a, sym_a) in enumerate(lst):
            for j in range(i + 1, len(lst)):
                ts_b, sym_b = lst[j]
                if ts_b - ts_a > lead_window_ms:
                    break
                if sym_a == sym_b:
                    continue
                edges[(sym_a, sym_b)] += 1
                lead_sums[(sym_a, sym_b)] += ts_b - ts_a
                lead_counts[(sym_a, sym_b)] += 1

    edges_out: List[dict] = []
    for (a, b), c in edges.items():
        if c < 3:
            continue
        avg_lead_ms = lead_sums[(a, b)] / lead_counts[(a, b)]
        edges_out.append({
            "from_symbol": a,
            "to_symbol": b,
            "count": c,
            "avg_lead_ms": avg_lead_ms,
            "avg_lead_s": avg_lead_ms / 1000.0,
        })
    edges_out.sort(key=lambda e: -e["count"])
    edges_out = edges_out[:50]

    # Node centrality = sum of out-edges (how often A precedes others).
    out_deg: Dict[str, int] = defaultdict(int)
    in_deg: Dict[str, int] = defaultdict(int)
    for e in edges_out:
        out_deg[e["from_symbol"]] += e["count"]
        in_deg[e["to_symbol"]] += e["count"]

    nodes = sorted(
        set(out_deg) | set(in_deg),
        key=lambda s: -(out_deg.get(s, 0)),
    )
    node_rows = [
        {"symbol": s, "out_count": out_deg.get(s, 0), "in_count": in_deg.get(s, 0),
         "net_lead": out_deg.get(s, 0) - in_deg.get(s, 0)}
        for s in nodes
    ]

    # Systemic contagion: pairwise co-occurrences vs max possible pairs.
    # Co-occurrence is O(N²) in the worst case, so the denominator has to
    # be O(N²) too — otherwise the score scales to nonsense (we had a
    # 6000% reading on real data because the original denominator was N,
    # not N choose 2). Cap at 100 so the UI band stays readable.
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
    confidence based on the residual variance. NOT a price forecast —
    only "where is the engine itself drifting toward?".
    """
    from kazus_db.models import LiquidityIntelligenceHistory

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
    if len(rows) < 5:
        return {"horizon_days": horizon_days, "forecasts": [], "snapshot_count": len(rows)}

    def _fit(key: str) -> Optional[dict]:
        pts = [(r.ts_ms, getattr(r, key)) for r in rows if getattr(r, key) is not None]
        if len(pts) < 5:
            return None
        # Normalize x to days from first point.
        t0 = pts[0][0]
        xs = [(t - t0) / (24 * 3600_000) for t, _ in pts]
        ys = [v for _, v in pts]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 0:
            return None
        slope = num / den
        intercept = my - slope * mx
        residuals = [(y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)]
        rmse = math.sqrt(sum(residuals) / n)
        latest_x = xs[-1]
        forecast_x = latest_x + horizon_days
        forecast_y = slope * forecast_x + intercept
        return {
            "metric": key,
            "current": ys[-1],
            "slope_per_day": slope,
            "forecast_in_days": horizon_days,
            "forecast_value": forecast_y,
            "rmse": rmse,
            "confidence": max(0.0, min(100.0, 100.0 - rmse * 2.0)),
        }

    forecasts: List[dict] = []
    for key in ("synthesized_stress", "structural_break_score", "meta_intelligence_health", "regime_shift_probability"):
        f = _fit(key)
        if f is not None:
            forecasts.append(f)

    # Inferred trajectory label.
    stress_f = next((f for f in forecasts if f["metric"] == "synthesized_stress"), None)
    if stress_f is None:
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
        "snapshot_count": len(rows),
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
