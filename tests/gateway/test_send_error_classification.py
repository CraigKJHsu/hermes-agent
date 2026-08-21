"""Tests for structured send-error classification (SendResult.error_kind).

Covers the platform-neutral ``classify_send_error`` vocabulary in
``gateway/platforms/base.py`` and its wiring into the Telegram adapter's
``send()`` failure path, so consumers can branch on a typed category instead
of substring-matching the raw provider message.
"""

import pytest

from gateway.platforms.base import (
    SEND_ERROR_KINDS,
    SendResult,
    classify_send_error,
)


class _FakeBadRequest(Exception):
    """Stand-in for a provider BadRequest carrying a message string."""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Message_too_long", "too_long"),
        ("Bad Request: message is too long", "too_long"),
        ("Bad Request: can't parse entities: unsupported start tag", "bad_format"),
        ("Bad Request: can't find end of the entity", "bad_format"),
        ("Forbidden: bot was blocked by the user", "forbidden"),
        ("Forbidden: user is deactivated", "forbidden"),
        ("Bad Request: not enough rights to send text messages", "forbidden"),
        ("Bad Request: chat not found", "not_found"),
        ("Bad Request: message to edit not found", "not_found"),
        ("Too Many Requests: retry after 12", "rate_limited"),
        ("Flood control exceeded", "rate_limited"),
        ("ConnectError: connection refused", "transient"),
        ("ConnectTimeout", "transient"),
        ("some entirely novel provider message", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_send_error_text(text, expected):
    assert classify_send_error(None, text) == expected


def test_classify_uses_exception_class_name():
    # The class name participates in classification even when str(exc) is empty.
    exc = type("Forbidden", (Exception,), {})()
    assert classify_send_error(exc) == "forbidden"


def test_classify_prefers_explicit_text_and_exception_together():
    exc = _FakeBadRequest("chat not found")
    assert classify_send_error(exc) == "not_found"


def test_every_classification_is_in_the_vocabulary():
    samples = [
        "message_too_long",
        "can't parse entities",
        "forbidden",
        "chat not found",
        "flood",
        "connecterror",
        "mystery",
        "",
    ]
    for s in samples:
        assert classify_send_error(None, s) in SEND_ERROR_KINDS


def test_unknown_never_masquerades_as_benign():
    # An unrecognized failure must classify as "unknown", never as a benign
    # category like too_long that a consumer might treat as a soft recovery.
    assert classify_send_error(None, "kaboom 500 internal") == "unknown"


def test_sendresult_error_kind_defaults_none_and_is_backward_compatible():
    # Existing call sites that never set error_kind keep working unchanged.
    ok = SendResult(success=True, message_id="42")
    assert ok.error_kind is None
    legacy_fail = SendResult(success=False, error="boom")
    assert legacy_fail.error_kind is None


def test_telegram_send_failure_populates_error_kind():
    """Telegram send() failures carry a typed error_kind alongside error."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    cfg = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter = TelegramAdapter(cfg)

    # Minimal bot whose send_message raises a parse/entity rejection.
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=Exception("Bad Request: can't parse entities: bad tag")
    )
    bot.send_chat_action = AsyncMock()
    # Force the legacy (non-rich) path and a connected bot.
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(adapter.send("123", "<b>broken"))
    assert result.success is False
    # Telegram has a plain-text fallback for parse errors inside the send loop,
    # so a raw parse failure that still escapes is classified for consumers.
    assert result.error_kind in SEND_ERROR_KINDS
    assert result.error_kind != "unknown" or result.error


def test_telegram_too_long_sets_too_long_kind():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    cfg = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter = TelegramAdapter(cfg)

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=Exception("Bad Request: message is too long")
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(adapter.send("123", "x" * 5000))
    assert result.success is False
    assert result.error == "message_too_long"
    assert result.error_kind == "too_long"


def test_telegram_ambiguous_timeout_is_not_retried():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import TimedOut

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TimedOut("read acknowledgement timed out"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send(
            "123",
            "plain",
            metadata={"plain_text": True, "send_timeout": 2.0},
        ),
    )

    assert result.success is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["connect_timeout"] == 0.25
    assert kwargs["pool_timeout"] == 0.25
    assert kwargs["write_timeout"] == 0.25
    assert kwargs["read_timeout"] == 1.2


def test_telegram_post_write_network_failure_is_ambiguous_and_not_retried():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import NetworkError

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=NetworkError("connection reset while reading response"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send(
            "123",
            "plain",
            metadata={"plain_text": True, "send_timeout": 2.0},
        ),
    )

    assert result.success is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1


def test_telegram_generic_read_timeout_is_ambiguous_and_not_retried():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import NetworkError

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=NetworkError("read timeout"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(adapter.send("123", "plain"))

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1


def test_telegram_ordinary_send_retries_definite_dns_failure():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from telegram.error import NetworkError

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[
            NetworkError("temporary failure in name resolution"),
            SimpleNamespace(message_id=1),
        ],
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    with patch(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = asyncio.run(adapter.send("123", "ordinary"))

    assert result.success is True
    assert bot.send_message.await_count == 2


def test_telegram_ordinary_post_dispatch_network_failure_is_ambiguous():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import NetworkError

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=NetworkError("connection reset while reading response"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(adapter.send("123", "ordinary"))

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1


def test_telegram_bounded_dns_failure_is_definite():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import NetworkError

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=NetworkError("connection refused"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send(
            "123",
            "plain",
            metadata={"plain_text": True, "send_timeout": 2.0},
        ),
    )

    assert result.success is False
    assert result.delivery_ambiguous is False
    assert bot.send_message.await_count == 1


def test_telegram_delivery_deadline_rejects_too_small_budget_before_dispatch():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()

    bot.send_message = AsyncMock()
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send_with_delivery_deadline(
            "123",
            "plain",
            metadata={"plain_text": True},
            timeout=0.19,
        ),
    )

    assert result.success is False
    assert result.retryable is True
    assert result.delivery_ambiguous is False
    assert bot.send_message.await_count == 0


def test_telegram_absolute_deadline_without_phase_evidence_is_ambiguous():
    import asyncio
    from unittest.mock import AsyncMock

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )

    async def _stall_before_request(*args, **kwargs):
        await asyncio.sleep(1)

    adapter.send = AsyncMock(side_effect=_stall_before_request)

    result = asyncio.run(
        adapter.send_with_delivery_deadline(
            "123",
            "plain",
            metadata={"plain_text": True},
            timeout=0.2,
        ),
    )

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True


def test_telegram_absolute_deadline_after_request_attempt_is_ambiguous():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()

    async def _stall_after_request(**kwargs):
        await asyncio.sleep(1)

    bot.send_message = AsyncMock(side_effect=_stall_after_request)
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send_with_delivery_deadline(
            "123",
            "plain",
            metadata={"plain_text": True},
            timeout=0.2,
        ),
    )

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1


def test_telegram_total_delivery_deadline_preserves_typed_ambiguity():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import TimedOut

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={}),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TimedOut("read acknowledgement timed out"),
    )
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    adapter._rich_messages_enabled = False

    result = asyncio.run(
        adapter.send_with_delivery_deadline(
            "123",
            "plain",
            metadata={"plain_text": True},
            timeout=2.0,
        ),
    )

    assert result.success is False
    assert result.retryable is False
    assert result.delivery_ambiguous is True
    assert bot.send_message.await_count == 1
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] is None
    assert kwargs["connect_timeout"] == 0.25
    assert kwargs["pool_timeout"] == 0.25
    assert kwargs["write_timeout"] == 0.25
    assert kwargs["read_timeout"] == 1.2
