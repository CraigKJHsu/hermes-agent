from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb
from proactive.policy_registry import policy_snapshot_marker, resolve_contract_policies


def _seed_blocked_review(conn):
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
    assert kb.block_task(
        conn,
        review_id,
        reason=(
            "managed_policy_read 回報 Topic policy binding not found；"
            "runtime capability fault"
        ),
        kind="capability",
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
        "HERMES_SESSION_SOURCE": "telegram",
        "HERMES_SESSION_INTERNAL": "false",
    }


def test_clawops_retry_review_requeues_only_existing_lane_bound_review(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_blocked_review(conn)

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
        assert len(kb.list_tasks(conn, include_archived=True)) == 2
        assert kb.get_task(conn, execution_id).status == "done"
        assert kb.get_task(conn, review_id).status == "ready"
        assert kb.block_task(
            conn,
            review_id,
            reason=(
                "managed_policy_read 回報 Topic policy binding not found；"
                "second runtime capability fault"
            ),
            kind="capability",
        )

    replay = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert replay["status"] == "rejected"
    assert "already consumed" in replay["reason"]


def test_clawops_retry_review_rejects_wrong_topic_and_missing_retry_intent(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-reject.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _execution_id, review_id = _seed_blocked_review(conn)

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

    values["HERMES_SESSION_THREAD_ID"] = "2120"
    values["HERMES_SESSION_MESSAGE_TEXT"] = (
        f"只查詢 Review {review_id} 狀態，不要重試"
    )
    missing_intent = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert missing_intent["status"] == "rejected"
    assert "does not explicitly request" in missing_intent["reason"]

    values["HERMES_SESSION_MESSAGE_TEXT"] = "請重試另一張 Review t_deadbeef"
    wrong_id = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert wrong_id["status"] == "rejected"
    assert "not bound to this review_task_id" in wrong_id["reason"]


def test_clawops_retry_review_requires_callback_and_repaired_block_class(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry-review-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        _execution_id, review_id = _seed_blocked_review(conn)

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
    missing_callback = json.loads(
        handle_clawops_retry_review({"review_task_id": review_id})
    )
    assert missing_callback["status"] == "rejected"
    assert "authenticated chat/topic" in missing_callback["reason"]
