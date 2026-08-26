from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.interaction_index import (
    AGENT_HANDOFF,
    EXECUTION_TRACE,
    HUMAN_CONVERSATION,
    InteractionIndex,
)


def _state_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            chat_id TEXT,
            thread_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            platform_message_id TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO sessions VALUES ('human-session', 'telegram', 'chat-1', 'topic-2');
        INSERT INTO sessions VALUES ('cron-session', 'cron', NULL, NULL);
        INSERT INTO sessions VALUES ('unknown-session', 'private-plugin-worker', NULL, NULL);
        INSERT INTO messages VALUES
            (1, 'human-session', 'user', 'Please inspect this', NULL, NULL, NULL, 100, 'tg-1', 1),
            (2, 'human-session', 'assistant', NULL, NULL, '[{"name":"clawops_delegate"}]', NULL, 101, NULL, 1),
            (3, 'human-session', 'tool', '{"status":"queued"}', 'call-1', NULL, 'clawops_delegate', 102, NULL, 1),
            (4, 'human-session', 'assistant', 'I handed it off.', NULL, NULL, NULL, 103, 'tg-2', 1),
            (5, 'cron-session', 'user', 'scheduled prompt', NULL, NULL, NULL, 104, NULL, 1),
            (6, 'human-session', 'user', 'trusted callback envelope', NULL, NULL, NULL, 105, NULL, 1),
            (7, 'human-session', 'assistant', 'internal planning text', NULL,
             '[{"name":"clawops_delegate"}]', NULL, 107, NULL, 1),
            (11, 'human-session', 'user', 'pending callback text', NULL, NULL, NULL, 108, NULL, 1),
            (12, 'unknown-session', 'assistant', 'unknown internal text', NULL, NULL, NULL, 109, NULL, 1),
            (13, 'human-session', 'user', '[KJ HSU] [SYSTEM: Grace Loop callback]
execution_task_id=exec-1
grace_review_task_id=old-review', NULL, NULL, NULL, 1000, NULL, 1);
        """
    )
    conn.commit()
    conn.close()


def _kanban_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            assignee TEXT,
            session_id TEXT,
            executor_backend TEXT,
            executor_profile TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            executor_backend TEXT,
            backend_run_id TEXT,
            backend_agent_id TEXT,
            metadata TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE grace_delegations (
            delegation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            execution_task_id TEXT,
            review_task_id TEXT,
            telegram_message_path TEXT
        );
        CREATE TABLE grace_loop_callbacks (
            review_task_id TEXT PRIMARY KEY,
            execution_task_id TEXT NOT NULL,
            session_id TEXT,
            last_event_id INTEGER NOT NULL,
            delivered_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_expires INTEGER,
            attempt_event_id INTEGER
        );
        INSERT INTO tasks VALUES
            ('exec-1', 'clawops-browser', 'human-session', 'openclaw', 'browser-readonly'),
            ('review-1', 'default', 'human-session', 'hermes', 'grace-review');
        INSERT INTO grace_delegations VALUES
            ('delegation-1', 'human-session', 'exec-1', 'review-1',
             '{"schema_version":"1.0","trace_id":"tgtrace-1","platform":"telegram","chat_id":"chat-1","inbound_message_id":"tg-1","outbound_message_ids":["tg-out-1"],"hops":[{"stage":"human_approval","identifiers":{"approval_message_id":"tg-approval-1"}}]}');
        INSERT INTO task_runs VALUES
            (9, 'exec-1', 'clawops-browser', 'openclaw', 'backend-9', 'browser-agent',
             '{"backend_session_key":"agent:browser:subagent:run-9"}');
        INSERT INTO task_events VALUES
            (10, 'exec-1', NULL, 'created', '{"assignee":"clawops-browser"}', 110),
            (11, 'exec-1', 9, 'backend_run_bound',
             '{"backend_run_id":"backend-9","backend_agent_id":"browser-agent"}', 111),
            (12, 'exec-1', 9, 'completed', '{"summary":"verified"}', 112),
            (13, 'review-1', NULL, 'grace_correction_requested', '{"reason":"missing evidence"}', 113),
            (14, 'exec-1', 9, 'blocked', '{"reason":"callback trigger"}', 104),
            (15, 'exec-1', 9, 'blocked', '{"reason":"pending callback"}', 107);
        INSERT INTO grace_loop_callbacks VALUES
            ('review-1', 'exec-1', 'human-session', 14, 106, 1, NULL, 14),
            ('review-pending', 'exec-1', 'human-session', 15, NULL, 1, 120, 15);
        """
    )
    conn.commit()
    conn.close()


def _openclaw_home(path: Path) -> None:
    sessions = path / "agents" / "browser" / "sessions"
    sessions.mkdir(parents=True)
    transcript = sessions / "native-9.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({
                "type": "session",
                "id": "native-9",
                "timestamp": "1970-01-01T00:01:50Z",
            }),
            json.dumps({
                "type": "message",
                "id": "oc-user",
                "timestamp": "1970-01-01T00:01:54Z",
                "message": {
                    "role": "user",
                    "content": "delegated input",
                    "timestamp": "1970-01-01T00:01:54Z",
                },
            }),
            json.dumps({
                "type": "message",
                "id": "oc-assistant",
                "timestamp": "1970-01-01T00:01:55Z",
                "message": {
                    "role": "assistant",
                    "content": "execution result",
                    "timestamp": "1970-01-01T00:01:55Z",
                    "reasoning": "must never be surfaced",
                },
            }),
        ])
        + "\n",
        encoding="utf-8",
    )
    unlinked = sessions / "native-other.jsonl"
    unlinked.write_text(
        json.dumps({
            "type": "message",
            "id": "oc-user",
            "timestamp": "1970-01-01T00:01:56Z",
            "message": {"role": "user", "content": "native input"},
        })
        + "\n",
        encoding="utf-8",
    )
    (sessions / "sessions.json").write_text(
        json.dumps({
            "agent:browser:subagent:run-9": {
                "sessionId": "native-9",
                "sessionFile": str(transcript),
            },
            "agent:browser:main": {
                "sessionId": "native-other",
                "sessionFile": str(unlinked),
            },
        }),
        encoding="utf-8",
    )


def _index(tmp_path: Path) -> InteractionIndex:
    state = tmp_path / "state.db"
    kanban = tmp_path / "kanban.db"
    openclaw = tmp_path / ".openclaw"
    _state_db(state)
    _kanban_db(kanban)
    _openclaw_home(openclaw)
    return InteractionIndex(
        hermes_home=tmp_path,
        openclaw_home=openclaw,
        state_db_path=state,
        kanban_db_path=kanban,
    )


def test_default_timeline_only_contains_human_grace_messages(tmp_path):
    result = _index(tmp_path).query(limit=50)

    assert [item["interaction_subtype"] for item in result["interactions"]] == [
        "grace_to_human",
        "human_to_grace",
    ]
    assert {item["interaction_class"] for item in result["interactions"]} == {
        HUMAN_CONVERSATION
    }


def test_telegram_trace_resolves_by_message_task_run_and_trace(tmp_path):
    index = _index(tmp_path)

    for selector in (
        {"chat_id": "chat-1", "message_id": "tg-1"},
        {"chat_id": "chat-1", "message_id": "tg-out-1"},
        {"chat_id": "chat-1", "message_id": "tg-approval-1"},
        {"task_id": "exec-1"},
        {"run_id": "9"},
        {"trace_id": "tgtrace-1"},
        {"delegation_id": "delegation-1"},
    ):
        result = index.trace_telegram(**selector)
        assert result["count"] == 1
        assert result["traces"][0]["delegation_id"] == "delegation-1"
        assert result["traces"][0]["telegram_message_path"]["trace_id"] == "tgtrace-1"

    with pytest.raises(ValueError, match="requires --chat-id"):
        index.trace_telegram(message_id="tg-1")


def test_internal_timeline_uses_relations_instead_of_message_text(tmp_path):
    result = _index(tmp_path).query(
        limit=100,
        include_internal=True,
        session_id="human-session",
    )
    by_subtype = {item["interaction_subtype"]: item for item in result["interactions"]}

    assert by_subtype["grace_to_clawops"]["delegation_id"] == "delegation-1"
    assert by_subtype["clawops_to_openclaw"]["backend_run_id"] == "backend-9"
    assert by_subtype["clawops_to_grace"]["interaction_class"] == AGENT_HANDOFF
    assert by_subtype["grace_correction"]["from_actor"] == "grace"
    assert by_subtype["grace_tool_trace"]["interaction_class"] == EXECUTION_TRACE
    assert by_subtype["clawops_callback_to_grace"]["interaction_class"] == AGENT_HANDOFF
    assert by_subtype["clawops_callback_to_grace"]["from_actor"] == "clawops"


def test_openclaw_user_role_is_not_classified_as_human(tmp_path):
    result = _index(tmp_path).query(
        limit=100,
        include_internal=True,
        delegation_id="delegation-1",
    )
    openclaw = [
        item for item in result["interactions"] if item["source_system"] == "openclaw"
    ]

    assert len(openclaw) == 2
    assert {item["interaction_class"] for item in openclaw} == {EXECUTION_TRACE}
    request = next(
        item for item in openclaw if item["source_record_id"].endswith(":oc-user")
    )
    assert (request["from_actor"], request["to_actor"]) == ("clawops", "openclaw")
    assert request["delegation_id"] == "delegation-1"
    assert all(
        "must never be surfaced" not in item["content_preview"] for item in openclaw
    )


def test_openclaw_ids_include_session_identity(tmp_path):
    result = _index(tmp_path).query(
        limit=100,
        include_internal=True,
        include_unlinked_openclaw=True,
    )
    duplicate_message_ids = [
        item
        for item in result["interactions"]
        if item["source_system"] == "openclaw"
        and item["source_record_id"].endswith(":oc-user")
    ]

    assert len(duplicate_message_ids) == 2
    assert len({item["interaction_id"] for item in duplicate_message_ids}) == 2


def test_text_beside_tool_calls_is_never_exposed_as_human_conversation(tmp_path):
    index = _index(tmp_path)

    public = index.query(limit=50)
    assert all(
        "internal planning text" not in item["content_preview"]
        for item in public["interactions"]
    )

    internal = index.query(limit=50, include_internal=True)
    tool_call = next(
        item for item in internal["interactions"] if item["source_record_id"] == "7"
    )
    assert tool_call["interaction_class"] == EXECUTION_TRACE
    assert "internal planning text" not in tool_call["content_preview"]


def test_unknown_source_and_pending_callback_stay_out_of_public_timeline(tmp_path):
    index = _index(tmp_path)
    conn = sqlite3.connect(index.state_db_path)
    conn.execute(
        """
        INSERT INTO messages VALUES
            (14, 'human-session', 'user', '[KJ HSU] approved
[KJ HSU] [SYSTEM: Grace Loop callback]
execution_task_id=exec-1
grace_review_task_id=composite-review', NULL, NULL, NULL, 1100, NULL, 1)
        """
    )
    conn.commit()
    conn.close()
    public = index.query(limit=50)
    previews = {item["content_preview"] for item in public["interactions"]}

    assert "unknown internal text" not in previews
    assert "pending callback text" not in previews
    assert all("Grace Loop callback" not in preview for preview in previews)

    internal = index.query(limit=50, include_internal=True)
    legacy = next(
        item for item in internal["interactions"] if item["source_record_id"] == "13"
    )
    assert legacy["interaction_subtype"] == "clawops_callback_to_grace"
    assert legacy["classification_basis"] == "structured_callback_envelope_marker"
    assert legacy["delegation_id"] == "delegation-1"

    composite = [
        item for item in internal["interactions"] if item["source_record_id"] == "14"
    ]
    assert {item["interaction_class"] for item in composite} == {
        HUMAN_CONVERSATION,
        AGENT_HANDOFF,
    }
    human_part = next(
        item for item in composite if item["interaction_class"] == HUMAN_CONVERSATION
    )
    callback_part = next(
        item for item in composite if item["interaction_class"] == AGENT_HANDOFF
    )
    assert human_part["content_preview"] == "[KJ HSU] approved"
    assert "Grace Loop callback" not in human_part["content_preview"]
    assert "Grace Loop callback" in callback_part["content_preview"]


def test_class_filter_pages_past_newer_nonmatching_events(tmp_path):
    index = _index(tmp_path)
    conn = sqlite3.connect(index.kanban_db_path)
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'exec-1', 9, 'heartbeat', '{}', ?)",
        [(event_id, 200 + event_id) for event_id in range(20, 140)],
    )
    conn.commit()
    conn.close()

    result = index.query(
        limit=1,
        include_internal=True,
        interaction_classes=[AGENT_HANDOFF],
    )

    assert len(result["interactions"]) == 1
    assert result["interactions"][0]["interaction_class"] == AGENT_HANDOFF


def test_compound_cursor_keeps_equal_timestamp_records(tmp_path):
    index = _index(tmp_path)
    conn = sqlite3.connect(index.state_db_path)
    conn.executemany(
        """
        INSERT INTO messages
            (id, session_id, role, content, timestamp, platform_message_id, active)
        VALUES (?, 'human-session', 'assistant', ?, 120, ?, 1)
        """,
        [
            (8, "same-second-a", "tg-8"),
            (9, "same-second-b", "tg-9"),
            (10, "same-second-c", "tg-10"),
        ],
    )
    conn.commit()
    conn.close()

    first = index.query(limit=1)
    second = index.query(
        limit=1,
        before=first["next_before"],
        before_id=first["next_before_id"],
    )

    assert first["interactions"][0]["source_record_id"] == "10"
    assert second["interactions"][0]["source_record_id"] == "9"


def test_session_filter_includes_direct_unlinked_kanban_task(tmp_path):
    index = _index(tmp_path)
    conn = sqlite3.connect(index.kanban_db_path)
    conn.execute(
        "INSERT INTO tasks VALUES ('direct-1', 'clawops', 'direct-session', 'hermes', 'worker')"
    )
    conn.execute(
        "INSERT INTO task_events VALUES (16, 'direct-1', NULL, 'created', '{}', 116)"
    )
    conn.commit()
    conn.close()

    result = index.query(
        limit=20,
        include_internal=True,
        session_id="direct-session",
    )

    assert any(item["task_id"] == "direct-1" for item in result["interactions"])


def test_session_and_delegation_filters_are_conjunctive(tmp_path):
    result = _index(tmp_path).query(
        limit=20,
        include_internal=True,
        session_id="different-session",
        delegation_id="delegation-1",
    )

    assert result["interactions"] == []


def test_long_composite_row_uses_bounded_head_and_tail(tmp_path):
    index = _index(tmp_path)
    content = (
        "[KJ HSU] "
        + ("approved " * 3_000)
        + "\n[KJ HSU] [SYSTEM: Grace Loop callback]\n"
        + "execution_task_id=exec-1\n"
        + "grace_review_task_id=long-review"
    )
    conn = sqlite3.connect(index.state_db_path)
    conn.execute(
        "INSERT INTO messages VALUES (15, 'human-session', 'user', ?, NULL, NULL, NULL, 1200, NULL, 1)",
        (content,),
    )
    conn.commit()
    conn.close()

    internal = index.query(limit=50, include_internal=True)
    parts = [
        item for item in internal["interactions"] if item["source_record_id"] == "15"
    ]

    assert {item["interaction_class"] for item in parts} == {
        HUMAN_CONVERSATION,
        AGENT_HANDOFF,
    }


def test_unattributed_local_user_fails_closed_without_callback_provenance(tmp_path):
    index = _index(tmp_path)
    conn = sqlite3.connect(index.state_db_path)
    conn.execute("INSERT INTO sessions VALUES ('cli-no-proof', 'cli', NULL, NULL)")
    conn.execute(
        "INSERT INTO messages VALUES (16, 'cli-no-proof', 'user', 'local input', NULL, NULL, NULL, 1300, NULL, 1)"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(index.kanban_db_path)
    conn.execute("DROP TABLE grace_loop_callbacks")
    conn.commit()
    conn.close()

    public = index.query(limit=50)
    internal = index.query(limit=50, include_internal=True)

    assert all(item["source_record_id"] != "16" for item in public["interactions"])
    row = next(
        item for item in internal["interactions"] if item["source_record_id"] == "16"
    )
    assert row["interaction_class"] == "unclassified"


def test_openclaw_scan_budget_is_reported(tmp_path, monkeypatch):
    import hermes_cli.interaction_index as module

    monkeypatch.setattr(module, "_OPENCLAW_SCAN_LINES", 1)
    result = _index(tmp_path).query(
        limit=100,
        include_internal=True,
        include_unlinked_openclaw=True,
    )
    source = next(item for item in result["sources"] if item["source"] == "openclaw")

    assert "scan budget reached" in source["detail"]


def test_query_does_not_modify_source_database_schema(tmp_path):
    index = _index(tmp_path)

    def schema(path: Path):
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        conn.close()
        return rows

    before = (schema(index.state_db_path), schema(index.kanban_db_path))
    index.query(limit=100, include_internal=True)
    after = (schema(index.state_db_path), schema(index.kanban_db_path))

    assert after == before
