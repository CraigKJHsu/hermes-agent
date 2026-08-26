from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb


def _seed_loop(conn):
    execution_id = kb.create_task(
        conn,
        title="execution",
        assignee="clawops-review",
    )
    review_id = kb.create_task(
        conn,
        title="review",
        assignee="default",
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
        ) VALUES ('gd-tool-cancel', ?, 'request-tool-cancel',
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
    return execution_id, review_id


def _session_values(message_text="停止執行"):
    return {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2120",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_MESSAGE_ID": "stop-message-1",
        "HERMES_SESSION_MESSAGE_TEXT": message_text,
        "HERMES_SESSION_SOURCE": "telegram",
    }


def test_clawops_cancel_stops_existing_loop_without_creating_a_cancel_card(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "cancel-tool.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_loop(conn)

    values = _session_values()
    # Live installs do not need a new owner setting when the authenticated
    # caller is the original durable Loop requester.
    values["HERMES_SESSION_OWNER_USER_ID"] = ""
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_cancel

    result = json.loads(
        handle_clawops_cancel(
            {"task_id": execution_id, "reason": "KJ requested stop."}
        )
    )

    assert result["status"] == "cancelled"
    assert result["task_created"] is False
    assert result["termination_confirmed"] is True
    assert result["execution_task_id"] == execution_id
    assert result["grace_review_task_id"] == review_id
    with kb.connect_closing(db_path) as conn:
        assert len(kb.list_tasks(conn, include_archived=True)) == 2
        assert kb.get_task(conn, execution_id).status == "blocked"
        assert kb.get_task(conn, review_id).status == "blocked"
        assert kb.get_grace_delegation(
            conn, delegation_id="gd-tool-cancel",
        )["state"] == "cancelled"


def test_clawops_cancel_rejects_wrong_owner_topic_and_negated_intent(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "cancel-tool-reject.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, _review_id = _seed_loop(conn)

    values = _session_values()
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_cancel

    values["HERMES_SESSION_USER_ID"] = "someone-else"
    wrong_owner = json.loads(handle_clawops_cancel({"task_id": execution_id}))
    assert wrong_owner["status"] == "rejected"
    assert "configured owner" in wrong_owner["reason"]

    values["HERMES_SESSION_USER_ID"] = "kj"
    values["HERMES_SESSION_THREAD_ID"] = "different-topic"
    wrong_topic = json.loads(handle_clawops_cancel({"task_id": execution_id}))
    assert wrong_topic["status"] == "rejected"
    assert "authenticated chat/topic" in wrong_topic["reason"]

    values["HERMES_SESSION_THREAD_ID"] = "2120"
    values["HERMES_SESSION_MESSAGE_TEXT"] = "不要停止，請繼續執行"
    negated = json.loads(handle_clawops_cancel({"task_id": execution_id}))
    assert negated["status"] == "rejected"
    assert "does not explicitly request" in negated["reason"]


def test_cancel_intent_does_not_treat_flow_changes_as_runtime_cancellation():
    from plugins.openclaw_bridge.clawops_delegate import (
        _is_explicit_cancel_message,
    )

    assert _is_explicit_cancel_message("停止執行") is True
    assert _is_explicit_cancel_message("停停") is True
    assert _is_explicit_cancel_message("請修改取消流程") is False
    assert _is_explicit_cancel_message("不要停止，請繼續執行") is False
