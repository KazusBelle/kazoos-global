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
