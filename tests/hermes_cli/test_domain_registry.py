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


def _seed_dpr_registry(conn, *, objective_id: str) -> None:
    now = 1_789_486_000
    conn.execute(
        """
        INSERT INTO domain_entities (
            domain_key, entity_type, entity_id, label, status, attributes,
            schema_id, source_task_id, source_run_id,
            accepted_review_task_id, accepted_review_run_id,
            observed_at, created_at, updated_at
        ) VALUES ('solobizai','SoloBizAiCase','dpr-construction',
                  'DPR Construction','planned',?, 'solobizai.case.v1',
                  't_seed',99,'t_seed_review',100,?,?,?)
        """,
        (json.dumps({"episode_number": "EP09"}), now, now, now),
    )
    rows = [
        (
            "audio_brief", "internal", "audio-brief", "reserved", None, None,
            {"episode_number": "EP09"}, None,
            "domain_episode_reservation:der_11111111111111111111111111111111",
        ),
        (
            "facebook_page_post", "facebook", "facebook-page-post", "draft",
            None, None, {}, None, "",
        ),
        (
            "podcast_episode", "podcast", "podcast-episode", "planned",
            None, None, {"episode_number": "EP09"}, None, "",
        ),
    ]
    for artifact_type, platform, key, status, url, external_id, attrs, verified_at, ref in rows:
        conn.execute(
            """
            INSERT INTO domain_artifacts (
                domain_key, entity_type, entity_id, artifact_type, platform,
                artifact_key, status, public_url, external_id, attributes,
                verified_at, source_task_id, source_run_id,
                accepted_review_task_id, accepted_review_run_id,
                observed_at, created_at, updated_at, evidence_ref
            ) VALUES ('solobizai','SoloBizAiCase','dpr-construction',?,?,?,?,
                      ?,?,?,?, 't_seed',99,'t_seed_review',100,?,?,?,?)
            """,
            (
                artifact_type, platform, key, status, url, external_id,
                json.dumps(attrs), verified_at, now, now, now, ref,
            ),
        )
    conn.execute(
        """
        INSERT INTO domain_episode_reservations (
            reservation_id, receipt_version, domain_key, schema_id,
            entity_type, artifact_type, episode_number, episode_label,
            subject_entity_id, subject_label, objective_id,
            source_task_id, source_run_id, authorization_fingerprint,
            evidence, status, created_at, updated_at
        ) VALUES ('der_11111111111111111111111111111111','v2','solobizai',
                  'solobizai.case.v1','SoloBizAiCase','audio_brief',9,'EP09',
                  'dpr-construction','DPR Construction',?,'t_seed',99,?,
                  '{}','consumed',?,?)
        """,
        (objective_id, "a" * 64, now, now),
    )


def _verified_page_effect(*, state: str = "verified") -> dict:
    return {
        "platform": "facebook",
        "effect_key": "create",
        "state": state,
        "external_id": "531289396730654_122182697174694189",
        "details": {
            "verified": True,
            "published": True,
            "post_id": "531289396730654_122182697174694189",
            "photo_id": "122182697156694189",
            "permalink_url": (
                "https://www.facebook.com/122180328530694189/"
                "posts/122182697174694189"
            ),
            "created_time": "2026-09-15T15:31:44+0000",
            "message_sha256": "d" * 64,
            "image_sha256": "b" * 64,
        },
    }


def _incomplete_page_delta(*, external_id: str | None = None) -> dict:
    effect = _verified_page_effect()
    return {
        "operation": "upsert",
        "entity_id": "dpr-construction",
        "label": "DPR Construction／EP09",
        "status": "published",
        "artifacts": [{
            "artifact_type": "facebook_page_post",
            "platform": "facebook",
            "status": "published",
            "external_id": external_id or effect["external_id"],
            "public_url": effect["details"]["permalink_url"],
            "evidence_ref": "task_external_effect:facebook:create",
        }],
        "evidence_refs": ["task_external_effect:facebook:create"],
    }


@pytest.mark.parametrize("effect_state", ["verified", "existing"])
def test_verified_page_completion_recovers_full_registry_snapshot(
    tmp_path,
    effect_state,
    monkeypatch: pytest.MonkeyPatch,
):
    _disable_model_receipt_gate(monkeypatch)
    contract = _contract()
    contract["objective_ref"] = {
        "objective_id": "go_domain_recovery_test",
        "stage_key": "execute_external_action",
    }
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_dpr_registry(conn, objective_id="go_domain_recovery_test")
        task_id = kb.create_task(
            conn,
            title="Publish DPR Page",
            body=render_execution_body(contract),
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        effect = _verified_page_effect(state=effect_state)
        kb.record_external_effect(
            conn,
            task_id,
            expected_run_id=claimed.current_run_id,
            **effect,
        )

        assert kb.complete_task(
            conn,
            task_id,
            summary="published and controller-reconciled",
            expected_run_id=claimed.current_run_id,
            metadata={
                "policy_receipts": [],
                "external_effects": [effect],
                "domain_memory_deltas": [_incomplete_page_delta()],
            },
        )
        run = kb.latest_run(conn, task_id)
        assert run is not None
        recovered = run.metadata["domain_memory_deltas"][0]
        assert {item["artifact_type"] for item in recovered["artifacts"]} == {
            "facebook_page_post", "podcast_episode", "audio_brief",
        }
        page = next(
            item for item in recovered["artifacts"]
            if item["artifact_type"] == "facebook_page_post"
        )
        assert page["verified_at"] == "2026-09-15T15:31:44+00:00"
        assert page["attributes"]["source_run_id"] == claimed.current_run_id
        assert run.metadata["controller_domain_reconciliation"] == {
            "source": "verified_page_effect_and_registry",
            "source_task_id": task_id,
            "source_run_id": claimed.current_run_id,
            "source_effect_key": "create",
            "source_effect_state": effect_state,
            "external_id": effect["external_id"],
            "read_only": True,
        }
        reservation = conn.execute(
            "SELECT status FROM domain_episode_reservations"
        ).fetchone()
        assert reservation["status"] == "consumed"
        review_id = kb.create_task(
            conn,
            title="Grace review DPR reconciliation",
            body=render_review_body(contract, task_id),
            parents=(task_id,),
            executor_profile="grace-policy-review",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted", "policy_receipts": []},
        )
        audio = conn.execute(
            """
            SELECT source_task_id, source_run_id, accepted_review_task_id,
                   accepted_review_run_id, evidence_ref
              FROM domain_artifacts
             WHERE entity_id='dpr-construction' AND artifact_type='audio_brief'
            """
        ).fetchone()
        assert dict(audio) == {
            "source_task_id": "t_seed",
            "source_run_id": 99,
            "accepted_review_task_id": "t_seed_review",
            "accepted_review_run_id": 100,
            "evidence_ref": (
                "domain_episode_reservation:"
                "der_11111111111111111111111111111111"
            ),
        }


def test_verified_page_completion_recovery_rejects_conflicting_post(tmp_path):
    contract = _contract()
    contract["objective_ref"] = {
        "objective_id": "go_domain_recovery_conflict",
        "stage_key": "execute_external_action",
    }
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_dpr_registry(conn, objective_id="go_domain_recovery_conflict")
        task_id = kb.create_task(
            conn,
            title="Publish DPR Page",
            body=render_execution_body(contract),
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        effect = _verified_page_effect()
        kb.record_external_effect(
            conn,
            task_id,
            expected_run_id=claimed.current_run_id,
            **effect,
        )

        with pytest.raises(ValueError, match="external_id conflicts"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=claimed.current_run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [effect],
                    "domain_memory_deltas": [
                        _incomplete_page_delta(external_id="different-post")
                    ],
                },
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_verified_page_completion_rejects_nested_readback_without_photo_id(
    tmp_path,
):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id, run_id, effect = _claimed_dpr_recovery_task(
            conn,
            objective_id="go_domain_missing_photo",
        )
        details = dict(effect["details"])
        details.pop("photo_id")
        effect_without_photo = {**effect, "details": details}
        conn.execute(
            "UPDATE task_external_effects SET details=? WHERE task_id=?",
            (json.dumps(details), task_id),
        )
        nested_report = {
            **effect_without_photo,
            "details": {"readback": dict(details)},
        }

        with pytest.raises(ValueError, match="readback is incomplete"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [nested_report],
                    "domain_memory_deltas": [_incomplete_page_delta()],
                },
            )
        assert kb.get_task(conn, task_id).status == "running"


def _claimed_dpr_recovery_task(conn, *, objective_id: str):
    contract = _contract()
    contract["objective_ref"] = {
        "objective_id": objective_id,
        "stage_key": "execute_external_action",
    }
    _seed_dpr_registry(conn, objective_id=objective_id)
    task_id = kb.create_task(
        conn,
        title="Publish DPR Page",
        body=render_execution_body(contract),
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    effect = _verified_page_effect()
    kb.record_external_effect(
        conn,
        task_id,
        expected_run_id=claimed.current_run_id,
        **effect,
    )
    return task_id, claimed.current_run_id, effect


def test_verified_page_completion_rejects_bound_registry_page_conflict(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id, run_id, effect = _claimed_dpr_recovery_task(
            conn,
            objective_id="go_domain_registry_page_conflict",
        )
        conn.execute(
            """
            UPDATE domain_artifacts
               SET status='published', external_id='different-post',
                   public_url='https://www.facebook.com/different-post'
             WHERE entity_id='dpr-construction'
               AND artifact_type='facebook_page_post'
            """
        )

        with pytest.raises(ValueError, match="registry external_id conflicts"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [effect],
                    "domain_memory_deltas": [_incomplete_page_delta()],
                },
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_verified_page_completion_rejects_supplied_registry_provenance_conflict(
    tmp_path,
):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id, run_id, effect = _claimed_dpr_recovery_task(
            conn,
            objective_id="go_domain_registry_provenance_conflict",
        )
        raw_delta = _incomplete_page_delta()
        raw_delta["artifacts"].append({
            "artifact_type": "audio_brief",
            "platform": "internal",
            "artifact_key": "audio-brief",
            "status": "reserved",
            "attributes": {"episode_number": "EP10"},
            "evidence_ref": (
                "domain_episode_reservation:"
                "der_11111111111111111111111111111111"
            ),
        })

        with pytest.raises(ValueError, match=r"audio_brief\.attributes conflicts"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [effect],
                    "domain_memory_deltas": [raw_delta],
                },
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_consumed_reservation_preservation_requires_controller_recovery(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id, run_id, effect = _claimed_dpr_recovery_task(
            conn,
            objective_id="go_domain_reservation_scope",
        )
        recovered = kb.recover_verified_page_domain_deltas(
            conn,
            task_id=task_id,
            run_id=run_id,
            raw_deltas=[_incomplete_page_delta()],
            domain_spec=kb.grace_domain_memory_spec(
                render_execution_body(_contract())
            ),
            reported_external_effects=[effect],
        )
        assert recovered is not None

        with pytest.raises(ValueError, match="does not match its reservation receipt"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [effect],
                    "domain_memory_deltas": recovered["deltas"],
                },
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_verified_page_completion_revalidates_recovery_inside_write_transaction(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id, run_id, effect = _claimed_dpr_recovery_task(
            conn,
            objective_id="go_domain_recovery_revalidation",
        )
        original = kb.recover_verified_page_domain_deltas
        calls = 0

        def race_after_admission(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("controller Page recovery evidence changed")
            return original(*args, **kwargs)

        monkeypatch.setattr(
            kb,
            "recover_verified_page_domain_deltas",
            race_after_admission,
        )
        with pytest.raises(ValueError, match="evidence changed"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "policy_receipts": [],
                    "external_effects": [effect],
                    "domain_memory_deltas": [_incomplete_page_delta()],
                },
            )
        assert calls == 2
        assert kb.get_task(conn, task_id).status == "running"
