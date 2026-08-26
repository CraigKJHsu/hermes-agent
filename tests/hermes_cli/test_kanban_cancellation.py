from __future__ import annotations

import signal
import time

from hermes_cli import kanban_db as kb


def _seed_grace_loop(conn):
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
        ) VALUES ('gd-cancel', ?, 'request-cancel', 'telegram', 'chat-1',
                  '2120', 'agent:main:telegram:group:chat-1:2120',
                  'session-1', '{}', 0, 'queued', ?, ?, ?, ?)
        """,
        ("a" * 64, execution_id, review_id, now, now),
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
        contract_fingerprint="a" * 64,
    )
    kb.add_notify_sub(
        conn,
        task_id=execution_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
    )
    kb.add_notify_sub(
        conn,
        task_id=review_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
    )
    return execution_id, review_id


def test_cancel_grace_loop_persists_before_signal_and_avoids_protocol_violation(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "cancel.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _seed_grace_loop(conn)
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, execution_id)
        assert claimed is not None
        kb._set_worker_pid(conn, execution_id, 424242)

        cancellation = kb.cancel_grace_delegation(
            conn,
            execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            requested_by="kj",
            requested_message_id="msg-stop",
            reason="KJ requested stop.",
        )

        assert cancellation is not None
        assert cancellation["workers"] == [
            {
                "task_id": execution_id,
                "stage": "execution",
                "pid": 424242,
                "claim_lock": claimed.claim_lock,
            }
        ]
        assert kb.get_task(conn, execution_id).status == "blocked"
        assert kb.get_task(conn, review_id).status == "blocked"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, execution_id).status == "blocked"
        assert kb.get_task(conn, review_id).status == "blocked"
        assert kb.latest_run(conn, execution_id).outcome == "cancelled"
        assert kb.latest_run(conn, review_id).outcome == "cancelled"
        delegation = kb.get_grace_delegation(conn, delegation_id="gd-cancel")
        assert delegation["state"] == "cancelled"
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "cancelled"
        assert kb.list_notify_subs(conn, execution_id)
        assert kb.list_notify_subs(conn, review_id) == []

        alive = iter((True, False))
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: next(alive))
        signals = []
        terminations = kb.terminate_cancelled_workers(
            conn,
            cancellation["workers"],
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
        assert signals == [(424242, signal.SIGTERM)]
        assert terminations[0]["terminated"] is True

        # Even if the cancelled process is later reaped as a clean rc=0 exit,
        # it is no longer a running task and cannot become a protocol violation.
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.detect_crashed_workers(conn) == []
        kinds = [event.kind for event in kb.list_events(conn, execution_id)]
        assert "cancelled" in kinds
        assert "cancellation_termination" in kinds
        assert "protocol_violation" not in kinds
        assert "gave_up" not in kinds


def test_cancel_grace_loop_is_idempotent_and_lane_bound(tmp_path):
    db_path = tmp_path / "cancel-lane.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, _review_id = _seed_grace_loop(conn)
        assert kb.cancel_grace_delegation(
            conn,
            execution_id,
            platform="telegram",
            chat_id="wrong-chat",
            thread_id="2120",
            requested_by="kj",
            requested_message_id="wrong-lane",
        ) is None

        first = kb.cancel_grace_delegation(
            conn,
            execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            requested_by="kj",
            requested_message_id="stop-1",
        )
        second = kb.cancel_grace_delegation(
            conn,
            execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            requested_by="kj",
            requested_message_id="stop-2",
        )

        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        cancelled_events = [
            event
            for event in kb.list_events(conn, execution_id)
            if event.kind == "cancelled"
        ]
        assert len(cancelled_events) == 1
