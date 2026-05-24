"""Phase-18 Investigation & Casework Layer.

In-memory SQLite tests for CRUD, append-only history, evidence linking,
auto-draft from PRE_CASCADE genesis, and resolution workflow guards.

These tests intentionally use SQLite (not the production Postgres) to
stay self-contained — they exercise the research.py logic, not the
Postgres-specific migrations. The Postgres migration is exercised in
production by the worker on first boot.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kazus_db.models import (
    Base,
    Investigation,
    InvestigationEvent,
    InvestigationEvidence,
    InvestigationNote,
)
from kazus_logic.liquidity import research


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_create_minimal_case(db):
    case = research.investigation_create(
        db, title="Liquidity divergence on BTCUSDT", description="initial scan",
        created_by=1,
    )
    assert case["id"] > 0
    assert case["title"] == "Liquidity divergence on BTCUSDT"
    assert case["status"] == "OPEN"
    assert case["severity"] == "warn"
    assert case["origin_kind"] == "manual"
    # `created` event logged
    events = db.query(InvestigationEvent).filter(
        InvestigationEvent.investigation_id == case["id"]
    ).all()
    types = {e.event_type for e in events}
    assert "created" in types


def test_create_validates_title(db):
    with pytest.raises(ValueError):
        research.investigation_create(db, title="   ")


def test_create_validates_severity(db):
    with pytest.raises(ValueError):
        research.investigation_create(db, title="t", severity="catastrophic")


def test_list_filters(db):
    research.investigation_create(db, title="a", severity="critical", tags=["x"])
    research.investigation_create(db, title="b", severity="info", tags=["y"])
    research.investigation_create(db, title="c", severity="warn", tags=["x", "y"])

    out = research.investigation_list(db, severity="critical")
    assert {i["title"] for i in out["items"]} == {"a"}

    out = research.investigation_list(db, tag="x")
    assert {i["title"] for i in out["items"]} == {"a", "c"}

    out = research.investigation_list(db, search="B")
    assert {i["title"] for i in out["items"]} == {"b"}


def test_status_lifecycle_and_resolution_summary_required(db):
    case = research.investigation_create(db, title="t", created_by=1)
    # OPEN → INVESTIGATING
    updated = research.investigation_update(db, case["id"], status="INVESTIGATING", actor_id=1)
    assert updated["status"] == "INVESTIGATING"
    # INVESTIGATING → RESOLVED without summary should fail
    with pytest.raises(ValueError, match="resolution_summary"):
        research.investigation_update(db, case["id"], status="RESOLVED", actor_id=1)
    # With summary it works.
    resolved = research.investigation_update(
        db, case["id"], status="RESOLVED",
        resolution_summary="confirmed false positive after manual cross-check",
        actor_id=1,
    )
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolved_at_ms"] is not None
    # Reopen.
    reopened = research.investigation_update(db, case["id"], status="INVESTIGATING", actor_id=1)
    assert reopened["status"] == "INVESTIGATING"
    assert reopened["resolved_at_ms"] is None
    # Lifecycle events recorded.
    types = [e.event_type for e in db.query(InvestigationEvent).filter(
        InvestigationEvent.investigation_id == case["id"]
    ).order_by(InvestigationEvent.ts_ms.asc()).all()]
    assert "created" in types
    assert "status_change" in types
    assert "resolved" in types
    assert "reopened" in types


def test_archived_cannot_be_modified_directly(db):
    case = research.investigation_create(db, title="t")
    research.investigation_update(db, case["id"], status="ARCHIVED")
    with pytest.raises(ValueError, match="archived"):
        research.investigation_update(db, case["id"], status="OPEN")


def test_notes_append_only(db):
    case = research.investigation_create(db, title="t")
    n1 = research.investigation_add_note(db, case["id"], body="first hypothesis", note_type="hypothesis")
    n2 = research.investigation_add_note(db, case["id"], body="revised: it's noise", note_type="false_positive")
    notes = db.query(InvestigationNote).filter(
        InvestigationNote.investigation_id == case["id"]
    ).order_by(InvestigationNote.created_at_ms.asc()).all()
    assert [n.id for n in notes] == [n1["id"], n2["id"]]
    # No update method exists — body is final.
    assert not hasattr(research, "investigation_edit_note")
    assert not hasattr(research, "investigation_delete_note")


def test_note_validates_type_and_body(db):
    case = research.investigation_create(db, title="t")
    with pytest.raises(ValueError):
        research.investigation_add_note(db, case["id"], body="x", note_type="bogus")
    with pytest.raises(ValueError):
        research.investigation_add_note(db, case["id"], body="   ")


def test_evidence_link_and_idempotency(db):
    case = research.investigation_create(db, title="t")
    e1 = research.investigation_link_evidence(
        db, case["id"], evidence_type="operator_priority", ref_key="ops::sanity::propagation_loop",
    )
    # Re-linking the same ref returns same row (idempotent dedup).
    e2 = research.investigation_link_evidence(
        db, case["id"], evidence_type="operator_priority", ref_key="ops::sanity::propagation_loop",
    )
    assert e1["id"] == e2["id"]
    count = db.query(InvestigationEvidence).filter(
        InvestigationEvidence.investigation_id == case["id"]
    ).count()
    assert count == 1


def test_evidence_unlink_logs_event(db):
    case = research.investigation_create(db, title="t")
    e = research.investigation_link_evidence(
        db, case["id"], evidence_type="symbol", ref_key="BTCUSDT",
    )
    result = research.investigation_unlink_evidence(db, case["id"], e["id"])
    assert result["removed"] is True
    events = db.query(InvestigationEvent).filter(
        InvestigationEvent.investigation_id == case["id"],
        InvestigationEvent.event_type == "evidence_unlinked",
    ).all()
    assert len(events) == 1


def test_evidence_validates_type(db):
    case = research.investigation_create(db, title="t")
    with pytest.raises(ValueError):
        research.investigation_link_evidence(
            db, case["id"], evidence_type="bogus", ref_key="x",
        )


def test_initial_evidence_at_create_time(db):
    case = research.investigation_create(
        db, title="t",
        initial_evidence=[
            {"evidence_type": "symbol", "ref_key": "ETHUSDT", "note": "primary"},
            {"evidence_type": "alert", "ref_key": "alert-42", "ref_id": 42},
        ],
    )
    detail = research.investigation_detail(db, case["id"])
    assert detail["evidence_count"] == 2
    types = {e["evidence_type"] for e in detail["evidence"]}
    assert types == {"symbol", "alert"}


def test_auto_draft_from_pre_cascade(db):
    fp_genesis = {
        "verdict": "PRE_CASCADE",
        "genesis_score": 82.0,
        "confidence": 0.7,
        "fetched_at_ms": 1716000000000,
        "probes": [
            {"kind": "fragmentation_growth", "contributing": True, "score": 80},
            {"kind": "resiliency_decay", "contributing": True, "score": 70},
            {"kind": "propagation_widening", "contributing": True, "score": 90},
            {"kind": "stress_acceleration", "contributing": False, "score": 10},
        ],
    }
    case = research.investigation_auto_draft_from_genesis(db, fp_genesis)
    assert case is not None
    assert case["origin_kind"] == "auto_pre_cascade"
    assert case["severity"] == "critical"
    assert case["replay_anchor_ms"] == 1716000000000
    # Same fingerprint twice → no second draft.
    again = research.investigation_auto_draft_from_genesis(db, fp_genesis)
    assert again is None
    # Non-PRE_CASCADE → no draft.
    none = research.investigation_auto_draft_from_genesis(db, {"verdict": "CALM"})
    assert none is None


def test_auto_draft_skipped_when_active_case_exists(db):
    g = {
        "verdict": "PRE_CASCADE",
        "genesis_score": 80,
        "confidence": 0.5,
        "fetched_at_ms": 1,
        "probes": [{"kind": "fragmentation_growth", "contributing": True, "score": 80}],
    }
    a = research.investigation_auto_draft_from_genesis(db, g)
    # Even after status change to INVESTIGATING (still active), no new draft.
    research.investigation_update(db, a["id"], status="INVESTIGATING")
    b = research.investigation_auto_draft_from_genesis(db, g)
    assert b is None
    # After RESOLVED, a fresh draft for the same fingerprint IS allowed.
    research.investigation_update(
        db, a["id"], status="RESOLVED", resolution_summary="handled",
    )
    c = research.investigation_auto_draft_from_genesis(db, g)
    assert c is not None
    assert c["id"] != a["id"]


def test_detail_404_pattern(db):
    out = research.investigation_detail(db, 999)
    assert out["found"] is False
    with pytest.raises(LookupError):
        research.investigation_update(db, 999, status="OPEN")
    with pytest.raises(LookupError):
        research.investigation_add_note(db, 999, body="x")


# ─── Pass B ───────────────────────────────────────────────────────────


def test_collaborators_and_handoff_logged(db):
    case = research.investigation_create(db, title="t", created_by=1)
    # Set primary + collaborators.
    updated = research.investigation_update(
        db, case["id"], actor_id=1,
        primary_symbol="btcusdt", related_symbols=["ethusdt", "solusdt"],
        collaborators=[2, 3],
    )
    assert updated["primary_symbol"] == "BTCUSDT"
    assert updated["related_symbols"] == ["ETHUSDT", "SOLUSDT"]
    assert updated["collaborators"] == [2, 3]
    # Reassign with handoff note.
    research.investigation_update(
        db, case["id"], actor_id=1, assigned_to=4, handoff_note="going on rotation",
    )
    events = [e.event_type for e in db.query(
        __import__("kazus_db.models", fromlist=["InvestigationEvent"]).InvestigationEvent
    ).filter_by(investigation_id=case["id"]).all()]
    assert "primary_symbol_change" in events
    assert "related_symbols_change" in events
    assert "collaborators_change" in events
    assert "assigned" in events


def test_last_touched_by_updates_on_every_event(db):
    case = research.investigation_create(db, title="t", created_by=1)
    assert case["last_touched_by"] == 1
    research.investigation_add_note(db, case["id"], body="x", author_id=7)
    after = research.investigation_detail(db, case["id"])
    assert after["last_touched_by"] == 7


def test_mention_event_logged(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_add_note(db, case["id"], body="cc @alice and @BOB please review", author_id=1)
    Ev = __import__("kazus_db.models", fromlist=["InvestigationEvent"]).InvestigationEvent
    import json
    mentions = [
        e for e in db.query(Ev).filter_by(investigation_id=case["id"]).all()
        if e.event_type == "mention"
    ]
    assert len(mentions) == 1
    payload = json.loads(mentions[0].payload_json)
    assert sorted(payload["handles"]) == ["alice", "bob"]


def test_causal_tree_minimal_seeded_by_symbols(db):
    case = research.investigation_create(
        db, title="t",
        primary_symbol="BTCUSDT",
        related_symbols=["ETHUSDT"],
    )
    tree = research.investigation_causal_tree(db, case["id"])
    assert tree["found"] is True
    node_ids = {n["id"] for n in tree["nodes"]}
    assert f"case::{case['id']}" in node_ids
    assert "sym::BTCUSDT" in node_ids
    assert "sym::ETHUSDT" in node_ids
    # case_subject edges from case to seed symbols.
    case_subj = [e for e in tree["edges"] if e["kind"] == "case_subject"]
    assert len(case_subj) == 2
    # Every edge has rationale.
    for e in tree["edges"]:
        assert e.get("rationale")


def test_causal_tree_includes_cross_case_reference(db):
    a = research.investigation_create(db, title="a")
    b = research.investigation_create(db, title="b", description=f"see also #{a['id']}")
    tree = research.investigation_causal_tree(db, b["id"])
    refs = [e for e in tree["edges"] if e["kind"] == "case_reference"]
    assert any(e["to"] == f"case::{a['id']}" for e in refs)


def test_similar_finds_same_fingerprint(db):
    g = {
        "verdict": "PRE_CASCADE", "genesis_score": 80, "confidence": 0.6, "fetched_at_ms": 1,
        "probes": [{"kind": "fragmentation_growth", "contributing": True, "score": 80}],
    }
    a = research.investigation_auto_draft_from_genesis(db, g)
    # Close the first so the second auto-draft is allowed.
    research.investigation_update(db, a["id"], status="RESOLVED", resolution_summary="ok")
    b = research.investigation_auto_draft_from_genesis(db, g)
    sim = research.investigation_similar(db, b["id"])
    assert sim["found"] is True
    # The closed prior case should appear because RESOLVED ones are included.
    ids = [s["id"] for s in sim["similar"]]
    assert a["id"] in ids
    # Reasons must explicitly mention the fingerprint match.
    match = next(s for s in sim["similar"] if s["id"] == a["id"])
    assert any("fingerprint" in r for r in match["reasons"])


def test_similar_overlap_via_symbols(db):
    a = research.investigation_create(db, title="a", primary_symbol="BTCUSDT",
                                       related_symbols=["ETHUSDT", "SOLUSDT"])
    b = research.investigation_create(db, title="b", primary_symbol="BTCUSDT",
                                       related_symbols=["ETHUSDT"])
    sim = research.investigation_similar(db, b["id"], min_score=0)
    match = next((s for s in sim["similar"] if s["id"] == a["id"]), None)
    assert match is not None
    assert match["similarity_score"] > 0
    assert any("symbol" in r.lower() for r in match["reasons"])


def test_export_markdown_stable_sections(db):
    case = research.investigation_create(
        db, title="t", description="primary case",
        primary_symbol="BTCUSDT", tags=["alpha"], created_by=1,
    )
    research.investigation_add_note(db, case["id"], body="my note", author_id=1)
    research.investigation_link_evidence(
        db, case["id"], evidence_type="symbol", ref_key="ETHUSDT",
    )
    out = research.investigation_export_markdown(db, case["id"])
    md = out["markdown"]
    for section in [
        "## 1. Summary", "## 2. Resolution", "## 3. Linked evidence",
        "## 4. Operator notes", "## 5. Investigation tree",
        "## 6. Timeline", "## 7. Similar prior cases", "## 8. Audit metadata",
    ]:
        assert section in md, f"missing section: {section}"
    # Sanity: contains case title & symbol.
    assert "BTCUSDT" in md
    assert "my note" in md


def test_export_404_for_missing(db):
    out = research.investigation_export_markdown(db, 999)
    assert out["found"] is False


# ─── Pass A: Phase 19 Replay Intelligence ─────────────────────────────


def test_create_leaves_case_in_pending_status(db):
    """Integrity Repair Pass §4: case creation no longer captures
    inline; lands in PENDING for the worker to drain."""
    case = research.investigation_create(db, title="t", created_by=1)
    assert case["capture_status"] == "PENDING"
    # No snapshot row yet.
    assert research._replay_load_snapshot(db, case["id"]) is None


def test_capture_pending_drains_queue(db):
    """Worker drain path: PENDING → CAPTURED + snapshot row created."""
    case_a = research.investigation_create(db, title="a", created_by=1)
    case_b = research.investigation_create(db, title="b", created_by=1)
    result = research.investigation_capture_pending(db, limit=10)
    assert set(result["captured_ids"]) >= {case_a["id"], case_b["id"]}
    detail = research.investigation_detail(db, case_a["id"])
    assert detail["capture_status"] == "CAPTURED"
    snap = research._replay_load_snapshot(db, case_a["id"])
    assert snap is not None
    assert snap["revision"] == 1
    assert snap["is_active"] is True
    # All sections present in payload.
    for sec in ("operator_priorities", "sanity_audit", "crisis_genesis",
                "adaptation_state", "narrative_causality"):
        assert sec in snap["payload"]


def test_capture_idempotent_without_force(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    again = research.investigation_replay_capture(db, case["id"])
    assert again["captured"] is False
    assert "active snapshot exists" in (again.get("reason") or "")


def test_recapture_with_force_is_append_only(db):
    """Integrity Repair Pass §1: recapture must NEVER destroy prior
    payload. New revision is inserted; old revision is preserved with
    is_active=False."""
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    rev1 = research._replay_load_snapshot(db, case["id"])
    assert rev1["revision"] == 1
    assert rev1["is_active"] is True

    forced = research.investigation_replay_capture(db, case["id"], force=True, captured_by=2)
    assert forced["captured"] is True
    assert forced["revision"] == 2

    # Active snapshot is now rev2.
    rev2 = research._replay_load_snapshot(db, case["id"])
    assert rev2["revision"] == 2
    assert rev2["is_active"] is True

    # Old rev1 is preserved (append-only invariant).
    rev1_after = research._replay_load_snapshot(db, case["id"], revision=1)
    assert rev1_after is not None
    assert rev1_after["is_active"] is False
    assert rev1_after["payload"] is not None

    # Event log records both captures.
    Ev = __import__("kazus_db.models", fromlist=["InvestigationEvent"]).InvestigationEvent
    types = [e.event_type for e in db.query(Ev).filter_by(investigation_id=case["id"]).all()]
    assert "replay_captured" in types
    assert "replay_recaptured" in types


def test_replay_history_lists_all_revisions(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    research.investigation_replay_capture(db, case["id"], force=True, captured_by=2)
    research.investigation_replay_capture(db, case["id"], force=True, captured_by=3)
    hist = research.investigation_replay_history(db, case["id"])
    assert hist["found"] is True
    assert hist["total_revisions"] == 3
    assert hist["active_revision"] == 3
    revs = [r["revision"] for r in hist["revisions"]]
    assert revs == [1, 2, 3]
    actives = [r["is_active"] for r in hist["revisions"]]
    assert actives == [False, False, True]


def test_replay_diff_revisions_uses_same_delta(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    research.investigation_replay_capture(db, case["id"], force=True)
    diff = research.investigation_replay_diff_revisions(db, case["id"])
    assert diff["found"] is True
    assert diff["comparison_mode"] == "frozen_vs_frozen"
    assert diff["from_revision"] == 1
    assert diff["to_revision"] == 2
    # Same revision == empty diff.
    same = research.investigation_replay_diff_revisions(
        db, case["id"], from_revision=1, to_revision=1,
    )
    assert same["diff_count"] == 0


def test_replay_diff_carries_explicit_comparison_mode(db):
    """Integrity Repair Pass §2: diff response must declare
    comparison_mode so the UI cannot mis-label the comparison source."""
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    diff = research.investigation_replay_diff(db, case["id"])
    assert diff["comparison_mode"] == "frozen_vs_now"


def test_capture_retry_resets_failed_to_pending(db):
    case = research.investigation_create(db, title="t", created_by=1)
    # Simulate a failed capture.
    from kazus_db.models import Investigation
    row = db.query(Investigation).filter_by(id=case["id"]).first()
    row.capture_status = "FAILED"
    row.capture_error = "synthetic failure"
    db.commit()
    out = research.investigation_capture_retry(db, case["id"])
    assert out["requeued"] is True
    refreshed = research.investigation_detail(db, case["id"])
    assert refreshed["capture_status"] == "PENDING"
    assert refreshed["capture_error"] is None


def test_state_frozen_returns_snapshot(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    state = research.investigation_replay_state(db, case["id"], mode="frozen")
    assert state["found"] is True
    assert state["mode"] == "frozen"
    assert state["is_frozen"] is True
    assert state["snapshot_present"] is True
    assert state["payload"] is not None
    assert state["revision"] == 1
    assert state["is_active"] is True


def test_state_frozen_by_revision(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    research.investigation_replay_capture(db, case["id"], force=True)
    # Load archived revision 1 explicitly.
    s = research.investigation_replay_state(db, case["id"], mode="frozen", revision=1)
    assert s["snapshot_present"] is True
    assert s["revision"] == 1
    assert s["is_active"] is False
    # Bogus revision → snapshot_present=False with explicit warning.
    s_missing = research.investigation_replay_state(db, case["id"], mode="frozen", revision=999)
    assert s_missing["snapshot_present"] is False
    assert "revision 999" in (s_missing.get("warning") or "")


def test_state_live_reconstructs_at_anchor(db):
    case = research.investigation_create(db, title="t", created_by=1)
    state = research.investigation_replay_state(db, case["id"], mode="live")
    assert state["found"] is True
    assert state["mode"] == "live"
    assert state["is_frozen"] is False
    # Each surface in reconstruction publishes data_quality.
    rec = state["reconstructed"]
    assert "intel_snapshot" in rec
    assert rec["intel_snapshot"]["data_quality"] in ("HIGH", "PARTIAL", "PRUNED", "INSUFFICIENT")


def test_state_validates_mode(db):
    case = research.investigation_create(db, title="t", created_by=1)
    import pytest as _pt
    with _pt.raises(ValueError):
        research.investigation_replay_state(db, case["id"], mode="bogus")


def test_replay_timeline_includes_case_events_in_window(db):
    case = research.investigation_create(db, title="t", created_by=1)
    # Anchor at created_at so case events fall inside the default window.
    tl = research.investigation_replay_timeline(db, case["id"])
    assert tl["found"] is True
    sources = {k["source"] for k in tl["keyframes"]}
    assert "case" in sources  # at least the 'created' event
    # Keyframes sorted ascending.
    ts_list = [k["ts_ms"] for k in tl["keyframes"]]
    assert ts_list == sorted(ts_list)


def test_replay_diff_no_drift_immediately_after_capture(db):
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_capture_pending(db)
    diff = research.investigation_replay_diff(db, case["id"])
    assert diff["found"] is True
    assert diff["frozen_present"] is True
    assert diff["comparison_mode"] == "frozen_vs_now"
    # Right after capture, no drift expected (live surfaces == frozen).
    assert diff["diff_count"] == 0


def test_replay_diff_without_snapshot(db):
    """PENDING case (not yet captured) → frozen_present=False."""
    case = research.investigation_create(db, title="t", created_by=1)
    diff = research.investigation_replay_diff(db, case["id"])
    assert diff["found"] is True
    assert diff["frozen_present"] is False
    assert diff["comparison_mode"] == "frozen_vs_now"


def test_evidence_link_auto_snapshots_upstream(db):
    """Integrity Repair Pass §3: evidence link without explicit
    snapshot auto-fetches the upstream row's content so the case
    retains forensic continuity past retention prune."""
    # Pre-populate an upstream alert.
    from kazus_db.models import LiquidityAlertHistory, InvestigationEvidence
    db.add(LiquidityAlertHistory(
        alert_id="alert-x", symbol="BTCUSDT", kind="loss_spike",
        severity="warn", regime="normal", confidence=0.7, priority=55.0,
        trigger="t", started_at_ms=1000, last_seen_at_ms=2000,
    ))
    db.flush()
    upstream_id = db.query(LiquidityAlertHistory).first().id

    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_link_evidence(
        db, case["id"], evidence_type="alert", ref_key=f"alert-{upstream_id}",
        ref_id=upstream_id,  # no explicit snapshot
    )
    ev = (
        db.query(InvestigationEvidence)
        .filter_by(investigation_id=case["id"], evidence_type="alert")
        .first()
    )
    assert ev.snapshot_json is not None
    import json as _json
    snap = _json.loads(ev.snapshot_json)
    assert snap["_snapshot_kind"] == "alert"
    assert snap["symbol"] == "BTCUSDT"
    assert snap["kind"] == "loss_spike"


def test_timeline_marks_pruned_upstream(db):
    """If linked alert row is deleted (simulating retention prune),
    timeline must surface the snapshot fallback with is_pruned=True
    instead of silently dropping the event."""
    from kazus_db.models import LiquidityAlertHistory
    db.add(LiquidityAlertHistory(
        alert_id="alert-y", symbol="ETHUSDT", kind="liquidation_burst",
        severity="critical", regime="normal", confidence=0.8, priority=80.0,
        trigger="x", started_at_ms=500, last_seen_at_ms=600,
    ))
    db.flush()
    aid = db.query(LiquidityAlertHistory).first().id
    case = research.investigation_create(db, title="t", created_by=1)
    research.investigation_link_evidence(
        db, case["id"], evidence_type="alert", ref_key=f"alert-{aid}", ref_id=aid,
    )
    # Now prune the upstream row.
    db.query(LiquidityAlertHistory).filter_by(id=aid).delete()
    db.commit()
    tl = research.investigation_timeline(db, case["id"])
    pruned_events = [e for e in tl["events"] if e.get("is_pruned")]
    assert len(pruned_events) == 1
    p = pruned_events[0]
    assert p["source"] == "alert"
    assert p["payload"]["symbol"] == "ETHUSDT"
    assert "pruned upstream" in (p.get("note") or "")


def test_replay_propagation_returns_frames(db):
    case = research.investigation_create(
        db, title="t", primary_symbol="BTCUSDT",
        related_symbols=["ETHUSDT"], created_by=1,
    )
    prop = research.investigation_replay_propagation(db, case["id"])
    assert prop["found"] is True
    assert "BTCUSDT" in prop["symbols"]
    assert "ETHUSDT" in prop["symbols"]
    assert isinstance(prop["frames"], list)
    assert prop["frame_count"] >= 1
    # Every frame has a ts_ms inside the window.
    ws, we = prop["window_start_ms"], prop["window_end_ms"]
    for f in prop["frames"]:
        assert ws <= f["ts_ms"] <= we + prop["bucket_ms"]


def test_replay_404_for_missing_case(db):
    out_state = research.investigation_replay_state(db, 999, mode="frozen")
    assert out_state["found"] is False
    out_diff = research.investigation_replay_diff(db, 999)
    assert out_diff["found"] is False
    out_tl = research.investigation_replay_timeline(db, 999)
    assert out_tl["found"] is False
    import pytest as _pt
    with _pt.raises(LookupError):
        research.investigation_replay_capture(db, 999, force=True)


def test_replay_safety_pruned_intel(db):
    # Build a case anchored years in the past — no intel rows in window.
    from kazus_db.models import Investigation
    case = research.investigation_create(db, title="ancient", created_by=1)
    # Manually push anchor backwards (intel_history is empty in tests anyway).
    row = db.query(Investigation).filter_by(id=case["id"]).first()
    row.replay_anchor_ms = 1_000_000_000_000  # 2001
    db.commit()
    state = research.investigation_replay_state(db, case["id"], mode="live")
    rec = state["reconstructed"]
    # With empty intel history, INSUFFICIENT is expected.
    assert rec["intel_snapshot"]["data_quality"] == "INSUFFICIENT"


def test_timeline_includes_case_events_and_notes(db):
    case = research.investigation_create(db, title="t")
    research.investigation_add_note(db, case["id"], body="first note")
    research.investigation_link_evidence(
        db, case["id"], evidence_type="symbol", ref_key="BTCUSDT",
    )
    tl = research.investigation_timeline(db, case["id"])
    assert tl["found"] is True
    sources = {e["source"] for e in tl["events"]}
    assert "case" in sources
    assert "note" in sources
    # Chronological descending.
    ts_list = [e["ts_ms"] for e in tl["events"]]
    assert ts_list == sorted(ts_list, reverse=True)
