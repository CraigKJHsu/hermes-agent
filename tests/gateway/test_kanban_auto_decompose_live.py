"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from gateway.kanban_watchers import (
    _is_loop_breaker_triage,
    _resolve_auto_decompose_settings,
)


def test_enabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is True
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_per_tick_respected_and_clamped():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": True, "auto_decompose_per_tick": 7}}
    )
    assert (enabled, per_tick) == (True, 7)

    # 0 is treated as "unset" by the `or 3` fallback → default 3 (a 0 per-tick
    # cap would disable progress, so falling back to the default is the safe read).
    _, per_tick_zero = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose_per_tick": 0}}
    )
    assert per_tick_zero == 3

    # A genuine negative value clamps up to 1.
    _, per_tick_neg = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose_per_tick": -5}}
    )
    assert per_tick_neg == 1


def test_malformed_per_tick_falls_back_to_default():
    _, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose_per_tick": "lots"}}
    )
    assert per_tick == 3


def test_config_read_error_fails_safe_disabled():
    """A transient config read failure must DISABLE auto-decompose, never
    silently fall back to the default-on behaviour the user turned off."""

    def _boom():
        raise RuntimeError("config read failed")

    enabled, per_tick = _resolve_auto_decompose_settings(_boom)
    assert enabled is False
    assert per_tick == 3


def test_non_dict_config_fails_safe():
    enabled, _ = _resolve_auto_decompose_settings(lambda: None)
    assert enabled is True  # no kanban key → default-on (not an error path)
    enabled2, _ = _resolve_auto_decompose_settings(lambda: ["not", "a", "dict"])
    assert enabled2 is True


def test_live_toggle_takes_effect_between_calls():
    """Simulate a user flipping the flag while the dispatcher runs: a later
    resolution reflects the new value without any restart."""
    state = {"kanban": {"auto_decompose": True}}
    assert _resolve_auto_decompose_settings(lambda: state)[0] is True
    # User edits config.yaml mid-run.
    state["kanban"]["auto_decompose"] = False
    assert _resolve_auto_decompose_settings(lambda: state)[0] is False


def test_repeated_block_triage_is_reserved_for_human_decision():
    assert _is_loop_breaker_triage(
        SimpleNamespace(block_kind="needs_input", block_recurrences=2)
    ) is True
    assert _is_loop_breaker_triage(
        SimpleNamespace(block_kind="capability", block_recurrences=3)
    ) is True


def test_fresh_triage_remains_auto_decomposable():
    assert _is_loop_breaker_triage(
        SimpleNamespace(block_kind=None, block_recurrences=0)
    ) is False
    assert _is_loop_breaker_triage(None) is False


def test_background_board_scope_cannot_redirect_concurrent_delegate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("canary")
    barrier = Barrier(2)

    def scan_canary():
        with kb.scoped_current_board("canary"):
            barrier.wait()
            assert kb.get_current_board() == "canary"
            barrier.wait()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scan_canary)
        barrier.wait()
        try:
            assert kb.get_current_board() == kb.DEFAULT_BOARD
            assert kb.kanban_db_path() == tmp_path / "kanban.db"
        finally:
            barrier.wait()
        future.result()
