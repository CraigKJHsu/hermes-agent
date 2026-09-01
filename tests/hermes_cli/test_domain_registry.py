from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import render_execution_body, render_review_body
from proactive.loop_contract import validate_loop_contract


def _contract() -> dict:
    return validate_loop_contract({
        "identity": {
            "project": "SoloBizAi",
            "topic_name": "AI BizWeek",
            "thread_id": "4641",
            "request_instance_id": "domain-registry-test",
        },
        "original_request": "發布 Carter's Junk Away 案例",
        "grace_interpretation": "發布並保存案例與各媒體狀態",
        "trigger": "KJ 要求發布",
        "completion_mode": "terminal",
        "goal": {
            "objective": "發布 Carter's Junk Away EP04",
            "deliverables": ["Facebook Page post"],
            "non_goals": ["不發布 Podcast"],
        },
        "scope": {
            "allowed": ["SoloBizAi Facebook Page"],
            "forbidden": ["其他 Facebook Page"],
        },
        "verification": {
            "checks": ["Graph API readback"],
            "evidence_required": ["post id and public URL"],
            "acceptance_criteria": ["published post matches source"],
        },
        "stop_rules": {
            "success": ["post verified"],
            "blocked": ["Page identity mismatch"],
            "no_progress": ["same error twice"],
            "max_iterations": 4,
            "max_runtime_seconds": 900,
        },
        "memory": {
            "namespace": "telegram:-1003938559457:4641/solobizai",
            "working": ["current publish evidence"],
            "promote_on_acceptance": ["Carter EP04 registry updated"],
        },
        "routing": {"task_type": "facebook_page_api_publish"},
        "domain_memory": {
            "schema_id": "solobizai.case.v1",
            "mode": "mutate",
            "expected_total": 1,
        },
    })


def _delta() -> dict:
    return {
        "operation": "upsert",
        "entity_id": "carters-junk-away-ep04",
        "label": "Carter's Junk Away",
        "status": "published",
        "attributes": {"episode_number": "EP04"},
        "artifacts": [
            {
                "artifact_type": "facebook_page_post",
                "platform": "facebook",
                "status": "published",
                "external_id": "123_456",
                "public_url": "https://www.facebook.com/123/posts/456",
                "verified_at": "2026-08-29T12:00:00+08:00",
                "evidence_ref": "task_external_effect:facebook:create",
            },
            {
                "artifact_type": "podcast_episode",
                "platform": "podcast",
                "artifact_key": "podcast_episode:ep04",
                "status": "not_published",
            },
            {
                "artifact_type": "audio_brief",
                "platform": "internal",
                "artifact_key": "audio_brief:ep04",
                "status": "not_published",
            },
        ],
        "evidence_refs": ["task_external_effect:facebook:create"],
    }


def _disable_model_receipt_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from proactive import model_routing

    monkeypatch.setattr(
        model_routing,
        "execution_receipt_from_env",
        lambda _raw: {"test": True},
    )
    monkeypatch.setattr(
        model_routing,
        "validate_grace_acceptance_receipt",
        lambda *_args, **_kwargs: None,
    )


def test_only_accepted_review_projects_execution_delta(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    _disable_model_receipt_gate(monkeypatch)
    db_path = tmp_path / "kanban.db"
    contract = _contract()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Publish Carter EP04",
            body=render_execution_body(contract),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="published and verified",
            metadata={
                "policy_receipts": [],
                "external_effects": [
                    {
                        "platform": "facebook",
                        "effect_key": "create",
                        "state": "created",
                        "external_id": "123_456",
                        "details": {
                            "public_url": "https://www.facebook.com/123/posts/456"
                        },
                    }
                ],
                "domain_memory_deltas": [_delta()],
            },
        )
        assert conn.execute("SELECT COUNT(*) FROM domain_entities").fetchone()[0] == 0

        review_id = kb.create_task(
            conn,
            title="Grace review Carter EP04",
            body=render_review_body(contract, execution_id),
            parents=(execution_id,),
            executor_profile="grace-policy-review",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted", "policy_receipts": []},
        )

        report = kb.domain_inventory_report(
            conn,
            domain_key="solobizai",
            entity_type="SoloBizAiCase",
        )
        assert report["registry_total"] == 1
        assert report["complete"] is True
        assert report["coverage_status"] == "verified_complete"
        assert report["expected_total"] == 1
        assert report["expected_total_source"] == "certified_registry_baseline"
        assert len(report["entities"][0]["artifacts"]) == 3
        assert (
            report["entities"][0]["artifacts"][1]["evidence_ref"]
            == "task_external_effect:facebook:create"
        )
        aggregate_report = kb.domain_inventory_report(
            conn,
            domain_key="solobizai",
        )
        assert aggregate_report["expected_total"] == 1
        assert aggregate_report["complete"] is True
        assert (
            conn.execute("SELECT COUNT(*) FROM domain_entity_events").fetchone()[0] == 1
        )
        event = conn.execute(
            "SELECT delta, accepted_review_task_id FROM domain_entity_events"
        ).fetchone()
        assert json.loads(event["delta"])["entity_id"] == "carters-junk-away-ep04"
        assert event["accepted_review_task_id"] == review_id


def test_inventory_without_certified_baseline_reports_unknown(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        report = kb.domain_inventory_report(
            conn,
            domain_key="solobizai",
            entity_type="SoloBizAiCase",
        )
    assert report["registry_total"] == 0
    assert report["expected_total"] is None
    assert report["count_matches"] is None
    assert report["coverage_status"] == "unknown_expected_total"


def test_mutation_completion_rejects_missing_delta(tmp_path):
    db_path = tmp_path / "kanban.db"
    contract = _contract()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Publish without delta",
            body=render_execution_body(contract),
        )
        with pytest.raises(ValueError, match="requires at least one"):
            kb.complete_task(
                conn,
                execution_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [
                        {
                            "platform": "facebook",
                            "effect_key": "create",
                            "state": "created",
                            "external_id": "123_456",
                        }
                    ],
                },
            )
        assert kb.get_task(conn, execution_id).status != "done"
