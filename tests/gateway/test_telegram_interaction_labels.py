import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.display_config import resolve_display_setting
from gateway.telegram_interaction_labels import (
    METADATA_KEY,
    decorate_telegram_message,
    delegation_result_from_messages,
    delegation_was_queued,
    initialize_turn_interaction_context,
    interaction_metadata,
    is_queued_clawops_delegation,
    propagate_interaction_context,
)
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_direct_message_has_clear_human_grace_label():
    metadata = interaction_metadata("direct", ["你", "Grace"])
    rendered = decorate_telegram_message("我了解了。", metadata)
    assert rendered == "🟢 直接對話｜你 ↔ Grace\n\n我了解了。"


def test_delegation_label_exposes_full_assigned_agent_path():
    metadata = interaction_metadata(
        "handoff",
        ["你", "Grace", "ClawOps", "BrowserOps"],
        assigned_agent="BrowserOps",
    )
    rendered = decorate_telegram_message("任務已建立。", metadata)
    assert rendered.startswith(
        "🟠 任務交接｜你 → Grace → ClawOps → BrowserOps\n\n"
    )


def test_decorator_is_idempotent_for_telegram_edits():
    metadata = interaction_metadata(
        "callback", ["BrowserOps", "ClawOps", "Grace", "你"]
    )
    once = decorate_telegram_message("已完成驗收。", metadata)
    assert decorate_telegram_message(once, metadata) == once


def test_latest_turn_delegation_result_does_not_reuse_old_history():
    messages = [
        {
            "role": "tool",
            "tool_name": "clawops_delegate",
            "content": json.dumps({"status": "queued", "assigned_agent": "OldAgent"}),
        },
        {"role": "assistant", "content": "舊回覆"},
        {"role": "user", "content": "這次只是聊天"},
        {"role": "assistant", "content": "直接回答"},
    ]
    assert delegation_result_from_messages(messages) is None


def test_current_turn_delegation_result_keeps_ids_and_assignment():
    payload = {
        "status": "queued",
        "assigned_agent": "ResearchOps",
        "delegation_id": "d_123",
        "execution_task_id": "t_exec",
        "grace_review_task_id": "t_review",
    }
    messages = [
        {"role": "user", "content": "請處理"},
        {
            "role": "tool",
            "tool_name": "clawops_delegate",
            "content": json.dumps(payload),
        },
        {"role": "assistant", "content": "已建立"},
    ]
    assert delegation_result_from_messages(messages) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "approval_required"},
        {"status": "rejected"},
        {"status": "failed"},
        {"status": "queued", "execution_task_id": "t_exec"},
        {
            "status": "queued",
            "execution_task_id": "t_exec",
            "grace_review_task_id": "",
        },
    ],
)
def test_only_created_task_pair_counts_as_successful_handoff(payload):
    assert delegation_was_queued(payload) is False


def test_queued_task_pair_counts_as_successful_handoff():
    assert delegation_was_queued(
        {
            "status": "queued",
            "execution_task_id": "t_exec",
            "grace_review_task_id": "t_review",
        }
    ) is True


def test_handoff_requires_clawops_tool_identity():
    payload = {
        "status": "queued",
        "execution_task_id": "t_exec",
        "grace_review_task_id": "t_review",
    }
    assert is_queued_clawops_delegation("other_tool", payload) is False
    assert is_queued_clawops_delegation("clawops_delegate", payload) is True


def test_new_turn_resets_prior_handoff_without_aliasing_context():
    prior = {
        "trace_id": "trace-1",
        **interaction_metadata(
            "handoff", ["你", "Grace", "ClawOps", "BrowserOps"]
        ),
    }
    current = initialize_turn_interaction_context(prior)
    assert current["trace_id"] == "trace-1"
    assert current[METADATA_KEY] == {
        "kind": "direct",
        "path": ["你", "Grace"],
        "assigned_agent": "",
    }
    assert prior[METADATA_KEY]["kind"] == "handoff"


def test_callback_turn_gets_fresh_reverse_route():
    current = initialize_turn_interaction_context(
        {
            "internal_kind": "grace_callback",
            "execution_assignee": "BrowserOps",
        }
    )
    assert current[METADATA_KEY] == {
        "kind": "callback",
        "path": ["BrowserOps", "ClawOps", "Grace", "你"],
        "assigned_agent": "",
    }


def test_recursive_turn_propagates_final_descriptor_without_aliasing():
    outer = initialize_turn_interaction_context({})
    queued = interaction_metadata(
        "handoff", ["你", "Grace", "ClawOps", "BrowserOps"]
    )
    assert propagate_interaction_context(outer, queued) is True
    assert outer[METADATA_KEY]["kind"] == "handoff"
    queued[METADATA_KEY]["kind"] = "mutated"
    assert outer[METADATA_KEY]["kind"] == "handoff"


def test_interaction_labels_are_opt_in_and_platform_overrideable():
    assert resolve_display_setting({}, "telegram", "interaction_labels") is False
    config = {
        "display": {
            "platforms": {"telegram": {"interaction_labels": True}}
        }
    }
    assert resolve_display_setting(config, "telegram", "interaction_labels") is True


def test_unknown_metadata_is_left_unchanged():
    metadata = {METADATA_KEY: {"kind": "mystery"}}
    assert decorate_telegram_message("原文", metadata) == "原文"


@pytest.mark.asyncio
async def test_telegram_send_renders_trusted_interaction_metadata():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=123),
    )
    adapter._bot.send_chat_action = AsyncMock()
    adapter._rich_messages_enabled = False
    metadata = {
        **interaction_metadata(
            "execution", ["你", "Grace", "ClawOps", "BrowserOps"]
        ),
        "plain_text": True,
    }
    result = await adapter.send("12345", "正在執行。", metadata=metadata)
    assert result.success is True
    assert adapter._bot.send_message.await_args.kwargs["text"] == (
        "🔵 Agent 執行｜你 → Grace → ClawOps → BrowserOps\n\n正在執行。"
    )
