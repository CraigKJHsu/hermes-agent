#!/usr/bin/env python3
"""Context-bound, read-only access to managed Topic policies."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from typing import Any, Mapping

from tools.registry import registry

_CARTER_PAGE_HERO_VERSION = "2026-08-28.3"
_CARTER_PAGE_HERO_SHA256 = (
    "3cf13f26cdeaf80d38153b51c7ef1591eb74249994bc26ea76f337353e344cd0"
)
_AI_BIZWEEK_READINESS_TASKS = {
    "page_hero_policy_replacement": "t_8b14cafd",
    "runtime_probe_repair": "t_70bf2afe",
}
_AI_BIZWEEK_OBSOLETE_TASKS = {
    "t_7987e610": (
        "Obsolete stale policy snapshot review for ai-bizweek-page-hero "
        "2026-08-28.1; not a current Carter EP04 blocker."
    ),
    "t_60588d23": (
        "Restricted OpenClaw read-only evidence check stopped fail-closed because "
        "that runtime could not read Hermes DB evidence; use this managed policy "
        "readback instead."
    ),
    "t_b8fc5528": (
        "Grace review accepted the fail-closed stop; not a package-production "
        "acceptance signal and not a current blocker."
    ),
}
_CARTER_SOURCE_SESSION_ID = "20260827_225416_960cfd29"
_CARTER_SOURCE_MESSAGE_ID = 43616


def _policy_summary(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_id": policy.get("policy_id"),
        "version": policy.get("version"),
        "sha256": policy.get("sha256"),
        "resolution": policy.get("resolution"),
    }


def _latest_run_summary(runs: list[Any]) -> dict[str, Any] | None:
    if not runs:
        return None
    run = runs[-1]
    metadata = run.metadata if isinstance(run.metadata, Mapping) else {}
    runtime_fix = metadata.get("runtime_fix") if isinstance(metadata, Mapping) else None
    if not isinstance(runtime_fix, Mapping):
        runtime_fix = {}
    probe = metadata.get("manual_probe") if isinstance(metadata, Mapping) else None
    if not isinstance(probe, Mapping):
        probe = {}
    return {
        "run_id": run.id,
        "status": run.status,
        "outcome": run.outcome,
        "ended_at": run.ended_at,
        "executor_backend": run.executor_backend,
        "backend_run_id": run.backend_run_id,
        "backend_status": run.backend_status,
        "metadata_keys": sorted(metadata.keys()) if isinstance(metadata, Mapping) else [],
        "manual_probe_ok": bool(
            metadata.get("manual_probe_ok") or runtime_fix.get("manual_probe_ok")
        ),
        "plugin_discovery_attempted": bool(
            metadata.get("plugin_discovery_attempted")
            or runtime_fix.get("plugin_discovery_attempted")
            or probe.get("plugin_discovery_attempted")
        ),
        "external_actions_performed": bool(
            metadata.get("external_actions_performed")
            or runtime_fix.get("external_actions_performed")
        ),
        **(
            {"runtime_fix_root_cause": runtime_fix.get("root_cause")}
            if runtime_fix.get("root_cause")
            else {}
        ),
    }


def _ai_bizweek_known_readiness_tasks() -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tasks: dict[str, Any] = {}
        for label, task_id in _AI_BIZWEEK_READINESS_TASKS.items():
            task = kb.get_task(conn, task_id)
            if task is None:
                tasks[label] = {"task_id": task_id, "status": "missing"}
                continue
            latest_run = _latest_run_summary(kb.list_runs(conn, task_id))
            tasks[label] = {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "completed_at": task.completed_at,
                "block_kind": task.block_kind,
                "result_excerpt": " ".join(str(task.result or "").split())[:700],
                **({"latest_run": latest_run} if latest_run else {}),
            }
        obsolete = []
        for task_id, note in _AI_BIZWEEK_OBSOLETE_TASKS.items():
            task = kb.get_task(conn, task_id)
            if task is None:
                continue
            obsolete.append(
                {
                    "task_id": task_id,
                    "status": task.status,
                    "block_kind": task.block_kind,
                    "note": note,
                }
            )
        return {"tasks": tasks, "obsolete_tasks": obsolete}
    finally:
        conn.close()


def _ai_bizweek_operational_readiness(
    policies: list[Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> dict[str, Any] | None:
    page_hero = next(
        (policy for policy in policies if policy.get("policy_id") == "ai-bizweek-page-hero"),
        None,
    )
    if page_hero is None:
        return None
    if (
        str(page_hero.get("version") or "") != _CARTER_PAGE_HERO_VERSION
        or str(page_hero.get("sha256") or "") != _CARTER_PAGE_HERO_SHA256
    ):
        return None

    try:
        task_evidence = _ai_bizweek_known_readiness_tasks()
        evidence_error = None
    except Exception as exc:
        task_evidence = {"tasks": {}, "obsolete_tasks": []}
        evidence_error = f"{type(exc).__name__}: {exc}"

    tasks = task_evidence.get("tasks") if isinstance(task_evidence, Mapping) else {}
    if not isinstance(tasks, Mapping):
        tasks = {}
    policy_task = tasks.get("page_hero_policy_replacement")
    runtime_task = tasks.get("runtime_probe_repair")
    if not isinstance(policy_task, Mapping):
        policy_task = {}
    if not isinstance(runtime_task, Mapping):
        runtime_task = {}
    latest_run = runtime_task.get("latest_run")
    if not isinstance(latest_run, Mapping):
        latest_run = {}

    tasks_done = (
        policy_task.get("status") == "done"
        and runtime_task.get("status") == "done"
    )
    worker_probe_ready = bool(latest_run.get("manual_probe_ok"))
    complete = tasks_done and worker_probe_ready and evidence_error is None
    return {
        "kind": "ai_bizweek_carter_ep04_readiness",
        "source": "managed_policy_read",
        "scope": dict(scope),
        "complete": complete,
        "status": "ready_for_fresh_package_task" if complete else "blocked_or_unknown",
        "active_page_hero": _policy_summary(page_hero),
        "tasks": dict(tasks),
        "obsolete_tasks": list(task_evidence.get("obsolete_tasks") or []),
        "worker_probe": {
            "verified": worker_probe_ready,
            "manual_probe_ok": worker_probe_ready,
            "plugin_discovery_attempted": bool(
                latest_run.get("plugin_discovery_attempted")
            ),
        },
        "external_actions_performed": bool(
            (latest_run or {}).get("external_actions_performed")
        ),
        "canonical_note": (
            "If complete=true, this is the current Grace-readable readiness readback "
            "for Carter's Junk Away / EP04 package production. Do not ask KJ to "
            "provide t_70bf2afe evidence, and do not treat obsolete stale-review "
            "tasks as current blockers."
        ),
        "guidance": [
            "Start a fresh Carter's Junk Away / EP04 package-production task from the current Topic policies.",
            "Do not reuse stale task snapshots such as t_7987e610.",
            "Keep Page Hero, Audio Brief, and Facebook Page copy as separate deliverable lanes.",
        ],
        **({"evidence_error": evidence_error} if evidence_error else {}),
    }


def _ai_bizweek_carter_source_materials(
    policies: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    page_hero = next(
        (
            policy
            for policy in policies
            if policy.get("policy_id") == "ai-bizweek-page-hero"
        ),
        None,
    )
    if page_hero is None:
        return None
    if (
        str(page_hero.get("version") or "") != _CARTER_PAGE_HERO_VERSION
        or str(page_hero.get("sha256") or "") != _CARTER_PAGE_HERO_SHA256
    ):
        return None

    try:
        from hermes_constants import get_default_hermes_root

        conn = sqlite3.connect(get_default_hermes_root() / "state.db")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT id, session_id, role, content, timestamp
                  FROM messages
                 WHERE id = ?
                   AND session_id = ?
                   AND role = 'user'
                """,
                (_CARTER_SOURCE_MESSAGE_ID, _CARTER_SOURCE_SESSION_ID),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {
            "kind": "ai_bizweek_carter_ep04_source_material",
            "available": False,
            "source": "hermes_state_db",
            "error": f"{type(exc).__name__}: {exc}",
            "guidance": [
                "Do not ask KJ to repost until this source lookup has been repaired or explicitly ruled unavailable.",
            ],
        }

    if row is None:
        return {
            "kind": "ai_bizweek_carter_ep04_source_material",
            "available": False,
            "source": "hermes_state_db",
            "expected_message": {
                "session_id": _CARTER_SOURCE_SESSION_ID,
                "message_id": _CARTER_SOURCE_MESSAGE_ID,
            },
            "guidance": [
                "Search accepted Carter EP04 package attachments before asking KJ to repost.",
            ],
        }

    raw_text = str(row["content"] or "")
    source_text = raw_text.removeprefix("[KJ HSU] ").strip()
    final_instruction = "\n\n請提供完整發布包，並嚴格遵守instructions中的所有要求。"
    facebook_page_text = source_text
    if facebook_page_text.endswith(final_instruction):
        facebook_page_text = facebook_page_text[: -len(final_instruction)].rstrip()
    return {
        "kind": "ai_bizweek_carter_ep04_source_material",
        "available": True,
        "source": "hermes_state_db.messages",
        "session_id": str(row["session_id"]),
        "message_id": int(row["id"]),
        "role": str(row["role"]),
        "timestamp": row["timestamp"],
        "raw_message_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "facebook_page_source_sha256": hashlib.sha256(
            facebook_page_text.encode("utf-8")
        ).hexdigest(),
        "facebook_page_source_text": facebook_page_text,
        "raw_message_text": raw_text,
        "canonical_note": (
            "This is the KJ-provided Carter's Junk Away Facebook Page source text "
            "from Topic 4641 session history. Use it as task-scoped source "
            "material for source-vs-output diff; do not ask KJ to repost it."
        ),
        "guidance": [
            "Embed facebook_page_source_text into the production Loop Contract source_materials.",
            "Use raw_message_text only as audit context; the final instruction line is not Page body.",
            "If a newer explicit KJ/Grace discussion overrides this source, record that authorization and diff.",
        ],
    }


def managed_policy_read(*, session_id: str | None) -> str:
    """Read policies bound to the caller's persisted messaging Topic."""
    from hermes_constants import get_default_hermes_root
    from hermes_state import SessionDB
    from proactive.policy_registry import (
        PolicyRegistryError,
        resolve_task_policy_snapshots,
        resolve_topic_policies_for_scope,
    )

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return json.dumps({"success": False, "error": "trusted session_id is required"})

    db = SessionDB(db_path=get_default_hermes_root() / "state.db")
    try:
        session = db.get_session(clean_session_id)
    finally:
        db.close()
    if not session:
        return json.dumps({"success": False, "error": "trusted session was not found"})

    source = str(session.get("source") or "").strip().lower()
    chat_id = str(session.get("chat_id") or "").strip()
    thread_id = str(session.get("thread_id") or "").strip()

    if not chat_id or not thread_id:
        kanban_task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not kanban_task_id:
            return json.dumps(
                {
                    "success": False,
                    "error": "current session is not bound to a Topic or policy-pinned task",
                }
            )
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            task = kb.get_task(conn, kanban_task_id)
        finally:
            conn.close()
        if task is None:
            return json.dumps(
                {"success": False, "error": "trusted Kanban task was not found"}
            )
        try:
            result = resolve_task_policy_snapshots(str(task.body or ""))
        except PolicyRegistryError as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
        from proactive.ai_bizweek_asset_policy import (
            ai_bizweek_asset_guidance,
            ai_bizweek_content_guidance,
        )

        asset_guidance = ai_bizweek_asset_guidance(
            result["policies"],
            task_body=str(task.body or ""),
        )
        content_guidance = ai_bizweek_content_guidance(result["policies"])
        scope = {"kind": "kanban_task", "task_id": kanban_task_id}
        readiness = _ai_bizweek_operational_readiness(result["policies"], scope)
        source_materials = _ai_bizweek_carter_source_materials(result["policies"])
        review_policy_receipts = [
            {
                "role": "review",
                "policy_id": policy["policy_id"],
                "version": policy["version"],
                "sha256": policy["sha256"],
                "loaded": True,
                **(
                    {"latest_active_verified": True}
                    if policy.get("resolution") == "latest_active"
                    else {}
                ),
            }
            for policy in result["policies"]
        ]
        return json.dumps(
            {
                "success": True,
                "scope": scope,
                "review_policy_receipts": review_policy_receipts,
                **(
                    {"asset_policy_guidance": asset_guidance}
                    if asset_guidance
                    else {}
                ),
                **(
                    {"content_policy_guidance": content_guidance}
                    if content_guidance
                    else {}
                ),
                **(
                    {"operational_readiness_evidence": readiness}
                    if readiness
                    else {}
                ),
                **(
                    {"content_source_evidence": source_materials}
                    if source_materials
                    else {}
                ),
                **result,
            },
            ensure_ascii=False,
        )

    try:
        result = resolve_topic_policies_for_scope(source, chat_id, thread_id)
    except PolicyRegistryError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    from proactive.ai_bizweek_asset_policy import (
        ai_bizweek_asset_guidance,
        ai_bizweek_content_guidance,
    )

    asset_guidance = ai_bizweek_asset_guidance(result["policies"])
    content_guidance = ai_bizweek_content_guidance(result["policies"])
    scope = {
        "platform": source,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }
    readiness = _ai_bizweek_operational_readiness(result["policies"], scope)
    source_materials = _ai_bizweek_carter_source_materials(result["policies"])
    return json.dumps(
        {
            "success": True,
            "scope": scope,
            **({"asset_policy_guidance": asset_guidance} if asset_guidance else {}),
            **(
                {"content_policy_guidance": content_guidance}
                if content_guidance
                else {}
            ),
            **(
                {"operational_readiness_evidence": readiness}
                if readiness
                else {}
            ),
            **(
                {"content_source_evidence": source_materials}
                if source_materials
                else {}
            ),
            **result,
        },
        ensure_ascii=False,
    )


registry.register(
    name="managed_policy_read",
    toolset="managed_policy",
    schema={
        "name": "managed_policy_read",
        "description": (
            "Read the complete, current, hash-verified managed policies bound to "
            "this exact messaging Topic. Always use this before answering questions "
            "about formal instructions, brand/channel rules, active policy versions, "
            "or policy SHA values, and before compiling policy-governed work. Topic "
            "Memory or Mem0 summaries are not substitutes. This tool is read-only and "
            "accepts no namespace; scope comes from the trusted current messaging "
            "session or the current policy-pinned Kanban task. In Grace review tasks, "
            "use this tool to verify the task's pinned snapshot and copy its exact "
            "review_policy_receipts into kanban_complete metadata.policy_receipts. "
            "For AI BizWeek image work, also use asset_policy_guidance to compile only "
            "the requested asset family and to reject mixed Page Hero / Audio Brief layouts. "
            "For AI BizWeek Facebook Page copy, also use content_policy_guidance to "
            "preserve KJ-provided Page text in full unless explicit KJ/Grace discussion "
            "authorized specific edits. For Carter's Junk Away / EP04 readiness, if "
            "operational_readiness_evidence.complete=true, treat it as the canonical "
            "readback and do not ask KJ to provide t_70bf2afe evidence or reuse obsolete "
            "stale-review tasks. For Carter's Junk Away / EP04 Page source text, use "
            "content_source_evidence.facebook_page_source_text when available and do not "
            "ask KJ to repost the same full text."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=lambda args, **kw: managed_policy_read(session_id=kw.get("session_id")),
    max_result_size_chars=120_000,
)
