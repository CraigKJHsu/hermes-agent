from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from hermes_cli import kanban_db as kb
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
        "HERMES_SESSION_SOURCE": "telegram",
        "HERMES_SESSION_INTERNAL": "false",
    }


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
