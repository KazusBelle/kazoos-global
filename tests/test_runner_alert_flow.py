"""
Integration tests for the worker's event-driven alert flow.

Covered:
  - _load_sent_ids accepts None / "[]" / "[\"a\"]" / garbage
  - _collect_new_events: brand-new event is returned, dedupes against
    sent_event_ids, clears state on OTE exit
  - _send_setup_alert: persists event_id (and STB suppresses) only after
    Telegram succeeds; failed Telegram leaves sent_event_ids untouched

Uses an in-memory SQLite engine and monkeypatches `worker.app.runner.SessionLocal`
plus the render / telegram functions so no photos are produced and no real
network calls are made.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from typing import List, Tuple

import pytest

# Force a sqlite URL so worker.app.db / worker.app.settings don't try to
# connect to Postgres on import.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "y")

# `backend/app/__init__.py` and `worker/app/__init__.py` collide on the
# top-level package name `app`. Conftest puts backend first, so `app.runner`
# would resolve to backend. Load the worker runner under a unique module
# name via importlib so we can test it in isolation.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER_APP = os.path.join(_ROOT, "worker", "app")


def _load_worker_runner():
    """Import worker/app/runner.py as `worker_runner` (with its sibling
    modules — db, settings, telegram, chart_image — also loaded under
    worker_app.* prefixes so relative imports resolve)."""
    if "worker_runner" in sys.modules:
        return sys.modules["worker_runner"]

    # Establish the parent package `worker_app` pointing at worker/app/.
    pkg_spec = importlib.util.spec_from_file_location(
        "worker_app",
        os.path.join(_WORKER_APP, "__init__.py"),
        submodule_search_locations=[_WORKER_APP],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["worker_app"] = pkg
    pkg_spec.loader.exec_module(pkg)

    for name in ("settings", "db", "telegram", "chart_image"):
        spec = importlib.util.spec_from_file_location(
            f"worker_app.{name}", os.path.join(_WORKER_APP, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"worker_app.{name}"] = mod
        spec.loader.exec_module(mod)

    # Load runner.py but rewrite its relative imports by pre-installing the
    # submodules under worker_app.* and then loading runner with that
    # parent package.
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
    """Rebuild a SessionLocal bound to in-memory SQLite, install it on the
    runner module, and yield helpers."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kazus_db.models import Base

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    runner_mod = _load_worker_runner()
    monkeypatch.setattr(runner_mod, "SessionLocal", SessionLocal)

    yield runner_mod, SessionLocal


def _make_snap(events_d1=(), events_h1=(), in_ote_d1=True, in_ote_h1=True):
    from kazus_logic.compute import SymbolSnapshot
    from kazus_logic.engine import ZoneResult

    def _zr(in_ote: bool) -> ZoneResult:
        return ZoneResult(
            zone="ote" if in_ote else "discount",
            in_ote=in_ote,
            setup=None,
            retracement=0.7,
            direction="bearish",
            fib_low=0.0, fib_high=1.0,
            ote_low_price=0.0, ote_high_price=1.0,
        )

    return SymbolSnapshot(
        symbol="TESTUSDT",
        price=100.0,
        global_result=_zr(in_ote_d1),
        local_result=_zr(in_ote_h1),
        global_trend="down",
        local_trend="down",
        global_setup_events=list(events_d1),
        local_setup_events=list(events_h1),
    )


def _make_event(kind: str, event_id: str, suppresses=()):
    from kazus_logic.compute import SetupEvent
    return SetupEvent(
        kind=kind, event_id=event_id,
        fvg_top=102.0, fvg_bottom=101.0, fvg_kind="bullish",
        trigger_open_ms=3000, direction="bearish", suppresses=tuple(suppresses),
    )


def test_load_sent_ids_handles_garbage():
    runner_mod = _load_worker_runner()
    _load_sent_ids = runner_mod._load_sent_ids
    assert _load_sent_ids(None) == set()
    assert _load_sent_ids("") == set()
    assert _load_sent_ids("[]") == set()
    assert _load_sent_ids('["a","b"]') == {"a", "b"}
    assert _load_sent_ids("not-json") == set()
    assert _load_sent_ids('{"x":1}') == set()  # dict, not list


def test_collect_new_events_returns_unseen_only(runner_with_sqlite):
    runner_mod, SessionLocal = runner_with_sqlite
    snap = _make_snap(events_h1=[_make_event("Created", "new:3000")])
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert [(tf, e.event_id) for tf, e in pending] == [("H1", "new:3000")]


def test_collect_new_events_skips_already_sent(runner_with_sqlite):
    import json
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    # Pre-seed AlertState as if "new:3000" was already alerted.
    with SessionLocal() as db:
        db.add(AlertState(
            symbol="TESTUSDT", timeframe="H1", in_ote=True,
            sent_event_ids=json.dumps(["new:3000"]),
        ))
        db.commit()

    snap = _make_snap(events_h1=[_make_event("Created", "new:3000")])
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert pending == []


def test_ote_exit_clears_sent_event_ids(runner_with_sqlite):
    import json
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    with SessionLocal() as db:
        db.add(AlertState(
            symbol="TESTUSDT", timeframe="H1", in_ote=True,
            sent_event_ids=json.dumps(["new:3000", "inv:2000"]),
        ))
        db.commit()

    # Snapshot says we're OUT of OTE → state should be reset.
    snap = _make_snap(in_ote_h1=False)
    with SessionLocal() as db:
        pending = runner_mod._collect_new_events(db, snap, {"H1"})
        db.commit()
    assert pending == []

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").one()
        assert row.in_ote is False
        assert row.sent_event_ids == "[]"


def test_send_setup_alert_persists_event_id_after_send(runner_with_sqlite, monkeypatch):
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    # Telegram OK; render returns nothing (text-only fallback path).
    sent_messages: List[str] = []

    async def fake_send_text(token, chat_id, text):
        sent_messages.append(text)
        return True

    async def fake_send_photo(*a, **k):
        return False  # force fallback to text

    async def fake_send_media(*a, **k):
        return False

    monkeypatch.setattr(runner_mod, "send_telegram", fake_send_text)
    monkeypatch.setattr(runner_mod, "send_telegram_photo", fake_send_photo)
    monkeypatch.setattr(runner_mod, "send_telegram_media_group", fake_send_media)
    monkeypatch.setattr(runner_mod, "render_setup_png", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "render_htf_png", lambda *a, **k: None)

    snap = _make_snap(events_h1=[_make_event("Created", "new:3000")])

    class FakeSettings:
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    asyncio.run(runner_mod._send_setup_alert(FakeSettings(), snap, "H1", snap.local_setup_events[0]))

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").one()
        assert "new:3000" in row.sent_event_ids
    assert sent_messages, "telegram should have been called"


def test_send_setup_alert_does_not_persist_when_telegram_fails(runner_with_sqlite, monkeypatch):
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    async def fake_send_text(*a, **k):
        return False

    async def fake_send_photo(*a, **k):
        return False

    async def fake_send_media(*a, **k):
        return False

    monkeypatch.setattr(runner_mod, "send_telegram", fake_send_text)
    monkeypatch.setattr(runner_mod, "send_telegram_photo", fake_send_photo)
    monkeypatch.setattr(runner_mod, "send_telegram_media_group", fake_send_media)
    monkeypatch.setattr(runner_mod, "render_setup_png", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "render_htf_png", lambda *a, **k: None)

    snap = _make_snap(events_h1=[_make_event("Created", "new:3000")])

    class FakeSettings:
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    asyncio.run(runner_mod._send_setup_alert(FakeSettings(), snap, "H1", snap.local_setup_events[0]))

    with SessionLocal() as db:
        # No row should have been created (or if created, sent_event_ids is empty).
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").first()
        if row is not None:
            assert "new:3000" not in (row.sent_event_ids or "")


def test_stb_persists_suppressed_event_ids(runner_with_sqlite, monkeypatch):
    from kazus_db.models import AlertState

    runner_mod, SessionLocal = runner_with_sqlite

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(runner_mod, "send_telegram", ok)
    monkeypatch.setattr(runner_mod, "send_telegram_photo", ok)
    monkeypatch.setattr(runner_mod, "send_telegram_media_group", ok)
    monkeypatch.setattr(runner_mod, "render_setup_png", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "render_htf_png", lambda *a, **k: None)

    stb = _make_event("STB", "stb:3000", suppresses=("new:3000", "inv:3000"))
    snap = _make_snap(events_h1=[stb])

    class FakeSettings:
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    asyncio.run(runner_mod._send_setup_alert(FakeSettings(), snap, "H1", stb))

    with SessionLocal() as db:
        row = db.query(AlertState).filter_by(symbol="TESTUSDT", timeframe="H1").one()
        for needle in ("stb:3000", "new:3000", "inv:3000"):
            assert needle in row.sent_event_ids, f"{needle} missing from {row.sent_event_ids}"
