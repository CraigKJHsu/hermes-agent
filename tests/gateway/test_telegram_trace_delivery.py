from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb
from hermes_cli.telegram_message_path import build_telegram_message_path
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_delivered_message_reports_ambiguous_when_trace_receipt_cannot_persist(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    kb.init_db()
    path = build_telegram_message_path(
        chat_id="chat-1",
        user_id="kj",
        inbound_message_id="in-1",
        session_key="agent:main:telegram:private:chat-1",
        session_id="session-1",
    )
    with kb.connect() as conn:
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="d" * 64,
            request_instance_id="request-1",
            platform="telegram",
            chat_id="chat-1",
            thread_id="",
            session_key=path["session_key"],
            session_id=path["session_id"],
            resolved_route={"assignment": {"agent": "openclaw"}},
            approval_required=False,
            telegram_message_path=path,
        )
    path = delegation["telegram_message_path"]

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._bot = object()
    adapter._should_attempt_rich = lambda _content, metadata=None: True

    async def rich_send(_chat_id, _content, _reply_to, _metadata):
        return SendResult(success=True, message_id="out-1")

    adapter._try_send_rich = rich_send

    def fail_persistence(*_args, **_kwargs):
        raise OSError("simulated durable store failure")

    monkeypatch.setattr(
        kb,
        "update_grace_delegation_telegram_message_path",
        fail_persistence,
    )
    metadata = {
        "notify": True,
        "kanban_board": "default",
        "telegram_message_path": path,
    }

    result = asyncio.run(adapter.send("chat-1", "done", metadata=metadata))

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True
    assert result.message_id == "out-1"
    assert metadata["telegram_trace_persistence"] == "ambiguous"
