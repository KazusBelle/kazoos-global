"""PHASE 3B Observation Report — pure statistical core (read-only analysis).

DB-free: exercises percentile math, divergence summary, and the refusal-first
threshold-discovery decision across all three branches. No runtime, no verdicts.
"""

from __future__ import annotations

import random

from kazus_logic.liquidity import exec_validation_report as r


def test_percentile_linear():
    s = [0, 1, 2, 3, 4]
    assert r.percentile(s, 0) == 0
    assert r.percentile(s, 100) == 4
    assert r.percentile(s, 50) == 2
    assert r.percentile(s, 25) == 1
    assert r.percentile([], 50) is None
    assert r.percentile([7], 90) == 7


def test_divergence_summary_signed_and_abs():
    vals = [-3.0, -1.0, 1.0, 1.0, 2.0]
    out = r.divergence_summary(vals)
    assert out["n"] == 5
    assert abs(out["signed_mean"] - 0.0) < 1e-9          # bias ~0
    # abs percentiles computed on |v| = [1,1,1,2,3]
    assert out["abs_pctls"][50] == 1.0
    assert out["abs_mean"] == (1 + 1 + 1 + 2 + 3) / 5


def test_summary_empty():
    assert r.divergence_summary([])["n"] == 0


# ── threshold discovery: all three decision branches ───────────────────────

def test_insufficient_data():
    out = r.discover_threshold([0.1, 0.2, 0.3], min_n=200)
    assert out["decision"] == r.INSUFFICIENT_DATA
    assert out["n"] == 3 and out["min_n"] == 200


def test_no_defensible_threshold_on_monotone_decay():
    # Exponential-ish decaying |divergence| (the expected real-world shape):
    # monotone-decaying histogram → no interior antimode → refuse.
    random.seed(1)
    vals = [random.expovariate(2.0) for _ in range(5000)]
    out = r.discover_threshold(vals)
    assert out["decision"] == r.NO_DEFENSIBLE_THRESHOLD
    assert "p90" in out["reference"] and "p95" in out["reference"]


def test_threshold_recommended_on_clear_bimodal():
    # Two well-separated clusters (a real structural break) → antimode found.
    random.seed(2)
    lo = [random.gauss(1.0, 0.2) for _ in range(2500)]
    hi = [random.gauss(8.0, 0.4) for _ in range(2500)]
    vals = [abs(x) for x in lo + hi]
    out = r.discover_threshold(vals)
    assert out["decision"] == r.THRESHOLD_RECOMMENDED
    # antimode should fall in the empty gap between the two clusters (~2..7)
    assert 2.0 < out["candidate_band_bps"] < 7.0


def test_degenerate_all_zero():
    out = r.discover_threshold([0.0] * 1000)
    assert out["decision"] == r.NO_DEFENSIBLE_THRESHOLD


def test_decision_labels_are_method_level_not_per_burst():
    # Guard: the report's vocabulary is method decisions, NOT VALIDATED/DIVERGENT.
    for label in (r.THRESHOLD_RECOMMENDED, r.NO_DEFENSIBLE_THRESHOLD, r.INSUFFICIENT_DATA):
        assert label not in ("VALIDATED", "DIVERGENT")
