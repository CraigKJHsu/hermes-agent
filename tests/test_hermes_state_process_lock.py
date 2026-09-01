"""Regression tests for bounded in-process state.db lock waits."""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB, _ProcessLockTimeout


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session_db.create_session(session_id="s1", source="telegram")
    yield session_db
    session_db.close()


def test_get_session_fails_fast_behind_long_state_operation(db):
    db._lock._timeout_seconds = 0.02
    assert db._lock.acquire(timeout=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(_ProcessLockTimeout, match="process mutex wait exceeded"):
            db.get_session("s1")
    finally:
        db._lock.release()

    assert time.monotonic() - started < 0.5


def test_write_fails_fast_behind_long_state_operation(db):
    db._lock._timeout_seconds = 0.02
    assert db._lock.acquire(timeout=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(_ProcessLockTimeout, match="process mutex wait exceeded"):
            db.update_system_prompt("s1", "prompt")
    finally:
        db._lock.release()

    assert time.monotonic() - started < 0.5
