from __future__ import annotations

import hashlib
import json
import time

import pytest
import yaml

from hermes_cli import kanban_db as kb


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


def _readonly_group_status_args(listing_id, subject):
    args = _nested_args()
    args.update({
        "original_request": (
            "請分別唯讀檢查 Marketplace listing，不要產生核准 token"
        ),
        "grace_interpretation": (
            f"獨立唯讀檢查 {subject} listing {listing_id} 的社團狀態"
        ),
        "task_type": "secondhand_commerce_group_status",
        "risk_level": "low",
        "external_targets": [
            f"facebook:marketplace:{listing_id}",
        ],
        "user_facing_delivery": {
            "delivery": "inline_only",
            "kind": "commerce_group_status",
            "required": True,
            "subject_keys": [
                subject.casefold(),
            ],
        },
    })
    args["goal"]["objective"] = (
        f"唯讀開啟 Marketplace listing {listing_id} 的 More options 與 "
        "List in more places，讀取社團清單"
    )
    args["scope"] = {
        "allowed": [
            "唯讀開啟 Facebook Marketplace listing "
            f"{listing_id} 的 More options → List in more places，"
            "只讀取可見社團名稱與狀態",
        ],
        "forbidden": [
            "不得勾選 checkbox、不得提交、不得發布，"
            "不得變更任何外部狀態",
        ],
    }
    return args


def test_worker_readonly_alias_is_canonicalized_before_contract_build():
    from plugins.openclaw_bridge.clawops_delegate import _canonical_sections

    listing_id = "27700220586305145"
    args = _nested_args()
    args.update({
        "task_type": "facebook_marketplace_readonly",
        "external_targets": [
            f"facebook:marketplace-group-status:{listing_id}",
        ],
        "completion_mode": "intermediate",
    })

    _canonical_sections(args)

    assert args["task_type"] == "secondhand_commerce_group_status"
    assert args["external_targets"] == [
        f"Facebook Marketplace listing ID {listing_id}",
    ]
    assert args["user_facing_delivery"] == {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": [f"facebook_marketplace:{listing_id}"],
    }
    assert args["completion_mode"] == "intermediate"
    assert args["scope"]["allowed"] == [
        "Read-only Facebook Marketplace listing "
        f"{listing_id}: open More options → List in more places and read "
        "visible destination names/status only",
    ]


def test_generic_browser_readonly_fallback_does_not_canonicalize_mutation():
    from plugins.openclaw_bridge.clawops_delegate import _canonical_sections

    listing_id = "27700220586305145"
    args = _nested_args()
    args.update({
        "task_type": "browser_readonly",
        "external_targets": [listing_id],
    })
    args["goal"]["objective"] = (
        "Open Facebook Marketplace More options → List in more places and post"
    )
    args["scope"] = {
        "allowed": [
            "Read-only Facebook Marketplace listing "
            f"{listing_id}: open More options → List in more places, then Post",
        ],
        "forbidden": ["Do not change external state"],
    }

    _canonical_sections(args)

    assert args["task_type"] == "browser_readonly"
    assert args["external_targets"] == [listing_id]
    assert "user_facing_delivery" not in args


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


def _facebook_discovery_review_body(listing_id, group_ids):
    groups = "、".join(group_ids)
    contract = {
        "memory": {
            "working": [
                f"後續外部跨貼必須嚴格綁定群組IDs：{groups}",
            ],
        },
    }
    return (
        "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
        "```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
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


def test_approval_candidate_requires_the_whole_message_to_be_an_approval_turn():
    from plugins.openclaw_bridge.clawops_delegate import (
        _approval_token_candidate,
    )

    assert _approval_token_candidate("核准 fe341e4c447cde20") == (
        "fe341e4c447cde20"
    )
    assert _approval_token_candidate("收到：核准 short，謝謝！") == "short"
    assert _approval_token_candidate(
        "這是唯讀任務，不需要建立發布核准、跨貼核准或多個核准 token。"
    ) == ""
    assert _approval_token_candidate(
        "請檢查 listing 36803832485927906；本次不需要核准 token。"
    ) == ""


def test_listing_id_cannot_be_resolved_as_an_approval_token():
    from plugins.openclaw_bridge.clawops_delegate import (
        _resolve_approval_challenge,
    )

    with pytest.raises(ValueError, match="exactly 16 lowercase hexadecimal"):
        _resolve_approval_challenge("36803832485927906")


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

    assert result["status"] == "queued", result
    assert result["project"] == "ingrids_marketing"
    with kb.connect_closing(db_path) as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["grace_review_task_id"])
        parents = kb.parent_ids(conn, review.id)
        callback = kb.get_grace_loop_callback(conn, review.id)
    assert execution.assignee.startswith("clawops-")
    assert execution.goal_mode is True
    assert "original user wording is audit evidence only" in execution.body
    assert "請執行下一步" not in execution.body
    assert "original_request_sha256" in execution.body
    assert "Do not use a review-required block for this execution card" in execution.body
    assert "metadata.approval_needed" in execution.body
    assert review.assignee == "default"
    assert review.status == "todo"
    assert parents == [execution.id]
    assert execution.session_id == (
        f"grace-loop:{result['delegation_id']}:execution"
    )
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


def test_saved_candidate_filter_returns_inline_data_without_worker(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2'\n"
        "    topic_name: 二手拍賣\n    project: secondhand_commerce\n"
        "    memory_namespace: topic:2/secondhand_commerce\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-candidates",
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": values.get(key, default),
    )
    captured = {}

    def fake_filter(_conn, **kwargs):
        captured.update(kwargs)
        return {
            "recommended": [{
                "destination_name": "二手新舊咖啡設備大賣場",
                "status": "not_posted",
                "status_label": "未刊登",
                "reason": "咖啡設備主題直接相符",
            }],
            "optional": [],
            "excluded": [],
            "task_created": False,
            "browser_opened": False,
            "external_state_changed": False,
        }

    monkeypatch.setattr(kb, "filter_saved_commerce_candidates", fake_filter)
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.compile_and_delegate",
        lambda *args, **kwargs: pytest.fail("candidate filtering delegated"),
    )
    args = _readonly_group_status_args(
        "36803832485927906", "Carimali",
    )
    args["grace_interpretation"] = (
        "只使用已保存的 27 筆證據篩選 Carimali 咖啡機候選社團；"
        "這是內部準備，不接觸 Facebook。"
    )
    args["memory"]["working"] = [
        "Use only preserved saved evidence; 27 named destinations are known."
    ]

    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "saved_candidates_ready"
    assert result["task_created"] is False
    assert result["browser_opened"] is False
    assert captured["listing_id"] == "36803832485927906"
    assert captured["product_category"] == "coffee_equipment"
    with kb.connect_closing(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


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


def test_restart_resume_is_not_misclassified_as_grace_callback(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-restart-resume",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_SESSION_INTERNAL_KIND": "restart_resume",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(_nested_args()))

    assert result["status"] == "queued", result
    assert result["execution_task_id"]
    assert result["grace_review_task_id"]


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


def test_internal_callback_crosspost_is_bound_to_accepted_listing_and_groups(
    tmp_path,
    monkeypatch,
):
    listing_id = "37276725125275496"
    group_ids = ["1333742673375089", "897927458651235"]
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
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "callback-owner",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    review_body = _facebook_discovery_review_body(listing_id, group_ids)
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn,
            title="review",
            body=review_body,
            created_by="grace-loop-compiler",
            parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="f" * 64,
            suffix="facebook-scope-callback",
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
            metadata={
                "review_outcome": "accepted",
                "verified_evidence": {
                    "listing_id": listing_id,
                    "url": (
                        "https://www.facebook.com/marketplace/item/"
                        f"{listing_id}/"
                    ),
                },
            },
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner="callback-owner",
        )
        with kb.write_txn(conn):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, status, started_at, ended_at, outcome, metadata
                ) VALUES (?, 'done', 10, 11, 'completed', ?)
                """,
                (
                    review_id,
                    json.dumps({
                        "review_outcome": "accepted",
                        "verified_evidence": {
                            "listing_id": "99999999999999999",
                        },
                    }),
                ),
            )
        assert kb.accepted_grace_callback_facebook_crosspost_scope(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
        ) == (listing_id, frozenset(group_ids))

    args = _nested_args()
    args.update({
        "task_type": "facebook_marketplace_group_publish",
        "risk_level": "medium",
        "external_targets": [
            f"Facebook Marketplace listing ID: {listing_id}",
            f"Facebook group ID: {group_ids[0]}",
            "Facebook group ID: 709787531936565",
        ],
        "facebook_crosspost": {
            "transport": "browser",
            "marketplace_listing_id": listing_id,
            "group_ids": [group_ids[0], "709787531936565"],
        },
        "origin_callback_review_id": review_id,
        "origin_callback_event_id": callback["event_id"],
    })
    args["goal"]["objective"] = "將既有 Marketplace 刊登跨貼到指定社團"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    drifted = json.loads(handle_clawops_delegate(args))

    assert drifted["status"] == "rejected"
    assert "locked by the origin Loop Contract" in drifted["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0

    omitted = json.loads(json.dumps(args))
    omitted.pop("facebook_crosspost")
    omitted["external_targets"] = ["Facebook Marketplace"]
    missing_binding = json.loads(handle_clawops_delegate(omitted))

    assert missing_binding["status"] == "rejected"
    assert "requires a nonempty facebook_crosspost" in missing_binding["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0

    args["external_targets"] = [
        f"https://www.facebook.com/marketplace/item/{listing_id}/",
        *[
            f"https://www.facebook.com/groups/{group_id}/"
            for group_id in group_ids
        ],
    ]
    args["facebook_crosspost"]["group_ids"] = group_ids
    exact = json.loads(handle_clawops_delegate(args))

    assert exact["status"] == "approval_required"
    assert exact["task_created"] is False


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


def test_saved_evidence_finalization_fails_closed_without_configured_owner(
    monkeypatch,
):
    from plugins.openclaw_bridge import clawops_delegate as delegate_module

    session_values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "allowed-user",
        "HERMES_SESSION_OWNER_USER_ID": "",
        "HERMES_SESSION_INTERNAL": "false",
    }
    monkeypatch.setattr(
        delegate_module,
        "get_session_env",
        lambda key, default="": session_values.get(key, default),
    )

    result = json.loads(delegate_module.handle_clawops_finalize_saved_evidence({
        "execution_task_id": "t_2f6b540f",
        "board": "default",
    }))

    assert result["status"] == "rejected"
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
    assert challenge["state"] == "consumed"
    assert challenge["approved_message_id"] == "msg-kj-approval"

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
    values["HERMES_SESSION_ID"] = "grace-session-2"
    refresh_args = dict(args)
    refresh_args["_approval_refresh_token"] = old_token
    values["HERMES_SESSION_THREAD_ID"] = "1"
    values["HERMES_SESSION_KEY"] = "agent:main:telegram:group:chat-1:1"
    wrong_lane = json.loads(handle_clawops_delegate(refresh_args))
    assert wrong_lane["status"] == "rejected"
    assert "bound to another conversation lane" in wrong_lane["reason"]
    values["HERMES_SESSION_THREAD_ID"] = "2"
    values["HERMES_SESSION_KEY"] = "agent:main:telegram:group:chat-1:2"
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
    assert replacement["session_id"] == "grace-session-2"


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
    from proactive import grace_task_compiler

    challenge = json.loads(handle_clawops_delegate(args))
    token = challenge["approval_token"]
    args["approval_token"] = token
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-saga-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"

    original_subscribe = grace_task_compiler.subscribe_clawops_task
    calls = {"count": 0}

    def fail_once(*call_args, **call_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated subscription failure")
        return original_subscribe(*call_args, **call_kwargs)

    monkeypatch.setattr(
        grace_task_compiler, "subscribe_clawops_task", fail_once,
    )
    first = json.loads(handle_clawops_delegate(args))
    assert first["status"] == "rejected"
    assert "simulated subscription failure" in first["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tasks_after_failure = kb.list_tasks(conn)
    assert len(tasks_after_failure) == 2
    assert next(
        task for task in tasks_after_failure
        if task.title.startswith("ClawOps:")
    ).status == "blocked"

    monkeypatch.setattr(
        grace_task_compiler, "subscribe_clawops_task", original_subscribe,
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
    from proactive import clawops_intake

    challenge = json.loads(handle_clawops_delegate(args))
    token = challenge["approval_token"]
    args["approval_token"] = token
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-drift-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"
    original_route = clawops_intake.route_clawops_objective

    def drifted_route(*route_args, **route_kwargs):
        route = original_route(*route_args, **route_kwargs)
        changed = json.loads(json.dumps(route))
        changed["assignment"]["allowed_tools"].append("new_external_tool")
        return changed

    monkeypatch.setattr(
        clawops_intake, "route_clawops_objective", drifted_route,
    )
    rejected = json.loads(handle_clawops_delegate(args))
    assert rejected["status"] == "rejected"
    assert "route changed after contract authorization" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tasks = kb.list_tasks(conn)
    assert tasks == []

    monkeypatch.setattr(
        clawops_intake, "route_clawops_objective", original_route,
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
    assert "every fresh authenticated execution request" in (
        CLAWOPS_DELEGATE_SCHEMA["description"]
    )
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


def test_facebook_marketplace_crosspost_requires_structured_exact_ids(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-crosspost",
        "HERMES_SESSION_MESSAGE_TEXT": "請準備跨貼",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "facebook_marketplace_group_publish",
        "risk_level": "medium",
        "external_targets": [
            "Facebook Marketplace existing listing → "
            "Facebook group 1333742673375089",
        ],
    })
    args["goal"]["objective"] = "將既有 Marketplace 刊登跨貼到指定社團"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "facebook_crosspost" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0

    args["external_targets"] = [
        "Facebook 市集項目 915975414881937 → 社團 "
        "https://www.facebook.com/groups/1333742673375089",
    ]
    localized_rejected = json.loads(handle_clawops_delegate(args))
    assert localized_rejected["status"] == "rejected"
    assert "facebook_crosspost" in localized_rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0

    args["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "915975414881937",
        "group_ids": ["1333742673375089"],
    }
    args["external_targets"] = [
        "Facebook Marketplace item 915975414881937 → "
        "Facebook Group 1333742673375089",
    ]
    approved_shape = json.loads(handle_clawops_delegate(args))

    assert approved_shape["status"] == "approval_required"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge = kb.get_grace_approval_challenge(
            conn,
            approved_shape["approval_token"],
        )
    persisted = json.loads(challenge["delegation_args"])
    assert persisted["facebook_crosspost"] == args["facebook_crosspost"]


def test_facebook_page_publish_requires_structured_exact_action(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-page-post",
        "HERMES_SESSION_MESSAGE_TEXT": "請準備發布粉專貼文",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    page_url = "https://www.facebook.com/solobizai"
    args.update({
        "task_type": "facebook_page_api_publish",
        "risk_level": "medium",
        "external_targets": [page_url],
    })
    args["goal"]["objective"] = "發布一篇新貼文到 SoloBizAi 粉專"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "facebook_page_post" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0

    args["external_targets"] = ["Meta Graph API"]
    missing_bindings = json.loads(handle_clawops_delegate(args))
    assert missing_bindings["status"] == "rejected"
    assert "message_sha256/image_sha256" in missing_bindings["reason"]

    args["external_targets"] = [
        page_url,
        "Facebook 粉專 SoloBizAi",
    ]
    ambiguous = json.loads(handle_clawops_delegate(args))
    assert ambiguous["status"] == "rejected"
    assert "facebook_page_post" in ambiguous["reason"]

    args["external_targets"] = [
        "Facebook 粉專 https://facebook.com/SoloBizAi/",
    ]
    noncanonical = json.loads(handle_clawops_delegate(args))
    assert noncanonical["status"] == "rejected"
    assert "facebook_page_post" in noncanonical["reason"]

    args["facebook_page_post"] = {
        "action": "create_post",
        "page_url": page_url,
        "transport": "graph_api",
        "message_sha256": "a" * 64,
        "image_sha256": "b" * 64,
    }
    args["external_targets"] = [page_url]
    approval = json.loads(handle_clawops_delegate(args))

    assert approval["status"] == "approval_required"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge = kb.get_grace_approval_challenge(
            conn,
            approval["approval_token"],
        )
    persisted = json.loads(challenge["delegation_args"])
    assert persisted["facebook_page_post"] == args["facebook_page_post"]


def test_marketplace_price_update_is_structured_before_approval_and_browser_guard(
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
        "HERMES_SESSION_ID": "grace-session-price",
        "HERMES_SESSION_MESSAGE_ID": "msg-price-request",
        "HERMES_SESSION_MESSAGE_TEXT": "請將咖啡機價格更新為 89000",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    listing_id = "1666446304587399"
    args = _nested_args()
    args.update({
        "task_type": "browser_ops",
        "risk_level": "medium",
        "external_targets": [
            f"Facebook Marketplace listing ID {listing_id}",
        ],
        "facebook_marketplace_price_update": {
            "action": "update_price",
            "transport": "browser",
            "marketplace_listing_id": listing_id,
            "currency": "TWD",
            "price_twd": 89000,
        },
    })
    args["goal"]["objective"] = (
        "將 Facebook Marketplace 咖啡機售價更新為 NT$89,000"
    )
    args["scope"]["allowed"] = ["僅更新 Facebook Marketplace 價格"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    challenge = json.loads(handle_clawops_delegate(args))

    assert challenge["status"] == "approval_required"
    token = challenge["approval_token"]
    args["approval_token"] = token
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-price-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {token}"

    queued = json.loads(handle_clawops_delegate(args))

    assert queued["status"] == "queued"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task = kb.get_task(conn, queued["execution_task_id"])
        contract = kb._grace_compiled_contract(task.body)
        permission = kb.grace_task_facebook_marketplace_price_update_permission(
            conn,
            task.id,
        )
    assert contract["routing"]["task_type"] == (
        "facebook_marketplace_price_update"
    )
    assert contract["routing"]["resolved"]["task_type"] == (
        "facebook_marketplace_price_update"
    )
    assert contract["routing"]["resolved"]["approval_checklist"] == (
        "Facebook Marketplace Price Update"
    )
    assert contract["external_targets"] == [
        f"Facebook Marketplace item {listing_id}",
    ]
    assert contract["facebook_marketplace_price_update"]["price_twd"] == 89000
    assert permission == {"listing_id": listing_id, "price_twd": 89000}


def test_generic_marketplace_price_update_is_rejected_before_approval(
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
        "HERMES_SESSION_ID": "grace-session-price-missing",
        "HERMES_SESSION_MESSAGE_ID": "msg-price-missing",
        "HERMES_SESSION_MESSAGE_TEXT": "請將 Marketplace 售價更新為 89000",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "browser_ops",
        "risk_level": "medium",
        "external_targets": [
            "Facebook 市集刊登 ID 1666446304587399",
        ],
    })
    args["goal"]["objective"] = "把 Facebook 市集售價改成 89000"
    args["scope"]["allowed"] = ["僅修改價格"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "facebook_marketplace_price_update" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0


def test_page_publish_detector_rejects_legacy_urls_without_misclassifying_groups():
    from plugins.openclaw_bridge.clawops_delegate import (
        _requires_structured_facebook_page_post,
    )

    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/SoloBizAi?locale=zh_TW"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/pages/SoloBizAi/531289396730654"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["http://www.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://m.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/SoloBizAi/groups/"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook Group 123 https://www.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook group name: Fan Page Owners"],
    ) is False
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook 社團：粉絲專頁經營交流"],
    ) is False
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook Group / Page SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook Marketplace / Page SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_republish",
        ["https://www.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "cross_platform_listing",
        ["https://www.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["SoloBizAi 粉專"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["SoloBizAi Page"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Solo Biz AI Page"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["SoloBizAi 粉絲頁"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "facebook_page_api_publish",
        ["https://www.facebook.com/SoloBizAi"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "facebook_page_api_publish",
        ["Meta Graph API"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["SoloBizAI"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["AI BizWeek｜SoloBiz AI 一人公司商業誌"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["Facebook group name: SoloBizAI"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/groups/coffeegear"],
    ) is False
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/groups/../solobizai"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/groups/%2e%2e/solobizai"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/marketplace/%2e%2e/solobizai"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com:443/%73olobizai"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com.:443/%73olobizai"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://www.facebook.com/marketplace/item/123"],
    ) is False
    assert _requires_structured_facebook_page_post(
        "browser_ops",
        ["https://www.facebook.com/solobizai"],
    ) is False
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://web.facebook.com/%73%6f%6c%6f%62%69%7a%61%69"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://mbasic.facebook.com/%73%6f%6c%6f%62%69%7a%61%69"],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        ["https://facebook.com/marketplace/.."],
    ) is True
    assert _requires_structured_facebook_page_post(
        "browser_publish",
        [
            "Facebook group name: AI BizWeek｜SoloBiz AI 一人公司商業誌 "
            "https://www.facebook.com/groups/example"
        ],
    ) is True


def test_marketplace_price_detector_uses_word_bounded_english_verbs():
    from plugins.openclaw_bridge.clawops_delegate import (
        _requires_structured_facebook_marketplace_price_update,
    )

    target = ["Facebook Marketplace item 1666446304587399"]
    assert _requires_structured_facebook_marketplace_price_update(
        "browser_ops",
        target,
        {"objective": "Inspect asset price settings"},
        {"allowed": ["Read visible price settings"]},
    ) is False
    assert _requires_structured_facebook_marketplace_price_update(
        "browser_ops",
        target,
        {"objective": "Set the price to NT$89,000"},
        {"allowed": ["Update the listing price"]},
    ) is True


def test_marketplace_group_publish_has_dedicated_non_graph_task_type():
    from plugins.openclaw_bridge.clawops_delegate import (
        _requires_structured_facebook_crosspost,
    )

    targets = [
        "Facebook Marketplace item 111",
        "Facebook Group 222",
    ]
    assert _requires_structured_facebook_crosspost(
        "facebook_marketplace_group_publish",
        targets,
    ) is True
    assert _requires_structured_facebook_crosspost(
        "facebook_page_api_publish",
        targets,
    ) is False


def test_facebook_marketplace_crosspost_accepts_exact_names_for_approval(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-crosspost-names",
        "HERMES_SESSION_MESSAGE_TEXT": "請將 Carimali 刊登到這兩個具名社團",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "facebook_marketplace_group_publish",
        "risk_level": "medium",
        "external_targets": [
            "Facebook Marketplace listing ID 36803832485927906",
            "Facebook group: 咖啡器材買賣維修社團（絕對不要網銀操作，賣家一週限一則，建議賣家標明電話號碼）",
            "Facebook group: 二手新舊咖啡設備大賣場",
            "Facebook group: 👍二手餐飲設備。開店撿便宜👍流量大，更新快！",
            "Facebook group: 咖啡周邊商品、新品/二手 買賣",
            "Facebook group: 餐飲美食交流.經營管理.二手中古.新舊餐飲設備買賣.頂讓.",
        ],
        "facebook_crosspost": {
            "transport": "browser",
            "marketplace_listing_id": "36803832485927906",
            "group_names": [
                "咖啡器材買賣維修社團（絕對不要網銀操作，賣家一週限一則，建議賣家標明電話號碼）",
                "二手新舊咖啡設備大賣場",
                "👍二手餐飲設備。開店撿便宜👍流量大，更新快！",
                "咖啡周邊商品、新品/二手 買賣",
                "餐飲美食交流.經營管理.二手中古.新舊餐飲設備買賣.頂讓.",
            ],
        },
    })
    args["goal"]["objective"] = "將既有 Carimali Marketplace 刊登跨貼到指定社團"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "approval_required", result
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        challenge = kb.get_grace_approval_challenge(
            conn,
            result["approval_token"],
        )
    persisted = json.loads(challenge["delegation_args"])
    assert persisted["facebook_crosspost"] == args["facebook_crosspost"]


def test_execution_blocker_callback_preserves_name_bound_crosspost_scope(
    tmp_path,
    monkeypatch,
):
    listing_id = "36803832485927906"
    group_names = [
        "咖啡器材買賣維修社團（絕對不要網銀操作，賣家一週限一則，建議賣家標明電話號碼）",
        "二手新舊咖啡設備大賣場",
    ]
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-name-scope-request",
        "HERMES_SESSION_MESSAGE_TEXT": "請將 Carimali 刊登到兩個具名社團",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "facebook_marketplace_group_publish",
        "risk_level": "medium",
        "external_targets": [
            f"Facebook Marketplace listing ID {listing_id}",
            *[f"Facebook group: {name}" for name in group_names],
        ],
        "facebook_crosspost": {
            "transport": "browser",
            "marketplace_listing_id": listing_id,
            "group_names": group_names,
        },
    })
    args["goal"]["objective"] = "將既有 Carimali Marketplace 刊登跨貼到指定社團"
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    challenge = json.loads(handle_clawops_delegate(args))
    assert challenge["status"] == "approval_required", challenge
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-name-scope-approval"
    values["HERMES_SESSION_MESSAGE_TEXT"] = challenge["exact_reply"]
    args["approval_token"] = challenge["approval_token"]
    queued = json.loads(handle_clawops_delegate(args))
    assert queued["status"] == "queued", queued

    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        claimed = kb.claim_task(conn, queued["execution_task_id"])
        assert claimed is not None
        assert kb.block_task(
            conn,
            queued["execution_task_id"],
            reason="controlled browser pre-dispatch capability blocker",
            kind="capability",
            expected_run_id=claimed.current_run_id,
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=queued["grace_review_task_id"],
            event_id=callback["event_id"],
            lease_owner="name-scope-owner",
        )
        assert kb.grace_callback_facebook_crosspost_scopes(
            conn,
            review_task_id=queued["grace_review_task_id"],
            event_id=callback["event_id"],
        ) == (listing_id, frozenset(), frozenset(group_names))

    values.update({
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_SESSION_MESSAGE_TEXT": "[SYSTEM: callback]",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "name-scope-owner",
        "HERMES_GRACE_CALLBACK_BOARD": "default",
    })
    continuation = json.loads(json.dumps(args, ensure_ascii=False))
    continuation.pop("approval_token")
    continuation["approved"] = False
    continuation.update({
        "origin_callback_review_id": queued["grace_review_task_id"],
        "origin_callback_event_id": callback["event_id"],
        "origin_callback_board": "default",
    })
    next_challenge = json.loads(handle_clawops_delegate(continuation))
    assert next_challenge["status"] == "approval_required", next_challenge


def test_marketplace_readonly_inspection_requires_one_listing_per_contract(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-inspection",
        "HERMES_SESSION_MESSAGE_TEXT": "請唯讀檢查 Marketplace 可跨貼社團",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _nested_args()
    args.update({
        "task_type": "secondhand_commerce_group_status",
        "risk_level": "low",
        "external_targets": [
            "Facebook Marketplace item 111",
            "Facebook Marketplace item 222",
        ],
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": ["Facebook Marketplace listing ID 111"],
        },
    })
    args["goal"]["objective"] = (
        "唯讀開啟 Facebook Marketplace More options 與 "
        "List in more places"
    )
    args["scope"] = {
        "allowed": [
            "Read-only Facebook Marketplace listing 111: open More options → "
            "List in more places and read visible destination names/status only",
        ],
        "forbidden": [
            "不得勾選 checkbox、不得提交跨貼、不得 Post 或 Publish，"
            "不得變更任何外部狀態",
        ],
    }
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "split multiple listings" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    args["external_targets"] = ["Facebook Marketplace item 111"]
    accepted_shape = json.loads(handle_clawops_delegate(args))

    assert accepted_shape["status"] == "queued", accepted_shape
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
        tasks = kb.list_tasks(conn)
    execution = next(task for task in tasks if task.assignee != "grace")
    contract = kb._grace_compiled_contract(execution.body)
    assert contract["user_facing_delivery"]["subject_keys"] == [
        "facebook_marketplace:111",
    ]


def test_marketplace_readonly_preserves_conflicting_delivery_subject(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-conflicting-subject",
        "HERMES_SESSION_MESSAGE_TEXT": "請唯讀檢查 listing 111",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    args = _readonly_group_status_args("111", "facebook_marketplace:222")
    args["external_targets"] = ["Facebook Marketplace listing ID 111"]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    rejected = json.loads(handle_clawops_delegate(args))

    assert rejected["status"] == "rejected"
    assert "matching the delivery subject" in rejected["reason"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_generic_browser_readonly_marketplace_fallback_is_canonicalized(
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
        "HERMES_SESSION_MESSAGE_TEXT": "請接續安全的候選社團唯讀階段",
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
            "僅以唯讀模式檢視 Facebook Marketplace listing "
            f"{listing_id} 的 More options → List in more places，"
            "識別可見候選社團及狀態。"
        ),
        "deliverables": ["候選社團名稱與目前可見狀態"],
        "non_goals": ["不勾選或送出"],
    }
    args["scope"] = {
        "allowed": [
            "僅對 Facebook Marketplace listing "
            f"{listing_id} 進行 More options → List in more places 的唯讀檢視",
            "只讀取候選社團名稱與狀態",
        ],
        "forbidden": [
            "任何 Facebook 外部狀態變更",
            "勾選或取消任何社團 checkbox",
            "按 Post、Publish、Submit",
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
        tasks = kb.list_tasks(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
    execution = next(task for task in tasks if task.assignee != "grace")
    assert '"task_type": "secondhand_commerce_group_status"' in execution.body
    assert f"Facebook Marketplace listing ID {listing_id}" in execution.body
    assert '"task_type": "browser_readonly"' not in execution.body


def test_authenticated_exact_readonly_message_overrides_stale_publish_args(
    tmp_path,
    monkeypatch,
):
    listing_id = "36803832485927906"
    exact_message = (
        "請只查核 Carimali（Marketplace Listing ID：36803832485927906）。"
        "執行 Facebook Marketplace 唯讀社團狀態查核，直接列出社團名稱與狀態。"
        "不勾選、不發布、不修改、不建立核准 token。"
    )
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-exact-readonly",
        "HERMES_SESSION_MESSAGE_TEXT": exact_message,
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    stale_args = _external_listing_args()
    stale_args["approved"] = True
    stale_args["approval_token"] = "fe341e4c447cde20"
    stale_args["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "999",
        "group_ids": ["888"],
    }
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(stale_args))

    assert result["status"] == "queued", result
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 2
    execution = next(task for task in tasks if task.assignee != "grace")
    assert execution.assignee == "clawops-browser"
    assert listing_id in execution.body
    assert "secondhand_commerce_group_status" in execution.body
    assert "clawops.facebook_marketplace_readonly" in execution.body
    assert '"browser_navigate"' in execution.body
    assert '"browser_click"' in execution.body
    assert "蝦皮" not in execution.body
    assert "Facebook Marketplace item 999" not in execution.body


def test_authenticated_readonly_retry_shorthand_uses_canonical_contract(
    tmp_path,
    monkeypatch,
):
    listing_id = "36803832485927906"
    values = {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": "2",
        "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2",
        "HERMES_SESSION_ID": "grace-session-1",
        "HERMES_SESSION_MESSAGE_ID": "msg-readonly-retry-shorthand",
        "HERMES_SESSION_MESSAGE_TEXT": (
            "[KJ HSU] 只讀重試 Carimali Listing ID 36803832485927906"
        ),
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    guessed_args = _external_listing_args()
    guessed_args["user_facing_delivery"] = {
        "delivery": "inline_only",
        "kind": "commerce_group_status",
        "required": True,
        "subject_keys": [
            "facebook_marketplace_listing:36803832485927906"
        ],
    }
    guessed_args["external_targets"] = [
        "facebook_marketplace_listing:36803832485927906"
    ]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(guessed_args))

    assert result["status"] == "queued", result
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 2
    execution = next(task for task in tasks if task.assignee != "grace")
    assert execution.assignee == "clawops-browser"
    assert listing_id in execution.body
    assert 'facebook_marketplace_listing:36803832485927906' not in execution.body
    assert "Facebook Marketplace listing ID 36803832485927906" in execution.body


def test_two_fresh_messages_can_queue_two_readonly_scoped_contracts(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-two-listings",
        "HERMES_SESSION_MESSAGE_TEXT": "請分別唯讀檢查 Carimali 與 Celestron",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    carimali = _readonly_group_status_args("36803832485927906", "Carimali")
    celestron = _readonly_group_status_args("27909676598721497", "Celestron")
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    carimali_challenge = json.loads(handle_clawops_delegate(carimali))
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-two-listings-next"
    values["HERMES_SESSION_MESSAGE_TEXT"] = "接著唯讀檢查 Celestron"
    celestron_challenge = json.loads(handle_clawops_delegate(celestron))

    assert carimali_challenge["status"] == "queued", carimali_challenge
    assert celestron_challenge["status"] == "queued", celestron_challenge
    assert (
        carimali_challenge["execution_task_id"]
        != celestron_challenge["execution_task_id"]
    )
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM grace_approval_challenges"
        ).fetchone()[0] == 0
        assert len(kb.list_tasks(conn)) == 4


def test_same_listing_browser_blocker_stops_callback_redelegation(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-browser-retry",
        "HERMES_SESSION_MESSAGE_TEXT": "重新唯讀檢查 Carimali",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    monkeypatch.setattr(
        kb,
        "find_recent_commerce_browser_blocker",
        lambda _conn, _contract: {
            "capability_key": (
                "facebook:marketplace-group-status:36803832485927906"
            ),
            "task_id": "t_prior",
            "reason": "snapshot timed out",
            "blocked_at": 100,
            "retry_after": 1000,
        },
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(
        _readonly_group_status_args("36803832485927906", "Carimali")
    ))

    assert result["status"] == "capability_blocked"
    assert result["task_created"] is False
    assert result["task_id"] == "t_prior"
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert kb.list_tasks(conn) == []


def test_legacy_shared_request_challenge_is_reissued_as_child_after_conflict(
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
        "HERMES_SESSION_MESSAGE_ID": "msg-legacy-two-listings",
        "HERMES_SESSION_MESSAGE_TEXT": "請分別唯讀檢查兩個 listing",
        "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    carimali = _external_listing_args()
    carimali["external_targets"] = [
        "Facebook Marketplace item 36803832485927906",
    ]
    carimali["goal"]["objective"] = "發布 Carimali Marketplace 刊登"
    carimali["scope"]["allowed"] = ["Carimali Marketplace 刊登"]
    celestron = json.loads(json.dumps(carimali))
    celestron["external_targets"] = [
        "Facebook Marketplace item 27909676598721497",
    ]
    celestron["goal"]["objective"] = "發布 Celestron Marketplace 刊登"
    celestron["scope"]["allowed"] = ["Celestron Marketplace 刊登"]
    import plugins.openclaw_bridge.clawops_delegate as delegate_module

    child_deriver = delegate_module._approval_child_request_instance_id
    monkeypatch.setattr(
        delegate_module,
        "_approval_child_request_instance_id",
        lambda parent, _fingerprint: parent,
    )
    legacy_carimali = json.loads(delegate_module.handle_clawops_delegate(carimali))
    legacy_celestron = json.loads(delegate_module.handle_clawops_delegate(celestron))
    assert (
        legacy_carimali["request_instance_id"]
        == legacy_celestron["request_instance_id"]
    )

    carimali_token = legacy_carimali["approval_token"]
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-approve-legacy-carimali"
    values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {carimali_token}"
    queued_carimali = json.loads(delegate_module.handle_clawops_delegate(
        dict(carimali, approval_token=carimali_token),
    ))
    assert queued_carimali["status"] == "queued", queued_carimali

    monkeypatch.setattr(
        delegate_module,
        "_approval_child_request_instance_id",
        child_deriver,
    )
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-legacy-two-listings"
    values["HERMES_SESSION_MESSAGE_TEXT"] = "請分別唯讀檢查兩個 listing"
    replacement = json.loads(delegate_module.handle_clawops_delegate(celestron))

    assert replacement["status"] == "approval_required", replacement
    assert replacement["approval_token"] != legacy_celestron["approval_token"]
    assert (
        replacement["request_instance_id"]
        != legacy_celestron["request_instance_id"]
    )
    values["HERMES_SESSION_MESSAGE_ID"] = "msg-approve-child-celestron"
    values["HERMES_SESSION_MESSAGE_TEXT"] = (
        f"核准 {replacement['approval_token']}"
    )
    queued_celestron = json.loads(delegate_module.handle_clawops_delegate(
        dict(celestron, approval_token=replacement["approval_token"]),
    ))
    assert queued_celestron["status"] == "queued", queued_celestron
    assert (
        queued_celestron["execution_task_id"]
        != queued_carimali["execution_task_id"]
    )


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


def test_execution_blocker_can_create_durable_approval_checkpoint(
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
        "HERMES_SESSION_MESSAGE_ID": "",
        "HERMES_SESSION_MESSAGE_TEXT": "[SYSTEM: callback]",
        "HERMES_SESSION_INTERNAL": "true",
        "HERMES_GRACE_CALLBACK_BOARD": "default",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "execution-blocker-lease",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    kb.init_db()
    with kb.connect_closing() as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook read-only verification",
            assignee="clawops-content",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_callback_delegation(
            conn,
            execution_id=execution_id,
            review_id=review_id,
            contract_fingerprint="d" * 64,
            suffix="execution-approval-blocker",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="d" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="Facebook navigation requires a fresh read-only approval",
            kind="needs_input",
        )
        callback = kb.list_due_grace_loop_callbacks(conn)[0]
        assert callback["event_stage"] == "execution"
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner=values["HERMES_GRACE_CALLBACK_LEASE_OWNER"],
        )

    from plugins.openclaw_bridge.clawops_delegate import (
        handle_clawops_delegate,
        handle_grace_callback_outcome,
    )

    args = _external_listing_args()
    args["task_type"] = "browser_readonly"
    args["risk_level"] = "low"
    args.update({
        "origin_callback_review_id": review_id,
        "origin_callback_event_id": callback["event_id"],
        "origin_callback_board": "default",
    })
    missing_targets = json.loads(json.dumps(args))
    missing_targets.pop("external_targets")
    missing_target_result = json.loads(
        handle_clawops_delegate(missing_targets),
    )
    assert missing_target_result["status"] == "rejected"
    assert "requires explicit external_targets" in missing_target_result["reason"]
    challenge = json.loads(handle_clawops_delegate(args))

    assert challenge["status"] == "approval_required"
    narrowed_args = json.loads(json.dumps(args))
    narrowed_args["external_targets"] = ["Facebook Marketplace"]
    narrowed_args["goal"]["objective"] = (
        "僅驗證 Facebook Marketplace 賣家刊登介面的唯讀可達性"
    )
    narrowed_args["scope"]["allowed"] = [
        "僅在 Facebook Marketplace 既有登入工作階段唯讀導覽",
        "產出證據報告",
    ]
    narrowed_challenge = json.loads(handle_clawops_delegate(narrowed_args))
    assert narrowed_challenge["status"] == "approval_required"
    assert narrowed_challenge["approval_token"] != challenge["approval_token"]
    assert narrowed_challenge["platform"] == "Facebook Marketplace"
    assert all(
        "蝦皮" not in item
        for item in narrowed_challenge["scope"]
    )
    args = narrowed_args
    challenge = narrowed_challenge
    with kb.connect_closing() as conn:
        challenge_row = kb.get_grace_approval_challenge(
            conn, challenge["approval_token"],
        )
    persisted_args = json.loads(challenge_row["delegation_args"])
    assert persisted_args["approved"] is False
    assert persisted_args["external_targets"] == ["Facebook Marketplace"]
    assert (
        persisted_args["origin_callback_review_id"]
        == review_id
    )
    assert persisted_args["origin_callback_event_id"] == callback["event_id"]
    from plugins.openclaw_bridge.clawops_delegate import (
        recover_clawops_approval_args,
    )

    assert (
        recover_clawops_approval_args(challenge["approval_token"])
        == persisted_args
    )
    outcome = json.loads(handle_grace_callback_outcome({
        "review_task_id": review_id,
        "event_id": callback["event_id"],
        "outcome_kind": "approval_blocked",
        "payload": {
            "action": args["goal"]["objective"],
            "platform": challenge["platform"],
            "scope": challenge["scope"],
            "exact_question": challenge["exact_reply"],
        },
    }))
    assert outcome["status"] == "recorded"

    with kb.connect_closing() as conn:
        assert kb.finish_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=callback["event_id"],
            lease_owner=values["HERMES_GRACE_CALLBACK_LEASE_OWNER"],
        )
        recorded = kb.get_grace_loop_callback(conn, review_id)
        assert recorded["outcome_kind"] == "approval_blocked"
        assert len(kb.list_tasks(conn)) == 2

    values.update({
        "HERMES_SESSION_MESSAGE_ID": "fresh-generic-approval",
        "HERMES_SESSION_MESSAGE_TEXT": "同意",
        "HERMES_SESSION_INTERNAL": "false",
        "HERMES_GRACE_CALLBACK_BOARD": "",
        "HERMES_GRACE_CALLBACK_LEASE_OWNER": "",
    })
    replay = json.loads(handle_clawops_delegate(args))

    assert replay["status"] == "approval_required"
    assert replay["approval_token"] == challenge["approval_token"]
    assert replay["exact_reply"] == challenge["exact_reply"]
    with kb.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == 2
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE grace_approval_challenges SET expires_at = 0 "
                "WHERE token = ?",
                (challenge["approval_token"],),
            )

    values.update({
        "HERMES_SESSION_ID": "grace-session-2",
        "HERMES_SESSION_MESSAGE_ID": "fresh-expired-refresh",
        "HERMES_SESSION_MESSAGE_TEXT": "",
    })
    refresh_args = json.loads(json.dumps(args))
    refresh_args["_approval_refresh_token"] = challenge["approval_token"]
    refreshed = json.loads(handle_clawops_delegate(refresh_args))

    assert refreshed["status"] == "approval_required"
    assert refreshed["approval_token"] != challenge["approval_token"]
    with kb.connect_closing() as conn:
        refreshed_row = kb.get_grace_approval_challenge(
            conn, refreshed["approval_token"],
        )
        assert refreshed_row["session_id"] == "grace-session-2"
        assert len(kb.list_tasks(conn)) == 2

    values.update({
        "HERMES_SESSION_MESSAGE_ID": "fresh-rotated-session-approval",
        "HERMES_SESSION_MESSAGE_TEXT": (
            f"核准 {refreshed['approval_token']}"
        ),
    })
    approval_args = json.loads(json.dumps(args))
    approval_args["approval_token"] = refreshed["approval_token"]
    queued = json.loads(handle_clawops_delegate(approval_args))

    assert queued["status"] == "queued"
    with kb.connect_closing() as conn:
        approved_delegation = kb.get_grace_delegation(
            conn, delegation_id=queued["delegation_id"],
        )
        assert approved_delegation["approval_required"] == 1
        assert approved_delegation["state"] == "queued"
        assert len(kb.list_tasks(conn)) == 4


def test_delegate_auto_materializes_unknown_topic_main_project(tmp_path, monkeypatch):
    registry = tmp_path / "registry.yaml"
    registry.write_text("version: 1\ncontexts: []\n", encoding="utf-8")
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "chat-1",
            "HERMES_SESSION_THREAD_ID": "999",
            "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:999",
            "HERMES_SESSION_ID": "grace-session-unknown-topic",
            "HERMES_SESSION_MESSAGE_ID": "msg-unknown-topic",
        }.get(key, default),
    )
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    result = json.loads(handle_clawops_delegate(_args()))
    assert result["status"] == "queued", result
    assert result["project"].startswith("telegram_chat_1_999_")
    assert result["execution_task_id"].startswith("t_")
    assert result["grace_review_task_id"].startswith("t_")
    persisted_registry = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert persisted_registry["contexts"][0]["project_kind"] == "lightweight_main"
    assert db_path.exists()


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
