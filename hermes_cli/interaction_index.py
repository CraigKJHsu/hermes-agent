"""Read-only, provenance-first index of Hermes collaboration interactions.

The index deliberately does not copy or mutate the source stores.  It reads
Hermes messages, Kanban delegation events, and linked OpenClaw transcripts and
normalises them into one timeline.  Classification is based on source metadata
and relational identifiers; message text is never used to guess an actor.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

from hermes_constants import get_hermes_home


HUMAN_CONVERSATION = "human_conversation"
AGENT_HANDOFF = "agent_handoff"
EXECUTION_TRACE = "execution_trace"
UNCLASSIFIED = "unclassified"

INTERACTION_CLASSES = frozenset({
    HUMAN_CONVERSATION,
    AGENT_HANDOFF,
    EXECUTION_TRACE,
    UNCLASSIFIED,
})

_INTERNAL_SESSION_SOURCES = frozenset({
    "cron",
    "subagent",
    "batch",
    "background",
    "system",
})
_LOCAL_HUMAN_SOURCES = frozenset({"api", "cli", "desktop", "tui"})
_HUMAN_SESSION_SOURCES = frozenset({
    *_LOCAL_HUMAN_SOURCES,
    "api_server",
    "dingtalk",
    "discord",
    "email",
    "feishu",
    "google_chat",
    "imessage",
    "irc",
    "line",
    "matrix",
    "mattermost",
    "msgraph",
    "qqbot",
    "signal",
    "simplex",
    "slack",
    "sms",
    "teams",
    "telegram",
    "wecom",
    "whatsapp",
    "whatsapp_cloud",
    "yuanbao",
})
_OPENCLAW_SCAN_BYTES = 8 * 1024 * 1024
_OPENCLAW_SCAN_LINES = 20_000
_OPENCLAW_SESSION_SCAN_BYTES = 1024 * 1024
_OPENCLAW_SESSION_SCAN_LINES = 2_500
_OPENCLAW_INDEX_BYTES = 2 * 1024 * 1024
_HERMES_CONTENT_SCAN_CHARS = 16 * 1024
_SOURCE_SCAN_ROWS = 10_000
_KANBAN_CONTEXT_ROWS = 50_000
_LEGACY_CALLBACK_MARKER = "[SYSTEM: Grace Loop callback]"
_LEGACY_CALLBACK_FIELD = re.compile(
    r"(?m)^(execution_task_id|grace_review_task_id)=([^\s]+)\s*$"
)
_DELEGATION_START_EVENTS = frozenset({"created", "specified", "promoted"})
_WORKER_RESULT_EVENTS = frozenset({
    "blocked",
    "completed",
    "crashed",
    "gave_up",
    "protocol_violation",
    "timed_out",
})
_OPENCLAW_EVENTS = frozenset({
    "backend_heartbeat",
    "backend_lifecycle",
    "backend_run_bound",
    "runtime_finalization_failed",
    "runtime_finalization_requested",
})
_SAFE_EVENT_PREVIEW_FIELDS = (
    "summary",
    "reason",
    "blocker",
    "status",
    "previous_status",
    "kind",
    "executor_backend",
    "backend_agent_id",
    "backend_run_id",
    "delivery",
)


@dataclass(frozen=True)
class Interaction:
    interaction_id: str
    occurred_at: float
    interaction_class: str
    interaction_subtype: str
    from_actor: str
    to_actor: str
    source_system: str
    source_record_type: str
    source_record_id: str
    classification_basis: str
    visibility: str
    redaction_state: str
    content_preview: str
    origin_session_id: Optional[str] = None
    delegation_id: Optional[str] = None
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    backend_run_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _DelegationLink:
    delegation_id: str
    session_id: str
    execution_task_id: Optional[str]
    review_task_id: Optional[str]


@dataclass(frozen=True)
class _CallbackWindow:
    session_id: str
    event_id: int
    started_at: float
    delivered_at: float
    execution_task_id: str
    review_task_id: str
    delegation_id: Optional[str]


@dataclass
class _KanbanContext:
    execution_links: dict[str, _DelegationLink]
    review_links: dict[str, _DelegationLink]
    delegation_links: dict[str, _DelegationLink]
    backend_sessions: dict[str, dict[str, Any]]
    callback_windows: list[_CallbackWindow]
    callback_provenance_available: bool
    truncated: bool

    @classmethod
    def empty(cls) -> "_KanbanContext":
        return cls({}, {}, {}, {}, [], False, False)


class InteractionIndex:
    """Build a unified interaction timeline without writing source data."""

    def __init__(
        self,
        *,
        hermes_home: Optional[Path] = None,
        openclaw_home: Optional[Path] = None,
        state_db_path: Optional[Path] = None,
        kanban_db_path: Optional[Path] = None,
        preview_chars: int = 360,
    ) -> None:
        home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
        self.hermes_home = home
        self.state_db_path = Path(state_db_path) if state_db_path else home / "state.db"
        self.kanban_db_path = (
            Path(kanban_db_path) if kanban_db_path else home / "kanban.db"
        )
        self.openclaw_home = (
            Path(openclaw_home)
            if openclaw_home is not None
            else Path.home() / ".openclaw"
        )
        self.preview_chars = max(80, min(int(preview_chars), 2_000))

    def query(
        self,
        *,
        limit: int = 200,
        before: Optional[float] = None,
        before_id: Optional[str] = None,
        session_id: Optional[str] = None,
        delegation_id: Optional[str] = None,
        interaction_classes: Optional[Iterable[str]] = None,
        include_internal: bool = False,
        include_unlinked_openclaw: bool = False,
    ) -> dict[str, Any]:
        """Return a newest-first, cross-source interaction timeline."""
        limit = max(1, min(int(limit), 500))
        if (before is None) != (before_id is None):
            raise ValueError("before and before_id must be provided together")
        selected_classes = self._normalise_classes(
            interaction_classes, include_internal=include_internal
        )
        if not include_internal and selected_classes - {HUMAN_CONVERSATION}:
            raise ValueError("include_internal=true is required for internal classes")
        source_limit = limit + 1
        interactions: list[Interaction] = []
        sources: list[dict[str, Any]] = []

        kanban_context = _KanbanContext.empty()
        # Kanban callback rows are also provenance for distinguishing internal
        # callback prompts from authenticated human inputs in Hermes sessions,
        # so load the context even when the UI requests human messages only.
        needs_kanban = True
        if needs_kanban:
            try:
                with self._read_only_connection(self.kanban_db_path) as conn:
                    kanban_context = self._load_kanban_context(conn)
                    if include_internal or delegation_id:
                        kanban_rows, kanban_truncated = self._read_kanban_interactions(
                            conn,
                            context=kanban_context,
                            limit=source_limit,
                            before=before,
                            before_id=before_id,
                            session_id=session_id,
                            delegation_id=delegation_id,
                            selected_classes=selected_classes,
                        )
                        interactions.extend(kanban_rows)
                    else:
                        kanban_truncated = False
                sources.append(
                    self._source_status(
                        "kanban",
                        self.kanban_db_path,
                        detail=(
                            "context or event scan budget reached"
                            if kanban_context.truncated or kanban_truncated
                            else None
                        ),
                    )
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                sources.append(
                    self._source_status("kanban", self.kanban_db_path, error=exc)
                )

        # A delegation filter identifies task records, not a unique slice of
        # the human transcript.  Do not guess which human message "belongs"
        # to it; callers can follow origin_session_id explicitly.
        if not delegation_id:
            try:
                with self._read_only_connection(self.state_db_path) as conn:
                    hermes_rows, hermes_truncated = self._read_hermes_interactions(
                        conn,
                        limit=source_limit,
                        before=before,
                        before_id=before_id,
                        session_id=session_id,
                        include_internal=include_internal,
                        context=kanban_context,
                        selected_classes=selected_classes,
                    )
                    interactions.extend(hermes_rows)
                sources.append(
                    self._source_status(
                        "hermes",
                        self.state_db_path,
                        detail=(
                            "message scan budget reached" if hermes_truncated else None
                        ),
                    )
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                sources.append(
                    self._source_status("hermes", self.state_db_path, error=exc)
                )

        if include_internal and EXECUTION_TRACE in selected_classes:
            try:
                openclaw_rows, openclaw_truncated = self._read_openclaw_interactions(
                    context=kanban_context,
                    limit=source_limit,
                    before=before,
                    before_id=before_id,
                    session_id=session_id,
                    delegation_id=delegation_id,
                    include_unlinked=include_unlinked_openclaw,
                )
                interactions.extend(openclaw_rows)
                sources.append(
                    self._source_status(
                        "openclaw",
                        self.openclaw_home,
                        detail=(
                            f"{len(openclaw_rows)} normalized messages"
                            + ("; scan budget reached" if openclaw_truncated else "")
                        ),
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                sources.append(
                    self._source_status("openclaw", self.openclaw_home, error=exc)
                )

        filtered = [
            item for item in interactions if item.interaction_class in selected_classes
        ]
        filtered.sort(
            key=lambda item: (item.occurred_at, item.interaction_id), reverse=True
        )
        has_more = len(filtered) > limit
        selected = filtered[:limit]
        counts: dict[str, int] = {name: 0 for name in sorted(INTERACTION_CLASSES)}
        for item in selected:
            counts[item.interaction_class] += 1
        truncated = any(
            "budget reached" in str(source.get("detail") or "") for source in sources
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "order": "newest_first",
            "limit": limit,
            "has_more": has_more,
            "truncated": truncated,
            "next_before": selected[-1].occurred_at if has_more and selected else None,
            "next_before_id": selected[-1].interaction_id
            if has_more and selected
            else None,
            "counts": counts,
            "sources": sources,
            "interactions": [item.to_dict() for item in selected],
        }

    def trace_telegram(
        self,
        *,
        trace_id: str = "",
        chat_id: str = "",
        message_id: str = "",
        delegation_id: str = "",
        task_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        """Resolve one canonical Telegram path by any durable relation id."""
        selectors = {
            key: str(value or "").strip()
            for key, value in {
                "trace_id": trace_id,
                "message_id": message_id,
                "delegation_id": delegation_id,
                "task_id": task_id,
                "run_id": run_id,
            }.items()
            if str(value or "").strip()
        }
        if len(selectors) != 1:
            raise ValueError("Provide exactly one Telegram trace selector")
        normalized_chat_id = str(chat_id or "").strip()
        if "message_id" in selectors and not normalized_chat_id:
            raise ValueError("Telegram --message-id requires --chat-id")
        if normalized_chat_id and "message_id" not in selectors:
            raise ValueError("Telegram --chat-id is only valid with --message-id")
        with self._read_only_connection(self.kanban_db_path) as conn:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(grace_delegations)")
            }
            if "telegram_message_path" not in columns:
                raise ValueError(
                    "kanban.db has not been migrated for telegram_message_path"
                )
            name, value = next(iter(selectors.items()))
            if name == "delegation_id":
                clause, params = "gd.delegation_id = ?", [value]
            elif name == "task_id":
                clause = "(gd.execution_task_id = ? OR gd.review_task_id = ?)"
                params = [value, value]
            elif name == "run_id":
                clause = (
                    "EXISTS (SELECT 1 FROM task_runs r WHERE CAST(r.id AS TEXT) = ? "
                    "AND (r.task_id = gd.execution_task_id OR r.task_id = gd.review_task_id))"
                )
                params = [value]
            elif name == "trace_id":
                clause = "json_extract(gd.telegram_message_path, '$.trace_id') = ?"
                params = [value]
            else:
                clause = (
                    "(json_extract(gd.telegram_message_path, '$.chat_id') = ? AND ("
                    "json_extract(gd.telegram_message_path, '$.inbound_message_id') = ? "
                    "OR EXISTS (SELECT 1 FROM json_each(COALESCE(" 
                    "json_extract(gd.telegram_message_path, '$.outbound_message_ids'), '[]')) "
                    "WHERE CAST(json_each.value AS TEXT) = ?) "
                    "OR EXISTS (SELECT 1 FROM json_tree(gd.telegram_message_path, '$.hops') "
                    "WHERE json_tree.key IN ('telegram_message_id', 'approval_message_id') "
                    "AND CAST(json_tree.value AS TEXT) = ?)))"
                )
                params = [normalized_chat_id, value, value, value]
            rows = conn.execute(
                f"""
                SELECT gd.*
                  FROM grace_delegations gd
                 WHERE {clause}
                 ORDER BY gd.rowid DESC
                 LIMIT 20
                """,
                params,
            ).fetchall()
        from hermes_cli.telegram_message_path import normalize_message_path

        traces: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            path = normalize_message_path(record.get("telegram_message_path"))
            if not path:
                continue
            linked = self.query(
                limit=500,
                delegation_id=str(record["delegation_id"]),
                include_internal=True,
            )
            traces.append(
                {
                    "delegation_id": str(record["delegation_id"]),
                    "state": str(record.get("state") or ""),
                    "telegram_message_path": path,
                    "interactions": linked["interactions"],
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "selector": {
                **selectors,
                **({"chat_id": normalized_chat_id} if normalized_chat_id else {}),
            },
            "count": len(traces),
            "traces": traces,
        }

    @staticmethod
    def _is_before_cursor(
        item: Interaction,
        *,
        before: Optional[float],
        before_id: Optional[str],
    ) -> bool:
        if before is None or before_id is None:
            return True
        return (item.occurred_at, item.interaction_id) < (float(before), before_id)

    @staticmethod
    def _normalise_classes(
        values: Optional[Iterable[str]], *, include_internal: bool
    ) -> frozenset[str]:
        if values is None:
            return (
                INTERACTION_CLASSES
                if include_internal
                else frozenset({HUMAN_CONVERSATION})
            )
        selected = {str(value).strip() for value in values if str(value).strip()}
        unknown = selected - INTERACTION_CLASSES
        if unknown:
            raise ValueError(
                f"Unknown interaction classes: {', '.join(sorted(unknown))}"
            )
        return frozenset(selected)

    @staticmethod
    def _source_status(
        source: str,
        path: Path,
        *,
        detail: Optional[str] = None,
        error: Optional[Exception] = None,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "available": error is None,
            "path": str(path),
            "detail": detail if error is None else f"{type(error).__name__}: {error}",
        }

    @staticmethod
    def _read_only_connection(path: Path) -> sqlite3.Connection:
        if not path.is_file():
            raise FileNotFoundError(path)
        encoded = quote(str(path.resolve()), safe="/")
        errors: list[Exception] = []
        for query in ("mode=ro", "mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect(
                    f"file:{encoded}?{query}",
                    uri=True,
                    timeout=0.25,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error as exc:
                errors.append(exc)
        raise errors[-1]

    def _preview(self, value: Any) -> str:
        if value is None:
            return ""
        text = " ".join(str(value).replace("\x00", "").split())
        if len(text) <= self.preview_chars:
            return text
        return text[: self.preview_chars - 1].rstrip() + "…"

    @staticmethod
    def _is_human_source(source: Any) -> bool:
        return str(source or "").strip().lower() in _HUMAN_SESSION_SOURCES

    def _read_hermes_interactions(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int,
        before: Optional[float],
        before_id: Optional[str],
        session_id: Optional[str],
        include_internal: bool,
        context: _KanbanContext,
        selected_classes: frozenset[str],
    ) -> tuple[list[Interaction], bool]:
        result: list[Interaction] = []
        local_before: Optional[tuple[float, int]] = None
        batch_size = max(100, min(limit * 2, 500))
        scanned = 0
        while len(result) < limit and scanned < _SOURCE_SCAN_ROWS:
            read_limit = min(batch_size, _SOURCE_SCAN_ROWS - scanned)
            batch, row_count, local_before = self._read_hermes_batch(
                conn,
                limit=read_limit,
                before=before,
                local_before=local_before,
                session_id=session_id,
                include_internal=include_internal,
                context=context,
            )
            scanned += row_count
            result.extend(
                item
                for item in batch
                if item.interaction_class in selected_classes
                and self._is_before_cursor(item, before=before, before_id=before_id)
            )
            if row_count < read_limit or local_before is None:
                break
        result.sort(
            key=lambda item: (item.occurred_at, item.interaction_id), reverse=True
        )
        return result[:limit], scanned >= _SOURCE_SCAN_ROWS and len(result) < limit

    def _read_hermes_batch(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int,
        before: Optional[float],
        local_before: Optional[tuple[float, int]],
        session_id: Optional[str],
        include_internal: bool,
        context: _KanbanContext,
    ) -> tuple[list[Interaction], int, Optional[tuple[float, int]]]:
        if not self._has_tables(conn, "sessions", "messages"):
            raise ValueError("state.db is missing sessions/messages tables")
        clauses: list[str] = ["m.active = 1"]
        params: list[Any] = []
        if before is not None:
            clauses.append("m.timestamp <= ?")
            params.append(float(before))
        if local_before is not None:
            clauses.append("(m.timestamp < ? OR (m.timestamp = ? AND m.id < ?))")
            params.extend((local_before[0], local_before[0], local_before[1]))
        if session_id:
            clauses.append("m.session_id = ?")
            params.append(session_id)
        if not include_internal:
            placeholders = ",".join("?" for _ in _HUMAN_SESSION_SOURCES)
            clauses.append(f"LOWER(COALESCE(s.source, '')) IN ({placeholders})")
            params.extend(sorted(_HUMAN_SESSION_SOURCES))
            clauses.append("m.role IN ('user', 'assistant')")
        params.append(limit)
        # Do not pull ``content`` or the full ``tool_calls`` JSON into the
        # ORDER BY scan. A handful of tool results can occupy many SQLite
        # overflow pages; selecting those blobs before LIMIT made a dashboard
        # request scan gigabytes of payload despite returning only 200 rows.
        # First select lightweight provenance, then hydrate content for just
        # the bounded result set.
        rows = conn.execute(
            f"""
            SELECT m.id, m.session_id, m.role, m.tool_call_id,
                   CASE WHEN m.role = 'assistant' AND m.tool_calls IS NOT NULL
                        THEN 1 ELSE 0 END AS has_tool_calls,
                   m.tool_name, m.timestamp, m.platform_message_id,
                   s.source, s.chat_id, s.thread_id
              FROM messages m
              JOIN sessions s ON s.id = m.session_id
             WHERE {" AND ".join(clauses)}
             ORDER BY m.timestamp DESC, m.id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()

        content_by_id: dict[int, Any] = {}
        content_ids = [
            int(row["id"])
            for row in rows
            if str(row["role"] or "").lower() != "tool"
            and not bool(row["has_tool_calls"])
            and self._is_human_source(row["source"])
        ]
        if content_ids:
            ids = content_ids
            slots = ",".join("?" for _ in ids)
            content_rows = conn.execute(
                f"""
                    SELECT id,
                           substr(content, 1, ?) AS content_head,
                           substr(content, -?) AS content_tail,
                           length(content) AS content_length
                      FROM messages
                     WHERE id IN ({slots})
                    """,
                [_HERMES_CONTENT_SCAN_CHARS, _HERMES_CONTENT_SCAN_CHARS, *ids],
            ).fetchall()
            for content_row in content_rows:
                head = str(content_row["content_head"] or "")
                if int(content_row["content_length"] or 0) > _HERMES_CONTENT_SCAN_CHARS:
                    tail = str(content_row["content_tail"] or "")
                    content_by_id[int(content_row["id"])] = (
                        head + "\n[content middle omitted]\n" + tail
                    )
                else:
                    content_by_id[int(content_row["id"])] = head

        callback_by_message_id = self._resolve_callback_message_ids(
            conn, rows=rows, context=context
        )

        result: list[Interaction] = []
        for row in rows:
            role = str(row["role"] or "").lower()
            source = str(row["source"] or "unknown").lower()
            human_source = self._is_human_source(source)
            callback = callback_by_message_id.get(int(row["id"]))
            content = content_by_id.get(int(row["id"]))
            legacy_callback = self._legacy_callback_metadata(content)
            has_tool_call = bool(
                row["has_tool_calls"] or row["tool_call_id"] or row["tool_name"]
            )
            if role == "assistant" and not has_tool_call and not content:
                continue
            interaction_delegation_id: Optional[str] = None
            interaction_task_id: Optional[str] = None
            interaction_id = f"hermes:message:{int(row['id']):020d}"
            if legacy_callback and legacy_callback.get("human_prefix") and human_source:
                result.append(
                    Interaction(
                        interaction_id=f"{interaction_id}:human",
                        occurred_at=float(row["timestamp"]),
                        interaction_class=HUMAN_CONVERSATION,
                        interaction_subtype="human_to_grace",
                        from_actor="human",
                        to_actor="grace",
                        source_system="hermes",
                        source_record_type="messages_composite_part",
                        source_record_id=str(row["id"]),
                        classification_basis="composite_row_human_prefix_before_callback",
                        visibility="human",
                        redaction_state="preview_only",
                        content_preview=self._preview(legacy_callback["human_prefix"]),
                        origin_session_id=str(row["session_id"]),
                        chat_id=(
                            str(row["chat_id"]) if row["chat_id"] is not None else None
                        ),
                        thread_id=(
                            str(row["thread_id"])
                            if row["thread_id"] is not None
                            else None
                        ),
                    )
                )
                interaction_id = f"{interaction_id}:callback"
                content = legacy_callback["callback_content"]
            if (callback is not None or legacy_callback is not None) and role == "user":
                interaction_class = AGENT_HANDOFF
                subtype = "clawops_callback_to_grace"
                from_actor, to_actor = "clawops", "grace"
                visibility = "internal"
                if callback is not None:
                    basis = "callback_session_event_window"
                    interaction_delegation_id = callback.delegation_id
                    interaction_task_id = callback.execution_task_id
                else:
                    basis = "structured_callback_envelope_marker"
                    interaction_task_id = legacy_callback.get("execution_task_id")
                    link = context.execution_links.get(interaction_task_id or "")
                    interaction_delegation_id = link.delegation_id if link else None
            elif (
                human_source
                and role == "user"
                and (
                    str(row["platform_message_id"] or "").strip()
                    or context.callback_provenance_available
                )
            ):
                interaction_class = HUMAN_CONVERSATION
                subtype = "human_to_grace"
                from_actor, to_actor = "human", "grace"
                visibility = "human"
                basis = (
                    "platform_message_id"
                    if str(row["platform_message_id"] or "").strip()
                    else (
                        "local_session_source_and_role"
                        if source in _LOCAL_HUMAN_SOURCES
                        else "session_role_after_callback_exclusion"
                    )
                )
            elif human_source and role == "user":
                interaction_class = UNCLASSIFIED
                subtype = "unattributed_gateway_input"
                from_actor, to_actor = "system", "grace"
                visibility = "internal"
                basis = "callback_provenance_unavailable"
            elif human_source and role == "assistant" and not has_tool_call and content:
                interaction_class = HUMAN_CONVERSATION
                subtype = "grace_to_human"
                from_actor, to_actor = "grace", "human"
                visibility = "human"
                basis = "session_source_role_and_no_tool_call"
            elif role == "tool" or has_tool_call:
                interaction_class = EXECUTION_TRACE
                subtype = "grace_tool_trace"
                from_actor, to_actor = (
                    (str(row["tool_name"] or "tool"), "grace")
                    if role == "tool"
                    else ("grace", str(row["tool_name"] or "tool"))
                )
                visibility = "internal"
                basis = "explicit_tool_metadata"
            elif source == "subagent":
                interaction_class = AGENT_HANDOFF
                subtype = "generic_subagent_message"
                from_actor, to_actor = (
                    ("grace", "subagent") if role == "user" else ("subagent", "grace")
                )
                visibility = "internal"
                basis = "session_source_and_role"
            else:
                interaction_class = UNCLASSIFIED
                subtype = "internal_session_message"
                from_actor, to_actor = source or "system", "grace"
                visibility = "internal"
                basis = "source_fallback"

            result.append(
                Interaction(
                    interaction_id=interaction_id,
                    occurred_at=float(row["timestamp"]),
                    interaction_class=interaction_class,
                    interaction_subtype=subtype,
                    from_actor=from_actor,
                    to_actor=to_actor,
                    source_system="hermes",
                    source_record_type="messages",
                    source_record_id=str(row["id"]),
                    classification_basis=basis,
                    visibility=visibility,
                    redaction_state="preview_only",
                    content_preview=self._preview(
                        content
                        if content is not None
                        else (
                            f"{row['tool_name'] or 'tool'} result (content hidden)"
                            if role == "tool"
                            else (
                                f"{row['tool_name'] or 'tool'} call (arguments hidden)"
                                if has_tool_call
                                else "internal session message (content hidden)"
                            )
                        )
                    ),
                    origin_session_id=str(row["session_id"]),
                    delegation_id=interaction_delegation_id,
                    task_id=interaction_task_id,
                    chat_id=str(row["chat_id"]) if row["chat_id"] is not None else None,
                    thread_id=(
                        str(row["thread_id"]) if row["thread_id"] is not None else None
                    ),
                )
            )
        next_local = (
            (float(rows[-1]["timestamp"]), int(rows[-1]["id"])) if rows else None
        )
        return result, len(rows), next_local

    @staticmethod
    def _legacy_callback_metadata(content: Any) -> Optional[dict[str, str]]:
        """Recognize the exact machine envelope used before durable provenance."""
        if not isinstance(content, str):
            return None
        marker_at = content.find(_LEGACY_CALLBACK_MARKER)
        if marker_at < 0:
            return None
        callback_start = marker_at
        speaker_prefix_at = content.rfind("[KJ HSU]", 0, marker_at)
        if (
            speaker_prefix_at >= 0
            and content[speaker_prefix_at:marker_at].strip() == "[KJ HSU]"
        ):
            callback_start = speaker_prefix_at
        callback_content = content[callback_start:].strip()
        metadata = {
            key: value
            for key, value in _LEGACY_CALLBACK_FIELD.findall(callback_content)
        }
        if not metadata.get("execution_task_id"):
            return None
        metadata["human_prefix"] = content[:callback_start].strip()
        metadata["callback_content"] = callback_content
        return metadata

    @staticmethod
    def _resolve_callback_message_ids(
        conn: sqlite3.Connection,
        *,
        rows: list[sqlite3.Row],
        context: _KanbanContext,
    ) -> dict[int, _CallbackWindow]:
        if not rows or not context.callback_windows:
            return {}
        minimum = min(float(row["timestamp"]) for row in rows)
        maximum = max(float(row["timestamp"]) for row in rows)
        session_ids = {str(row["session_id"]) for row in rows}
        matched: dict[int, _CallbackWindow] = {}
        for window in context.callback_windows:
            if window.session_id not in session_ids:
                continue
            if window.delivered_at < minimum or window.started_at > maximum:
                continue
            callback_rows = conn.execute(
                """
                SELECT id
                  FROM messages
                 WHERE session_id = ?
                   AND role = 'user'
                   AND timestamp >= ?
                   AND timestamp <= ?
                 ORDER BY timestamp ASC, id ASC
                """,
                (window.session_id, window.started_at, window.delivered_at),
            ).fetchall()
            for row in callback_rows:
                matched[int(row["id"])] = window
        return matched

    def _load_kanban_context(self, conn: sqlite3.Connection) -> _KanbanContext:
        if not self._has_tables(conn, "tasks", "task_runs", "task_events"):
            raise ValueError("kanban.db is missing task tables")
        context = _KanbanContext.empty()
        if self._has_tables(conn, "grace_delegations"):
            delegation_rows = conn.execute(
                """
                SELECT delegation_id, session_id, execution_task_id, review_task_id
                  FROM grace_delegations
                 ORDER BY rowid DESC
                 LIMIT ?
                """,
                (_KANBAN_CONTEXT_ROWS + 1,),
            ).fetchall()
            if len(delegation_rows) > _KANBAN_CONTEXT_ROWS:
                context.truncated = True
            for row in delegation_rows[:_KANBAN_CONTEXT_ROWS]:
                link = _DelegationLink(
                    delegation_id=str(row["delegation_id"]),
                    session_id=str(row["session_id"]),
                    execution_task_id=(
                        str(row["execution_task_id"])
                        if row["execution_task_id"] is not None
                        else None
                    ),
                    review_task_id=(
                        str(row["review_task_id"])
                        if row["review_task_id"] is not None
                        else None
                    ),
                )
                context.delegation_links[link.delegation_id] = link
                if link.execution_task_id:
                    context.execution_links[link.execution_task_id] = link
                if link.review_task_id:
                    context.review_links[link.review_task_id] = link

        if self._has_tables(conn, "grace_loop_callbacks"):
            context.callback_provenance_available = True
            callback_rows = conn.execute(
                """
                SELECT c.session_id, c.last_event_id, c.delivered_at,
                       c.execution_task_id, c.review_task_id,
                       CASE WHEN c.delivered_at IS NULL
                            THEN COALESCE(ae.created_at, e.created_at)
                            ELSE e.created_at END AS started_at,
                       COALESCE(
                           c.delivered_at,
                           c.lease_expires,
                           COALESCE(ae.created_at, e.created_at) + 900
                       ) AS window_end
                  FROM grace_loop_callbacks c
                  JOIN task_events e ON e.id = c.last_event_id
                  LEFT JOIN task_events ae ON ae.id = c.attempt_event_id
                 WHERE c.session_id IS NOT NULL
                   AND c.session_id != ''
                   AND c.last_event_id > 0
                   AND (c.delivered_at IS NOT NULL OR c.attempts > 0)
                 ORDER BY c.rowid DESC
                 LIMIT ?
                """,
                (_KANBAN_CONTEXT_ROWS + 1,),
            ).fetchall()
            if len(callback_rows) > _KANBAN_CONTEXT_ROWS:
                context.truncated = True
            for row in callback_rows[:_KANBAN_CONTEXT_ROWS]:
                execution_task_id = str(row["execution_task_id"])
                link = context.execution_links.get(execution_task_id)
                context.callback_windows.append(
                    _CallbackWindow(
                        session_id=str(row["session_id"]),
                        event_id=int(row["last_event_id"]),
                        started_at=float(row["started_at"]),
                        delivered_at=min(float(row["window_end"]), time.time()),
                        execution_task_id=execution_task_id,
                        review_task_id=str(row["review_task_id"]),
                        delegation_id=link.delegation_id if link else None,
                    )
                )

        run_rows = conn.execute(
            """
            SELECT r.id, r.task_id, r.backend_run_id, r.backend_agent_id, r.metadata,
                   t.session_id
              FROM task_runs r
              JOIN tasks t ON t.id = r.task_id
             WHERE LOWER(COALESCE(r.executor_backend, '')) = 'openclaw'
                OR r.backend_run_id IS NOT NULL
             ORDER BY r.id DESC
             LIMIT ?
            """,
            (_KANBAN_CONTEXT_ROWS + 1,),
        ).fetchall()
        if len(run_rows) > _KANBAN_CONTEXT_ROWS:
            context.truncated = True
        for row in run_rows[:_KANBAN_CONTEXT_ROWS]:
            metadata = self._json_object(row["metadata"])
            task_id = str(row["task_id"])
            metadata_delegation_id = str(metadata.get("delegation_id") or "").strip()
            metadata_review_task_id = str(metadata.get("review_task_id") or "").strip()
            if metadata_delegation_id and task_id not in context.execution_links:
                link = context.delegation_links.get(metadata_delegation_id)
                if link is None:
                    link = _DelegationLink(
                        delegation_id=metadata_delegation_id,
                        session_id=(
                            str(row["session_id"]) if row["session_id"] else ""
                        ),
                        execution_task_id=task_id,
                        review_task_id=metadata_review_task_id or None,
                    )
                    context.delegation_links[metadata_delegation_id] = link
                context.execution_links[task_id] = link
                if metadata_review_task_id:
                    context.review_links.setdefault(metadata_review_task_id, link)
            session_key = str(metadata.get("backend_session_key") or "").strip()
            if not session_key:
                observation = metadata.get("backend_terminal_observation")
                if isinstance(observation, Mapping):
                    session_key = str(
                        observation.get("backend_session_key") or ""
                    ).strip()
            if not session_key:
                continue
            link = context.execution_links.get(task_id) or context.review_links.get(
                task_id
            )
            context.backend_sessions[session_key] = {
                "task_id": task_id,
                "run_id": str(row["id"]),
                "backend_run_id": (
                    str(row["backend_run_id"])
                    if row["backend_run_id"] is not None
                    else None
                ),
                "backend_agent_id": (str(row["backend_agent_id"] or "openclaw")),
                "delegation_id": link.delegation_id if link else None,
                "origin_session_id": (
                    link.session_id
                    if link
                    else (str(row["session_id"]) if row["session_id"] else None)
                ),
            }
        if context.truncated:
            # An incomplete callback window set cannot safely certify an
            # otherwise unattributed user-role row as human.
            context.callback_provenance_available = False
        return context

    def _read_kanban_interactions(
        self,
        conn: sqlite3.Connection,
        *,
        context: _KanbanContext,
        limit: int,
        before: Optional[float],
        before_id: Optional[str],
        session_id: Optional[str],
        delegation_id: Optional[str],
        selected_classes: frozenset[str],
    ) -> tuple[list[Interaction], bool]:
        result: list[Interaction] = []
        local_before: Optional[tuple[float, int]] = None
        batch_size = max(100, min(limit * 2, 500))
        scanned = 0
        while len(result) < limit and scanned < _SOURCE_SCAN_ROWS:
            read_limit = min(batch_size, _SOURCE_SCAN_ROWS - scanned)
            batch, row_count, local_before = self._read_kanban_batch(
                conn,
                context=context,
                limit=read_limit,
                before=before,
                local_before=local_before,
                session_id=session_id,
                delegation_id=delegation_id,
            )
            scanned += row_count
            result.extend(
                item
                for item in batch
                if item.interaction_class in selected_classes
                and self._is_before_cursor(item, before=before, before_id=before_id)
            )
            if row_count < read_limit or local_before is None:
                break
        result.sort(
            key=lambda item: (item.occurred_at, item.interaction_id), reverse=True
        )
        return result[:limit], scanned >= _SOURCE_SCAN_ROWS and len(result) < limit

    def _read_kanban_batch(
        self,
        conn: sqlite3.Connection,
        *,
        context: _KanbanContext,
        limit: int,
        before: Optional[float],
        local_before: Optional[tuple[float, int]],
        session_id: Optional[str],
        delegation_id: Optional[str],
    ) -> tuple[list[Interaction], int, Optional[tuple[float, int]]]:
        clauses: list[str] = []
        params: list[Any] = []
        selected_task_ids: Optional[set[str]] = None
        if delegation_id:
            link = context.delegation_links.get(delegation_id)
            if link is None or (session_id and link.session_id != session_id):
                return [], 0, None
            selected_task_ids = {
                value
                for value in (link.execution_task_id, link.review_task_id)
                if value
            }
        elif session_id:
            selected_task_ids = {
                task_id
                for task_id, link in (
                    list(context.execution_links.items())
                    + list(context.review_links.items())
                )
                if link.session_id == session_id
            }
            if selected_task_ids:
                slots = ",".join("?" for _ in selected_task_ids)
                clauses.append(f"(t.session_id = ? OR e.task_id IN ({slots}))")
                params.append(session_id)
                params.extend(sorted(selected_task_ids))
            else:
                clauses.append("t.session_id = ?")
                params.append(session_id)
            selected_task_ids = None
        if selected_task_ids is not None:
            if not selected_task_ids:
                return [], 0, None
            slots = ",".join("?" for _ in selected_task_ids)
            clauses.append(f"e.task_id IN ({slots})")
            params.extend(sorted(selected_task_ids))
        if before is not None:
            clauses.append("e.created_at <= ?")
            params.append(int(float(before)))
        if local_before is not None:
            clauses.append("(e.created_at < ? OR (e.created_at = ? AND e.id < ?))")
            params.extend((int(local_before[0]), int(local_before[0]), local_before[1]))
        params.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT e.id, e.task_id, e.run_id, e.kind, e.payload, e.created_at,
                   t.assignee, t.session_id, t.executor_backend, t.executor_profile,
                   r.profile AS run_profile, r.backend_run_id, r.backend_agent_id
              FROM task_events e
              LEFT JOIN tasks t ON t.id = e.task_id
              LEFT JOIN task_runs r ON r.id = e.run_id
              {where}
             ORDER BY e.created_at DESC, e.id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()

        result: list[Interaction] = []
        for row in rows:
            task_id = str(row["task_id"])
            execution_link = context.execution_links.get(task_id)
            review_link = context.review_links.get(task_id)
            link = execution_link or review_link
            kind = str(row["kind"] or "unknown")
            backend = str(row["executor_backend"] or "hermes").lower()
            payload = self._json_object(row["payload"])
            interaction_class, subtype, from_actor, to_actor, basis = (
                self._classify_kanban_event(
                    kind=kind,
                    is_execution=execution_link is not None,
                    is_review=review_link is not None,
                    executor_backend=backend,
                    assignee=row["assignee"],
                    profile=row["run_profile"] or row["executor_profile"],
                    payload=payload,
                )
            )
            preview_bits = [kind.replace("_", " ")]
            for key in _SAFE_EVENT_PREVIEW_FIELDS:
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    preview_bits.append(f"{key}={value}")
            backend_run_id = row["backend_run_id"] or payload.get("backend_run_id")
            result.append(
                Interaction(
                    interaction_id=f"kanban:event:{int(row['id']):020d}",
                    occurred_at=float(row["created_at"]),
                    interaction_class=interaction_class,
                    interaction_subtype=subtype,
                    from_actor=from_actor,
                    to_actor=to_actor,
                    source_system="kanban",
                    source_record_type="task_events",
                    source_record_id=str(row["id"]),
                    classification_basis=basis,
                    visibility="internal",
                    redaction_state="allowlisted_event_fields",
                    content_preview=self._preview(" · ".join(preview_bits)),
                    origin_session_id=(
                        link.session_id
                        if link
                        else (str(row["session_id"]) if row["session_id"] else None)
                    ),
                    delegation_id=link.delegation_id if link else None,
                    task_id=task_id,
                    run_id=str(row["run_id"]) if row["run_id"] is not None else None,
                    backend_run_id=(str(backend_run_id) if backend_run_id else None),
                )
            )
        next_local = (
            (float(rows[-1]["created_at"]), int(rows[-1]["id"])) if rows else None
        )
        return result, len(rows), next_local

    @staticmethod
    def _classify_kanban_event(
        *,
        kind: str,
        is_execution: bool,
        is_review: bool,
        executor_backend: str,
        assignee: Any,
        profile: Any,
        payload: Mapping[str, Any],
    ) -> tuple[str, str, str, str, str]:
        if is_review:
            if kind == "user_facing_report_recorded":
                return (
                    AGENT_HANDOFF,
                    "grace_report_delivery",
                    "grace",
                    "human",
                    "review_task_relation",
                )
            if kind == "grace_correction_requested":
                return (
                    AGENT_HANDOFF,
                    "grace_correction",
                    "grace",
                    "clawops",
                    "review_task_relation",
                )
            if kind == "completed":
                return (
                    AGENT_HANDOFF,
                    "grace_review_completed",
                    "grace",
                    "clawops",
                    "review_task_relation",
                )
            return (
                EXECUTION_TRACE,
                "grace_review_event",
                "system",
                "grace",
                "review_task_relation",
            )

        if is_execution:
            if kind in _DELEGATION_START_EVENTS:
                return (
                    AGENT_HANDOFF,
                    "grace_to_clawops",
                    "grace",
                    "clawops",
                    "execution_task_relation",
                )
            if kind == "grace_correction_requested":
                return (
                    AGENT_HANDOFF,
                    "grace_correction",
                    "grace",
                    "clawops",
                    "execution_task_relation",
                )
            if kind in _WORKER_RESULT_EVENTS:
                return (
                    AGENT_HANDOFF,
                    "clawops_to_grace",
                    "clawops",
                    "grace",
                    "execution_task_relation",
                )
            if kind == "backend_run_bound":
                return (
                    EXECUTION_TRACE,
                    "clawops_to_openclaw",
                    "clawops",
                    "openclaw",
                    "backend_event_kind",
                )
            if kind in _OPENCLAW_EVENTS or executor_backend == "openclaw":
                return (
                    EXECUTION_TRACE,
                    "openclaw_execution",
                    "openclaw",
                    "clawops",
                    "backend_metadata",
                )
            return (
                EXECUTION_TRACE,
                "clawops_execution",
                "clawops",
                "system",
                "execution_task_relation",
            )

        actor_hint = " ".join(str(value or "").lower() for value in (assignee, profile))
        if kind == "backend_run_bound":
            return (
                EXECUTION_TRACE,
                "clawops_to_openclaw",
                "clawops",
                "openclaw",
                "backend_event_kind",
            )
        if executor_backend == "openclaw" or kind in _OPENCLAW_EVENTS:
            return (
                EXECUTION_TRACE,
                "openclaw_execution",
                "openclaw",
                "system",
                "backend_metadata",
            )
        if "clawops" in actor_hint:
            return (
                EXECUTION_TRACE,
                "clawops_execution",
                "clawops",
                "system",
                "worker_profile_metadata",
            )
        if payload.get("delivery") or kind == "user_facing_report_recorded":
            return (
                AGENT_HANDOFF,
                "grace_report_delivery",
                "grace",
                "human",
                "event_kind",
            )
        return UNCLASSIFIED, "kanban_event", "system", "system", "source_fallback"

    def _read_openclaw_interactions(
        self,
        *,
        context: _KanbanContext,
        limit: int,
        before: Optional[float],
        before_id: Optional[str],
        session_id: Optional[str],
        delegation_id: Optional[str],
        include_unlinked: bool,
    ) -> tuple[list[Interaction], bool]:
        openclaw_root = self.openclaw_home.resolve()
        agents_root = (openclaw_root / "agents").resolve()
        try:
            agents_root.relative_to(openclaw_root)
        except ValueError as exc:
            raise ValueError(
                "OpenClaw agents directory escapes configured root"
            ) from exc
        if not agents_root.is_dir():
            raise FileNotFoundError(agents_root)
        result: list[Interaction] = []
        scan_budget = {
            "bytes": _OPENCLAW_SCAN_BYTES,
            "lines": _OPENCLAW_SCAN_LINES,
            "truncated": False,
        }
        index_bytes_remaining = _OPENCLAW_INDEX_BYTES
        for index_path in sorted(agents_root.glob("*/sessions/sessions.json")):
            sessions_dir = index_path.parent.resolve()
            try:
                sessions_dir.relative_to(agents_root)
                resolved_index = index_path.resolve()
                resolved_index.relative_to(sessions_dir)
            except (OSError, RuntimeError, ValueError):
                scan_budget["truncated"] = True
                continue
            with resolved_index.open("rb") as handle:
                raw_index_bytes = handle.read(index_bytes_remaining + 1)
            index_size = len(raw_index_bytes)
            if index_size > index_bytes_remaining:
                scan_budget["truncated"] = True
                break
            index_bytes_remaining -= index_size
            raw_index = json.loads(raw_index_bytes.decode("utf-8", errors="replace"))
            if not isinstance(raw_index, Mapping):
                continue
            for session_key, session_meta in raw_index.items():
                if not isinstance(session_meta, Mapping):
                    continue
                link = context.backend_sessions.get(str(session_key))
                if link is None and not include_unlinked:
                    continue
                if session_id and (
                    not link or link.get("origin_session_id") != session_id
                ):
                    continue
                if delegation_id and (
                    not link or link.get("delegation_id") != delegation_id
                ):
                    continue
                transcript = self._safe_openclaw_transcript_path(
                    sessions_dir, session_meta
                )
                if transcript is None or not transcript.is_file():
                    continue
                agent_id = index_path.parents[1].name
                session_budget = {
                    "bytes": min(_OPENCLAW_SESSION_SCAN_BYTES, scan_budget["bytes"]),
                    "lines": min(_OPENCLAW_SESSION_SCAN_LINES, scan_budget["lines"]),
                    "truncated": False,
                }
                starting_bytes = session_budget["bytes"]
                starting_lines = session_budget["lines"]
                result.extend(
                    self._parse_openclaw_transcript(
                        transcript,
                        agent_id=agent_id,
                        session_key=str(session_key),
                        link=link,
                        limit=limit,
                        before=before,
                        before_id=before_id,
                        scan_budget=session_budget,
                    )
                )
                scan_budget["bytes"] -= starting_bytes - session_budget["bytes"]
                scan_budget["lines"] -= starting_lines - session_budget["lines"]
                scan_budget["truncated"] = bool(
                    scan_budget["truncated"] or session_budget["truncated"]
                )
                result.sort(
                    key=lambda item: (item.occurred_at, item.interaction_id),
                    reverse=True,
                )
                del result[limit:]
                if scan_budget["bytes"] <= 0 or scan_budget["lines"] <= 0:
                    scan_budget["truncated"] = True
                    break
            if scan_budget["bytes"] <= 0 or scan_budget["lines"] <= 0:
                break
        return result, bool(scan_budget["truncated"])

    @staticmethod
    def _safe_openclaw_transcript_path(
        sessions_dir: Path, session_meta: Mapping[str, Any]
    ) -> Optional[Path]:
        raw = str(session_meta.get("sessionFile") or "").strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = sessions_dir / candidate
        else:
            session_id = str(session_meta.get("sessionId") or "").strip()
            if not session_id:
                return None
            candidate = sessions_dir / f"{session_id}.jsonl"
        try:
            resolved = candidate.resolve()
            resolved.relative_to(sessions_dir)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    def _parse_openclaw_transcript(
        self,
        transcript: Path,
        *,
        agent_id: str,
        session_key: str,
        link: Optional[Mapping[str, Any]],
        limit: int,
        before: Optional[float],
        before_id: Optional[str],
        scan_budget: dict[str, Any],
    ) -> list[Interaction]:
        result: list[Interaction] = []
        cutoff_timestamp: Optional[float] = None
        for raw in self._reverse_lines(transcript, scan_budget=scan_budget):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping) or row.get("type") != "message":
                continue
            message = row.get("message")
            if not isinstance(message, Mapping):
                continue
            occurred_at = self._timestamp(
                message.get("timestamp") or row.get("timestamp")
            )
            if cutoff_timestamp is not None and occurred_at < cutoff_timestamp:
                break
            if before is not None and occurred_at > float(before):
                continue
            role = str(message.get("role") or "unknown").lower()
            if link:
                if role == "user":
                    subtype, from_actor, to_actor = (
                        "clawops_to_openclaw",
                        "clawops",
                        "openclaw",
                    )
                elif role == "assistant":
                    subtype, from_actor, to_actor = (
                        "openclaw_to_clawops",
                        "openclaw",
                        "clawops",
                    )
                else:
                    subtype, from_actor, to_actor = (
                        "openclaw_tool_trace",
                        "openclaw",
                        "tool",
                    )
                basis = "backend_session_key"
            else:
                if role == "user":
                    subtype, from_actor, to_actor = (
                        "openclaw_native_input",
                        "openclaw_client",
                        "openclaw",
                    )
                elif role == "assistant":
                    subtype, from_actor, to_actor = (
                        "openclaw_native_output",
                        "openclaw",
                        "openclaw_client",
                    )
                else:
                    subtype, from_actor, to_actor = (
                        "openclaw_native_trace",
                        "openclaw",
                        "tool",
                    )
                basis = "openclaw_native_session_only"
            message_id = str(row.get("id") or "").strip()
            if not message_id:
                # A line-relative fallback changes when append-only logs grow.
                # Rows without a native stable identity are not indexable.
                continue
            source_record_id = f"{session_key}:{message_id}"
            interaction = Interaction(
                interaction_id=f"openclaw:{agent_id}:{source_record_id}",
                occurred_at=occurred_at,
                interaction_class=EXECUTION_TRACE,
                interaction_subtype=subtype,
                from_actor=from_actor,
                to_actor=to_actor,
                source_system="openclaw",
                source_record_type="jsonl_message",
                source_record_id=source_record_id,
                classification_basis=basis,
                visibility="internal",
                redaction_state="text_blocks_preview_only",
                content_preview=self._preview(
                    self._openclaw_text_content(message.get("content"))
                ),
                origin_session_id=(
                    str(link.get("origin_session_id"))
                    if link and link.get("origin_session_id")
                    else None
                ),
                delegation_id=(
                    str(link.get("delegation_id"))
                    if link and link.get("delegation_id")
                    else None
                ),
                task_id=(
                    str(link.get("task_id")) if link and link.get("task_id") else None
                ),
                run_id=(
                    str(link.get("run_id")) if link and link.get("run_id") else None
                ),
                backend_run_id=(
                    str(link.get("backend_run_id"))
                    if link and link.get("backend_run_id")
                    else None
                ),
            )
            if self._is_before_cursor(interaction, before=before, before_id=before_id):
                result.append(interaction)
                if len(result) == limit:
                    cutoff_timestamp = occurred_at
        result.sort(
            key=lambda item: (item.occurred_at, item.interaction_id), reverse=True
        )
        del result[limit:]
        return result

    @staticmethod
    def _reverse_lines(
        path: Path,
        *,
        scan_budget: dict[str, Any],
        block_size: int = 64 * 1024,
    ):
        """Yield an append-only JSONL file from newest line to oldest."""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            remainder = b""
            while position > 0 and scan_budget["bytes"] > 0:
                read_size = min(block_size, position, scan_budget["bytes"])
                position -= read_size
                scan_budget["bytes"] -= read_size
                handle.seek(position)
                chunk = handle.read(read_size) + remainder
                parts = chunk.split(b"\n")
                remainder = parts[0]
                for line in reversed(parts[1:]):
                    if line:
                        if scan_budget["lines"] <= 0:
                            scan_budget["truncated"] = True
                            return
                        scan_budget["lines"] -= 1
                        yield line.decode("utf-8", errors="replace")
            if position > 0:
                scan_budget["truncated"] = True
                return
            if remainder:
                if scan_budget["lines"] <= 0:
                    scan_budget["truncated"] = True
                    return
                scan_budget["lines"] -= 1
                yield remainder.decode("utf-8", errors="replace")

    @staticmethod
    def _openclaw_text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("type") or "").lower()
            if kind in {"text", "input_text", "output_text"}:
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    text_parts.append(value)
            elif kind in {"toolcall", "tool_call", "tooluse", "tool_use"}:
                text_parts.append("[tool call]")
        return " ".join(text_parts)

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            numeric = float(text)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _has_tables(conn: sqlite3.Connection, *names: str) -> bool:
        if not names:
            return True
        slots = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({slots})",
            names,
        ).fetchall()
        return {str(row[0]) for row in rows} == set(names)
