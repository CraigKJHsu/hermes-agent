"""Native Hermes evidence binding must reach the same deterministic delivery reader."""

import json
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def completed(tmp_path):
    with kb.connect_closing(tmp_path / "native.db") as conn:
        contract = {
            "user_facing_delivery": {
                "required": True,
                "kind": "content_package",
                "delivery": "inline_only",
                "body_field": "domain_inventory_report",
            }
        }
        task = kb.create_task(
            conn,
            title="完整繁體中文報告",
            body="GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
            + json.dumps(contract)
            + "\n```",
        )
        body = "標題與包裝層已翻譯。\n\n6. Original section\nEXACT ORIGINAL SOURCE"
        assert kb.complete_task(
            conn,
            task,
            summary="native report",
            metadata={
                "domain_inventory_report": body,
                "external_effects": [],
                "user_facing_report": {
                    "kind": "content_package",
                    "delivery": "inline_only",
                    "complete": True,
                    "title": "完整繁體中文報告",
                    "body_field": "domain_inventory_report",
                    "body": body,
                    "assets": [],
                    "observed_at": int(time.time()),
                },
            },
        )
        yield conn, task, body


def test_native_completed_report_is_contract_bound_without_rewriting(completed):
    conn, task, body = completed
    report = kb.grace_inline_content_package_report(conn, task)
    assert report is not None and report["body"] == body
    assert report["complete"] is True
    assert report["body_field"] == "domain_inventory_report"
    assert not report["assets"]


@pytest.mark.parametrize(
    "fault",
    [
        "openclaw",
        "blocked",
        "missing",
        "wrong_type",
        "conflicting_acceptance",
        "conflicting_string",
    ],
)
def test_native_fallback_does_not_relax_other_provenance(completed, fault):
    conn, task, body = completed
    run = kb.latest_run(conn, task)
    metadata = dict(run.metadata)
    if fault == "openclaw":
        conn.execute(
            "UPDATE task_runs SET executor_backend='openclaw' WHERE id=?", (run.id,)
        )
    elif fault == "blocked":
        conn.execute(
            "UPDATE task_runs SET status='blocked',outcome='blocked' WHERE id=?",
            (run.id,),
        )
    elif fault == "missing":
        metadata.pop("domain_inventory_report")
    elif fault == "wrong_type":
        metadata["domain_inventory_report"] = {"body": body}
    elif fault == "conflicting_acceptance":
        metadata["acceptance_evidence"] = {"domain_inventory_report": False}
    elif fault == "conflicting_string":
        metadata["acceptance_evidence"] = {"domain_inventory_report": "Wrong source"}
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id)
    )
    conn.commit()
    assert kb.grace_inline_content_package_report(conn, task) is None


def test_existing_native_acceptance_evidence_remains_supported(completed):
    conn, task, body = completed
    run = kb.latest_run(conn, task)
    metadata = dict(run.metadata)
    metadata.pop("domain_inventory_report")
    metadata["acceptance_evidence"] = {"domain_inventory_report": body}
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id)
    )
    conn.commit()
    report = kb.grace_inline_content_package_report(conn, task)
    assert report is not None and report["body"] == body
