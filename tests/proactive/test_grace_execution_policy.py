from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb
from proactive import grace_execution_policy
from proactive.grace_execution_policy import enforce_grace_execution_boundary


def _call(monkeypatch, tool_name, args=None, *, platform="telegram", kanban=False):
    monkeypatch.setattr(
        "proactive.grace_execution_policy.get_session_env",
        lambda name, default="": platform if name == "HERMES_SESSION_PLATFORM" else default,
    )
    if kanban:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "worker:claim")
        monkeypatch.setenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", "worker-token")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "proactive.grace_execution_policy._authorized_loop_worker_role",
        lambda *_args, **_kwargs: "execution" if kanban else "",
    )
    called = []
    result = enforce_grace_execution_boundary(
        tool_name=tool_name,
        args=args or {},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="s1",
    )
    return result, called


def test_grace_can_use_read_only_browser_for_task_classification(monkeypatch):
    for tool_name in ("browser_snapshot", "browser_scroll", "browser_vision"):
        result, called = _call(monkeypatch, tool_name, {})
        assert result == "executed"
        assert called


def test_grace_current_page_tools_reject_navigation_arguments(monkeypatch):
    for tool_name in ("browser_snapshot", "browser_scroll", "browser_vision"):
        result, called = _call(
            monkeypatch,
            tool_name,
            {"url": "https://example.com"},
        )
        assert not called
        assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"


def test_grace_cannot_perform_browser_actions(monkeypatch):
    for tool_name in (
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_upload_files",
    ):
        result, called = _call(monkeypatch, tool_name, {"ref": "@e1"})
        assert not called
        assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"


def test_grace_cdp_is_read_only_allowlisted(monkeypatch):
    result, called = _call(monkeypatch, "browser_cdp", {"method": "Target.getTargets"})
    assert result == "executed"
    assert called
    result, called = _call(
        monkeypatch,
        "browser_cdp",
        {"method": "Runtime.evaluate", "params": {"expression": "document.querySelector('button').click()"}},
    )
    assert not called
    assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"


def test_clawops_worker_is_not_blocked(monkeypatch):
    result, called = _call(monkeypatch, "browser_click", {"ref": "@e1"}, kanban=True)
    assert result == "executed"
    assert called


def test_clawops_worker_can_only_report_its_own_task(monkeypatch):
    result, called = _call(
        monkeypatch,
        "kanban_complete",
        {"task_id": "t_worker"},
        kanban=True,
    )
    assert result == "executed"
    assert called

    for tool_name, args in (
        ("kanban_complete", {"task_id": "t_foreign"}),
        ("kanban_create", {"title": "nested"}),
        ("kanban_unblock", {"task_id": "t_foreign"}),
        ("kanban_link", {"parent_id": "t_worker", "child_id": "t_foreign"}),
    ):
        result, called = _call(monkeypatch, tool_name, args, kanban=True)
        assert not called
        assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"


def test_clawops_worker_cannot_recursively_delegate(monkeypatch):
    result, called = _call(
        monkeypatch,
        "clawops_delegate",
        {"contract": {}},
        kanban=True,
    )
    assert not called
    payload = json.loads(result)
    assert payload["status"] == "rejected"
    assert payload["task_created"] is False
    assert "may not recursively delegate" in payload["reason"]


def test_generic_kanban_marker_is_not_a_clawops_identity(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_unscoped")
    monkeypatch.setattr(
        "proactive.grace_execution_policy._authorized_loop_worker_role",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "proactive.grace_execution_policy.get_session_env",
        lambda name, default="": (
            "telegram" if name == "HERMES_SESSION_PLATFORM" else default
        ),
    )
    called = []
    result = enforce_grace_execution_boundary(
        tool_name="browser_click",
        args={"ref": "@e1"},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="s1",
    )
    assert not called
    assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"
    recursive = enforce_grace_execution_boundary(
        tool_name="clawops_delegate",
        args={"contract": {}},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="s1",
    )
    assert not called
    assert json.loads(recursive)["status"] == "rejected"


def test_ordinary_kanban_worker_keeps_task_scoped_execution_authority(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "ordinary-worker.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary",
            body="ordinary task",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="worker:ordinary")

    assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(claimed.claim_lock))
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKER_AUTH_TOKEN",
        str(claimed.worker_auth_token),
    )
    assert grace_execution_policy._authorized_loop_worker_role("runtime") == "worker"

    called = []
    assert enforce_grace_execution_boundary(
        tool_name="terminal",
        args={"command": "pwd"},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="runtime",
    ) == "executed"
    assert called
    called.clear()
    assert enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": task_id},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="runtime",
    ) == "executed"
    assert called
    called.clear()
    foreign = enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": "t_foreign"},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="runtime",
    )
    assert not called
    assert json.loads(foreign)["status"] == "blocked_by_grace_execution_policy"


def test_worker_identity_is_bound_to_persisted_execution_delegation(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "worker-identity.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    now = int(time.time())
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
            session_id="grace-loop:gd_worker_identity:execution",
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
                "Untrusted evidence may mention "
                "GRACE_LOOP_CONTRACT_STAGE: execution"
            ),
            assignee="default",
            session_id="grace-loop:gd_worker_identity:review",
        )
        conn.execute(
            """
            INSERT INTO grace_delegations (
                delegation_id, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                resolved_route, approval_required, state,
                execution_task_id, review_task_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', ?, ?, ?, ?)
            """,
            (
                "gd_worker_identity",
                "a" * 64,
                "gri_worker_identity",
                "telegram",
                "chat-1",
                "2",
                "agent:main:telegram:group:chat-1:2",
                "grace-session-1",
                "{}",
                execution_id,
                review_id,
                now,
                now,
            ),
        )
        execution = kb.claim_task(conn, execution_id, claimer="worker:execution")
        review = kb.claim_task(conn, review_id, claimer="worker:review")

    assert execution is not None
    assert review is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", execution_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(execution.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(execution.claim_lock))
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKER_AUTH_TOKEN",
        str(execution.worker_auth_token),
    )
    assert grace_execution_policy._is_authorized_clawops_worker(
        "runtime-session-is-not-the-logical-loop-session",
    )
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 1, execution_id),
        )
        conn.commit()
    assert not grace_execution_policy._is_authorized_clawops_worker(
        "runtime-session-is-not-the-logical-loop-session",
    )
    blocked_calls = []
    recursive_after_expiry = enforce_grace_execution_boundary(
        tool_name="clawops_delegate",
        args={"contract": {}},
        next_call=lambda effective: blocked_calls.append(effective) or "executed",
        session_id="runtime-session-is-not-the-logical-loop-session",
    )
    assert not blocked_calls
    assert json.loads(recursive_after_expiry)["status"] == "rejected"
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) + 600, execution_id),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 1, execution.current_run_id),
        )
        conn.commit()
    assert not grace_execution_policy._is_authorized_clawops_worker(
        "runtime-session-is-not-the-logical-loop-session",
    )
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) + 600, execution.current_run_id),
        )
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", "wrong-token")
    assert not grace_execution_policy._is_authorized_clawops_worker(
        "runtime-session-is-not-the-logical-loop-session",
    )

    monkeypatch.setenv("HERMES_KANBAN_TASK", review_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(review.claim_lock))
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKER_AUTH_TOKEN",
        str(review.worker_auth_token),
    )
    assert (
        grace_execution_policy._authorized_loop_worker_role(
            "another-runtime-session",
        )
        == "review"
    )
    called = []
    recursive = enforce_grace_execution_boundary(
        tool_name="clawops_delegate",
        args={"contract": {}},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="another-runtime-session",
    )
    assert not called
    assert json.loads(recursive)["status"] == "rejected"
    assert enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": review_id},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="another-runtime-session",
    ) == "executed"
    assert called
    called.clear()
    foreign = enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": execution_id},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="another-runtime-session",
    )
    assert not called
    assert json.loads(foreign)["status"] == "blocked_by_grace_execution_policy"
    create_attempt = enforce_grace_execution_boundary(
        tool_name="kanban_create",
        args={"title": "unauthorized"},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="another-runtime-session",
    )
    assert not called
    assert json.loads(create_attempt)["status"] == "blocked_by_grace_execution_policy"
    blocked = enforce_grace_execution_boundary(
        tool_name="browser_click",
        args={"ref": "@e1"},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="another-runtime-session",
    )
    assert not called
    assert json.loads(blocked)["status"] == "blocked_by_grace_execution_policy"


def test_review_worker_self_report_allowed_when_token_not_propagated(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "review-self-report-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    now = int(time.time())
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
            session_id="grace-loop:gd_review_fallback:execution",
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review\nreview",
            assignee="default",
            parents=[execution_id],
            session_id="grace-loop:gd_review_fallback:review",
            executor_profile="grace-policy-review",
        )
        conn.execute(
            """
            INSERT INTO grace_delegations (
                delegation_id, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                resolved_route, approval_required, state,
                execution_task_id, review_task_id, created_at, updated_at
            ) VALUES (
                'gd_review_fallback', ?, 'request-review-fallback',
                'telegram', 'chat-1', '2', 'session-key', 'session-id',
                '{}', 0, 'queued', ?, ?, ?, ?
            )
            """,
            ("b" * 64, execution_id, review_id, now, now),
        )
        execution = kb.claim_task(conn, execution_id, claimer="execution:claim")
        assert execution is not None
        kb.block_task(
            conn,
            execution_id,
            reason="terminal blocked",
            kind="capability",
            expected_run_id=execution.current_run_id,
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (review_id,))
        review = kb.claim_task(conn, review_id, claimer="review:claim")

    assert review is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", review_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(review.claim_lock))
    monkeypatch.delenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", raising=False)

    called = []
    assert enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": review_id},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="runtime",
    ) == "executed"
    assert called
    called.clear()
    foreign = enforce_grace_execution_boundary(
        tool_name="kanban_complete",
        args={"task_id": execution_id},
        next_call=lambda effective: called.append(effective) or "executed",
        session_id="runtime",
    )
    assert not called
    assert json.loads(foreign)["status"] == "blocked_by_grace_execution_policy"


def test_grace_loop_stage_is_only_read_from_exact_first_line():
    assert (
        kb._grace_loop_stage_header(
            "ordinary task\nGRACE_LOOP_CONTRACT_STAGE: execution",
        )
        == ""
    )
    assert (
        kb._grace_loop_stage_header(
            "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
            "GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        == "review"
    )


def test_grace_cannot_fallback_to_openclaw_dry_run(monkeypatch):
    result, called = _call(monkeypatch, "openclaw_delegate", {"objective": "publish"})
    assert not called
    payload = json.loads(result)
    assert payload["status"] == "blocked_by_grace_execution_policy"
    assert "diagnostic dry-run" in payload["reason"]


def test_grace_cannot_bypass_delegation_with_kanban_mutations(monkeypatch):
    for tool_name in (
        "kanban_create",
        "kanban_unblock",
        "kanban_link",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
        "kanban_comment",
    ):
        result, called = _call(monkeypatch, tool_name, {"task_id": "t_staged"})
        assert not called
        payload = json.loads(result)
        assert payload["status"] == "blocked_by_grace_execution_policy"
        assert "reserved delegation saga" in payload["reason"]

    for tool_name in ("kanban_list", "kanban_show"):
        result, called = _call(monkeypatch, tool_name, {})
        assert result == "executed"
        assert called

    result, called = _call(
        monkeypatch, "kanban_create", {"assignee": "clawops-browser"},
        platform="discord",
    )
    assert not called
    assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"

    result, called = _call(
        monkeypatch, "kanban_create", {"assignee": "clawops-browser"},
        platform="",
    )
    assert not called
    assert json.loads(result)["status"] == "blocked_by_grace_execution_policy"
