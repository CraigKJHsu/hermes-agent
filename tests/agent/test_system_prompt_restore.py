"""Tests for ``agent.conversation_loop._restore_or_build_system_prompt``.

Validates the gateway DB-roundtrip path that keeps the system prompt
byte-stable across turns (fresh AIAgent → must restore from session DB
instead of rebuilding).  Covers:

  * Successful restore from a stored prompt (present row).
  * Legitimate first-turn build (no history).
  * Silent-failure recovery paths:
      - DB read raises → WARNING + fresh build
      - Row has system_prompt=NULL → WARNING + fresh build
      - Row has system_prompt="" → WARNING + fresh build
      - DB write fails → WARNING (subsequent turns will miss cache)
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from agent.conversation_loop import (
    _SYSTEM_PROMPT_BUILD_LOCK,
    _restore_or_build_system_prompt,
)


@pytest.fixture(autouse=True)
def _isolate_live_grace_policy():
    """Keep prompt-cache unit tests independent of ~/.hermes/AGENTS.md."""
    with (
        patch(
            "proactive.prompt_policy.ensure_active_policy_prompt",
            side_effect=lambda prompt: prompt,
        ),
        patch(
            "proactive.prompt_policy.stored_prompt_matches_active_policy",
            return_value=True,
        ),
    ):
        yield


def _make_agent(session_db=None, prebuilt_prompt: str = "BUILT_PROMPT"):
    """Construct the minimal agent fake the helper needs."""
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "test-session-id"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent._session_db = session_db
    agent._build_system_prompt = MagicMock(return_value=prebuilt_prompt)
    return agent


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestStoredPromptReuse:
    def test_present_row_is_reused_verbatim(self, caplog):
        """Continuing session with a stored prompt → reuse byte-for-byte."""
        stored = "Stored prompt from turn 1 — byte-identical reuse"
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt == stored
        agent._build_system_prompt.assert_not_called()
        db.update_system_prompt.assert_not_called()
        # No warnings on the happy path
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_present_row_with_unicode_preserved(self):
        """Non-ASCII bytes in the stored prompt are not mangled."""
        stored = "Stored prompt with unicode: ☤ ⚗ ◆ — and emoji 🦊"
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])
        assert agent._cached_system_prompt == stored

    def test_present_row_with_stale_runtime_identity_rebuilds(self, caplog):
        """Stored prompts are cache gold unless their runtime identity is stale.

        A live /model switch updates the agent and DB model_config immediately.
        If the old system_prompt snapshot still says the previous model,
        blindly restoring it makes the next turn call the new model while the
        model reads old `Model:` metadata ("what model are you?" lies).
        """
        stored = (
            "You are Hermes Agent.\n\n"
            "Conversation started: Tuesday, June 16, 2026\n"
            "Session ID: test-session-id\n"
            "Model: anthropic/claude-opus-4.8-fast\n"
            "Provider: openrouter"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(
            session_db=db,
            prebuilt_prompt=(
                "You are Hermes Agent.\n\n"
                "Conversation started: Tuesday, June 16, 2026\n"
                "Session ID: test-session-id\n"
                "Model: openai/gpt-5.5\n"
                "Provider: openrouter"
            ),
        )
        agent.model = "openai/gpt-5.5"

        with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt.endswith(
            "Model: openai/gpt-5.5\nProvider: openrouter"
        )
        agent._build_system_prompt.assert_called_once_with(None)
        db.update_system_prompt.assert_called_once_with(
            agent.session_id, agent._cached_system_prompt
        )
        assert any("stale runtime identity" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Legitimate fresh-build paths (no history, no DB)
# ---------------------------------------------------------------------------


class TestLegitimateFreshBuild:
    def test_no_history_skips_db_and_builds_fresh(self, caplog):
        """First turn with empty history → build fresh, don't touch the DB."""
        db = MagicMock()
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [])

        # No history → DB read skipped entirely
        db.get_session.assert_not_called()
        agent._build_system_prompt.assert_called_once_with(None)
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Persisted to DB
        db.update_system_prompt.assert_called_once_with(agent.session_id, "BUILT_PROMPT")
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_no_db_skips_persistence(self):
        """When session DB is None, build and skip persistence silently."""
        agent = _make_agent(session_db=None)
        _restore_or_build_system_prompt(agent, None, [])
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"

    def test_timed_out_build_uses_policy_verified_safe_prompt(
        self, monkeypatch, caplog
    ):
        """A wedged prompt builder must not hold the whole gateway turn."""
        release = threading.Event()
        started = threading.Event()
        db = MagicMock()
        agent = _make_agent(session_db=db)
        agent._touch_activity = MagicMock()

        def _blocked_build(_system_message):
            started.set()
            release.wait(timeout=2)
            return "LATE_FULL_PROMPT"

        agent._build_system_prompt.side_effect = _blocked_build
        monkeypatch.setenv("HERMES_SYSTEM_PROMPT_BUILD_TIMEOUT", "0.01")

        with (
            patch(
                "proactive.prompt_policy.ensure_active_policy_prompt",
                side_effect=lambda prompt: prompt + "\n\nVERIFIED_GRACE_POLICY",
            ),
            caplog.at_level(logging.ERROR, logger="agent.conversation_loop"),
        ):
            _restore_or_build_system_prompt(agent, "TOPIC_CONTEXT", [])

        assert started.is_set()
        assert agent._system_prompt_fallback_used is True
        assert "[Runtime recovery mode]" in agent._cached_system_prompt
        assert "TOPIC_CONTEXT" in agent._cached_system_prompt
        assert "VERIFIED_GRACE_POLICY" in agent._cached_system_prompt
        assert any("timed out" in r.getMessage() for r in caplog.records)
        assert agent._touch_activity.call_args_list[-1].args == (
            "initial system prompt ready (safe fallback)",
        )
        db.update_system_prompt.assert_called_once_with(
            agent.session_id, agent._cached_system_prompt
        )

        # Let the daemon finish and prove it cannot keep the global builder
        # slot wedged for sibling tests or later sessions.
        release.set()
        assert _SYSTEM_PROMPT_BUILD_LOCK.acquire(timeout=1)
        _SYSTEM_PROMPT_BUILD_LOCK.release()

    def test_persisted_recovery_prompt_is_rebuilt_on_next_turn(self):
        """A one-turn fallback must not become a permanent cached prefix."""
        stored = (
            "[Runtime recovery mode] reduced prompt\n"
            "Model: test-model\nProvider: openrouter"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db, prebuilt_prompt="FULL_PROMPT")

        _restore_or_build_system_prompt(
            agent, None, [{"role": "user", "content": "previous"}]
        )

        agent._build_system_prompt.assert_called_once_with(None)
        assert agent._cached_system_prompt != stored
        assert "FULL_PROMPT" in agent._cached_system_prompt


# ---------------------------------------------------------------------------
# Silent-failure recovery — these are the new A/B logging paths
# ---------------------------------------------------------------------------


class TestSilentFailureWarnings:
    def test_db_read_exception_warns_and_rebuilds(self, caplog):
        """DB read raising → WARNING + fall through to fresh build."""
        db = MagicMock()
        db.get_session.side_effect = RuntimeError("disk full")
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        # Built fresh
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Loud warning about the read failure
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("get_session failed" in r.getMessage() for r in warnings), \
            f"Expected a get_session warning, got: {[r.getMessage() for r in warnings]}"
        assert any("disk full" in r.getMessage() for r in warnings)

    def test_null_system_prompt_warns_about_unusable_stored_state(self, caplog):
        """Row exists but system_prompt is NULL → WARNING + fresh build."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": None}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        agent._build_system_prompt.assert_called_once()
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("is null" in m and "rebuilding" in m for m in warnings), \
            f"Expected null-stored-prompt warning, got: {warnings}"

    def test_empty_system_prompt_warns_about_silent_persistence_bug(self, caplog):
        """Row exists but system_prompt is '' → WARNING about silent write bug."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": ""}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        agent._build_system_prompt.assert_called_once()
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("is empty" in m and "rebuilding" in m for m in warnings), \
            f"Expected empty-stored-prompt warning, got: {warnings}"

    def test_db_write_failure_warns_loudly(self, caplog):
        """update_system_prompt raising → WARNING (was DEBUG before)."""
        db = MagicMock()
        # No prior row (first turn)
        db.get_session.return_value = None
        db.update_system_prompt.side_effect = RuntimeError("database is locked")
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [])

        # Built and assigned the cache anyway
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Warning surfaced
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "update_system_prompt failed" in m and "database is locked" in m
            for m in warnings
        ), f"Expected write-failure warning, got: {warnings}"

    def test_no_history_with_null_row_does_not_warn(self, caplog):
        """First turn (no history) hitting a null row is not surprising — no warn."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": None}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            # Empty history → DB read is skipped entirely
            _restore_or_build_system_prompt(agent, None, [])

        db.get_session.assert_not_called()
        # No "rebuilding from scratch" warning because history is empty
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("rebuilding" in m for m in warnings)


# ---------------------------------------------------------------------------
# Byte-stability invariant
# ---------------------------------------------------------------------------


class TestPromptStabilityInvariant:
    def test_restored_prompt_is_byte_identical_to_stored(self):
        """The restored prompt must equal the stored bytes exactly — no
        normalization, trimming, or concat that could shift the prefix.

        This is the core invariant: any byte-level change at this point
        invalidates KV cache on every prefix-cache backend.
        """
        stored = (
            "You are Hermes Agent.\n"
            "\n"
            "Conversation started: Sunday, May 17, 2026\n"
            "Session ID: 20260517_153500_abc123\n"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        # Identity check — must be the same object reference for maximum
        # confidence we're not slicing/copying/normalizing.
        assert agent._cached_system_prompt == stored
        # Byte-level check
        assert agent._cached_system_prompt.encode("utf-8") == stored.encode("utf-8")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
