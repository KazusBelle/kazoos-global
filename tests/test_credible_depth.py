"""Credible Depth per-side decomposition + observable imbalance.

These exercise the survivorship-filtered per-side outputs added on top of
the existing combined `credible_depth_usd`. Focus areas mirror the
governance acceptance criteria: UNKNOWN propagation, the observed-zero vs
UNKNOWN distinction, the >=400ms survivorship filter, delta sign, and
replay determinism. No DB / IO — pure reads off SymbolState.
"""

from __future__ import annotations

from kazus_logic.liquidity.realtime.metrics import (
    CREDIBLE_MIN_AGE_MS,
    credible_ask_depth_usd,
    credible_bid_depth_usd,
    credible_depth_delta_usd,
    credible_depth_sides,
    credible_depth_usd,
)
from kazus_logic.liquidity.realtime.orderbook import SymbolState

NOW = 10_000_000
OLD = NOW - 1_000          # age 1000ms → survives the 400ms filter
FRESH = NOW - 100          # age 100ms  → filtered out


def _state(bids=None, asks=None, best_bid=None, best_ask=None) -> SymbolState:
    s = SymbolState(symbol="TEST")
    s.bids = dict(bids or {})
    s.asks = dict(asks or {})
    s.best_bid = best_bid
    s.best_ask = best_ask
    return s


# A book centred on mid≈100, all levels aged past the survivorship floor and
# inside the ±0.5% band ([99.5, 100.5]).
def _live_book() -> SymbolState:
    return _state(
        bids={99.6: (10.0, OLD), 99.8: (5.0, OLD)},
        asks={100.2: (3.0, OLD), 100.4: (2.0, OLD)},
    )


def test_unknown_propagates_when_mid_unavailable():
    # No book, no bookTicker → mid is None → every output is UNKNOWN (None),
    # never a fabricated 0.
    s = _state()
    assert credible_depth_sides(s, NOW) == (None, None)
    assert credible_depth_usd(s, NOW) is None
    assert credible_bid_depth_usd(s, NOW) is None
    assert credible_ask_depth_usd(s, NOW) is None
    assert credible_depth_delta_usd(s, NOW) is None


def test_observed_zero_is_distinct_from_unknown():
    # Mid is known (from bookTicker) but the depth20 dicts are empty: this is
    # an *observed* absence of persistent visible liquidity → 0.0, NOT None.
    s = _state(best_bid=99.9, best_ask=100.1)
    bid, ask = credible_depth_sides(s, NOW)
    assert bid == 0.0 and ask == 0.0
    assert credible_depth_usd(s, NOW) == 0.0
    assert credible_depth_delta_usd(s, NOW) == 0.0


def test_per_side_sums_and_combined_identity():
    s = _live_book()
    bid, ask = credible_depth_sides(s, NOW)
    assert bid == 99.6 * 10.0 + 99.8 * 5.0
    assert ask == 100.2 * 3.0 + 100.4 * 2.0
    # Combined output must equal the sum of the two sides exactly — they are
    # derived from one walk and cannot drift apart.
    assert credible_depth_usd(s, NOW) == bid + ask
    assert credible_bid_depth_usd(s, NOW) == bid
    assert credible_ask_depth_usd(s, NOW) == ask


def test_delta_sign_and_value():
    s = _live_book()
    bid, ask = credible_depth_sides(s, NOW)
    # bid-heavy book → positive observable imbalance toward bids.
    assert credible_depth_delta_usd(s, NOW) == bid - ask > 0


def test_survivorship_filter_excludes_fresh_liquidity():
    base = _live_book()
    base_bid, _ = credible_depth_sides(base, NOW)
    # Add a large but fresh bid level (age < 400ms). It must contribute zero.
    s = _live_book()
    s.bids[99.7] = (100.0, FRESH)
    bid, _ = credible_depth_sides(s, NOW)
    assert bid == base_bid
    # And a level aged exactly to the floor *does* count (>= boundary).
    s2 = _live_book()
    s2.bids[99.7] = (100.0, NOW - CREDIBLE_MIN_AGE_MS)
    bid2, _ = credible_depth_sides(s2, NOW)
    assert bid2 == base_bid + 99.7 * 100.0


def test_band_excludes_far_levels():
    s = _live_book()
    s.bids[99.0] = (1000.0, OLD)   # below lo=99.5 → out of band
    bid, _ = credible_depth_sides(s, NOW)
    assert bid == 99.6 * 10.0 + 99.8 * 5.0  # far level excluded


def test_replay_deterministic():
    s = _live_book()
    first = (
        credible_depth_sides(s, NOW),
        credible_depth_usd(s, NOW),
        credible_depth_delta_usd(s, NOW),
    )
    second = (
        credible_depth_sides(s, NOW),
        credible_depth_usd(s, NOW),
        credible_depth_delta_usd(s, NOW),
    )
    assert first == second
