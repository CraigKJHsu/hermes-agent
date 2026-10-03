"""Regression tests for bounded in-process state.db lock waits."""

from __future__ import annotations

import sqlite3
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


@pytest.mark.parametrize("query", ["needle", "記憶系統", "確認"])
def test_search_does_not_use_gateway_shared_connection(db, query):
    """Search callbacks must not share SQLite's mutex with gateway readers."""
    db.append_message("s1", "user", "needle 記憶系統 確認")
    db._lock._timeout_seconds = 0.02
    assert db._lock.acquire(timeout=0.1)
    try:
        hits = db.search_messages(query)
    finally:
        db._lock.release()
    assert [hit["session_id"] for hit in hits] == ["s1"]
    assert hits[0]["context"][0]["content"] == "needle 記憶系統 確認"
    db.append_message("s1", "assistant", "gateway write remains usable")


def test_search_lock_wait_obeys_read_budget(db):
    db.append_message("s1", "user", "needle")
    db._conn.execute("PRAGMA journal_mode=DELETE")
    locker = sqlite3.connect(db.db_path, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    db._SEARCH_TIMEOUT_S = 0.08
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="search.*budget"):
            db.search_messages("needle")
    finally:
        locker.rollback()
        locker.close()
    assert 0.05 <= time.monotonic() - started < 1
    db._SEARCH_TIMEOUT_S = 15
    assert db.search_messages("needle")
