# Interaction Index

The Interaction Index is a read-only provenance layer for the MissionCrew
collaboration timeline. It does not merge or rewrite the underlying stores.

## Sources

| Source | Authority | Normalized records |
|---|---|---|
| Hermes | `HERMES_HOME/state.db` | Human-facing messages and internal tool traces |
| Kanban | `HERMES_HOME/kanban.db` | Delegations, worker events, results, and Grace review events |
| OpenClaw | `~/.openclaw/agents/*/sessions/` | Native messages linked through `backend_session_key` |

The default query returns only human-facing Hermes messages. Internal records
must be explicitly requested. Unlinked OpenClaw sessions require a second,
separate opt-in.

## Classification contract

Classification never uses conversational prose to infer an actor.

1. Explicit source metadata such as Hermes `sessions.source`, message `role`,
   `platform_message_id`, and tool-call columns.
2. Relational links from `grace_delegations` to execution and review task IDs.
3. Grace callback lifecycle windows from `grace_loop_callbacks` and the exact
   triggering `task_events` row. User-role rows in that same session and
   callback lifecycle window are internal ClawOps-to-Grace callback attempts,
   not human messages. The gateway serializes the active callback turn, so
   external inputs are queued outside that interval. This avoids inspecting
   the callback's text prefix.
   Undelivered callback attempts use their attempt/lease timestamps and a
   bounded fallback window; merely queued callbacks do not open a window.
4. Legacy callback rows whose mutable callback registry no longer retains the
   historical event use the exact machine envelope marker plus its structured
   task-ID fields. Free-form prose is not inspected. If one stored row contains
   a queued human prefix followed by that envelope, the index emits two
   provenance-linked interactions instead of forcing the composite row into a
   single class.
5. Kanban `task_id`, `run_id`, `executor_backend`, and `backend_run_id`.
6. OpenClaw `backend_session_key` to its native session index and JSONL.
7. If none of the above is sufficient, emit `unclassified`; do not guess.

The top-level classes are:

- `human_conversation`: human to Grace or Grace to human.
- `agent_handoff`: Grace to ClawOps contracts, worker results, review, and
  user-report delivery records.
- `execution_trace`: tool calls, ClawOps worker events, and OpenClaw execution.
- `unclassified`: source records without sufficient provenance.

OpenClaw `role=user` is not a human identity. For a linked backend session it
means ClawOps submitted an execution request to OpenClaw. For an unlinked
native session it is labelled `openclaw_client`, never `human`.

## Privacy and safety

- Source SQLite databases are opened with `mode=ro` and `PRAGMA query_only=ON`;
  immutable read-only mode is a compatibility fallback.
- The index returns bounded previews rather than copying full records.
- Assistant rows carrying tool-call metadata are always internal, even when
  they also contain text.
- Human-facing session sources use an affirmative platform/CLI allowlist;
  unknown or plugin-defined sources remain internal and `unclassified`.
- User-role rows without a platform message ID fail closed when callback
  provenance is unavailable or truncated, including local CLI sources.
- Kanban previews use an allowlist of event fields and never expose task bodies.
- OpenClaw previews extract text blocks only and do not expose reasoning.
- OpenClaw transcripts are read newest-first under a request-wide byte/line
  scan budget plus a per-session fairness cap. Session indexes have a separate
  aggregate byte cap, and all resolved paths must remain beneath the configured
  OpenClaw root. Budget exhaustion is reported in source status. Session
  identity is part of every OpenClaw record ID, and rows without a stable native
  message ID are not indexed.
- Hermes and Kanban sparse-class scans and Kanban context construction have
  explicit row caps; source status reports when a cap is reached.
- Hermes content hydration reads bounded head and tail segments so a long
  composite row cannot hide a trailing callback envelope.
- Delegated raw user wording remains subject to the existing
  `_worker_safe_contract` boundary.

## API

The authenticated Dashboard plugin exposes:

```text
GET /api/plugins/interaction-viewer/interactions
```

Query parameters include `session_id`, `delegation_id`, `limit`,
`include_internal`, `include_unlinked_openclaw`, and comma-separated `classes`.
Internal classes require `include_internal=true`. Pagination uses the compound
`before` plus `before_id` cursor returned as `next_before` and
`next_before_id`; both values must be supplied together so equal-timestamp
records cannot be skipped. Responses are newest-first and retain source record
IDs so an operator can audit every classification decision.
The Dashboard exposes the same compound cursor through its load-more control.
Superseded filter requests are discarded by request generation so an older
internal response cannot overwrite a newer public view.

## Bounded-history limitation

This implementation is intentionally stateless and read-only. Source status
reports when a scan cap is reached, but the current compound time/ID cursor does
not preserve per-source SQLite scan frontiers or OpenClaw byte offsets. Very
deep pages beyond those caps therefore require a future source-aware cursor
contract (or a separate derived index). The API must not interpret a reported
scan cap as proof that the underlying source has no older records.
