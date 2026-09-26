from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from proactive.domain_memory import validate_delta_external_effect_refs


LANE = {
    "platform": "telegram",
    "chat_id": "-1003938559457",
    "thread_id": "4641",
    "project": "telegram_1003938559457_4641_bff429b6e587",
}


def _body(*, stage: str, contract: dict) -> str:
    return (
        f"GRACE_LOOP_CONTRACT_STAGE: {stage}\n"
        "```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )


def _source_pair(
    conn,
    *,
    episode: str,
    subject: str,
) -> dict:
    artifact_digest = hashlib.sha256(f"{subject}:{episode}".encode()).hexdigest()
    artifact_filename = (
        f"{subject.replace(' ', '_')}_{episode}_audio_brief.png"
    )
    contract = {"identity": dict(LANE)}
    execution_id = kb.create_task(
        conn,
        title=f"Prepare {subject} {episode}",
        body=_body(stage="execution", contract=contract),
        assignee="clawops-ops",
        project_namespace=LANE["project"],
    )
    review_id = kb.create_task(
        conn,
        title=f"Grace review {subject} {episode}",
        body=_body(stage="grace_review", contract=contract),
        assignee="default",
        parents=(execution_id,),
        project_namespace=LANE["project"],
    )
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, state, execution_task_id, review_task_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', ?, ?, ?, ?, '{}', 'queued', ?, ?, ?, ?)
        """,
        (
            f"gd_{execution_id}",
            f"fp_{execution_id}",
            f"gri_{execution_id}",
            LANE["chat_id"],
            LANE["thread_id"],
            "agent:main:telegram:group:-1003938559457:4641",
            f"session_{execution_id}",
            execution_id,
            review_id,
            now,
            now,
        ),
    )
    execution_metadata = json.dumps(
        {
            "acceptance_evidence": {
                "episode": episode,
                "subject": subject,
                "asset_family": "audio_brief",
                "artifact": {
                    "filename": artifact_filename,
                    "sha256": artifact_digest,
                },
            },
            "user_facing_report": {"title": f"{subject}／{episode}"},
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            task_id, status, started_at, ended_at, outcome, summary, metadata
        ) VALUES (?, 'done', ?, ?, 'completed', ?, ?)
        """,
        (execution_id, now, now, f"Prepared {subject} {episode}", execution_metadata),
    )
    execution_run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    review_metadata = json.dumps(
        {
            "review_outcome": "accepted",
            "workflow_review_source": {
                "parent_execution_task_id": execution_id,
                "parent_execution_run_id": execution_run_id,
            },
            "acceptance_evidence": {
                "episode": episode,
                "subject": subject,
                "asset_family": "audio_brief",
                "artifact": {
                    "filename": artifact_filename,
                    "sha256": artifact_digest,
                },
            },
            "user_facing_report": {"title": f"{subject}／{episode} accepted"},
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            task_id, status, started_at, ended_at, outcome, summary, metadata
        ) VALUES (?, 'done', ?, ?, 'completed', ?, ?)
        """,
        (review_id, now, now, f"Accepted {subject} {episode}", review_metadata),
    )
    review_run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute("UPDATE tasks SET status='done' WHERE id IN (?, ?)", (execution_id, review_id))
    return {
        "artifact_receipt": {
            "version": "v2",
            "schema_id": "solobizai.case.v1",
            "domain_key": "solobizai",
            "entity_type": "SoloBizAiCase",
            "artifact_type": "audio_brief",
            "subject_entity_id": subject.casefold().replace(" ", "-"),
            "subject_label": subject,
            "episode_label": episode,
            "sha256": artifact_digest,
            "execution_record": {
                "json_pointer": "/acceptance_evidence/artifact",
                "locator_field": "filename",
                "locator": artifact_filename,
            },
            "review_record": {
                "json_pointer": "/acceptance_evidence/artifact",
                "locator_field": "filename",
                "locator": artifact_filename,
            },
        },
        "execution_task_id": execution_id,
        "execution_run_id": execution_run_id,
        "review_task_id": review_id,
        "review_run_id": review_run_id,
    }


def _seed_registered_episodes(conn, episodes: tuple[int, ...]) -> None:
    now = int(time.time())
    for number in episodes:
        entity_id = f"case-ep{number:02d}"
        conn.execute(
            """
            INSERT INTO domain_entities (
                domain_key, entity_type, entity_id, label, status, attributes,
                schema_id, source_task_id, source_run_id,
                accepted_review_task_id, accepted_review_run_id,
                observed_at, created_at, updated_at
            ) VALUES (
                'solobizai', 'SoloBizAiCase', ?, ?, 'published', ?,
                'solobizai.case.v1', 'source-task', 1,
                'source-review', 2, ?, ?, ?
            )
            """,
            (
                entity_id,
                f"Case EP{number:02d}",
                json.dumps({"episode_number": f"EP{number:02d}"}),
                now,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO domain_artifacts (
                domain_key, entity_type, entity_id, artifact_type, platform,
                artifact_key, status, external_id, attributes, evidence_ref,
                source_task_id, source_run_id, accepted_review_task_id,
                accepted_review_run_id, observed_at, created_at, updated_at
            ) VALUES (
                'solobizai', 'SoloBizAiCase', ?, 'audio_brief', 'internal',
                ?, 'unknown', ?, ?, '', 'source-task', 1,
                'source-review', 2, ?, ?, ?
            )
            """,
            (
                entity_id,
                f"audio-brief-ep{number:02d}",
                f"EP{number:02d}",
                json.dumps({"episode_number": f"EP{number:02d}"}),
                now,
                now,
                now,
            ),
        )


def _target(
    conn,
    sources: list[dict],
    *,
    stage_key: str = "prepare",
    live_shaped_zero_effect_contract: bool = False,
) -> tuple[str, int]:
    authorization = {
        "schema_id": "solobizai.case.v1",
        "domain_key": "solobizai",
        "entity_type": "SoloBizAiCase",
        "artifact_type": "audio_brief",
        "subject_entity_id": "dpr-construction",
        "subject_label": "DPR Construction",
        "objective_id": "go_dpr",
        "sequence_strategy": "next_after_contiguous_occupancy",
        "accepted_occupancy_sources": sources,
    }
    contract = {
        "identity": dict(LANE),
        "objective_ref": {"objective_id": "go_dpr", "stage_key": stage_key},
        "domain_memory": {
            "schema_id": "solobizai.case.v1",
            "domain_key": "solobizai",
            "entity_type": "SoloBizAiCase",
            "mode": "mutate",
        },
        "memory": {
            "working": [
                "HERMES_DOMAIN_EPISODE_RESERVATION_V2:"
                + json.dumps(authorization, ensure_ascii=False, sort_keys=True)
            ]
        },
    }
    if not live_shaped_zero_effect_contract:
        contract["external_effect_budget"] = 0
    task_id = kb.create_task(
        conn,
        title="Prepare DPR package",
        body=_body(stage="execution", contract=contract),
        assignee="clawops-ops",
        project_namespace=LANE["project"],
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return task_id, claimed.current_run_id


def _reserved_deltas(reservation: dict) -> list[dict]:
    episode = reservation["episode_label"]
    evidence_ref = "domain_episode_reservation:" + reservation["reservation_id"]
    return [
        {
            "operation": "upsert",
            "entity_id": "dpr-construction",
            "label": "DPR Construction",
            "status": "planned",
            "attributes": {"episode_number": episode},
            "artifacts": [
                {
                    "artifact_type": "facebook_page_post",
                    "artifact_key": "facebook-page-post",
                    "status": "draft",
                },
                {
                    "artifact_type": "podcast_episode",
                    "artifact_key": "podcast-episode",
                    "status": "planned",
                },
                {
                    "artifact_type": "audio_brief",
                    "artifact_key": "audio-brief",
                    "status": "reserved",
                    "attributes": {"episode_number": episode},
                    "evidence_ref": evidence_ref,
                },
            ],
            "evidence_refs": [evidence_ref],
        }
    ]


@pytest.mark.parametrize(
    ("contract", "expected"),
    [
        ({}, True),
        ({"external_effect_budget": 0}, True),
        ({"external_targets": []}, True),
        ({"external_effect_budget": 0, "external_targets": []}, True),
        ({"external_effect_budget": 1}, False),
        ({"external_effect_budget": None}, False),
        ({"external_targets": None}, False),
        ({"external_targets": [{"url": "https://example.invalid"}]}, False),
        ({"external_effect_budget": 0, "external_targets": ["facebook"]}, False),
        ({"external_effect_budget": 1, "external_targets": []}, False),
    ],
)
def test_successor_rebind_zero_effect_contract_is_fail_closed(contract, expected):
    assert kb._domain_episode_contract_is_zero_effect(contract) is expected


def test_reserves_ep09_from_registry_plus_pinned_accepted_pairs(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        task_id, run_id = _target(conn, sources)

        first = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )
        replay = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )
        inventory = kb.domain_inventory_report(
            conn, domain_key="solobizai", entity_type="SoloBizAiCase"
        )

    assert first["episode_label"] == "EP09"
    assert first["status"] == "reserved"
    assert first["readback_verified"] is True
    assert first["idempotent_replay"] is False
    assert first["evidence"]["occupied_episode_numbers"] == list(range(1, 9))
    assert replay["reservation_id"] == first["reservation_id"]
    assert replay["idempotent_replay"] is True
    assert inventory["registry_total"] == 6
    assert inventory["episode_reservations"][0]["episode_label"] == "EP09"


def test_reserved_delta_requires_and_consumes_exact_receipt(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        task_id, run_id = _target(conn, sources)
        reservation = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )
        deltas = _reserved_deltas(reservation)
        forged = json.loads(json.dumps(deltas))
        forged[0]["artifacts"][2]["evidence_ref"] = (
            "domain_episode_reservation:der_" + "0" * 32
        )
        forged[0]["evidence_refs"] = [forged[0]["artifacts"][2]["evidence_ref"]]
        with pytest.raises(ValueError, match="reservation is missing"):
            kb.complete_task(
                conn,
                task_id,
                summary="forged",
                metadata={"domain_memory_deltas": forged},
                expected_run_id=run_id,
            )
        assert kb.complete_task(
            conn,
            task_id,
            summary="prepared",
            metadata={"domain_memory_deltas": deltas},
            expected_run_id=run_id,
        )
        assert conn.execute(
            "SELECT status FROM domain_episode_reservations WHERE reservation_id=?",
            (reservation["reservation_id"],),
        ).fetchone()[0] == "reserved"
        normalized_deltas = kb.get_run(conn, run_id).metadata["domain_memory_deltas"]
        kb._apply_domain_projection(
            conn,
            projection={
                "spec": kb.grace_domain_memory_spec(kb.get_task(conn, task_id).body),
                "deltas": normalized_deltas,
                "source_task_id": task_id,
                "source_run_id": run_id,
                "objective_id": "go_dpr",
            },
            accepted_review_task_id="review-task",
            accepted_review_run_id=999,
            now=int(time.time()),
        )
        assert conn.execute(
            "SELECT status FROM domain_episode_reservations WHERE reservation_id=?",
            (reservation["reservation_id"],),
        ).fetchone()[0] == "consumed"


def test_unconsumed_reservation_rebinds_to_retry_run(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        task_id, first_run_id = _target(conn, sources)
        first = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=first_run_id
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="retry",
            kind="capability",
            expected_run_id=first_run_id,
        )
        assert kb.unblock_task(conn, task_id)
        retried = kb.claim_task(conn, task_id, claimer="retry-worker")
        second = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=retried.current_run_id
        )

    assert second["reservation_id"] == first["reservation_id"]
    assert second["source_run_id"] == retried.current_run_id
    assert second["evidence"]["rebindings"] == [
        {
            "source_task_id": task_id,
            "source_run_id": first_run_id,
            "rebound_at": second["updated_at"],
        }
    ]


def test_unconsumed_reservation_rebinds_after_same_task_timeout(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        task_id, first_run_id = _target(conn, sources)
        first = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=first_run_id
        )
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1, worker_pid=999999 "
            "WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (int(time.time()) - 2, first_run_id),
        )
        assert kb.enforce_max_runtime(
            conn, signal_fn=lambda _pid, _sig: None
        ) == [task_id]
        retried = kb.claim_task(conn, task_id, claimer="retry-worker")
        second = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=retried.current_run_id
        )

    assert second["reservation_id"] == first["reservation_id"]
    assert second["source_run_id"] == retried.current_run_id
    assert second["evidence"]["rebindings"][-1]["source_run_id"] == first_run_id


@pytest.mark.parametrize("callback_matches_execution", [True, False])
@pytest.mark.parametrize("successor_stage", ["prepare_r2", "prepare_r10"])
@pytest.mark.parametrize("reservation_run_outcome", ["crashed", "timed_out"])
def test_unconsumed_reservation_rebinds_to_terminal_objective_successor(
    tmp_path, callback_matches_execution, successor_stage,
    reservation_run_outcome,
):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        kb.create_grace_objective(
            conn,
            objective_id="go_dpr",
            platform="telegram",
            chat_id=LANE["chat_id"],
            thread_id=LANE["thread_id"],
            session_key="fixture",
            title="DPR package",
            objective="Complete the DPR package",
            original_request_sha256="a" * 64,
            required_stage_keys=["prepare", "publish"],
            terminal_stage_key="publish",
            acceptance_criteria=["verified"],
        )
        first_task_id, first_run_id = _target(
            conn, sources, live_shaped_zero_effect_contract=True,
        )
        first = kb.reserve_next_domain_episode(
            conn, task_id=first_task_id, expected_run_id=first_run_id
        )
        first_review_id = kb.create_task(
            conn,
            title="First review",
            body=_body(stage="grace_review", contract={}),
            parents=(first_task_id,),
        )
        now = int(time.time())
        conn.execute(
            """INSERT INTO grace_delegations (
                   delegation_id,contract_fingerprint,platform,chat_id,thread_id,
                   session_key,session_id,resolved_route,state,execution_task_id,
                   review_task_id,created_at,updated_at,objective_id,stage_key,
                   request_instance_id
               ) VALUES (
                   'gd-first','fp-first','telegram',?,?,'fixture','session','{}',
                   'queued',?,?,?,?,'go_dpr','prepare','request-first'
               )""",
            (
                LANE["chat_id"], LANE["thread_id"], first_task_id,
                first_review_id, now, now,
            ),
        )
        assert kb.block_task(
            conn, first_task_id, reason="kernel migrated", kind="capability",
            expected_run_id=first_run_id,
        )
        conn.execute(
            "UPDATE task_runs SET status=?,outcome=? WHERE id=?",
            (reservation_run_outcome, reservation_run_outcome, first_run_id),
        )
        if reservation_run_outcome == "crashed":
            conn.execute(
                "INSERT INTO task_events(task_id,run_id,kind,created_at) "
                "VALUES (?,?,'protocol_violation',?)",
                (first_task_id, first_run_id, now),
            )
        conn.execute(
            """INSERT INTO task_events(task_id,kind,payload,created_at)
               VALUES (?,'gave_up',?,?)""",
            (
                first_task_id,
                json.dumps({"trigger_outcome": "crashed", "limit_source": "forced"}),
                now,
            ),
        )
        callback_event_id = int(
            conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        )
        conn.execute(
            """UPDATE grace_objective_stages
                  SET status='done',delegation_id='gd-first',execution_task_id=?,
                      review_task_id=?,outcome_kind='intermediate_blocked'
                WHERE objective_id='go_dpr' AND stage_key='prepare'""",
            (first_task_id, first_review_id),
        )
        conn.execute(
            """INSERT INTO grace_loop_callbacks (
                   review_task_id,execution_task_id,platform,chat_id,thread_id,
                   contract_fingerprint,state,created_at,objective_id,stage_key,
                   outcome_event_id,outcome_kind
               ) VALUES (
                   ?,?,'telegram',?,?,'callback','delivered',?,'go_dpr','prepare',?,
                   'intermediate_blocked'
               )""",
            (
                first_review_id,
                first_task_id if callback_matches_execution else "t_other",
                LANE["chat_id"], LANE["thread_id"], now, callback_event_id,
            ),
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_dpr", stage_key=successor_stage,
        )
        second_task_id, second_run_id = _target(
            conn,
            sources,
            stage_key=successor_stage,
            live_shaped_zero_effect_contract=True,
        )
        second_review_id = kb.create_task(
            conn,
            title="Successor review",
            body=_body(stage="grace_review", contract={}),
            parents=(second_task_id,),
        )
        conn.execute(
            """INSERT INTO grace_delegations (
                   delegation_id,contract_fingerprint,platform,chat_id,thread_id,
                   session_key,session_id,resolved_route,state,execution_task_id,
                   review_task_id,created_at,updated_at,objective_id,stage_key,
                   request_instance_id
               ) VALUES (
                   'gd-second','fp-second','telegram',?,?,'fixture','session','{}',
                   'queued',?,?,?,?,'go_dpr',?,'request-second'
               )""",
            (
                LANE["chat_id"], LANE["thread_id"], second_task_id,
                second_review_id, now, now, successor_stage,
            ),
        )
        conn.execute(
            """UPDATE grace_objective_stages
                  SET status='queued',delegation_id='gd-second',execution_task_id=?,
                      review_task_id=?
                WHERE objective_id='go_dpr' AND stage_key=?""",
            (second_task_id, second_review_id, successor_stage),
        )
        if callback_matches_execution:
            second = kb.reserve_next_domain_episode(
                conn, task_id=second_task_id, expected_run_id=second_run_id
            )
        else:
            with pytest.raises(ValueError, match="another owner"):
                kb.reserve_next_domain_episode(
                    conn, task_id=second_task_id, expected_run_id=second_run_id
                )
            return

    assert second["reservation_id"] == first["reservation_id"]
    assert second["source_task_id"] == second_task_id
    assert second["source_run_id"] == second_run_id
    assert second["evidence"]["rebindings"][-1]["source_task_id"] == first_task_id


def test_active_owner_cannot_have_reservation_stolen(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        first_task_id, first_run_id = _target(conn, sources)
        first = kb.reserve_next_domain_episode(
            conn, task_id=first_task_id, expected_run_id=first_run_id
        )
        second_task_id, second_run_id = _target(conn, sources)
        with pytest.raises(ValueError, match="another owner"):
            kb.reserve_next_domain_episode(
                conn, task_id=second_task_id, expected_run_id=second_run_id
            )
        row = conn.execute(
            "SELECT source_task_id, source_run_id "
            "FROM domain_episode_reservations WHERE reservation_id = ?",
            (first["reservation_id"],),
        ).fetchone()
        assert tuple(row) == (first_task_id, first_run_id)


def test_prior_reservations_are_scoped_by_entity_type(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO domain_episode_reservations (
                reservation_id, domain_key, schema_id, entity_type,
                artifact_type, episode_number, episode_label,
                subject_entity_id, subject_label, objective_id,
                source_task_id, source_run_id, authorization_fingerprint,
                evidence, status, created_at, updated_at
            ) VALUES (
                'der_11111111111111111111111111111111', 'solobizai',
                'other.case.v1', 'OtherCase', 'audio_brief', 9, 'EP09',
                'other-subject', 'Other Subject', 'go_other',
                'other-task', 1, 'other-fingerprint', '{}', 'reserved', ?, ?
            )
            """,
            (now, now),
        )
        sources = [
            _source_pair(conn, episode="EP06", subject="TurboHome"),
            _source_pair(conn, episode="EP07", subject="D Squared"),
        ]
        task_id, run_id = _target(conn, sources)
        reservation = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )

    assert reservation["episode_label"] == "EP09"


def test_refuses_gap_instead_of_guessing_next_episode(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        sources = [_source_pair(conn, episode="EP06", subject="TurboHome")]
        task_id, run_id = _target(conn, sources)
        with pytest.raises(ValueError, match="EP07"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM domain_episode_reservations"
        ).fetchone()[0] == 0


def test_same_label_with_distinct_entity_ids_is_a_conflict(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6a = _source_pair(conn, episode="EP06", subject="Same Company")
        source6b = _source_pair(conn, episode="EP06", subject="Same Company")
        source6b["artifact_receipt"]["subject_entity_id"] = "Same-Company"
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6a, source6b, source7])
        with pytest.raises(ValueError, match="conflicting accepted subjects"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_receipt_subject_id_is_trimmed_once_and_preserved(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source6["artifact_receipt"]["subject_entity_id"] = " turbohome "
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        reservation = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )

    receipts = reservation["evidence"]["accepted_occupancy_receipts"]
    assert receipts[0]["subject_entity_id"] == "turbohome"
    assert receipts[0]["artifact_receipt"]["subject_entity_id"] == "turbohome"


def test_refuses_non_string_stable_subject_id(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source6["artifact_receipt"]["subject_entity_id"] = 17
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="subject_entity_id is required"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_bad_pointer_escape(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source6["artifact_receipt"]["execution_record"]["json_pointer"] = (
            "/acceptance_evidence/~2artifact"
        )
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="execution_record is invalid"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_refuses_aliased_array_index_in_json_pointer(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        run = kb.get_run(conn, source6["execution_run_id"])
        artifact = run.metadata["acceptance_evidence"]["artifact"]
        metadata = dict(run.metadata)
        metadata["acceptance_evidence"] = {"items": [artifact]}
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), source6["execution_run_id"]),
        )
        source6["artifact_receipt"]["execution_record"]["json_pointer"] = (
            "/acceptance_evidence/items/00"
        )
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="pointer is missing"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_accepts_rfc6901_root_json_pointer(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        run = kb.get_run(conn, source6["execution_run_id"])
        artifact = run.metadata["acceptance_evidence"]["artifact"]
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(artifact), source6["execution_run_id"]),
        )
        source6["artifact_receipt"]["execution_record"]["json_pointer"] = ""
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        reservation = kb.reserve_next_domain_episode(
            conn, task_id=task_id, expected_run_id=run_id
        )

    assert reservation["episode_label"] == "EP09"


def test_refuses_non_string_json_pointer_instead_of_coercing_to_root(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source6["artifact_receipt"]["execution_record"]["json_pointer"] = None
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="execution_record is invalid"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_json_pointer_token_whitespace_is_not_trimmed(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source6["artifact_receipt"]["execution_record"]["json_pointer"] = (
            "/acceptance_evidence/artifact "
        )
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="pointer is missing"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_unaccepted_source(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1, 2, 3, 4, 5, 8))
        source6 = _source_pair(conn, episode="EP06", subject="TurboHome")
        source7 = _source_pair(conn, episode="EP07", subject="D Squared")
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps({"review_outcome": "blocked"}), source7["review_run_id"]),
        )
        task_id, run_id = _target(conn, [source6, source7])
        with pytest.raises(ValueError, match="was not accepted"):
            kb.reserve_next_domain_episode(
                conn, task_id=task_id, expected_run_id=run_id
            )


def test_refuses_accepted_source_from_another_artifact_family(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        source = _source_pair(conn, episode="EP06", subject="TurboHome")
        for run_id in (source["execution_run_id"], source["review_run_id"]):
            run = kb.get_run(conn, run_id)
            metadata = json.loads(
                json.dumps(run.metadata).replace("audio_brief", "video")
            )
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), run_id),
            )
        with pytest.raises(ValueError, match="record does not match"):
            kb._accepted_episode_source_receipt(
                conn,
                contract={"identity": dict(LANE)},
                artifact_type="audio_brief",
                source={**source, "episode_number": 6},
            )


def test_refuses_review_bound_to_another_execution_run(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        source = _source_pair(conn, episode="EP06", subject="TurboHome")
        review_run = kb.get_run(conn, source["review_run_id"])
        metadata = dict(review_run.metadata)
        metadata["workflow_review_source"]["parent_execution_run_id"] += 1
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), source["review_run_id"]),
        )
        with pytest.raises(ValueError, match="another execution run"):
            kb._accepted_episode_source_receipt(
                conn,
                contract={"identity": dict(LANE)},
                artifact_type="audio_brief",
                source={
                    **source,
                    "episode_number": 6,
                },
            )


def test_refuses_episode_and_subject_substring_collisions(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        source = _source_pair(conn, episode="EP010", subject="Joanne")
        with pytest.raises(ValueError, match="declared episode"):
            kb._accepted_episode_source_receipt(
                conn,
                contract={"identity": dict(LANE)},
                artifact_type="audio_brief",
                source={
                    **source,
                    "episode_number": 1,
                    "episode_label": "EP01",
                    "subject_label": "Ann",
                },
            )


def test_refuses_episode_label_with_alphanumeric_suffix(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        source = _source_pair(conn, episode="EP01A", subject="Ann")
        with pytest.raises(ValueError, match="declared episode"):
            kb._accepted_episode_source_receipt(
                conn,
                contract={"identity": dict(LANE)},
                artifact_type="audio_brief",
                source={
                    **source,
                    "episode_number": 1,
                    "episode_label": "EP01",
                },
            )


def test_ignores_episode_attributes_from_unrelated_artifact_type(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _seed_registered_episodes(conn, (1,))
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO domain_artifacts (
                domain_key, entity_type, entity_id, artifact_type, platform,
                artifact_key, status, attributes, evidence_ref,
                source_task_id, accepted_review_task_id,
                observed_at, created_at, updated_at
            ) VALUES (
                'solobizai', 'SoloBizAiCase', 'case-ep01', 'video', 'internal',
                'unrelated-video', 'draft', ?, '', 'source-task',
                'source-review', ?, ?, ?
            )
            """,
            (json.dumps({"episode_number": "EP50"}), now, now, now),
        )
        occupancy = kb._registered_episode_occupancy(
            conn,
            domain_key="solobizai",
            entity_type="SoloBizAiCase",
            artifact_type="audio_brief",
        )

    assert [item["episode_number"] for item in occupancy] == [1]


def _replace_with_v1_reservation_table(conn) -> None:
    conn.execute("DROP TABLE domain_episode_reservations")
    conn.execute(
        """
        CREATE TABLE domain_episode_reservations (
            reservation_id TEXT PRIMARY KEY,
            domain_key TEXT NOT NULL,
            schema_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            episode_number INTEGER NOT NULL,
            episode_label TEXT NOT NULL,
            subject_entity_id TEXT NOT NULL,
            subject_label TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            source_task_id TEXT NOT NULL,
            source_run_id INTEGER NOT NULL,
            authorization_fingerprint TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE (domain_key, artifact_type, episode_number),
            UNIQUE (domain_key, artifact_type, objective_id, subject_entity_id)
        )
        """
    )
    conn.commit()


def test_migrates_empty_v1_reservation_table_to_v2(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _replace_with_v1_reservation_table(conn)
        kb._migrate_domain_episode_reservations_v2(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(domain_episode_reservations)")
        }
        schema = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='domain_episode_reservations'"
        ).fetchone()[0]

    assert "receipt_version" in columns
    assert "domain_key, entity_type, artifact_type, episode_number" in schema


def test_refuses_v2_migration_when_v1_table_is_not_empty(tmp_path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        _replace_with_v1_reservation_table(conn)
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO domain_episode_reservations VALUES (
                'der_22222222222222222222222222222222', 'solobizai',
                'solobizai.case.v1', 'SoloBizAiCase', 'audio_brief',
                9, 'EP09', 'subject', 'Subject', 'go_subject',
                'task', 1, 'fingerprint', '{}', 'reserved', ?, ?
            )
            """,
            (now, now),
        )
        conn.commit()
        with pytest.raises(sqlite3.DatabaseError, match="requires an empty table"):
            kb._migrate_domain_episode_reservations_v2(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM domain_episode_reservations"
        ).fetchone()[0] == 1


def test_internal_reserved_artifact_needs_no_public_effect_receipt():
    validate_delta_external_effect_refs(
        [
            {
                "evidence_refs": ["domain_episode_reservation:der_123"],
                "artifacts": [
                    {
                        "artifact_type": "audio_brief",
                        "platform": "internal",
                        "status": "reserved",
                        "evidence_ref": "domain_episode_reservation:der_123",
                    }
                ],
            }
        ],
        [],
    )
