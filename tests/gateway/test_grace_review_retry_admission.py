from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource


@pytest.mark.asyncio
async def test_exact_review_retry_runs_before_stale_objective_prompt(
    monkeypatch, tmp_path,
):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1003938559457",
        chat_type="group",
        thread_id="4641",
        user_id="kj-owner",
        user_name="KJ HSU",
        message_id="telegram-991",
    )
    event = MessageEvent(
        text="請重試 Grace Review t_27788777",
        source=source,
        message_id="telegram-991",
    )
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(supports_async_delivery=True)}
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._start_response_fast_lane = lambda _event: None
    runner._deliver_response_fast_lane = AsyncMock(return_value=None)
    runner._cache_session_source = lambda *_args: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._record_telegram_topic_binding = lambda *_args, **_kwargs: None
    runner._resume_grace_callback_handoff = lambda **_kwargs: None
    runner._configured_external_action_owner = lambda _source: "kj-owner"
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    now = datetime.now()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1003938559457:4641",
        session_id="session-4641",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        chat_type="group",
    )

    retry = MagicMock(return_value=json.dumps({
        "status": "queued",
        "task_created": False,
        "execution_task_id": "t_98e305ad",
        "grace_review_task_id": "t_27788777",
    }))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.handle_clawops_retry_review",
        retry,
    )
    objective_prompt = MagicMock(side_effect=RuntimeError("behavior.manifest_mismatch"))
    monkeypatch.setattr("proactive.prompt_policy.active_objectives_prompt", objective_prompt)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:telegram:group:-1003938559457:4641",
        1,
    )

    assert "t_27788777" in response
    assert "已重新排入" in response
    retry.assert_called_once_with({"review_task_id": "t_27788777"})
    objective_prompt.assert_not_called()
    runner._run_agent = AsyncMock()
    runner._run_agent.assert_not_called()


def test_review_retry_control_phrase_is_exact():
    from plugins.openclaw_bridge.clawops_delegate import (
        grace_review_retry_task_id,
    )

    assert grace_review_retry_task_id("請重試 Grace Review t_27788777") == "t_27788777"
    assert grace_review_retry_task_id(" 請重試  Grace Review t_27788777。 ") == "t_27788777"
    assert grace_review_retry_task_id("不要重試 Grace Review t_27788777") == ""
    assert grace_review_retry_task_id("請重試 Grace Review t_27788777 然後發布") == ""
