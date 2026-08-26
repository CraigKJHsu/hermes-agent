from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _isolated_openclaw_loop_backend(monkeypatch):
    """Delegate contract tests must not depend on the live OpenClaw gateway."""

    def accepted(args, **_kwargs):
        backend_agent_id = args.get("backend_agent_id") or "missioncrew-executor"
        return {
            "task_id": args["task_id"],
            "status": "queued",
            "summary": "OpenClaw accepted the Loop Contract.",
            "artifacts": [],
            "tool_calls": [{"name": "openclaw_bridge_http"}],
            "audit_log": ["accepted"],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "Poll.",
            "protocol_version": "2.0",
            "protocol_correlated": True,
            "delegation_id": args["delegation_id"],
            "attempt_id": args["attempt_id"],
            "contract_fingerprint": args["contract_fingerprint"],
            "identity_correlated": True,
            "backend_run_id": "openclaw-loop-test-run",
            "backend_agent_id": backend_agent_id,
            "backend_session_key": f"agent:{backend_agent_id}:subagent:test-loop",
        }

    monkeypatch.setattr(
        "proactive.openclaw_async_executor.delegate_loop_contract_to_openclaw",
        accepted,
    )


def _args():
    return {
        "original_request": "請執行下一步",
        "grace_interpretation": "只完成 ingrids.app Lighthouse 文件核對",
        "trigger": "KJ 明確要求執行",
        "objective": "完成 Lighthouse 文件核對",
        "deliverables": ["核對報告"],
        "non_goals": ["不發布"],
        "scope_allowed": ["Project Lighthouse 文件"],
        "scope_forbidden": ["二手拍賣與其他 Topic"],
        "verification_checks": ["逐檔核對"],
        "evidence_required": ["檔案路徑與檢查結果"],
        "acceptance_criteria": ["證據齊全且未跨專案"],
        "stop_success": ["全部驗收條件通過"],
        "stop_blocked": ["需要外部發布批准"],
        "stop_no_progress": ["相同失敗連續兩次"],
        "max_iterations": 6,
        "max_runtime_seconds": 1800,
        "working_memory": ["本次任務狀態"],
        "promote_on_acceptance": ["已驗證結論"],
        "task_type": "research",
        "completion_mode": "terminal",
        "risk_level": "low",
        "approved": False,
    }


def _nested_args():
    legacy = _args()
    return {
        "original_request": legacy["original_request"],
        "grace_interpretation": legacy["grace_interpretation"],
        "trigger": legacy["trigger"],
        "goal": {
            "objective": legacy["objective"],
            "deliverables": legacy["deliverables"],
            "non_goals": legacy["non_goals"],
        },
        "scope": {
            "allowed": legacy["scope_allowed"],
            "forbidden": legacy["scope_forbidden"],
        },
        "verification": {
            "checks": legacy["verification_checks"],
            "evidence_required": legacy["evidence_required"],
            "acceptance_criteria": legacy["acceptance_criteria"],
        },
        "stop_rules": {
            "success": legacy["stop_success"],
            "blocked": legacy["stop_blocked"],
            "no_progress": legacy["stop_no_progress"],
            "max_iterations": legacy["max_iterations"],
            "max_runtime_seconds": legacy["max_runtime_seconds"],
        },
        "memory": {
            "working": legacy["working_memory"],
            "promote_on_acceptance": legacy["promote_on_acceptance"],
        },
        "task_type": legacy["task_type"],
        "completion_mode": legacy["completion_mode"],
        "risk_level": legacy["risk_level"],
        "approved": legacy["approved"],
    }


def _external_listing_args():
    args = _nested_args()
    args["external_targets"] = ["Facebook Marketplace", "蝦皮賣場"]
    args["goal"]["objective"] = "將二手商品正式發布到 Facebook 與蝦皮"
    args["goal"]["deliverables"] = ["Facebook 刊登完成", "蝦皮刊登完成"]
    args["scope"]["allowed"] = ["Facebook Marketplace", "蝦皮賣場"]
    args["task_type"] = "secondhand_commerce_cross_platform_listing"
    args["risk_level"] = "medium"
    return args


def _configure_secondhand_context(tmp_path, monkeypatch, values):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )


def _bind_callback_delegation(
    conn,
    *,
    execution_id,
    review_id,
    contract_fingerprint,
    suffix,
):
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', 'chat-1', '2', ?, ?, '{}', 0,
                  'queued', ?, ?, ?, ?)
        """,
        (
            f"gd-{suffix}",
            contract_fingerprint,
            f"request-{suffix}",
            "agent:main:telegram:group:chat-1:2",
            "grace-session-1",
            execution_id,
            review_id,
            now,
            now,
        ),
    )


@pytest.mark.parametrize(
    ("message", "accepted"),
    [
        ("核准 abc123", True),
        ("好的，核准 abc123", True),
        ("好吧，核准 abc123", True),
        ("好 核准 abc123。", True),
        ("收到：核准 abc123，謝謝！", True),
        ("可以，核准 abc123 麻煩了", True),
        ("好的\n核准 abc123", False),
        ("\n核准 abc123", False),
        ("核准 abc123\n", False),
        ("核准 abc123\r\n", False),
        ("轉述：核准 abc123", False),
        ("核准 abc123，並直接發布", False),
        ("核准 abc123 核准 def456", False),
        ("核准 ABC123", False),
    ],
)
def test_safe_approval_message_allows_only_harmless_framing(
    message,
    accepted,
):
    from plugins.openclaw_bridge.clawops_delegate import (
        _is_safe_approval_message,
    )

    assert _is_safe_approval_message(message, "abc123") is accepted


def test_delegate_rejects_explicit_stop_instead_of_creating_cancel_task(
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_SOURCE": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_MESSAGE_ID": "stop-1",
        "HERMES_SESSION_MESSAGE_TEXT": "停止執行",
        "HERMES_SESSION_INTERNAL": "false",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(_nested_args()))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "clawops_cancel" in result["reason"]


def test_delegate_creates_execution_and_terra_review_cards(tmp_path, monkeypatch):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '270'\n"
        "    topic_name: ingrids.app\n    project: ingrids_marketing\n"
        "    memory_namespace: topic:270/ingrids\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "270",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:270",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-99",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )

    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    result = json.loads(handle_clawops_delegate(_args()))

    assert result["status"] == "queued"
    assert result["project"] == "ingrids_marketing"
    with kb.connect_closing(db_path) as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["grace_review_task_id"])
        parents = kb.parent_ids(conn, review.id)
        callback = kb.get_grace_loop_callback(conn, review.id)
    assert execution.assignee == "openclaw"
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert execution.goal_mode is True
    assert "original user wording is audit evidence only" in execution.body
    assert "請執行下一步" not in execution.body
    assert "original_request_sha256" in execution.body
    assert "Do not use a review-required block for this execution card" in execution.body
    assert "metadata.approval_needed" in execution.body
    assert review.assignee == "default"
    assert review.status == "todo"
    assert parents == [execution.id]
    assert execution.session_id is None
    assert review.session_id == (
        f"grace-loop:{result['delegation_id']}:review"
    )
    assert callback["execution_task_id"] == execution.id
    assert callback["chat_type"] == "group"
    assert callback["notifier_profile"] == "default"
    assert callback["session_key"] == "agent:main:telegram:group:chat-1:270"
    assert callback["session_id"] == "grace-session-1"
    assert callback["message_id"] == "msg-99"
    assert len(callback["contract_fingerprint"]) == 64
    with kb.connect_closing(db_path) as conn:
        execution_subs = kb.list_notify_subs(conn, execution.id)
        review_subs = kb.list_notify_subs(conn, review.id)
    assert execution_subs[0]["notifier_profile"] == "default"
    assert review_subs[0]["notifier_profile"] == "default"


def test_delegate_accepts_canonical_nested_loop_contract(tmp_path, monkeypatch):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-canonical",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(_nested_args()))
    assert result["status"] == "queued"
    assert result["project"] == "secondhand_commerce"
    assert result["execution_task_id"]
    assert result["grace_review_task_id"]


def test_codex_local_operator_authorizes_external_action_without_telegram_spoof(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    aliases: [secondhand_commerce]\n"
        "    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    values = {
        "HERMES_SESSION_PLATFORM": "codex",
        "HERMES_SESSION_SOURCE": "codex_local_operator",
        "HERMES_SESSION_CHAT_ID": "",
        "HERMES_SESSION_THREAD_ID": "",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "codex:thread:019fc18d",
        "HERMES_SESSION_ID": "codex-session-1",
        "HERMES_SESSION_MESSAGE_ID": "codex-turn-1",
        "HERMES_CODEX_AUTHORIZATION_ID": "codex-auth-1",
        "HERMES_CODEX_THREAD_ID": "019fc18d",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    args = _external_listing_args()
    args["context_alias"] = "secondhand_commerce"
    args["approved"] = True

    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    assert result["assigned_agent"] == "openclaw"
    with kb.connect_closing(db_path) as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["grace_review_task_id"])
        delegation = conn.execute(
            "SELECT * FROM grace_delegations WHERE delegation_id = ?",
            (result["delegation_id"],),
        ).fetchone()
        challenge = conn.execute(
            "SELECT * FROM grace_approval_challenges ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert review.executor_backend == "hermes"
    assert delegation["platform"] == "telegram"
    assert delegation["chat_id"] == "chat-1"
    assert delegation["thread_id"] == "2"
    assert delegation["approved_message_id"] == "codex-approval:codex-auth-1"
    assert challenge["requested_message_id"] == "codex-request:codex-auth-1"
    assert challenge["approved_message_id"] == "codex-approval:codex-auth-1"
    assert '"source": "codex_local_operator"' in execution.body
    assert '"requested_by": "codex_local_operator"' in execution.body


def test_codex_local_operator_rejects_spoofed_request_instance(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    aliases: [secondhand_commerce]\n"
        "    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    values = {
        "HERMES_SESSION_PLATFORM": "codex",
        "HERMES_SESSION_SOURCE": "codex_local_operator",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "codex:thread:019fc18d",
        "HERMES_SESSION_ID": "codex-session-1",
        "HERMES_SESSION_MESSAGE_ID": "codex-turn-1",
        "HERMES_CODEX_AUTHORIZATION_ID": "codex-auth-1",
        "HERMES_CODEX_THREAD_ID": "019fc18d",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    args = _external_listing_args()
    args["context_alias"] = "secondhand_commerce"
    args["approved"] = True
    args["request_instance_id"] = "model-chosen-instance"

    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert "authorization-derived instance" in result["reason"]


def test_delegate_lifts_canonical_siblings_misnested_inside_scope(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-misnested",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args["scope"].update({
        "trigger": args.pop("trigger"),
        "verification": args.pop("verification"),
        "stop_rules": args.pop("stop_rules"),
        "task_type": args.pop("task_type"),
    })
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    assert result["execution_task_id"]
    assert result["grace_review_task_id"]


def test_identical_new_request_message_creates_a_new_delegation(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "request-1",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    spoofed_args = _nested_args()
    spoofed_args["request_instance_id"] = "model-chosen-instance"
    spoofed = json.loads(handle_clawops_delegate(spoofed_args))
    assert spoofed["status"] == "rejected"
    assert "authenticated message-derived instance" in spoofed["reason"]

    first = json.loads(handle_clawops_delegate(_nested_args()))
    values["HERMES_SESSION_MESSAGE_ID"] = "request-2"
    second = json.loads(handle_clawops_delegate(_nested_args()))

    assert first["status"] == "queued"
    assert second["status"] == "queued"
    assert second["delegation_id"] != first["delegation_id"]
    assert second["execution_task_id"] != first["execution_task_id"]
    assert second["grace_review_task_id"] != first["grace_review_task_id"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert len(kb.list_tasks(conn)) == 4


def test_delegate_internal_callback_can_create_but_not_consume_external_approval(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "other-allowed-user",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "callback-anchor",
        "HERMES_SESSION_MESSAGE_TEXT": "[SYSTEM: callback]",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "callback-owner",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn,
            title="review",
            parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="a" * 64,
            suffix="internal-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="a" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="callback-owner",
        )
    args = _external_listing_args()
    args["approved"] = False
    args["origin_callback_review_id"] = review_id
    args["origin_callback_event_id"] = callback["event_id"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "approval_required"
    assert result["task_created"] is False
    assert result["exact_reply"].startswith("核准 ")
    args["approval_token"] = result["approval_token"]
    consumed = json.loads(handle_clawops_delegate(args))
    assert consumed["status"] == "rejected"
    assert "cannot consume" in consumed["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge = kb.get_grace_approval_challenge(
            conn,
            result["approval_token"],
        )
        assert challenge["state"] == "pending"
        assert len(kb.list_tasks(conn)) == 2


def test_delegate_rejects_approved_external_work_from_non_owner(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "other-allowed-user",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-other",
        "HERMES_SESSION_MESSAGE_TEXT": "核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    args["approved"] = True
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "authenticated configured owner" in result["reason"]


def test_delegate_rejects_approved_external_work_without_persisted_owner(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "allowed-user",
        "HERMES_SESSION_OWNER_USER_ID": "",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-legacy-shared-session",
        "HERMES_SESSION_MESSAGE_TEXT": "核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    args["approved"] = True
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "explicitly configured owner" in result["reason"]


def test_delegate_records_scope_bound_approval_from_owner_turn(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-kj-request",
        "HERMES_SESSION_MESSAGE_TEXT": "請準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    # The action class, not Grace's boolean, must trigger the approval gate.
    args["approved"] = False
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    challenge_result = json.loads(handle_clawops_delegate(args))

    assert challenge_result["status"] == "approval_required"
    assert challenge_result["task_created"] is False
    token = challenge_result["approval_token"]
    assert challenge_result["exact_reply"] == f"核准 {token}"
    assert "可加" in challenge_result["reply_policy"]
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-kj-approval"
    args["approval_token"] = token

    values["HERMES_SESSION_MESSAGE_TEXT"] = (
        f"好的，核准 {token}，並直接發布"
    )
    expanded_message = json.loads(handle_clawops_delegate(args))
    assert expanded_message["status"] == "rejected"
    assert "不可附帶其他指令" in expanded_message["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        still_pending = kb.get_grace_approval_challenge(conn, token)
    assert still_pending["state"] == "pending"

    values["HERMES_SESSION_MESSAGE_TEXT"] = f"好的，核准 {token}"
    changed_route = json.loads(json.dumps(args))
    changed_route["risk_level"] = "high"
    route_swap = json.loads(handle_clawops_delegate(changed_route))
    assert route_swap["status"] == "rejected"
    assert "bound to another contract" in route_swap["reason"]

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        callback = kb.get_grace_loop_callback(
            conn, result["grace_review_task_id"]
        )
    worker_contract = json.loads(
        execution.body.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    provenance = worker_contract["approval_provenance"]
    assert '"source": "one_time_authenticated_owner_challenge"' in execution.body
    assert '"requested_message_id": "msg-kj-request"' in execution.body
    assert '"approved_message_id": "msg-kj-approval"' in execution.body
    assert '"scope_binding": "exact_loop_contract_fingerprint"' in execution.body
    assert '"internal": false' in execution.body
    assert '"user_id_sha256": "' in execution.body
    assert '"user_id": "kj"' not in execution.body
    assert len(provenance["contract_fingerprint"]) == 64
    assert provenance["contract_fingerprint"] == callback["contract_fingerprint"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge = kb.get_grace_approval_challenge(conn, token)
        delegation = kb.get_grace_delegation(
            conn, delegation_id=result["delegation_id"]
        )
    assert challenge["state"] == "consumed"
    assert challenge["approved_message_id"] == "msg-kj-approval"
    from hermes_cli.telegram_message_path import normalize_message_path

    delegation_path = normalize_message_path(delegation["telegram_message_path"])
    assert delegation_path["inbound_message_id"] == "msg-kj-request"
    approval_hop = next(
        hop for hop in delegation_path["hops"] if hop["stage"] == "human_approval"
    )
    assert approval_hop["identifiers"]["approval_message_id"] == "msg-kj-approval"

    values["HERMES_SESSION_MESSAGE_ID"] = "msg-kj-reuse"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"
    reused = json.loads(handle_clawops_delegate(args))
    assert reused["status"] == "queued"
    assert reused["execution_task_id"] == result["execution_task_id"]
    assert reused["grace_review_task_id"] == result["grace_review_task_id"]

    # Replaying the original request without its consumed token returns the
    # existing queue instead of issuing a second approval challenge.
    args.pop("approval_token")
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-kj-request"
    values["HERMES_SESSION_MESSAGE_TEXT"] = "請準備上架核准"
    replay_without_token = json.loads(handle_clawops_delegate(args))
    assert replay_without_token["status"] == "queued"
    assert replay_without_token["idempotent_replay"] is True
    assert replay_without_token["execution_task_id"] == result["execution_task_id"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge_count = conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0]
    assert challenge_count == 1


def test_approval_token_cannot_escape_to_nonapproval_contract(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    external_args = _external_listing_args()
    challenge = json.loads(handle_clawops_delegate(external_args))
    token = challenge["approval_token"]
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"好的，核准 {token}"

    changed_args = _nested_args()
    changed_args["approval_token"] = token
    changed_args["request_instance_id"] = challenge["request_instance_id"]
    result = json.loads(handle_clawops_delegate(changed_args))

    assert result["status"] == "rejected"
    assert "cannot authorize a non-approval route" in result["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        pending = kb.get_grace_approval_challenge(conn, token)
        tasks = kb.list_tasks(conn)
    assert pending["state"] == "pending"
    assert tasks == []


def test_expired_approval_refresh_preserves_request_instance(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    args = _external_listing_args()
    original = json.loads(handle_clawops_delegate(args))
    old_token = original["approval_token"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        conn.execute(
            "UPDATE grace_approval_challenges SET expires_at = ? "
            "WHERE token = ?",
            (int(time.time()) - 1, old_token),
        )

    values["HERMES_SESSION_MESSAGE_ID"] = "msg-expired-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = ""
    refresh_args = dict(args)
    refresh_args["_approval_refresh_token"] = old_token
    refreshed = json.loads(handle_clawops_delegate(refresh_args))

    assert refreshed["status"] == "approval_required"
    assert refreshed["approval_token"] != old_token
    assert refreshed["request_instance_id"] == original["request_instance_id"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        replacement = kb.get_grace_approval_challenge(
            conn, refreshed["approval_token"],
        )
    assert replacement["request_instance_id"] == original["request_instance_id"]
    assert replacement["requested_message_id"] == "msg-expired-approval"


def test_token_shaped_message_cannot_omit_approval_token_argument(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    args = _external_listing_args()
    challenge = json.loads(handle_clawops_delegate(args))
    token = challenge["approval_token"]
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert "must be validated with its approval_token" in result["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        pending = kb.get_grace_approval_challenge(conn, token)
        tasks = kb.list_tasks(conn)
    assert pending["state"] == "pending"
    assert tasks == []


def test_delegate_fails_closed_for_route_with_controlled_external_capabilities(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-hidden-action",
        "HERMES_SESSION_MESSAGE_TEXT": "繼續處理活動",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args["goal"]["objective"] = "完成活動收尾"
    args["goal"]["deliverables"] = ["通知客戶新的交付時間"]
    args["task_type"] = "product_marketing"
    args["external_targets"] = ["客戶通知管道"]
    args["approved"] = False
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "approval_required"
    assert result["task_created"] is False


def test_marketplace_readonly_target_queues_without_external_approval(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-marketplace-readonly",
        "HERMES_SESSION_MESSAGE_TEXT": "唯讀查核 Marketplace 候選社團",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args["task_type"] = "facebook_marketplace_readonly"
    args["external_targets"] = [
        "Facebook Marketplace listing ID 915975414881937"
    ]
    args["goal"]["objective"] = "唯讀查核 Marketplace 候選社團"
    args["scope"]["forbidden"] = [
        "任何選取、加入、刊登、分享或 Facebook 狀態變更"
    ]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["grace_review_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
    assert execution is not None
    assert review is not None
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["approval_grant_id"] == ""
    assert run.metadata["credential_refs"] == []


def test_scoped_browser_readonly_marketplace_fallback_is_canonicalized(
    tmp_path,
    monkeypatch,
):
    listing_id = "915975414881937"
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-browser-readonly-fallback",
        "HERMES_SESSION_MESSAGE_TEXT": "請接續安全的候選社團只讀階段",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "browser_readonly",
        "risk_level": "low",
        "approved": False,
        "external_targets": [listing_id],
    })
    args["goal"] = {
        "objective": (
            "只讀檢視 Facebook Marketplace listing "
            f"{listing_id} 的 More options → List in more places"
        ),
        "deliverables": ["候選社團名稱與目前可見狀態"],
        "non_goals": ["不勾選或送出"],
    }
    args["scope"] = {
        "allowed": [
            "只讀檢視 Facebook Marketplace listing "
            f"{listing_id} 的 More options → List in more places"
        ],
        "forbidden": [
            "不勾選任何社團 checkbox",
            "不按 Post、Publish 或 Submit",
            "不變更任何 Facebook 外部狀態",
        ],
    }
    args["verification"] = {
        "checks": ["完整讀取 List in more places 可見候選社團"],
        "evidence_required": ["可見名稱與狀態"],
        "acceptance_criteria": ["零 Facebook 狀態變更"],
    }
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued", result
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        challenge_count = conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0]
    assert execution is not None
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert challenge_count == 0
    assert '"task_type": "secondhand_commerce_group_status"' in execution.body
    assert f"Facebook Marketplace listing ID {listing_id}" in execution.body


def test_internal_instructions_artifact_does_not_request_public_approval(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-internal-instructions",
        "HERMES_SESSION_MESSAGE_TEXT": "只更新本 Topic Instructions",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args["task_type"] = "content_draft"
    args["goal"]["objective"] = "只更新本 Topic Instructions"
    args["external_targets"] = [
        "Internal Topic Instructions artifact only — no external platform action"
    ]
    args["scope"]["forbidden"] = ["不得操作任何外部平台"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        delegation = kb.get_grace_delegation(
            conn, delegation_id=result["delegation_id"]
        )
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["approval_grant_id"] == ""
    assert delegation is not None
    assert delegation["approval_required"] == 0


def test_explicit_zh_internal_targets_do_not_request_public_approval(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-zh-internal-instructions",
        "HERMES_SESSION_MESSAGE_TEXT": "只更新本 Topic Instructions",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args["task_type"] = "content_draft"
    args["goal"]["objective"] = "只更新本 Topic Instructions"
    args["external_targets"] = [
        "Facebook Page（僅修訂貼文文案結構與規則，不登入或操作）",
        "Gemini Notebook（僅產出貼入用 Prompt，不登入或操作）",
        "Podcast Hosting／Apple Podcasts（僅產出 Title 與 Description，不上架或操作）",
        "Spotify／Podcast Hosting（僅產出可貼入 Description 與精簡 Instructions 規則，不登入或上架）",
        "Facebook Page（僅校正內部文案與主圖資料，不登入、編輯或發布）",
    ]
    args["scope"]["forbidden"] = ["不得操作任何外部平台"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        delegation = kb.get_grace_delegation(
            conn, delegation_id=result["delegation_id"]
        )
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["allowed_tools"] == ["read", "write", "web_search"]
    assert delegation is not None
    assert delegation["approval_required"] == 0


def test_delegate_retry_resumes_same_saga_after_partial_failure(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-saga-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    from proactive import openclaw_async_executor

    challenge = json.loads(handle_clawops_delegate(args))
    token = challenge["approval_token"]
    args["approval_token"] = token
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-saga-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"

    original_subscribe = openclaw_async_executor.kb.add_notify_sub
    calls = {"count": 0}

    def fail_once(*call_args, **call_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated subscription failure")
        return original_subscribe(*call_args, **call_kwargs)

    monkeypatch.setattr(
        openclaw_async_executor.kb, "add_notify_sub", fail_once,
    )
    first = json.loads(handle_clawops_delegate(args))
    assert first["status"] == "rejected"
    assert "simulated subscription failure" in first["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tasks_after_failure = kb.list_tasks(conn)
    assert tasks_after_failure == []

    monkeypatch.setattr(
        openclaw_async_executor.kb, "add_notify_sub", original_subscribe,
    )
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-saga-retry"
    retry = json.loads(handle_clawops_delegate(args))
    assert retry["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tasks_after_retry = kb.list_tasks(conn)
        delegation = kb.get_grace_delegation(
            conn, delegation_id=retry["delegation_id"],
        )
        execution = kb.get_task(conn, retry["execution_task_id"])
    assert len(tasks_after_retry) == 2
    assert delegation["state"] == "queued"
    assert execution.status in {"ready", "running"}


def test_delegate_token_is_bound_to_resolved_worker_route(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-route-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    from plugins.openclaw_bridge import clawops_delegate as delegate

    challenge = json.loads(delegate.handle_clawops_delegate(args))
    token = challenge["approval_token"]
    original_route = delegate.route_clawops_objective

    def changed_route(*route_args, **route_kwargs):
        route = original_route(*route_args, **route_kwargs)
        changed = json.loads(json.dumps(route))
        changed["assignment"]["runtime_profile"] = "clawops-browser-v2"
        return changed

    monkeypatch.setattr(delegate, "route_clawops_objective", changed_route)
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-route-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"
    args["approval_token"] = token

    result = json.loads(delegate.handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "bound to another contract" in result["reason"]


def test_delegate_rejects_route_drift_between_authorization_and_enqueue(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-drift-request",
        "HERMES_SESSION_MESSAGE_TEXT": "準備上架核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    from plugins.openclaw_bridge import clawops_delegate

    challenge = json.loads(handle_clawops_delegate(args))
    token = challenge["approval_token"]
    args["approval_token"] = token
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-drift-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"
    original_route = clawops_delegate.route_clawops_objective

    def drifted_route(*route_args, **route_kwargs):
        route = original_route(*route_args, **route_kwargs)
        changed = json.loads(json.dumps(route))
        changed["assignment"]["allowed_tools"].append("new_external_tool")
        return changed

    monkeypatch.setattr(
        clawops_delegate, "route_clawops_objective", drifted_route,
    )
    rejected = json.loads(handle_clawops_delegate(args))
    assert rejected["status"] == "rejected"
    assert "bound to another contract" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tasks = kb.list_tasks(conn)
    assert tasks == []

    monkeypatch.setattr(
        clawops_delegate, "route_clawops_objective", original_route,
    )
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-drift-retry"
    resumed = json.loads(handle_clawops_delegate(args))
    assert resumed["status"] == "queued"


def test_delegate_rejects_unroutable_contract_before_requesting_approval(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-unroutable",
        "HERMES_SESSION_MESSAGE_TEXT": "準備核准",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _external_listing_args()
    args["risk_level"] = "critical"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "contract_risk_level_limit" in result["reason"]
    assert not (tmp_path / "kanban.db").exists()


def test_model_visible_schema_exposes_required_nested_parameters():
    from jsonschema import validate
    from plugins.openclaw_bridge.clawops_delegate import (
        CLAWOPS_DELEGATE_PARAMETERS,
        CLAWOPS_DELEGATE_SCHEMA,
    )
    from proactive.hubops_routing import registered_worker_task_types

    assert CLAWOPS_DELEGATE_SCHEMA["parameters"] is CLAWOPS_DELEGATE_PARAMETERS
    assert CLAWOPS_DELEGATE_PARAMETERS["properties"]["goal"]["required"] == [
        "objective", "deliverables", "non_goals",
    ]
    assert "goal" in CLAWOPS_DELEGATE_PARAMETERS["required"]
    assert "contract_fingerprint" not in CLAWOPS_DELEGATE_PARAMETERS["properties"]
    assert CLAWOPS_DELEGATE_PARAMETERS["properties"]["task_type"]["enum"] == [
        *registered_worker_task_types(),
        "secondhand_commerce_group_status",
    ]
    assert "user_facing_delivery" in CLAWOPS_DELEGATE_PARAMETERS["properties"]
    assert "listing" not in CLAWOPS_DELEGATE_PARAMETERS["properties"]["task_type"]["enum"]
    validate(_nested_args(), CLAWOPS_DELEGATE_PARAMETERS)


def test_callback_outcome_requires_active_internal_callback(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="a" * 64,
            suffix="active-internal-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="a" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="test-lease",
        )
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "test-lease",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import (
        handle_grace_callback_outcome,
    )
    args = {
        "review_task_id": review_id,
        "event_id": callback["event_id"],
        "outcome_kind": "closed",
        "payload": {"summary": "complete originating outcome"},
    }

    unsupported_block = json.loads(handle_grace_callback_outcome({
        **args,
        "outcome_kind": "approval_blocked",
        "payload": {
            "action": "publish",
            "platform": "Facebook",
            "scope": "one listing",
            "exact_question": "核准 missing",
        },
    }))
    assert unsupported_block["status"] == "rejected"
    assert "exactly one pending challenge" in unsupported_block["reason"]
    recorded = json.loads(handle_grace_callback_outcome(args))
    assert recorded["status"] == "recorded"
    replay = json.loads(handle_grace_callback_outcome(args))
    assert replay["status"] == "recorded"
    changed = json.loads(handle_grace_callback_outcome({
        **args,
        "outcome_kind": "approval_blocked",
        "payload": {
            "action": "publish",
            "platform": "Facebook",
            "scope": "one listing",
            "exact_question": "是否核准發布？",
        },
    }))
    assert changed["status"] == "rejected"
    assert "write-once" in changed["reason"]
    values["HERMES_SESSION_INTERNAL"] = "false"
    rejected = json.loads(handle_grace_callback_outcome(args))
    assert rejected["status"] == "rejected"
    assert "only inside an internal callback" in rejected["reason"]


def test_internal_continuation_requires_accepted_owner_fenced_callback(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "callback-anchor",
        "HERMES_SESSION_MESSAGE_TEXT": "[SYSTEM: callback]",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "owner-a",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="f" * 64,
            suffix="owner-fenced-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="f" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="owner-a",
        )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    args = _nested_args()
    args.update({
        "origin_callback_review_id": review_id,
        "origin_callback_event_id": callback["event_id"],
    })

    queued = json.loads(handle_clawops_delegate(args))
    assert queued["status"] == "queued"

    changed = json.loads(json.dumps(args))
    changed["goal"]["objective"] = "建立另一個不同的後續工作"
    rejected = json.loads(handle_clawops_delegate(changed))
    assert rejected["status"] == "rejected"
    assert "already reserved another continuation" in rejected["reason"]

    values["HERMES_GRACE_CALLBACK_LEASE_OWNER"] = "owner-b"
    wrong_owner = json.loads(handle_clawops_delegate(args))
    assert wrong_owner["status"] == "rejected"
    assert "not owned by this callback lease" in wrong_owner["reason"]


def test_blocked_callback_cannot_create_internal_continuation(
    tmp_path,
    monkeypatch,
):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "callback-anchor",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "owner-a",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="1" * 64,
            suffix="blocked-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="1" * 64,
        )
        assert kb.block_task(conn, review_id, reason="missing decision")
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="owner-a",
        )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    args = _nested_args()
    args.update({
        "origin_callback_review_id": review_id,
        "origin_callback_event_id": callback["event_id"],
    })

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "requires an accepted Grace-review" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert kb.get_grace_delegation(
            conn, contract_fingerprint="does-not-exist",
        ) is None


def test_callback_outcome_uses_originating_nondefault_board(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.create_board("secondhand")
    with kb.connect_closing(board="secondhand") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="b" * 64,
            suffix="nondefault-board-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="b" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="board-lease",
        )
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_BOARD": "secondhand",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "board-lease",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import (
        handle_grace_callback_outcome,
    )

    result = json.loads(handle_grace_callback_outcome({
        "review_task_id": review_id,
        "event_id": callback["event_id"],
        "outcome_kind": "closed",
        "payload": {"summary": "complete"},
    }))

    assert result["status"] == "recorded"
    with kb.connect_closing(board="secondhand") as conn:
        recorded = kb.get_grace_loop_callback(conn, review_id)
    assert recorded["outcome_kind"] == "closed"


def test_fresh_approval_continuation_preserves_nondefault_board(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    kb.create_board("secondhand")
    with kb.connect_closing(board="secondhand") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="c" * 64,
            suffix="approval-continuation-callback",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="c" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="board-lease",
        )
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "callback-anchor",
        "HERMES_SESSION_MESSAGE_TEXT": "[SYSTEM: callback]",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_BOARD": "secondhand",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "board-lease",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import (
        handle_clawops_delegate,
        handle_grace_callback_outcome,
    )

    args = _external_listing_args()
    args.update({
        "origin_callback_review_id": review_id,
        "origin_callback_event_id": callback["event_id"],
        "origin_callback_board": "secondhand",
    })
    internal_challenge = json.loads(handle_clawops_delegate(args))
    assert internal_challenge["status"] == "approval_required"
    changed_contract = json.loads(json.dumps(args))
    changed_contract["goal"]["objective"] += "（改成另一個核准範圍）"
    second_challenge = json.loads(handle_clawops_delegate(changed_contract))
    assert second_challenge["status"] == "rejected"
    assert "already created another approval challenge" in second_challenge["reason"]
    false_close = json.loads(handle_grace_callback_outcome({
        "review_task_id": review_id,
        "event_id": callback["event_id"],
        "outcome_kind": "closed",
        "payload": {"summary": "incorrectly closed despite approval challenge"},
    }))
    assert false_close["status"] == "rejected"
    assert "pending approval challenge" in false_close["reason"]
    outcome = json.loads(handle_grace_callback_outcome({
        "review_task_id": review_id,
        "event_id": callback["event_id"],
        "outcome_kind": "approval_blocked",
            "payload": {
                "action": args["goal"]["objective"],
                "platform": internal_challenge["platform"],
                "scope": internal_challenge["scope"],
                "exact_question": internal_challenge["exact_reply"],
            },
    }))
    assert outcome["status"] == "recorded"
    with kb.connect_closing(board="secondhand") as conn:
        assert kb.finish_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="board-lease",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE grace_approval_challenges SET expires_at = 0 "
                "WHERE token = ?",
                (internal_challenge["approval_token"],),
            )

    values.update({
        "HERMES_SESSION_MESSAGE_ID": "fresh-request",
        "HERMES_SESSION_MESSAGE_TEXT": "請進行上架核准",
        "HERMES_SESSION_INTERNAL": "false",
        "HERMES_GRACE_CALLBACK_BOARD": "",
    })
    wrong_board_args = json.loads(json.dumps(args))
    wrong_board_args["origin_callback_board"] = "default"
    wrong_board = json.loads(handle_clawops_delegate(wrong_board_args))
    assert wrong_board["status"] == "rejected"
    assert "durable approval checkpoint" in wrong_board["reason"]
    challenge = json.loads(handle_clawops_delegate(args))
    assert challenge["status"] == "approval_required"
    assert challenge["approval_token"] != internal_challenge["approval_token"]
    token = challenge["approval_token"]

    values["HERMES_SESSION_MESSAGE_ID"] = "fresh-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"
    approval_args = json.loads(json.dumps(args))
    approval_args["approval_token"] = token
    approval_args.pop("origin_callback_review_id")
    approval_args.pop("origin_callback_event_id")
    approval_args.pop("origin_callback_board")
    queued = json.loads(handle_clawops_delegate(approval_args))

    assert queued["status"] == "queued"
    with kb.connect_closing(board="secondhand") as conn:
        delegation = kb.get_grace_delegation(
            conn, delegation_id=queued["delegation_id"],
        )
        tasks = kb.list_tasks(conn)
    assert delegation["origin_review_task_id"] == review_id
    assert delegation["origin_event_id"] == callback["event_id"]
    assert delegation["state"] == "queued"
    assert len(tasks) == 4


def test_delegate_rejects_unknown_topic_without_creating_task(tmp_path, monkeypatch):
    registry = tmp_path / "registry.yaml"
    registry.write_text("version: 1\ncontexts: []\n", encoding="utf-8")
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "chat-1", "HERMES_SESSION_THREAD_ID": "999"}.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    result = json.loads(handle_clawops_delegate(_args()))
    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert not db_path.exists()


def test_scheduled_delegate_resolves_explicit_context_alias(tmp_path, monkeypatch):
    from gateway.session_context import (
        begin_cron_run_state,
        get_cron_functional_error,
    )

    begin_cron_run_state()
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-2\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    aliases: [auction_listing]\n    memory_namespace: topic:2/secondhand\n"
        "  - platform: telegram\n    chat_id: chat-2\n    thread_id: '3'\n"
        "    topic_name: 二手拍賣備援\n    project: secondhand_commerce\n"
        "    aliases: [auction_listing_alt]\n"
        "    memory_namespace: topic:3/secondhand\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    session_values = {
        "HERMES_SESSION_PLATFORM": "",
        "HERMES_SESSION_SOURCE": "cron",
        "HERMES_SESSION_KEY": "cron:4be652d3f356",
        "HERMES_SESSION_ID": "cron_4be652d3f356_20260730_213000",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": session_values.get(key, default),
    )
    args = _args()
    args["context_alias"] = "auction_listing"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))
    assert result["status"] == "queued"
    assert result["project"] == "secondhand_commerce"

    replay = json.loads(handle_clawops_delegate(args))
    assert replay["status"] == "queued"
    assert replay["execution_task_id"] == result["execution_task_id"]
    assert replay["grace_review_task_id"] == result["grace_review_task_id"]

    equivalent_args = dict(args)
    equivalent_args["context_alias"] = "  auction_listing  "
    equivalent = json.loads(handle_clawops_delegate(equivalent_args))
    assert equivalent["status"] == "queued"
    assert equivalent["execution_task_id"] == result["execution_task_id"]

    alternate_context_args = dict(args)
    alternate_context_args["context_alias"] = "auction_listing_alt"
    alternate_context = json.loads(
        handle_clawops_delegate(alternate_context_args)
    )
    assert alternate_context["status"] == "queued"
    assert alternate_context["execution_task_id"] != result["execution_task_id"]

    distinct_args = dict(args)
    distinct_args["grace_interpretation"] = "完成另一份獨立文件核對"
    distinct_args["objective"] = "完成另一份獨立文件核對"
    distinct_args["deliverables"] = ["另一份核對報告"]
    distinct = json.loads(handle_clawops_delegate(distinct_args))
    assert distinct["status"] == "queued"
    assert distinct["execution_task_id"] != result["execution_task_id"]

    spoofed_args = dict(args)
    spoofed_args["request_instance_id"] = "550e8400-e29b-41d4-a716-446655440000"
    spoofed = json.loads(handle_clawops_delegate(spoofed_args))
    assert spoofed["status"] == "rejected"
    assert "scheduler-derived instance" in spoofed["reason"]
    assert "scheduler-derived instance" in get_cron_functional_error()

    def fail_database_open(*_args, **_kwargs):
        raise OSError("database unavailable")

    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(kb, "connect_closing", fail_database_open)
        with pytest.raises(OSError, match="database unavailable"):
            handle_clawops_delegate(args)
    assert get_cron_functional_error() == "database unavailable"

    def fail_without_message(*_args, **_kwargs):
        raise RuntimeError

    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(kb, "connect_closing", fail_without_message)
        empty_failure = json.loads(handle_clawops_delegate(args))
    assert empty_failure["status"] == "rejected"
    assert empty_failure["reason"] == "RuntimeError"
    assert get_cron_functional_error() == "RuntimeError"

    recovered = json.loads(handle_clawops_delegate(args))
    assert recovered["status"] == "queued"
    assert recovered["execution_task_id"] == result["execution_task_id"]
    assert get_cron_functional_error() == ""


def test_scheduled_high_risk_browser_delegate_gets_task_scoped_authorization(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-2\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    aliases: [secondhand_commerce]\n    memory_namespace: topic:2/secondhand\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": {"HERMES_SESSION_PLATFORM": "cron"}.get(key, default),
    )
    args = _nested_args()
    args.update(
        context_alias="secondhand_commerce",
        request_instance_id="cron-secondhand-run-1",
        task_type="secondhand_commerce_cross_platform_listing",
        risk_level="high",
        approved=True,
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["task_created"] is False
    assert "Scheduled jobs cannot authorize external actions" in result["reason"]
    assert not db_path.exists()
