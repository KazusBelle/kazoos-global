"""
Integration tests for the worker's event-driven alert flow.

Covered:
  - _load_sent_ids accepts None / "[]" / "[\"a\"]" / garbage
  - _collect_new_events: brand-new event is returned, dedupes against
    sent_event_ids, clears state on OTE exit, ignores tfs the snapshot
    didn't compute (e.g. H1-M5 for non-BTC/ETH/SOL).
  - _dispatch_setup_alert: persists event_id only after Telegram succeeds;
    failed Telegram leaves sent_event_ids untouched.
  - _persist_setup_states / _load_prev_states roundtrip.

Uses an in-memory SQLite engine and monkeypatches `worker.app.runner.SessionLocal`
plus the telegram_alerts.send_setup_alert call so no photos are rendered and
no real network calls are made.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from typing import Dict, List, Tuple

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "y")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER_APP = os.path.join(_ROOT, "worker", "app")


def _load_worker_runner():
    """Import worker/app/runner.py as `worker_runner` (with its sibling
    modules — db, settings, telegram, chart_renderer, telegram_alerts —
    also loaded under worker_app.* prefixes so relative imports resolve).

    chart_renderer pulls in playwright at import time; tests therefore
    require the worker container's pip env (sqlalchemy + playwright)."""
    if "worker_runner" in sys.modules:
        return sys.modules["worker_runner"]

    pkg_spec = importlib.util.spec_from_file_location(
        "worker_app",
        os.path.join(_WORKER_APP, "__init__.py"),
        submodule_search_locations=[_WORKER_APP],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["worker_app"] = pkg
    pkg_spec.loader.exec_module(pkg)

    for name in ("settings", "db", "telegram", "chart_renderer", "telegram_alerts"):
        spec = importlib.util.spec_from_file_location(
            f"worker_app.{name}", os.path.join(_WORKER_APP, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"worker_app.{name}"] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        "worker_app.runner", os.path.join(_WORKER_APP, "runner.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worker_app.runner"] = mod
    sys.modules["worker_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runner_with_sqlite(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kazus_db.models import Base

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    runner_mod = _load_worker_runner()
    monkeypatch.setattr(runner_mod, "SessionLocal", SessionLocal)

    yield runner_mod, SessionLocal


def _make_snap(*, events: Dict[str, list] | None = None,
               states: Dict[str, "SetupState"] | None = None,
               in_ote_d1: bool = True, in_ote_h1: bool = True,
               symbol: str = "TESTUSDT"):
    from kazus_logic.compute import (
        ALERT_TF_D1, ALERT_TF_H1, SymbolSnapshot,
    )
    from kazus_logic.engine import ZoneResult
    from kazus_logic.setup import SetupState

    def _zr(in_ote: bool) -> ZoneResult:
        return ZoneResult(
            zone="ote" if in_ote else "discount",
            in_ote=in_ote,
            setup="yes" if in_ote else "no",
            retracement=0.7,
            direction="bullish",
            fib_low=0.0, fib_high=1.0,
            ote_low_price=100.0, ote_high_price=110.0,
        )

    events = events or {}
    states = states or {ALERT_TF_D1: SetupState(), ALERT_TF_H1: SetupState()}

    return SymbolSnapshot(
        symbol=symbol,
        price=105.0,
        global_result=_zr(in_ote_d1),
        local_result=_zr(in_ote_h1),
        global_trend="up",
        local_trend="up",
        confirmation_bars={ALERT_TF_D1: [], ALERT_TF_H1: []},
        htf_bars={ALERT_TF_D1: [], ALERT_TF_H1: []},
        setup_states=states,
        setup_events=events,
    )


def _make_event(kind: str, event_id: str, fvg_kind: str = "bearish"):
    from kazus_logic.setup import Fvg, SetupEvent
    return SetupEvent(
        kind=kind,
        event_id=event_id,
        trigger_ts=3000,
        fvg=Fvg(formed_at_idx=2, formed_at_ts=2000, top=107.0, bottom=105.0, kind=fvg_kind),
        swing_low=103.0,
    )


def test_load_sent_ids_handles_garbage():
    runner_mod = _load_worker_runner()
    _load_sent_ids = runner_mod._load_sent_ids
    assert _load_sent_ids(None) == set()
    assert _load_sent_ids("") == set()
    assert _load_sent_ids("[]") == set()
    assert _load_sent_ids('["a","b"]') == {"a", "b"}
    assert _load_sent_ids("not-json") == set()
    assert _load_sent_ids('{"x":1}') == set()


def test_collect_new_events_returns_unseen_only(runner_with_sqlite):
    runner_mod, SessionLocal = runner_with_sqlite
    snap = _make_snap(events={"H1": [_make_event("INV", "INV:TESTUSDT:H1:3000")]})
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert [(tf, e.event_id) for tf, e in pending] == [("H1", "INV:TESTUSDT:H1:3000")]


def test_collect_new_events_skips_already_sent(runner_with_sqlite):
    import json
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    with SessionLocal() as db:
        db.add(AlertState(
            symbol="TESTUSDT", timeframe="H1", in_ote=True,
            sent_event_ids=json.dumps(["INV:TESTUSDT:H1:3000"]),
        ))
        db.commit()

    snap = _make_snap(events={"H1": [_make_event("INV", "INV:TESTUSDT:H1:3000")]})
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert pending == []


def test_collect_new_events_ignores_h1_m5_when_not_in_snapshot(runner_with_sqlite):
    """Non-BTC/ETH/SOL symbols don't produce H1-M5 events. The collector
    should silently skip the tf rather than raising."""
    runner_mod, SessionLocal = runner_with_sqlite
    # snap has only D1/H1 events; H1-M5 missing from setup_events dict.
    snap = _make_snap(events={"H1": [_make_event("INV", "INV:TESTUSDT:H1:3000")]})
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"D1", "H1", "H1-M5"})
        db.commit()
    assert [(tf, e.event_id) for tf, e in pending] == [("H1", "INV:TESTUSDT:H1:3000")]


def test_ote_exit_clears_sent_event_ids(runner_with_sqlite):
    import json
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    with SessionLocal() as db:
        db.add(AlertState(
            symbol="TESTUSDT", timeframe="H1", in_ote=True,
            sent_event_ids=json.dumps(["INV:TESTUSDT:H1:3000"]),
        ))
        db.commit()

    snap = _make_snap(in_ote_h1=False, events={"H1": []})
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert pending == []

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").one()
        assert row.in_ote is False
        assert row.sent_event_ids == "[]"


def test_dispatch_setup_alert_persists_event_id_after_send(runner_with_sqlite, monkeypatch):
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    sent_calls: List[Tuple[str, str, str]] = []

    async def fake_send(settings, renderer, snap, tf, event):
        sent_calls.append((snap.symbol, tf, event.event_id))
        return True, f"caption for {event.event_id}"

    monkeypatch.setattr(runner_mod, "send_setup_alert", fake_send)

    ev = _make_event("INV", "INV:TESTUSDT:H1:3000")
    snap = _make_snap(events={"H1": [ev]})

    class FakeSettings:
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    asyncio.run(runner_mod._dispatch_setup_alert(FakeSettings(), None, snap, "H1", ev))

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").one()
        assert "INV:TESTUSDT:H1:3000" in (row.sent_event_ids or "")
    assert sent_calls, "telegram_alerts.send_setup_alert should have been called"


def test_dispatch_setup_alert_does_not_persist_when_send_fails(runner_with_sqlite, monkeypatch):
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    async def fake_send_fail(settings, renderer, snap, tf, event):
        return False, "caption"

    monkeypatch.setattr(runner_mod, "send_setup_alert", fake_send_fail)

    ev = _make_event("INV", "INV:TESTUSDT:H1:3000")
    snap = _make_snap(events={"H1": [ev]})

    class FakeSettings:
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    asyncio.run(runner_mod._dispatch_setup_alert(FakeSettings(), None, snap, "H1", ev))

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").first()
        if row is not None:
            assert "INV:TESTUSDT:H1:3000" not in (row.sent_event_ids or "")


def test_persist_and_load_setup_state_roundtrip(runner_with_sqlite):
    from kazus_logic.setup import Fvg, SetupState

    runner_mod, SessionLocal = runner_with_sqlite

    state = SetupState(
        state="STB", session_id="abc", search_start_ts=1000,
        swing_low=100.5, swing_low_ts=1000,
        first_bear_fvg=Fvg(formed_at_idx=2, formed_at_ts=2000, top=107.0, bottom=105.0, kind="bearish"),
        first_bull_fvg=Fvg(formed_at_idx=5, formed_at_ts=5000, top=110.0, bottom=108.0, kind="bullish"),
        inv_fired=True, cre_fired=True, stb_fired=True,
        inv_at_ts=3000, cre_at_ts=5000,
    )
    snap = _make_snap(states={"H1": state})

    with SessionLocal() as db:
        runner_mod._persist_setup_states(db, snap)
        db.commit()

    with SessionLocal() as db:
        loaded = runner_mod._load_prev_states(db, "TESTUSDT")
    assert "H1" in loaded
    r = loaded["H1"]
    assert r.state == "STB"
    assert r.session_id == "abc"
    assert r.swing_low == 100.5
    assert r.first_bear_fvg.top == 107.0
    assert r.first_bull_fvg.kind == "bullish"
    assert r.inv_fired and r.cre_fired and r.stb_fired
