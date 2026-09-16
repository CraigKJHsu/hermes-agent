from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from proactive.behavior_profiles import registry as behavior_registry
from proactive.policy_registry import (
    bind_topic_policies,
    create_policy_version,
    policy_snapshot_marker,
    resolve_contract_policies,
)


def _seed_blocked_review(
    conn,
    *,
    block_kind="capability",
    block_reason=(
        "managed_policy_read 回報 Topic policy binding not found；"
        "runtime capability fault"
    ),
):
    # The runtime repair teaches the resolver that a task-pinned null digest
    # means verified binding absence; creating a binding would make it stale.
    execution_id = kb.create_task(
        conn,
        title="execution",
        body="GRACE_LOOP_CONTRACT_STAGE: execution\n",
        assignee="openclaw",
        executor_profile="loop-contract",
    )
    assert kb.complete_task(conn, execution_id, summary="done")
    marker = policy_snapshot_marker(
        resolve_contract_policies(
            {
                "memory": {"namespace": "telegram:chat-1:2120/kj_profile"},
                "policy_requirements": [],
            }
        )
    )
    assert marker is not None
    review_id = kb.create_task(
        conn,
        title="review",
        body=f"GRACE_LOOP_CONTRACT_STAGE: grace_review\n{marker}\n",
        assignee="default",
        executor_profile="grace-policy-review",
        parents=(execution_id,),
    )
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES ('gd-review-retry', ?, 'request-review-retry',
                  'telegram', 'chat-1', '2120',
                  'agent:main:telegram:group:chat-1:2120', 'session-1',
                  '{}', 0, 'queued', ?, ?, ?, ?)
        """,
        ("b" * 64, execution_id, review_id, now, now),
    )
    kb.add_grace_loop_callback(
        conn,
        review_task_id=review_id,
        execution_task_id=execution_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        session_key="agent:main:telegram:group:chat-1:2120",
        session_id="session-1",
        user_id="kj",
        contract_fingerprint="b" * 64,
    )
    if block_reason == "__runtime_source__":
        block_reason = (
            "Grace 實質驗收已通過，但 runtime integrity gate 回覆 "
            "`Kanban review runtime source changed`；請重啟 dispatcher/gateway "
            f"後重試同一卡 {review_id}，不要另建 delegation。"
        )
    assert kb.block_task(
        conn,
        review_id,
        reason=block_reason,
        kind=block_kind,
    )
    return execution_id, review_id


def _session_values(message_text="請重試 Grace Review t_review"):
    return {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2120",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "",
        "HERMES_SESSION_MESSAGE_ID": "retry-message-1",
        "HERMES_SESSION_MESSAGE_TEXT": message_text,
        "HERMES_SESSION_ID": "session-1",
        "HERMES_SESSION_RESUME_REASON": "",
        "HERMES_SESSION_SOURCE": "telegram",
        "HERMES_SESSION_INTERNAL": "false",
    }


def _seed_interrupted_retry_state(
    path,
    review_id,
    *,
    resolved=False,
    user_timestamp=None,
    user_text=None,
    arguments=None,
    intervening=False,
    following_text=None,
    older_unresolved=False,
    older_malformed=False,
    call_id="call_interrupted_retry",
    call_type="function",
    session_user_id="kj",
):
    from hermes_state import SessionDB

    state = SessionDB(db_path=path)
    state.create_session("session-1", "telegram", user_id=session_user_id)
    state.record_gateway_session_peer(
        "session-1",
        source="telegram",
        user_id=session_user_id,
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_id="chat-1",
        chat_type="group",
        thread_id="2120",
    )
    now = time.time() if user_timestamp is None else user_timestamp
    if older_unresolved:
        state.append_message(
            "session-1",
            "assistant",
            "較舊且未解的重試 call。",
            tool_calls=[
                {
                    "id": "call_older_unresolved_retry",
                    "type": "function",
                    "function": {
                        "name": "clawops_retry_review",
                        "arguments": json.dumps({"review_task_id": review_id}),
                    },
                }
            ],
            timestamp=now - 1,
        )
    if older_malformed:
        state.append_message(
            "session-1",
            "assistant",
            "較舊且格式損壞的 tool call。",
            tool_calls={"not": "a call list"},
            timestamp=now - 1,
        )
    state.append_message(
        "session-1",
        "user",
        user_text
        or f"[KJ] 請重試原 Grace Review {review_id}，不要另建 delegation。",
        timestamp=now,
    )
    if intervening:
        state.append_message(
            "session-1",
            "assistant",
            "這是另一個 active transcript turn。",
            timestamp=now + 0.005,
        )
    state.append_message(
        "session-1",
        "assistant",
        "只重試原 Review。",
        tool_calls=[
            {
                "id": call_id,
                "type": call_type,
                "function": {
                    "name": "clawops_retry_review",
                    "arguments": (
                        json.dumps({"review_task_id": review_id})
                        if arguments is None
                        else arguments
                    ),
                },
            }
        ],
        timestamp=now + 0.01,
    )
    if following_text is not None:
        state.append_message(
            "session-1",
            "user",
            following_text,
            timestamp=now + 0.02,
        )
    if resolved:
        state.append_message(
            "session-1",
            "tool",
            '{"status":"rejected"}',
            tool_name="clawops_retry_review",
            tool_call_id=call_id,
            timestamp=now + 0.03,
        )
    state.close()


def _durable_state(conn, execution_id, review_id):
    delegation = conn.execute(
        """
        SELECT delegation_id, state, execution_task_id, review_task_id,
               platform, chat_id, thread_id, session_id
          FROM grace_delegations
         WHERE review_task_id = ?
        """,
        (review_id,),
    ).fetchone()
    callback = kb.get_grace_loop_callback(conn, review_id)
    return {
        "task_count": len(kb.list_tasks(conn, include_archived=True)),
        "execution_status": kb.get_task(conn, execution_id).status,
        "review_status": kb.get_task(conn, review_id).status,
        "delegation": dict(delegation) if delegation is not None else None,
        "callback": callback,
        "retry_receipts": conn.execute(
            """
            SELECT COUNT(*)
              FROM task_events
             WHERE task_id = ? AND kind = 'grace_review_retry_authorized'
            """,
            (review_id,),
        ).fetchone()[0],
    }


def _seed_two_timeout_recovery(conn):
    execution_id, review_id = _seed_blocked_review(conn)
    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='blocked'", (review_id,))
    conn.execute("DELETE FROM task_runs WHERE task_id=? AND outcome='blocked'", (review_id,))
    objective_id = "go_timeout_review"
    stage_key = "facebook_page_publish_preflight"
    kb.create_grace_objective(
        conn,
        objective_id=objective_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        session_key="agent:main:telegram:group:chat-1:2120",
        title="Page publication",
        objective="Review the read-only Page preflight",
        original_request_sha256="c" * 64,
        required_stage_keys=[stage_key, "execute_external_action"],
        terminal_stage_key="execute_external_action",
        acceptance_criteria=["Formal review accepted"],
    )
    policy = {
        "namespace": "telegram:chat-1:2120",
        "policy_requirements": [],
        "policy_snapshots": [],
        "policy_binding_snapshot": {
            "namespace": "telegram:chat-1:2120",
            "path": "",
            "sha256": "0" * 64,
        },
    }
    current_pin = behavior_registry._make_pin(
        "ai_bizweek", "v47", policy, project_namespace="ai_bizweek",
    )
    pin = dict(current_pin)
    pin.update({
        "behavior_bundle_hash": "1" * 64,
        "safety_kernel_hash": "2" * 64,
    })
    conn.execute(
        "INSERT INTO grace_objective_behavior_pins VALUES (?,?,?)",
        (objective_id, json.dumps(pin), json.dumps(policy)),
    )
    domain_memory = {
        "schema_id": "solobizai.case.v1",
        "mode": "query",
        "domain_key": "solobizai",
        "entity_type": "SoloBizAiCase",
        "expected_total": None,
        "required_entity_fields": ["entity_id", "label", "status"],
        "artifact_types": [
            "facebook_page_post", "podcast_episode", "audio_brief",
        ],
        "required_artifact_fields": ["artifact_type", "status"],
        "require_delta_on_acceptance": False,
    }
    contract = {
        "identity": {
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "2120",
            "project": "ai_bizweek",
        },
        "objective_ref": {"objective_id": objective_id, "stage_key": stage_key},
        "behavior_pin": pin,
        "memory": {"namespace": "telegram:chat-1:2120"},
        "domain_memory": domain_memory,
        "stop_rules": {"max_runtime_seconds": 900},
        "policy_snapshots": [],
    }
    admission = {
        "identity": {
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "2120",
            "project": "ai_bizweek",
        },
        "objective_ref": {"objective_id": objective_id, "stage_key": stage_key},
        "behavior_pin": pin,
        "memory": {"namespace": "telegram:chat-1:2120"},
    }
    conn.execute(
        "UPDATE tasks SET body=?, block_kind=NULL, consecutive_failures=2, "
        "max_runtime_seconds=900, max_retries=NULL WHERE id=?",
        (
            "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
            + behavior_registry.task_marker(admission)
            + "\n```json\n"
            + json.dumps(contract)
            + "\n```",
            review_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET body=? WHERE id=?",
        (
            "GRACE_LOOP_CONTRACT_STAGE: execution\n"
            + behavior_registry.task_marker(admission)
            + "\n```json\n"
            + json.dumps(contract)
            + "\n```",
            execution_id,
        ),
    )
    conn.execute(
        "UPDATE grace_delegations SET objective_id=?,stage_key=? "
        "WHERE review_task_id=?",
        (objective_id, stage_key, review_id),
    )
    conn.execute(
        "UPDATE grace_objective_stages SET status='queued',delegation_id=?,"
        "execution_task_id=?,review_task_id=? WHERE objective_id=? AND stage_key=?",
        (
            "gd-review-retry", execution_id, review_id, objective_id, stage_key,
        ),
    )
    run_ids = []
    now = int(time.time())
    for index in range(2):
        cursor = conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,max_runtime_seconds,started_at,ended_at,"
            "outcome,error,executor_backend) VALUES (?,?, 'timed_out',900,?,?,"
            "'timed_out',?,'hermes')",
            (
                review_id,
                "default",
                now - 2000 + index * 1000,
                now - 1100 + index * 1000,
                "elapsed 910s > limit 900s",
            ),
        )
        run_id = int(cursor.lastrowid)
        run_ids.append(run_id)
        kb._append_event(
            conn,
            review_id,
            "timed_out",
            {"elapsed_seconds": 910, "limit_seconds": 900},
            run_id=run_id,
        )
    kb._append_event(
        conn,
        review_id,
        "gave_up",
        {
            "failures": 2,
            "effective_limit": 2,
            "trigger_outcome": "timed_out",
            "error": "elapsed 910s > limit 900s",
        },
    )
    gave_up_event_id = int(conn.execute(
        "SELECT id FROM task_events WHERE task_id=? AND kind='gave_up' "
        "ORDER BY id DESC LIMIT 1",
        (review_id,),
    ).fetchone()[0])
    conn.execute(
        "UPDATE grace_loop_callbacks SET objective_id=?,stage_key=?,"
        "state='delivering',last_event_id=0,lease_event_id=?,lease_owner='old',"
        "lease_expires=?,attempts=3,attempt_event_id=?,outcome_event_id=NULL,"
        "outcome_kind=NULL,outcome_payload=NULL WHERE review_task_id=?",
        (
            objective_id,
            stage_key,
            gave_up_event_id,
            now - 1,
            gave_up_event_id,
            review_id,
        ),
    )
    return execution_id, review_id, objective_id, stage_key, run_ids, gave_up_event_id


def test_clawops_retry_review_requeues_only_existing_lane_bound_review(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)

    values = _session_values(f"請重試 Grace Review {review_id}，不要建立新 Execution")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result == {
        "status": "queued",
        "task_created": False,
        "delegation_id": "gd-review-retry",
        "execution_task_id": execution_id,
        "grace_review_task_id": review_id,
        "review_status": "ready",
    }
    with kb.connect_closing(db_path) as conn:
        success = _durable_state(conn, execution_id, review_id)
        assert success["task_count"] == initial["task_count"] == 2
        assert success["execution_status"] == initial["execution_status"] == "done"
        assert success["review_status"] == "ready"
        assert success["delegation"] == initial["delegation"]
        assert success["callback"] == initial["callback"]
        assert success["retry_receipts"] == 1
        assert kb.block_task(
            conn,
            review_id,
            reason=(
                "managed_policy_read 回報 Topic policy binding not found；"
                "second runtime capability fault"
            ),
            kind="capability",
        )
        before_replay = _durable_state(conn, execution_id, review_id)

    replay = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert replay["status"] == "rejected"
    assert "already consumed" in replay["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == before_replay


def test_clawops_retry_review_recovers_exact_two_timeouts_once(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "retry-review-timeout.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id, objective_id, _, run_ids, gave_up_event_id = (
            _seed_two_timeout_recovery(conn)
        )
        stale_pin = behavior_registry.get_pin(conn, objective_id)
        fingerprint = conn.execute(
            "SELECT contract_fingerprint FROM grace_delegations "
            "WHERE review_task_id=?",
            (review_id,),
        ).fetchone()[0]
        fenced_contract = kb._grace_compiled_contract(
            kb.get_task(conn, review_id).body,
        )
        initial_count = len(kb.list_tasks(conn, include_archived=True))

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "queued", result
    assert result["task_created"] is False
    with kb.connect_closing(db_path) as conn:
        review = kb.get_task(conn, review_id)
        assert review.status == "ready"
        assert review.max_runtime_seconds == 1500
        assert review.max_retries == 1
        assert review.consecutive_failures == 0
        assert len(kb.list_tasks(conn, include_archived=True)) == initial_count
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "delivering"
        assert callback["lease_event_id"] == gave_up_event_id
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='grace_review_retry_authorized' ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()[0])
        assert receipt["repaired_fault"] == "formal_review_cold_start_budget"
        assert receipt["timeout_run_ids"] == run_ids
        assert receipt["max_runtime_seconds"] == 1500
        assert receipt["max_retries"] == 1
        assert receipt["behavior_repin_previous_sha256"] == behavior_registry.digest(
            stale_pin
        )
        assert receipt["behavior_repin_next_sha256"] == behavior_registry.digest(
            behavior_registry.get_pin(conn, objective_id)
        )
        migrated_pin = behavior_registry.get_pin(conn, objective_id)
        assert migrated_pin["generation"] == stale_pin["generation"] + 1
        assert migrated_pin["behavior_profile_id"] == stale_pin["behavior_profile_id"]
        assert migrated_pin["behavior_profile_version"] == stale_pin["behavior_profile_version"]
        assert migrated_pin["policy_snapshot_hash"] == stale_pin["policy_snapshot_hash"]
        assert migrated_pin["project_namespace"] == stale_pin["project_namespace"]
        assert behavior_registry.guard_task(conn, review_id)[
            "safety_kernel_hash"
        ] == migrated_pin["safety_kernel_hash"]
        assert kb._grace_compiled_contract(kb.get_task(conn, review_id).body) == fenced_contract
        assert conn.execute(
            "SELECT contract_fingerprint FROM grace_delegations "
            "WHERE review_task_id=?",
            (review_id,),
        ).fetchone()[0] == fingerprint


def test_two_timeout_retry_recovers_session_reset_attention_callback(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "retry-review-timeout-attention.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _, review_id, _, _, _, _ = _seed_two_timeout_recovery(conn)
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='attention',"
            "lease_event_id=NULL,lease_owner=NULL,lease_expires=NULL,"
            "last_error=? WHERE review_task_id=?",
            (
                "origin session changed; sent safe handoff notice; "
                "completed callback outcome remains unresolved",
                review_id,
            ),
        )

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "queued", result
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "pending"
        assert callback["session_id"] == "session-1"
        assert callback["last_error"] is None
        assert callback["outcome_event_id"] is None


def test_attention_callback_rebind_rolls_back_when_migration_rejects(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "retry-review-attention-rollback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _, review_id, _, _, _, gave_up_event_id = _seed_two_timeout_recovery(conn)
        error = (
            "origin session changed; sent safe handoff notice; "
            "completed callback outcome remains unresolved"
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='attention',"
            "lease_event_id=NULL,lease_owner=NULL,lease_expires=NULL,"
            "last_error=? WHERE review_task_id=?",
            (error, review_id),
        )

    observed = {"pending_inside_transaction": False}

    def reject_migration(conn, **_kwargs):
        callback = kb.get_grace_loop_callback(conn, review_id)
        observed["pending_inside_transaction"] = callback["state"] == "pending"
        raise behavior_registry.BehaviorProfileError("forced migration rejection")

    monkeypatch.setattr(behavior_registry, "migrate_objective", reject_migration)
    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "rejected"
    assert observed["pending_inside_transaction"] is True
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "attention"
        assert callback["attempt_event_id"] == gave_up_event_id
        assert callback["last_error"] == error


def test_timeout_repin_completion_projects_sealed_parent_contract(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "retry-review-timeout-completion.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id, _, _, _, _ = _seed_two_timeout_recovery(conn)

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))
    assert result["status"] == "queued", result

    with kb.connect_closing(db_path) as conn:
        run_id = int(conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,max_runtime_seconds,started_at,executor_backend,metadata) "
            "VALUES (?,?,'running',1500,?,'hermes','{}')",
            (review_id, "default", int(time.time())),
        ).lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running',current_run_id=? WHERE id=?",
            (run_id, review_id),
        )
        kb._append_event(
            conn, review_id, "claimed", {"run_id": run_id}, run_id=run_id,
        )
        review_source = kb._workflow_review_source(conn, review_id)
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps({"workflow_review_source": review_source}), run_id),
        )
        review_body = kb.get_task(conn, review_id).body
        projection = kb._accepted_parent_domain_projection(
            conn, review_task_id=review_id, review_body=review_body,
        )

        assert projection["source_task_id"] == execution_id
        assert projection["deltas"] == []
        receipt_row = conn.execute(
            "SELECT id,payload FROM task_events WHERE task_id=? "
            "AND kind='grace_review_retry_authorized' ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        receipt = json.loads(receipt_row["payload"])
        for field in (
            "timeout_run_ids", "gave_up_event_id", "previous_max_runtime_seconds",
            "max_runtime_seconds", "max_retries",
            "behavior_repin_previous_generation", "behavior_repin_generation",
            "behavior_repin_objective_revision",
        ):
            corrupted = dict(receipt)
            corrupted.pop(field)
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(corrupted), receipt_row["id"]),
            )
            with pytest.raises(
                behavior_registry.BehaviorProfileError,
                match="behavior.timeout_recovery_history_invalid",
            ):
                kb._accepted_parent_domain_projection(
                    conn, review_task_id=review_id, review_body=review_body,
                )
        for field in (
            "gave_up_event_id", "previous_max_runtime_seconds",
            "max_runtime_seconds", "max_retries",
            "behavior_repin_previous_generation", "behavior_repin_generation",
            "behavior_repin_objective_revision",
        ):
            corrupted = dict(receipt)
            corrupted[field] = True
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(corrupted), receipt_row["id"]),
            )
            with pytest.raises(
                behavior_registry.BehaviorProfileError,
                match="behavior.timeout_recovery_history_invalid",
            ):
                kb._accepted_parent_domain_projection(
                    conn, review_task_id=review_id, review_body=review_body,
                )
        corrupted = dict(receipt)
        corrupted["timeout_run_ids"] = [True, receipt["timeout_run_ids"][1]]
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(corrupted), receipt_row["id"]),
        )
        with pytest.raises(
            behavior_registry.BehaviorProfileError,
            match="behavior.timeout_recovery_history_invalid",
        ):
            kb._accepted_parent_domain_projection(
                conn, review_task_id=review_id, review_body=review_body,
            )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (receipt_row["payload"], receipt_row["id"]),
        )
        run_metadata = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()[0])
        run_metadata["workflow_review_source"]["parent_execution_task_id"] = "t_other"
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(run_metadata), run_id),
        )
        with pytest.raises(
            behavior_registry.BehaviorProfileError,
            match="behavior.timeout_recovery_history_invalid",
        ):
            kb._accepted_parent_domain_projection(
                conn, review_task_id=review_id, review_body=review_body,
            )


@pytest.mark.parametrize(
    "break_kind",
    [
        "active_lease", "missing_timeout", "outcome", "wrong_stage",
        "external_effect", "non_object_gave_up", "contradictory_timeout",
        "mismatched_timeout_limit",
    ],
)
def test_clawops_retry_review_timeout_recovery_fails_closed(
    tmp_path, monkeypatch, break_kind,
):
    db_path = tmp_path / f"retry-review-timeout-{break_kind}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id, objective_id, _, _, gave_up_event_id = (
            _seed_two_timeout_recovery(conn)
        )
        if break_kind == "active_lease":
            conn.execute(
                "UPDATE grace_loop_callbacks SET lease_expires=? "
                "WHERE review_task_id=?",
                (int(time.time()) + 600, review_id),
            )
        elif break_kind == "missing_timeout":
            conn.execute(
                "DELETE FROM task_events WHERE task_id=? AND kind='timed_out' "
                "AND id=(SELECT MIN(id) FROM task_events WHERE task_id=? "
                "AND kind='timed_out')",
                (review_id, review_id),
            )
        elif break_kind == "outcome":
            conn.execute(
                "UPDATE grace_loop_callbacks SET outcome_event_id=?,"
                "outcome_kind='continued',outcome_payload='{}' "
                "WHERE review_task_id=?",
                (gave_up_event_id, review_id),
            )
        elif break_kind == "wrong_stage":
            conn.execute(
                "UPDATE grace_objectives SET current_stage_key='execute_external_action' "
                "WHERE objective_id=?",
                (objective_id,),
            )
        elif break_kind == "external_effect":
            conn.execute(
                "INSERT INTO task_external_effects "
                "(task_id,platform,effect_key,state,external_id,created_at,updated_at) "
                "VALUES (?,'facebook','create','existing','post-already-exists',1,1)",
                (execution_id,),
            )
        elif break_kind == "non_object_gave_up":
            conn.execute(
                "UPDATE task_events SET payload='[]' WHERE id=?",
                (gave_up_event_id,),
            )
        elif break_kind == "contradictory_timeout":
            conn.execute(
                "UPDATE task_runs SET error='elapsed 800s > limit 900s' "
                "WHERE task_id=? AND outcome='timed_out'",
                (review_id,),
            )
        else:
            conn.execute(
                "UPDATE task_runs SET error='elapsed 910s > limit 899s' "
                "WHERE task_id=? AND outcome='timed_out'",
                (review_id,),
            )
        initial = _durable_state(conn, execution_id, review_id)

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "rejected"
    if break_kind == "external_effect":
        assert "migration_callback_pending" in result["reason"]
    else:
        assert "exact current two-timeout recovery target" in result["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial
        review = kb.get_task(conn, review_id)
        assert review.max_runtime_seconds == 900
        assert review.max_retries is None


@pytest.mark.parametrize(
    ("profile_id", "version"),
    [("secondhand_commerce", "v47"), ("ai_bizweek", "v46")],
)
def test_timeout_review_repin_cannot_change_profile_or_version(
    tmp_path, monkeypatch, profile_id, version,
):
    db_path = tmp_path / f"review-repin-{profile_id}-{version}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _, review_id, objective_id, _, _, _ = _seed_two_timeout_recovery(conn)
        objective = kb.get_grace_objective(conn, objective_id)
        previous = behavior_registry.get_pin(conn, objective_id)
        body = kb.get_task(conn, review_id).body
        with pytest.raises(
            behavior_registry.BehaviorProfileError,
            match="migration_(project_mismatch|timeout_recovery_invalid)",
        ):
            behavior_registry.migrate_objective(
                conn,
                objective_id=objective_id,
                platform="telegram",
                chat_id="chat-1",
                thread_id="2120",
                profile_id=profile_id,
                version=version,
                expected_revision=objective["revision"],
                expected_pin_hash=behavior_registry.digest(previous),
                reason="Must preserve the sealed profile and version.",
                project_namespace=previous["project_namespace"],
                recovery_review_task_id=review_id,
                apply=True,
            )
        assert behavior_registry.get_pin(conn, objective_id) == previous
        assert kb.get_task(conn, review_id).body == body


def test_clawops_retry_review_recovers_exact_unresolved_call_after_restart(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-resume.db"
    state_path = tmp_path / "state.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_STATE_DB", str(state_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(state_path, review_id)

    from hermes_cli.review_retry_recovery import (
        recover_interrupted_review_retry,
    )

    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result["status"] == "queued"
    assert result["task_created"] is False
    with kb.connect_closing(db_path) as conn:
        state = _durable_state(conn, execution_id, review_id)
        assert state["review_status"] == "ready"
        receipt = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='grace_review_retry_authorized'",
            (review_id,),
        ).fetchone()
        assert "gateway-resume:session-1:call_interrupted_retry" in receipt[0]
    from hermes_state import SessionDB

    state_db = SessionDB(db_path=state_path, read_only=True)
    try:
        messages = state_db.get_messages("session-1")
    finally:
        state_db.close()
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_interrupted_retry"
    assert json.loads(messages[-1]["content"])["status"] == "queued"


def test_clawops_retry_review_recovery_accepts_configured_owner(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-recovery-owner.db"
    state_path = tmp_path / "state-recovery-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-recovery-owner"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(
        state_path,
        review_id,
        session_user_id="configured-owner",
    )

    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="configured-owner",
        owner_user_id="configured-owner",
        state_db_path=state_path,
        board="default",
    )

    assert result is not None
    assert result["status"] == "queued"


def test_clawops_retry_review_recovery_accepts_no_new_execution_wording(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-recovery-wording.db"
    state_path = tmp_path / "state-recovery-wording.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-recovery-wording"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(
        state_path,
        review_id,
        user_text=f"[KJ] 請重試 Grace Review {review_id}，不要建立新 Execution",
    )

    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result is not None
    assert result["status"] == "queued"


def test_clawops_retry_review_recovery_consumes_call_once_under_concurrency(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-concurrent.db"
    state_path = tmp_path / "state-concurrent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-concurrent"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(state_path, review_id)
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    barrier = Barrier(2)

    def recover():
        barrier.wait()
        return recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: recover(), range(2)))

    assert sum(result is not None for result in results) == 1
    from hermes_state import SessionDB

    state = SessionDB(db_path=state_path, read_only=True)
    try:
        messages = state.get_messages("session-1")
    finally:
        state.close()
    tool_results = [
        message
        for message in messages
        if message.get("tool_call_id") == "call_interrupted_retry"
    ]
    assert len(tool_results) == 1
    with kb.connect_closing(db_path) as conn:
        durable = _durable_state(conn, execution_id, review_id)
        assert durable["review_status"] == "ready"
        assert durable["retry_receipts"] == 1

def test_clawops_retry_review_recovery_retries_state_write_after_kanban_commit(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry
    from hermes_state import SessionDB

    db_path = tmp_path / "retry-review-state-retry.db"
    state_path = tmp_path / "state-retry.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-state-retry"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(state_path, review_id)
    original_execute_write = SessionDB._execute_write
    consume_calls = 0

    def fail_once_after_consume(self, callback):
        nonlocal consume_calls
        if callback.__name__ != "consume":
            return original_execute_write(self, callback)
        consume_calls += 1
        if consume_calls != 1:
            return original_execute_write(self, callback)

        def rollback_after_kanban_commit(conn):
            callback(conn)
            raise sqlite3.IntegrityError("synthetic state write failure")

        return original_execute_write(self, rollback_after_kanban_commit)

    monkeypatch.setattr(SessionDB, "_execute_write", fail_once_after_consume)
    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result is not None
    assert result["status"] == "queued"
    assert "tool_result_durable" not in result
    assert consume_calls == 2
    with sqlite3.connect(state_path) as state:
        assert state.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            ("session-1", "call_interrupted_retry"),
        ).fetchone()[0] == 1
    with kb.connect_closing(db_path) as conn:
        durable = _durable_state(conn, execution_id, review_id)
        assert durable["review_status"] == "ready"
        assert durable["retry_receipts"] == 1


def test_clawops_retry_review_recovery_marks_persistent_state_write_pending(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry
    from hermes_state import SessionDB

    db_path = tmp_path / "retry-review-state-pending.db"
    state_path = tmp_path / "state-pending.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-state-pending"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(state_path, review_id)
    original_execute_write = SessionDB._execute_write
    consume_calls = 0

    def fail_after_consume(self, callback):
        nonlocal consume_calls
        if callback.__name__ != "consume":
            return original_execute_write(self, callback)
        consume_calls += 1

        def rollback_after_kanban_commit(conn):
            callback(conn)
            raise sqlite3.IntegrityError("persistent state write failure")

        return original_execute_write(self, rollback_after_kanban_commit)

    monkeypatch.setattr(SessionDB, "_execute_write", fail_after_consume)
    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result is not None
    assert result["status"] == "state_write_pending"
    assert result["tool_result_durable"] is False
    assert consume_calls == 2
    with sqlite3.connect(state_path) as state:
        assert state.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            ("session-1", "call_interrupted_retry"),
        ).fetchone()[0] == 0
    with kb.connect_closing(db_path) as conn:
        durable = _durable_state(conn, execution_id, review_id)
        assert durable["review_status"] == "ready"
        assert durable["retry_receipts"] == 1

    monkeypatch.setattr(SessionDB, "_execute_write", original_execute_write)
    with sqlite3.connect(state_path) as state:
        state.execute(
            "UPDATE messages SET timestamp=? WHERE session_id=?",
            (time.time() - 3600, "session-1"),
        )
    replay = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
        freshness_seconds=1,
    )
    assert replay is not None
    assert replay["status"] == "queued"
    with sqlite3.connect(state_path) as state:
        assert state.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            ("session-1", "call_interrupted_retry"),
        ).fetchone()[0] == 1


def test_clawops_retry_review_recovery_rejects_resolved_or_wrong_lane_call(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, resolved, user_id in (
        ("resolved", True, "kj"),
        ("wrong-lane", False, "someone-else"),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        state_path = tmp_path / f"state-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        monkeypatch.setenv("HERMES_STATE_DB", str(state_path))
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id, resolved=resolved)
        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id=user_id,
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_normalized_stored_lane(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-stored-lane.db"
    state_path = tmp_path / "state-stored-lane.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-stored-lane"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(state_path, review_id)
    with sqlite3.connect(state_path) as state:
        state.execute(
            "UPDATE sessions SET user_id=? WHERE id=?",
            (" kj", "session-1"),
        )

    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result is None
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_session_key_or_chat_type_mismatch(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, overrides in (
        ("session-key", {"session_key": "agent:other:telegram:group:chat-1:2120"}),
        ("chat-type", {"chat_type": "dm"}),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        state_path = tmp_path / f"state-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)
        kwargs = {
            "session_id": "session-1",
            "session_key": "agent:main:telegram:group:chat-1:2120",
            "platform": "telegram",
            "chat_id": "chat-1",
            "chat_type": "group",
            "thread_id": "2120",
            "user_id": "kj",
            "state_db_path": state_path,
            "board": "default",
            **overrides,
        }

        assert recover_interrupted_review_retry(**kwargs) is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_block_newer_than_user_request(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, user_offset in (("newer", -60), ("same-time", 0)):
        db_path = tmp_path / f"retry-review-{suffix}-block.db"
        state_path = tmp_path / f"state-{suffix}-block.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            blocked_at = conn.execute(
                "SELECT created_at FROM task_events "
                "WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
                (review_id,),
            ).fetchone()[0]
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(
            state_path,
            review_id,
            user_timestamp=float(blocked_at) + user_offset,
        )

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_unprovable_block_event(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, column, value in (
        ("text-time", "created_at", "nan"),
        ("non-object-payload", "payload", "[]"),
    ):
        db_path = tmp_path / f"retry-review-invalid-block-{suffix}.db"
        state_path = tmp_path / f"state-invalid-block-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            event_id = conn.execute(
                "SELECT id FROM task_events WHERE task_id=? AND kind='blocked' "
                "ORDER BY id DESC LIMIT 1",
                (review_id,),
            ).fetchone()[0]
            conn.execute(
                f"UPDATE task_events SET {column}=? WHERE id=?",
                (value, event_id),
            )
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)

        assert (
            recover_interrupted_review_retry(
                session_id="session-1",
                session_key="agent:main:telegram:group:chat-1:2120",
                chat_type="group",
                platform="telegram",
                chat_id="chat-1",
                thread_id="2120",
                user_id="kj",
                state_db_path=state_path,
                board="default",
            )
            is None
        )
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_non_finite_time_inputs(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, user_timestamp, freshness in (
        ("transcript", float("inf"), None),
        ("freshness", None, float("inf")),
        ("zero-freshness", None, 0),
        ("negative-freshness", None, -1),
    ):
        db_path = tmp_path / f"retry-review-non-finite-{suffix}.db"
        state_path = tmp_path / f"state-non-finite-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(
            state_path,
            review_id,
            user_timestamp=user_timestamp,
        )

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
            freshness_seconds=freshness,
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_requires_fresh_user_turn(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-stale-user.db"
    state_path = tmp_path / "state-stale-user.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-stale-user"))
    kb.init_db(db_path)
    stale_user_at = time.time() - 60
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='blocked'",
            (stale_user_at - 1, review_id),
        )
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(
        state_path,
        review_id,
        user_timestamp=stale_user_at,
    )
    with sqlite3.connect(state_path) as state:
        state.execute(
            "UPDATE messages SET timestamp=? WHERE session_id=? AND role='assistant' "
            "AND tool_calls IS NOT NULL",
            (time.time(), "session-1"),
        )

    assert (
        recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
            freshness_seconds=10,
        )
        is None
    )
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_wrong_tool_result_in_history(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry
    from hermes_state import SessionDB

    db_path = tmp_path / "retry-review-wrong-result.db"
    state_path = tmp_path / "state-wrong-result.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-wrong-result"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(
        state_path,
        review_id,
        older_unresolved=True,
    )
    with sqlite3.connect(state_path) as raw:
        raw.execute("UPDATE messages SET id=id+10 WHERE id>1")
    state = SessionDB(db_path=state_path)
    state.append_message(
        "session-1",
        "tool",
        '{"status":"irrelevant"}',
        tool_name="some_other_tool",
        tool_call_id="call_older_unresolved_retry",
        timestamp=time.time(),
    )
    state.close()
    with sqlite3.connect(state_path) as raw:
        raw.execute(
            "UPDATE messages SET id=? WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            (2, "session-1", "call_older_unresolved_retry"),
        )

    assert (
        recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )
        is None
    )
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_partly_unreadable_board_set(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-board-read.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _execution_id, review_id = _seed_blocked_review(conn)
    original_connect = kb.connect_closing
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda include_archived=False: [
            {"slug": "default"},
            {"slug": "unreadable"},
        ],
    )

    def connect_closing(*args, **kwargs):
        if kwargs.get("board") == "unreadable":
            raise sqlite3.OperationalError("cannot inspect board")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(kb, "connect_closing", connect_closing)
    from hermes_cli.review_retry_recovery import _resolve_board

    assert (
        _resolve_board(
            review_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
        )
        is None
    )


def test_clawops_retry_review_recovery_board_hint_cannot_bypass_resolution(
    tmp_path, monkeypatch
):
    from hermes_cli import review_retry_recovery

    db_path = tmp_path / "retry-review-board-hint.db"
    state_path = tmp_path / "state-board-hint.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-board-hint"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(state_path, review_id)
    monkeypatch.setattr(review_retry_recovery, "_resolve_board", lambda *a, **k: None)

    result = review_retry_recovery.recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result is None
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_future_or_wrong_type_call(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, seed_kwargs in (
        ("future", {"user_timestamp": time.time() + 60}),
        ("wrong-type", {"call_type": "computer"}),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        state_path = tmp_path / f"state-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id, **seed_kwargs)

        assert (
            recover_interrupted_review_retry(
                session_id="session-1",
                session_key="agent:main:telegram:group:chat-1:2120",
                chat_type="group",
                platform="telegram",
                chat_id="chat-1",
                thread_id="2120",
                user_id="kj",
                state_db_path=state_path,
                board="default",
            )
            is None
        )
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_receipt_closes_call_after_reblock(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-receipt-reblock.db"
    state_path = tmp_path / "state-receipt-reblock.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-receipt-reblock"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    _seed_interrupted_retry_state(state_path, review_id)
    kwargs = {
        "session_id": "session-1",
        "session_key": "agent:main:telegram:group:chat-1:2120",
        "platform": "telegram",
        "chat_id": "chat-1",
        "chat_type": "group",
        "thread_id": "2120",
        "user_id": "kj",
        "state_db_path": state_path,
        "board": "default",
    }

    first = recover_interrupted_review_retry(**kwargs)
    assert first is not None
    with sqlite3.connect(state_path) as state:
        state.execute(
            "DELETE FROM messages WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            ("session-1", "call_interrupted_retry"),
        )
        state.execute(
            "UPDATE sessions SET message_count=message_count-1 WHERE id=?",
            ("session-1",),
        )
    with kb.connect_closing(db_path) as conn:
        assert kb.block_task(
            conn,
            review_id,
            reason="a later, unrelated review blocker",
            kind="capability",
        )
        before_replay = _durable_state(conn, execution_id, review_id)

    replay = recover_interrupted_review_retry(**kwargs)

    assert replay == first
    with kb.connect_closing(db_path) as conn:
        durable = _durable_state(conn, execution_id, review_id)
        assert durable == before_replay
        assert durable["retry_receipts"] == 1
    with sqlite3.connect(state_path) as state:
        rows = state.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='tool' "
            "AND tool_call_id=?",
            ("session-1", "call_interrupted_retry"),
        ).fetchall()
    assert [json.loads(row[0]) for row in rows] == [first]


def test_clawops_retry_review_recovery_requires_adjacent_affirmative_user_turn(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, user_text, intervening in (
        ("question", "[KJ] 為什麼重試 Grace Review {review_id} 會失敗？", False),
        ("denial", "[KJ] 我沒有要求重試 Grace Review {review_id}", False),
        (
            "conditional",
            "[KJ] 請重試原 Grace Review {review_id}，但要等我再次確認後",
            False,
        ),
        ("intervening", None, True),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        state_path = tmp_path / f"state-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(
            state_path,
            review_id,
            user_text=(
                user_text.format(review_id=review_id) if user_text else None
            ),
            intervening=intervening,
        )

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_requires_unique_active_tail_call(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, options in (
        ("not-tail", {"following_text": "cancel"}),
        ("ambiguous", {"older_unresolved": True}),
        ("malformed-history", {"older_malformed": True}),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        state_path = tmp_path / f"state-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id, **options)

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_invalid_ids(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, options in (
        ("task-id", {"arguments": json.dumps({"review_task_id": 123})}),
        ("call-id", {"call_id": 123}),
        ("task-id-space", {}),
        ("call-id-space", {"call_id": " call_interrupted_retry "}),
    ):
        db_path = tmp_path / f"retry-review-non-string-{suffix}.db"
        state_path = tmp_path / f"state-non-string-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        if suffix == "task-id-space":
            options = {
                "arguments": json.dumps({"review_task_id": f" {review_id} "})
            }
        _seed_interrupted_retry_state(state_path, review_id, **options)

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_ambiguous_call_ids(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix in ("conflicting", "invalid-present-id"):
        db_path = tmp_path / f"retry-review-call-id-{suffix}.db"
        state_path = tmp_path / f"state-call-id-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)
        with sqlite3.connect(state_path) as state:
            row = state.execute(
                "SELECT id,tool_calls FROM messages WHERE session_id=? "
                "AND role='assistant' AND tool_calls IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                ("session-1",),
            ).fetchone()
            calls = json.loads(row[1])
            calls[0]["call_id"] = "different_call_id"
            if suffix == "invalid-present-id":
                calls[0]["id"] = ""
            state.execute(
                "UPDATE messages SET tool_calls=? WHERE id=?",
                (json.dumps(calls), row[0]),
            )

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_requires_grace_review_task_invariants(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix in ("profile", "review-stage", "execution-stage", "parent-link"):
        db_path = tmp_path / f"retry-review-invariant-{suffix}.db"
        state_path = tmp_path / f"state-invariant-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            if suffix == "profile":
                conn.execute(
                    "UPDATE tasks SET executor_profile=? WHERE id=?",
                    ("generic-worker", review_id),
                )
            elif suffix == "review-stage":
                conn.execute(
                    "UPDATE tasks SET body=? WHERE id=?",
                    ("GRACE_LOOP_CONTRACT_STAGE: execution\n", review_id),
                )
            elif suffix == "execution-stage":
                conn.execute(
                    "UPDATE tasks SET body=? WHERE id=?",
                    ("not a Grace execution\n", execution_id),
                )
            else:
                conn.execute(
                    "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                    (execution_id, review_id),
                )
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_rejects_non_object_arguments(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-malformed-args.db"
    state_path = tmp_path / "state-malformed-args.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-malformed-args"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(state_path, review_id, arguments="[]")

    assert (
        recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )
        is None
    )
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_requires_callback_execution_link(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    db_path = tmp_path / "retry-review-callback-link.db"
    state_path = tmp_path / "state-callback-link.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-callback-link"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE grace_loop_callbacks SET execution_task_id='t_wrong' "
                "WHERE review_task_id=?",
                (review_id,),
            )
        initial = _durable_state(conn, execution_id, review_id)
    _seed_interrupted_retry_state(state_path, review_id)

    assert (
        recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )
        is None
    )
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_recovery_fails_closed_when_repair_check_errors(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    cases = (
        (
            "policy",
            "capability",
            "managed_policy_read 回報 Topic policy binding not found；runtime fault",
            "proactive.policy_registry.resolve_task_policy_snapshots",
            ValueError,
        ),
        (
            "runtime",
            "transient",
            "Kanban review runtime source changed；restart required",
            "hermes_cli.kanban_db._workflow_review_source",
            RuntimeError,
        ),
    )
    for suffix, block_kind, block_reason, target, error_type in cases:
        db_path = tmp_path / f"retry-review-{suffix}-check.db"
        state_path = tmp_path / f"state-{suffix}-check.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(
                conn,
                block_kind=block_kind,
                block_reason=block_reason,
            )
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)

        def fail_check(*_args, error_type=error_type):
            raise error_type("repair still absent")

        with monkeypatch.context() as patcher:
            patcher.setattr(target, fail_check)
            result = recover_interrupted_review_retry(
                session_id="session-1",
                session_key="agent:main:telegram:group:chat-1:2120",
                chat_type="group",
                platform="telegram",
                chat_id="chat-1",
                thread_id="2120",
                user_id="kj",
                state_db_path=state_path,
                board="default",
            )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_consumes_one_message_atomically(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-concurrent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    barrier = Barrier(2)

    def invoke():
        barrier.wait()
        return json.loads(
            handle_clawops_retry_review({"review_task_id": review_id})
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: invoke(), range(2)))

    assert [item["status"] for item in results].count("queued") == 1
    rejected = [item for item in results if item["status"] == "rejected"]
    assert len(rejected) == 1
    assert "already consumed" in rejected[0]["reason"]
    with kb.connect_closing(db_path) as conn:
        concurrent = _durable_state(conn, execution_id, review_id)
        assert concurrent["task_count"] == initial["task_count"] == 2
        assert concurrent["execution_status"] == "done"
        assert concurrent["review_status"] == "ready"
        assert concurrent["delegation"] == initial["delegation"]
        assert concurrent["callback"] == initial["callback"]
        assert concurrent["retry_receipts"] == 1


def test_clawops_retry_review_rejects_wrong_topic_and_missing_retry_intent(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-reject.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        initial = _durable_state(conn, execution_id, review_id)

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    values["HERMES_SESSION_THREAD_ID"] = "different-topic"
    wrong_topic = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert wrong_topic["status"] == "rejected"
    assert "authenticated chat/topic" in wrong_topic["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial

    values["HERMES_SESSION_THREAD_ID"] = "2120"
    values["HERMES_SESSION_MESSAGE_TEXT"] = (
        f"只查詢 Review {review_id} 狀態，不要重試"
    )
    missing_intent = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert missing_intent["status"] == "rejected"
    assert "does not explicitly request" in missing_intent["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial

    values["HERMES_SESSION_MESSAGE_TEXT"] = "請重試另一張 Review t_deadbeef"
    wrong_id = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert wrong_id["status"] == "rejected"
    assert "not bound to this review_task_id" in wrong_id["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_enforces_authenticated_lane_and_owner_fields(
    tmp_path, monkeypatch
):
    for field, mismatched in (
        ("HERMES_SESSION_PLATFORM", "discord"),
        ("HERMES_SESSION_CHAT_ID", "other-chat"),
        ("HERMES_SESSION_USER_ID", "someone-else"),
        ("HERMES_SESSION_OWNER_USER_ID", "configured-owner"),
    ):
        db_path = tmp_path / f"retry-review-auth-{field}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{field}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(conn)
            initial = _durable_state(conn, execution_id, review_id)
        values = _session_values(f"請重試 Grace Review {review_id}")
        values[field] = mismatched
        monkeypatch.setattr(
            "plugins.openclaw_bridge.clawops_delegate.get_session_env",
            lambda key, default="", values=values: values.get(key, default),
        )
        from plugins.openclaw_bridge.clawops_delegate import (
            handle_clawops_retry_review,
        )

        result = json.loads(
            handle_clawops_retry_review({"review_task_id": review_id})
        )
        assert result["status"] == "rejected"
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_accepts_matching_configured_owner(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-configured-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-owner"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
    values = _session_values(f"請重試 Grace Review {review_id}")
    values["HERMES_SESSION_USER_ID"] = "configured-owner"
    values["HERMES_SESSION_OWNER_USER_ID"] = "configured-owner"
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "queued"
    assert result["task_created"] is False
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, execution_id).status == "done"
        assert kb.get_task(conn, review_id).status == "ready"


def test_clawops_retry_review_requires_callback_and_repaired_block_class(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "DELETE FROM grace_loop_callbacks WHERE review_task_id = ?",
            (review_id,),
        )
        initial = _durable_state(conn, execution_id, review_id)
    missing_callback = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert missing_callback["status"] == "rejected"
    assert "authenticated chat/topic" in missing_callback["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_requeues_after_review_runtime_reload(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-runtime-reload.db"
    state_path = tmp_path / "state-runtime-reload.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(
            conn,
            block_kind="transient",
            block_reason="__runtime_source__",
        )
    _seed_interrupted_retry_state(state_path, review_id)
    # Simulate the gateway/dispatcher restart named by the blocked reason.
    # Behavior profile files can be installed concurrently by the live system,
    # so pin the digest at this test's restart boundary.
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", kb._review_runtime_digest())
    from hermes_cli.review_retry_recovery import (
        recover_interrupted_review_retry,
    )

    result = recover_interrupted_review_retry(
        session_id="session-1",
        session_key="agent:main:telegram:group:chat-1:2120",
        chat_type="group",
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="kj",
        state_db_path=state_path,
        board="default",
    )

    assert result["status"] == "queued"
    assert result["task_created"] is False
    with kb.connect_closing(db_path) as conn:
        state = _durable_state(conn, execution_id, review_id)
        assert state["review_status"] == "ready"
        receipt = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='grace_review_retry_authorized'",
            (review_id,),
        ).fetchone()
        assert '"repaired_fault": "review_runtime_reloaded"' in receipt[0]


def test_clawops_retry_review_recovery_rejects_composite_block_reason(
    tmp_path, monkeypatch
):
    from hermes_cli.review_retry_recovery import recover_interrupted_review_retry

    for suffix, block_kind, block_reason in (
        (
            "capability",
            "capability",
            "managed_policy_read 回報 Topic policy binding not found；"
            "runtime capability fault；另外缺少 browser credential",
        ),
        ("transient", "transient", "__runtime_source__"),
    ):
        db_path = tmp_path / f"retry-review-composite-{suffix}.db"
        state_path = tmp_path / f"state-composite-{suffix}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f".hermes-{suffix}"))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(
                conn,
                block_kind=block_kind,
                block_reason=block_reason,
            )
            if suffix == "transient":
                event = conn.execute(
                    "SELECT id,payload FROM task_events WHERE task_id=? "
                    "AND kind='blocked' ORDER BY id DESC LIMIT 1",
                    (review_id,),
                ).fetchone()
                payload = json.loads(event["payload"])
                payload["reason"] += "；另外缺少 browser credential"
                conn.execute(
                    "UPDATE task_events SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), event["id"]),
                )
            initial = _durable_state(conn, execution_id, review_id)
        _seed_interrupted_retry_state(state_path, review_id)

        result = recover_interrupted_review_retry(
            session_id="session-1",
            session_key="agent:main:telegram:group:chat-1:2120",
            chat_type="group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="kj",
            state_db_path=state_path,
            board="default",
        )

        assert result is None
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_rejects_unrelated_block_classes(
    tmp_path, monkeypatch
):
    for suffix, block_kind, block_reason, expected_reason in (
        (
            "needs-input",
            "needs_input",
            "A human decision is required.",
            "repaired capability blocker",
        ),
        (
            "other-capability",
            "capability",
            "browser credential is missing",
            "not the repaired managed-policy binding fault",
        ),
    ):
        db_path = tmp_path / f"retry-review-{suffix}.db"
        home = tmp_path / f".hermes-{suffix}"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_HOME", str(home))
        kb.init_db(db_path)
        with kb.connect_closing(db_path) as conn:
            execution_id, review_id = _seed_blocked_review(
                conn,
                block_kind=block_kind,
                block_reason=block_reason,
            )
            initial = _durable_state(conn, execution_id, review_id)
        values = _session_values(f"請重試 Grace Review {review_id}")
        monkeypatch.setattr(
            "plugins.openclaw_bridge.clawops_delegate.get_session_env",
            lambda key, default="", values=values: values.get(key, default),
        )
        from plugins.openclaw_bridge.clawops_delegate import (
            handle_clawops_retry_review,
        )

        result = json.loads(
            handle_clawops_retry_review({"review_task_id": review_id})
        )
        assert result["status"] == "rejected"
        assert expected_reason in result["reason"]
        with kb.connect_closing(db_path) as conn:
            assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_rejects_dependency_todo_without_reconciliation(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-dependency-todo.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-dependency"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(
            conn,
            block_kind="dependency",
            block_reason="approval_required checkpoint needs a new revision stage",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(time.time()), execution_id),
            )
        initial = _durable_state(conn, execution_id, review_id)
        assert initial["execution_status"] == "done"
        assert initial["review_status"] == "todo"
    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "rejected"
    assert "not blocked" in result["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial


def test_clawops_retry_review_reconciles_parent_done_run_with_stale_task_status(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-parent-stale-status.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes-parent"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
                (execution_id,),
            )
        initial = _durable_state(conn, execution_id, review_id)
        assert initial["execution_status"] == "ready"

    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "queued"
    assert result["execution_task_id"] == execution_id
    assert result["grace_review_task_id"] == review_id
    assert result["review_status"] == "ready"
    with kb.connect_closing(db_path) as conn:
        state = _durable_state(conn, execution_id, review_id)
        assert state["task_count"] == initial["task_count"]
        assert state["execution_status"] == "done"
        assert state["review_status"] == "ready"
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='status_reconciled'",
            (execution_id,),
        ).fetchone() is not None


def test_clawops_retry_review_rejects_when_policy_fault_is_not_repaired(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-policy-stale.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)

    create_policy_version(
        "new-policy",
        "v1",
        "# Newly added policy\n",
        owner_scope="topic",
        owner_id="telegram:chat-1:2120/kj_profile",
        activate=True,
    )
    bind_topic_policies(
        "telegram:chat-1:2120/kj_profile",
        [{"policy_id": "new-policy", "resolution": "latest_active"}],
        expected_binding_sha256=None,
    )
    values = _session_values(f"請重試 Grace Review {review_id}")
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_retry_review

    with kb.connect_closing(db_path) as conn:
        initial = _durable_state(conn, execution_id, review_id)

    result = json.loads(handle_clawops_retry_review({"review_task_id": review_id}))

    assert result["status"] == "rejected"
    assert "policy_stale" in result["reason"]
    with kb.connect_closing(db_path) as conn:
        assert _durable_state(conn, execution_id, review_id) == initial
