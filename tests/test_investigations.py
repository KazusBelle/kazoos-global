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
