"""Historical report formatting must preserve accepted evidence without a live write."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.user_facing_report import (
    delivery_contract_from_report,
    normalize_user_facing_report,
    render_user_facing_report_chunks,
    report_matches_user_facing_delivery,
)


def _body(*, thread="2", review=False):
    contract = {"identity": {"platform": "telegram", "chat_id": "chat-1", "thread_id": thread, "project": "secondhand"}}
    return "GRACE_LOOP_CONTRACT_STAGE: " + ("review" if review else "execution") + "\n```json\n" + json.dumps(contract) + "\n```"


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with kb.connect_closing() as conn:
        source = kb.create_task(
            conn, title="Accepted observation", body=_body(),
            project_namespace="secondhand",
        )
        observed = int(time.time()) - 3600
        originals = [{
            "group_id": str(100 + index), "group_name": f"社團 {index}", "source_listing_id": "1234",
            "status": "published" if index == 0 else "unknown", "visible": index == 0,
            "pending_review": False if index == 0 else None,
            "post_url": "https://www.facebook.com/groups/100/posts/900" if index == 0 else None,
            "observed_at": observed,
        } for index in range(2)]
        assert kb.complete_task(conn, source, metadata={
            "acceptance_evidence": {"publication_reconciliation": originals},
            "user_facing_report": {
                "kind": "content_package", "delivery": "inline_only", "complete": False,
                "title": "Accepted observation", "body": "One public, one unknown.",
                "body_field": "report", "observed_at": observed, "assets": [],
            },
        })
        source_run = kb.latest_run(conn, source)
        review = kb.create_task(conn, title="Accepted source review", body=_body(review=True), parents=[source])
        # Fixture: an already completed native review with an exact evidence receipt.
        kb._synthesize_ended_run(conn, review, outcome="completed", metadata={
            "review_outcome": "accepted", "workflow_review_source": {
                "parent_execution_task_id": source, "parent_execution_run_id": source_run.id,
                "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(source_run),
            },
        })
        review_run = kb.latest_run(conn, review)
        now = int(time.time())
        conn.execute("""INSERT INTO grace_delegations (
            delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,
            session_key,session_id,resolved_route,approval_required,state,
            execution_task_id,review_task_id,created_at,updated_at
            ) VALUES ('gd-source','source','source','telegram','chat-1','2','session','session','{"project":"secondhand"}',0,'queued',?,?,?,?)""",
            (source, review, now, now))
        recovery = kb.create_task(conn, title="Format accepted history", body=_body())
        rows = [{
            "subject_key": "facebook_marketplace_listing:1234", "subject_label": "望遠鏡",
            "destination_id": original["group_id"], "destination_name": original["group_name"],
            "source_listing_id": "1234", "source_task_id": source,
            "status": "public" if original["status"] == "published" else original["status"],
            "status_label": "歷史可見" if original["visible"] else "未知",
            "observed_at": observed, "verified_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
            "visible": original["visible"], "pending_review": original["pending_review"],
            "canonical_url": f"https://www.facebook.com/groups/{original['group_id']}/",
            "post_url": original["post_url"], "evidence_url": original["post_url"],
            "evidence": "引用已驗收的原始觀测，僅整理文字。", "evidence_gaps": [],
        } for original in originals]
        report = {
            "kind": "commerce_group_status", "delivery": "inline_only", "complete": False,
            "title": "歷史證據整理", "body_field": "commerce_group_status_report", "body": "一筆歷史可見，一筆未知，目前狀態未重新查核。",
            "assets": [], "evidence_gaps": ["目前狀態未查核"],
            "as_of": datetime.fromtimestamp(observed, timezone.utc).isoformat(), "observed_at": observed,
            "evidence_mode": "historical_verified",
            "evidence_source": {"execution_run_id": source_run.id, "review_run_id": review_run.id},
            "rows": rows,
            "coverage": [{
                "subject_key": rows[0]["subject_key"], "subject_label": "望遠鏡", "complete": False,
                "expected_total": 20, "named_count": 2, "gap_count": 18, "note": "尚有缺口，不代表完成刊登。",
                "destination_target": 20, "verified_published_count": 1, "unknown_count": 1,
                "remaining_verified_publication_gap": 19, "unnamed_gap": 18,
                **{key: None for key in ("candidate_count", "joined_count", "selectable_count", "submitted_count", "pending_count", "rejected_count", "not_submitted_count", "ready_count", "conditional_count", "excluded_count")},
            }],
        }
        # The current ledger is newer, different, and must not be overwritten by formatting.
        current = copy.deepcopy(report)
        for key in ("evidence_mode", "evidence_source"):
            current.pop(key)
        current["observed_at"] = now
        for row in current["rows"]:
            row["observed_at"] = now
            row["evidence"] = "較新的目前紀錄"
        kb.record_commerce_user_facing_report(conn, report=current, source_task_id=source)
        conn.commit()
        yield conn, recovery, report


def _ledger(conn):
    return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in (
        "commerce_group_ledger", "commerce_group_coverage", "commerce_group_migration_state",
    )}


def test_historical_report_completion_readback_and_render_preserve_evidence(history):
    conn, recovery, report = history
    before = _ledger(conn)
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report, "external_effects": []})
    persisted = kb.latest_run(conn, recovery).metadata["user_facing_report"]
    assert normalize_user_facing_report(persisted) == normalize_user_facing_report(report)
    assert _ledger(conn) == before
    assert persisted["coverage"][0]["submitted_count"] is None
    assert kb._commerce_report_has_current_subject_coverage(conn, execution_task_id=recovery, report=persisted, require_complete=False)
    assert not kb._commerce_report_has_current_subject_coverage(conn, execution_task_id=recovery, report=persisted)
    contract = delivery_contract_from_report(persisted)
    assert report_matches_user_facing_delivery(persisted, contract)
    assert not report_matches_user_facing_delivery(persisted, {**contract, "body_field": "wrong"})
    rendered = "".join(render_user_facing_report_chunks(persisted))
    assert report["body"] not in rendered
    assert "本次未重新查核 Facebook" in rendered
    assert report["rows"][0]["destination_name"] in rendered
    assert report["rows"][0]["status_label"] in rendered
    # The actual compact review API must expose canonical evidence even with no acceptance_evidence.
    from tools import kanban_tools
    review = kb.create_task(conn, title="Review historical report", body=_body(review=True), parents=[recovery])
    conn.commit()
    shown = json.loads(kanban_tools._handle_show({"task_id": review}))
    assert shown["parent_evidence"][0]["acceptance_evidence"] is None
    assert shown["parent_evidence"][0]["user_facing_report"] == persisted
    assert shown["parent_evidence"][0]["review_evidence"]["user_facing_report"] == persisted


def test_historical_report_accepts_controller_review_binding_fields(history):
    conn, recovery, report = history
    review_run = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    metadata = review_run.metadata
    metadata["workflow_review_source"].update(
        review_runtime_sha256="a" * 64,
        objective_plan_request_id="gpr_test",
        objective_plan_specification_sha256="b" * 64,
    )
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(metadata), review_run.id),
    )

    assert kb.complete_task(
        conn,
        recovery,
        metadata={"user_facing_report": report, "external_effects": []},
    )


def test_historical_report_merges_names_from_canonical_source_rows(history):
    conn, recovery, report = history
    execution_run = kb.get_run(
        conn, report["evidence_source"]["execution_run_id"],
    )
    metadata = execution_run.metadata
    for row in metadata["acceptance_evidence"]["publication_reconciliation"]:
        row.pop("group_name")
    canonical_rows = copy.deepcopy(report["rows"])
    for row in canonical_rows:
        row["subject_key"] = "product:望遠鏡"
    metadata["user_facing_report"] = {"rows": canonical_rows}
    for row in report["rows"]:
        row["subject_key"] = "product:望遠鏡"
    report["coverage"][0]["subject_key"] = "product:望遠鏡"
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(metadata), execution_run.id),
    )
    review_run = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    review_metadata = review_run.metadata
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        kb.workflow_review_evidence_hash(kb.get_run(conn, execution_run.id))
    )
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run.id),
    )

    assert kb.complete_task(
        conn, recovery,
        metadata={"user_facing_report": report, "external_effects": []},
    )


@pytest.mark.parametrize("corruption", ["status_label", "missing_coverage_subject"])
def test_historical_report_preserves_canonical_presentation_and_coverage(
    history, corruption,
):
    conn, recovery, report = history
    execution_run = kb.get_run(
        conn, report["evidence_source"]["execution_run_id"],
    )
    metadata = execution_run.metadata
    metadata["user_facing_report"] = {
        "rows": copy.deepcopy(report["rows"]),
        "coverage": [
            *copy.deepcopy(report["coverage"]),
            {
                "subject_key": "entirely-unresolved-subject",
                "subject_label": "尚未具名的商品",
                "complete": False,
                "expected_total": None,
                "named_count": 0,
                "gap_count": None,
                "note": "尚無目的地列",
            },
        ],
    }
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(metadata), execution_run.id),
    )
    review_run = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    review_metadata = review_run.metadata
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        kb.workflow_review_evidence_hash(kb.get_run(conn, execution_run.id))
    )
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run.id),
    )
    if corruption == "status_label":
        report["rows"][0]["status_label"] = "改寫後的未驗收標籤"
        expected = "source status_label"
    else:
        expected = "every accepted coverage subject"

    with pytest.raises(ValueError, match=expected):
        kb.complete_task(
            conn, recovery,
            metadata={"user_facing_report": report, "external_effects": []},
        )


@pytest.mark.parametrize("corruption", ["source_run", "review_run", "source_hash", "different_topic", "status", "post_url", "observed_at", "verified_at", "omit_unknown", "source_task", "invent_count", "complete"])
def test_historical_evidence_rejects_forgery_atomically(history, corruption):
    conn, recovery, report = history
    if corruption == "source_run":
        report["evidence_source"]["execution_run_id"] = 999999
    elif corruption == "review_run":
        report["evidence_source"]["review_run_id"] = report["evidence_source"]["execution_run_id"]
    elif corruption == "source_hash":
        run = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
        md = copy.deepcopy(run.metadata)
        md["acceptance_evidence"]["publication_reconciliation"][0]["visible"] = False
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md), run.id))
        conn.commit()
    elif corruption == "different_topic":
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (_body(thread="another"), recovery))
        conn.commit()
    elif corruption == "status":
        report["rows"][1].update(status="public", visible=True)
    elif corruption == "post_url":
        report["rows"][0]["post_url"] = "https://www.facebook.com/groups/100/posts/999"
    elif corruption == "observed_at":
        report["rows"][0]["observed_at"] += 60
    elif corruption == "verified_at":
        report["rows"][0]["verified_at"] = "fresh just now"
    elif corruption == "omit_unknown":
        report["rows"].pop()
        report["coverage"][0].update(named_count=1, gap_count=19, unnamed_gap=19, unknown_count=0)
    elif corruption == "source_task":
        report["rows"][0]["source_task_id"] = recovery
    elif corruption == "invent_count":
        report["coverage"][0]["submitted_count"] = 0
    elif corruption == "complete":
        report["complete"] = True
    before = _ledger(conn)
    with pytest.raises(ValueError):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    assert _ledger(conn) == before
    assert kb.get_task(conn, recovery).status != "done"
    assert kb.latest_run(conn, recovery) is None


def test_live_report_stale_guard_stays_enabled(history):
    conn, recovery, report = history
    report.pop("evidence_mode")
    report.pop("evidence_source")
    with pytest.raises(ValueError, match="stale destination"):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})


def _use_accepted_commerce_rows(conn, report):
    """Model a previously accepted aggregated report with older per-row provenance."""
    source = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
    md = copy.deepcopy(source.metadata)
    md.pop("acceptance_evidence")
    for row in report["rows"]:
        row["source_task_id"] = "t_original_observer"
        row["canonical_url"] = row["canonical_url"].rstrip("/")
        row["evidence_url"] = f"https://evidence.example/observations/{row['destination_id']}"
    report["coverage"][0].update(submitted_count=7, ready_count=1, conditional_count=1, excluded_count=0)
    report["rows"][0]["evidence_gaps"] = ["已驗收來源仍有一項限制，不得刪除。"]
    md["user_facing_report"] = {key: copy.deepcopy(value) for key, value in report.items() if key not in {"evidence_mode", "evidence_source"}}
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md), source.id))
    source = kb.get_run(conn, source.id)
    review = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    review_md = copy.deepcopy(review.metadata)
    review_md["workflow_review_source"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(source)
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_md), review.id))
    conn.commit()


def test_accepted_commerce_rows_keep_original_provenance_urls_and_counts(history):
    conn, recovery, report = history
    _use_accepted_commerce_rows(conn, report)
    before = _ledger(conn)
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    persisted = kb.latest_run(conn, recovery).metadata["user_facing_report"]
    assert persisted["rows"] == report["rows"]
    assert persisted["coverage"][0]["submitted_count"] == 7
    assert _ledger(conn) == before


@pytest.mark.parametrize("alteration", [
    "provenance", "canonical_url", "evidence_url", "erase_count", "row_gaps", "report_gaps",
    "status_label", "subject_label", "evidence", "coverage_note",
    "ready_count", "conditional_count", "excluded_count", "verified_published_count", "unknown_count", "unnamed_gap",
])
def test_accepted_commerce_rows_cannot_lose_source_evidence(history, alteration):
    conn, recovery, report = history
    _use_accepted_commerce_rows(conn, report)
    if alteration == "provenance":
        report["rows"][0]["source_task_id"] = kb.get_run(conn, report["evidence_source"]["execution_run_id"]).task_id
    elif alteration == "canonical_url":
        report["rows"][0]["canonical_url"] += "/"
    elif alteration == "evidence_url":
        report["rows"][0]["evidence_url"] = None
    elif alteration == "status_label":
        report["rows"][1]["status_label"] = "目前已驗證公開"
    elif alteration == "subject_label":
        report["rows"][1]["subject_label"] = "不同商品"
    elif alteration == "evidence":
        report["rows"][1]["evidence"] = "已即時重新查核"
    elif alteration == "coverage_note":
        report["coverage"][0]["note"] = "已全部完成"
    elif alteration == "row_gaps":
        report["rows"][0]["evidence_gaps"] = []
    elif alteration == "report_gaps":
        report["evidence_gaps"] = []
    else:
        report["coverage"][0]["submitted_count" if alteration == "erase_count" else alteration] = None
    before = _ledger(conn)
    with pytest.raises(ValueError):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    assert _ledger(conn) == before
    assert kb.get_task(conn, recovery).status != "done"


def test_legacy_accepted_empty_urls_roundtrip_as_unknown(history):
    conn, recovery, report = history
    _use_accepted_commerce_rows(conn, report)
    source = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
    md = copy.deepcopy(source.metadata)
    for key in ("canonical_url", "post_url", "evidence_url"):
        md["user_facing_report"]["rows"][1][key] = ""
        report["rows"][1][key] = ""
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md), source.id))
    review = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    md_review = copy.deepcopy(review.metadata)
    md_review["workflow_review_source"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(kb.get_run(conn, source.id))
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md_review), review.id))
    conn.commit()
    before = _ledger(conn)
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    stored = kb.latest_run(conn, recovery).metadata["user_facing_report"]
    assert all(stored["rows"][1][key] is None for key in ("canonical_url", "post_url", "evidence_url"))
    assert _ledger(conn) == before


def test_legacy_row_report_still_matches_body_field_discriminator(history):
    _, _, report = history
    for key in ("evidence_mode", "evidence_source", "title", "body", "body_field"):
        report.pop(key)
    report["body_field"] = "membership_action_report"
    report = normalize_user_facing_report(report)
    assert report["body_field"] == "membership_action_report"
    contract = {**delivery_contract_from_report(report), "body_field": "membership_action_report"}
    assert report_matches_user_facing_delivery(report, contract)
    assert report["rows"][0]["destination_name"] in "".join(render_user_facing_report_chunks(report))


def test_legacy_row_report_must_supply_required_body_field(history):
    _, _, report = history
    contract = {**delivery_contract_from_report(report), "body_field": "commerce_group_status_report"}
    report.pop("body_field")
    assert not report_matches_user_facing_delivery(report, contract)


def test_legacy_accepted_coverage_target_cannot_be_shrunk(history):
    from hermes_cli.user_facing_report import COMMERCE_COVERAGE_COUNTERS
    conn, recovery, report = history
    _use_accepted_commerce_rows(conn, report)
    source = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
    md = copy.deepcopy(source.metadata)
    for key in COMMERCE_COVERAGE_COUNTERS:
        md["user_facing_report"]["coverage"][0].pop(key, None)
        if key not in {"destination_target", "verified_published_count", "unknown_count", "unnamed_gap", "remaining_verified_publication_gap"}:
            report["coverage"][0][key] = None
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md), source.id))
    review = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    review_md = copy.deepcopy(review.metadata)
    review_md["workflow_review_source"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(kb.get_run(conn, source.id))
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_md), review.id))
    conn.commit()
    report["coverage"][0].update(expected_total=2, gap_count=0, destination_target=2, unnamed_gap=0, remaining_verified_publication_gap=1)
    normalize_user_facing_report(report)  # Self-consistent numbers are not sufficient proof.
    before = _ledger(conn)
    with pytest.raises(ValueError, match="source coverage.expected_total"):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    assert _ledger(conn) == before


def test_mutating_accepted_task_body_does_not_rehome_historical_evidence(history):
    conn, recovery, report = history
    source = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
    conn.execute("UPDATE tasks SET body=? WHERE id IN (?,?)", (_body(thread="another"), source.task_id, recovery))
    conn.commit()
    with pytest.raises(ValueError, match="same Topic"):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    # Restoring the recipient's original lane works even though the old task's
    # display body remains edited: accepted identity comes from native admission.
    conn.execute("UPDATE tasks SET body=? WHERE id=?", (_body(), recovery))
    conn.commit()
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report})


@pytest.mark.parametrize("key", ["ready_count", "conditional_count", "excluded_count"])
def test_historical_report_requires_explicit_unknown_counts(history, key):
    conn, recovery, report = history
    report["coverage"][0].pop(key)
    with pytest.raises(ValueError, match="explicit coverage counts"):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})


@pytest.mark.parametrize("legacy_contract", [None, {"identity": {"thread_id": "another"}}])
def test_legacy_source_identity_uses_native_admission(history, legacy_contract):
    conn, recovery, report = history
    source = kb.get_run(conn, report["evidence_source"]["execution_run_id"])
    md = copy.deepcopy(source.metadata)
    md.pop("loop_contract", None)
    if legacy_contract is not None:
        md["loop_contract"] = legacy_contract
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(md), source.id))
    review = kb.get_run(conn, report["evidence_source"]["review_run_id"])
    review_md = copy.deepcopy(review.metadata)
    review_md["workflow_review_source"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(kb.get_run(conn, source.id))
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_md), review.id))
    conn.execute("UPDATE tasks SET body=? WHERE id IN (?,?)", (_body(thread="another"), source.task_id, recovery))
    conn.commit()
    with pytest.raises(ValueError, match="same Topic"):
        kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    conn.execute("UPDATE tasks SET body=? WHERE id=?", (_body(), recovery))
    conn.commit()
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report})


def test_historical_callback_outcome_uses_accepted_source_not_current_ledger(history):
    conn, recovery, report = history
    assert kb.complete_task(conn, recovery, metadata={"user_facing_report": report})
    stored = kb.latest_run(conn, recovery).metadata["user_facing_report"]
    review = kb.create_task(conn, title="Review accepted historical delivery", parents=[recovery])
    kb.add_grace_loop_callback(
        conn, review_task_id=review, execution_task_id=recovery,
        platform="telegram", chat_id="chat-1", thread_id="2",
        session_key="session", session_id="session", contract_fingerprint="callback",
    )
    conn.execute("""INSERT INTO grace_delegations (
        delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,
        session_key,session_id,resolved_route,approval_required,state,
        execution_task_id,review_task_id,created_at,updated_at
        ) VALUES ('gd-callback','callback','callback','telegram','chat-1','2','session','session',
        '{"project":"secondhand"}',0,'queued',?,?,?,?)""", (recovery, review, int(time.time()), int(time.time())))
    conn.commit()
    assert kb.complete_task(conn, review, metadata={"review_outcome": "accepted"})
    callback = next(item for item in kb.list_due_grace_loop_callbacks(conn) if item["review_task_id"] == review)
    event = callback["event_id"]
    assert kb.claim_grace_loop_callback(conn, review_task_id=review, event_id=event, lease_owner="owner")
    params = dict(review_task_id=review, event_id=event, platform="telegram", chat_id="chat-1",
                  thread_id="2", session_id="session", lease_owner="owner", report=stored)
    kb.reserve_grace_user_facing_report_chunk(conn, **params, chunk_index=0, total_chunks=1)
    kb.confirm_grace_user_facing_report_chunk(conn, review_task_id=review, event_id=event,
                                             report=stored, chunk_index=0, total_chunks=1, message_id="sent")
    kb.record_grace_user_facing_report_delivery(conn, **params, chunk_count=1, chunk_index=0)
    before = _ledger(conn)
    outcome_params = {key: value for key, value in params.items() if key != "report"}
    with pytest.raises(ValueError, match="Incomplete user-facing report"):
        kb.record_grace_loop_callback_outcome(conn, **outcome_params, outcome_kind="closed", payload={"summary": "Must not close"})
    receipt = kb.record_grace_loop_callback_outcome(
        conn, **outcome_params, outcome_kind="terminal_blocked",
        payload={"summary": "Historical report delivered", "reason": "Original objective remains incomplete"},
    )
    assert receipt["outcome_kind"] == "terminal_blocked"
    assert _ledger(conn) == before
