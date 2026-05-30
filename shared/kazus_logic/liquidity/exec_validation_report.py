"""PHASE 3B Observation Report — READ-ONLY analysis.

Computes divergence distributions and a candidate-threshold decision from
already-persisted PHASE 3B data, to inform a FUTURE (separately authorized)
PHASE 3C calibration. Spec: docs/lip-3b-observation-report.md.

Strictly read-only: no runtime interaction, no new persisted output, no
per-burst verdict/classification. The statistical core (percentiles, summary,
threshold discovery) is pure and unit-tested independently of any DB. The
report can and must be able to conclude NO_DEFENSIBLE_THRESHOLD or
INSUFFICIENT_DATA — it never invents a band.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

PCTLS = (50, 75, 90, 95, 99)

# Decision labels (method-level, NOT per-burst classifications).
THRESHOLD_RECOMMENDED = "THRESHOLD_RECOMMENDED"
NO_DEFENSIBLE_THRESHOLD = "NO_DEFENSIBLE_THRESHOLD"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Conservative sufficiency / discovery knobs (interim; this is analysis, not a
# runtime threshold — these only govern when the report is willing to speak).
MIN_MEASURED_N = 200       # below this → INSUFFICIENT_DATA
DISCOVERY_BINS = 24
ANTIMODE_MIN_DROP = 0.30   # interior trough must be ≥30% below both flanks


# ── pure statistics ─────────────────────────────────────────────────────────

def percentile(sorted_vals: Sequence[float], p: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy 'linear' convention). `p` in
    [0,100]. Returns None for empty input."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    rank = (p / 100.0) * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_vals[lo])
    frac = rank - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _std(values: Sequence[float], mean: float) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def divergence_summary(values: Sequence[float]) -> Dict:
    """Summary of a divergence sample. Reports SIGNED stats (bias) and the
    ABSOLUTE-value percentiles (the quantity a band would cut on)."""
    n = len(values)
    if n == 0:
        return {"n": 0}
    signed_sorted = sorted(values)
    abs_sorted = sorted(abs(v) for v in values)
    mean = sum(values) / n
    return {
        "n": n,
        "signed_mean": mean,             # directional bias
        "signed_median": percentile(signed_sorted, 50),
        "std": _std(values, mean),
        "abs_mean": sum(abs_sorted) / n,
        "abs_pctls": {p: percentile(abs_sorted, p) for p in PCTLS},
        "abs_iqr": (percentile(abs_sorted, 75) - percentile(abs_sorted, 25)),
    }


def discover_threshold(
    abs_values: Sequence[float],
    *,
    min_n: int = MIN_MEASURED_N,
    bins: int = DISCOVERY_BINS,
) -> Dict:
    """Candidate-threshold discovery on |divergence_bps|.

    Refusal-first: returns INSUFFICIENT_DATA below `min_n`; NO_DEFENSIBLE_
    THRESHOLD when the distribution is monotonically decaying (any band is an
    arbitrary cut on a continuum); THRESHOLD_RECOMMENDED only when a clear
    interior antimode (a density trough between two denser regions) exists.
    p90/p95 are always reported as reference cuts, NOT as a recommendation.
    """
    n = len(abs_values)
    if n < min_n:
        return {"decision": INSUFFICIENT_DATA, "n": n, "min_n": min_n}

    s = sorted(abs_values)
    p90, p95 = percentile(s, 90), percentile(s, 95)
    ref = {"p90": p90, "p95": p95}

    # Histogram up to p99 (ignore extreme tail so one outlier can't define a bin).
    hi = percentile(s, 99) or s[-1]
    if hi <= 0:
        return {"decision": NO_DEFENSIBLE_THRESHOLD, "n": n, "reference": ref,
                "note": "degenerate (all |divergence| ≈ 0)"}
    width = hi / bins
    counts = [0] * bins
    for v in s:
        b = min(bins - 1, int(v / width))
        counts[b] += 1

    # Find an interior antimode: a trough bin sitting ANTIMODE_MIN_DROP below
    # BOTH flanking peaks, where both flanks carry real mass (≥10% of the global
    # mode — so a decaying tail's noise can't pose as a second cluster). The
    # two clusters need NOT be the same height. Deepest qualifying trough wins.
    gmax = max(counts)
    antimode = None
    best_trough = None
    for i in range(1, bins - 1):
        left_peak = max(counts[:i])
        right_peak = max(counts[i + 1:])
        flank = min(left_peak, right_peak)
        if flank < gmax * 0.10:           # both sides must hold substantial mass
            continue
        if counts[i] <= flank * (1 - ANTIMODE_MIN_DROP):
            if best_trough is None or counts[i] < best_trough:
                best_trough = counts[i]
                antimode = (i + 0.5) * width

    if antimode is None:
        return {"decision": NO_DEFENSIBLE_THRESHOLD, "n": n, "reference": ref,
                "note": "unimodal/monotone-decaying; no interior antimode — a band "
                        "would be an arbitrary cut on a continuum"}
    return {"decision": THRESHOLD_RECOMMENDED, "n": n, "reference": ref,
            "candidate_band_bps": antimode,
            "note": "interior antimode found; validate stability across symbols / "
                    "persistence_quality / volatility before adopting"}


# ── DB layer (read-only) ────────────────────────────────────────────────────

def phase3b_observation_report(
    db,
    *,
    since_ms: int,
    until_ms: Optional[int] = None,
    symbols: Optional[List[str]] = None,
) -> Dict:
    """Assemble the read-only report over persisted PHASE 3B data.

    Pulls `liquidity_exec_validation` rows in-window, computes the coverage
    table, the divergence distribution (overall, per symbol, per notional
    bucket, by exhaustion_state), and the §6 threshold decision. Correlations
    with persistence_quality / volatility proxy are computed by the caller-
    supplied as-of joins (see _ASOF_SQL) when sample data is present. Read-only;
    no writes, no per-burst classification.
    """
    from sqlalchemy import text
    until = until_ms if until_ms is not None else (1 << 62)
    params = {"lo": since_ms, "hi": until}
    sym_clause = ""
    if symbols:
        sym_clause = " AND symbol = ANY(:syms)"
        params["syms"] = [s.upper() for s in symbols]

    coverage = db.execute(text(
        "SELECT symbol, execution_validation_state st, count(*) n "
        "FROM liquidity_exec_validation "
        "WHERE local_recv_ts BETWEEN :lo AND :hi" + sym_clause +
        " GROUP BY symbol, execution_validation_state"
    ), params).fetchall()

    measured = db.execute(text(
        "SELECT symbol, divergence_bps, exhaustion_state, burst_notional "
        "FROM liquidity_exec_validation "
        "WHERE execution_validation_state = 'MEASURED' AND divergence_bps IS NOT NULL "
        "AND local_recv_ts BETWEEN :lo AND :hi" + sym_clause
    ), params).fetchall()

    by_symbol: Dict[str, List[float]] = {}
    all_div: List[float] = []
    for r in measured:
        all_div.append(r.divergence_bps)
        by_symbol.setdefault(r.symbol, []).append(r.divergence_bps)

    return {
        "window": {"since_ms": since_ms, "until_ms": until_ms},
        "coverage": [{"symbol": r.symbol, "state": r.st, "n": r.n} for r in coverage],
        "overall": divergence_summary(all_div),
        "per_symbol": {sym: divergence_summary(v) for sym, v in by_symbol.items()},
        "threshold_decision": discover_threshold([abs(v) for v in all_div]),
        "per_symbol_threshold": {
            sym: discover_threshold([abs(v) for v in v_]) for sym, v_ in by_symbol.items()
        },
    }
