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
    interaction_metadata,
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
    metadata = interaction_metadata("callback", ["BrowserOps", "ClawOps", "Grace", "你"])
    once = decorate_telegram_message("已完成驗收。", metadata)

    assert decorate_telegram_message(once, metadata) == once


def test_latest_turn_delegation_result_does_not_reuse_old_history():
    old_result = {
        "status": "queued",
        "assigned_agent": "OldAgent",
    }
    messages = [
        {
            "role": "tool",
            "tool_name": "clawops_delegate",
            "content": json.dumps(old_result),
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


def test_interaction_labels_are_opt_in_and_platform_overrideable():
    assert resolve_display_setting({}, "telegram", "interaction_labels") is False
    config = {
        "display": {
            "platforms": {
                "telegram": {"interaction_labels": True},
            }
        }
    }
    assert resolve_display_setting(config, "telegram", "interaction_labels") is True


def test_unknown_metadata_is_left_unchanged():
    assert decorate_telegram_message("原文", {METADATA_KEY: {"kind": "mystery"}}) == "原文"


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
            "execution",
            ["你", "Grace", "ClawOps", "BrowserOps"],
        ),
        "plain_text": True,
    }

    result = await adapter.send("12345", "正在執行。", metadata=metadata)

    assert result.success is True
    assert adapter._bot.send_message.await_args.kwargs["text"] == (
        "🔵 Agent 執行｜你 → Grace → ClawOps → BrowserOps\n\n正在執行。"
    )
