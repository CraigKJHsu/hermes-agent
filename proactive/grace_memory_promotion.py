"""Durable, layered memory promotion for accepted Grace Kanban reviews.

The Kanban database owns the promotion outbox.  This module processes claimed
rows by writing the auditable Topic Markdown source first, then synchronizing
Mem0 and the bounded built-in prompt memory.  A failure in either optional
target is returned as a retryable pending target; the accepted task itself is
never rolled back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home
from tools.memory_tool import ENTRY_DELIMITER, load_on_disk_store
from utils import atomic_replace


DEFAULT_WARN_USAGE_RATIO = 0.75
DEFAULT_CRITICAL_USAGE_RATIO = 0.90
DEFAULT_RETRY_SECONDS = 1800
DEFAULT_MAX_PROMPT_CHARS = 400
DEFAULT_WARN_PROMPT_CHARS = 250
DEFAULT_CRITICAL_MERGE_CHARS = 160


def _promotion_config() -> dict[str, Any]:
    values: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config

        memory_cfg = (load_config() or {}).get("memory", {}) or {}
        values = dict(memory_cfg.get("promotion", {}) or {})
    except Exception:
        pass
    return {
        "warn_usage_ratio": float(
            values.get("warn_usage_ratio", DEFAULT_WARN_USAGE_RATIO)
        ),
        "critical_usage_ratio": float(
            values.get("critical_usage_ratio", DEFAULT_CRITICAL_USAGE_RATIO)
        ),
        "retry_seconds": max(
            60, int(values.get("retry_seconds", DEFAULT_RETRY_SECONDS))
        ),
        "max_prompt_chars": max(
            80, int(values.get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))
        ),
        "warn_prompt_chars": max(
            80, int(values.get("warn_prompt_chars", DEFAULT_WARN_PROMPT_CHARS))
        ),
        "critical_merge_chars": max(
            80,
            int(
                values.get(
                    "critical_merge_chars", DEFAULT_CRITICAL_MERGE_CHARS
                )
            ),
        ),
    }


def prompt_memory_capacity() -> dict[str, Any]:
    """Return live prompt-memory usage and its warning level."""
    store = load_on_disk_store()
    entries = list(store.memory_entries)
    current = len(ENTRY_DELIMITER.join(entries)) if entries else 0
    limit = int(store.memory_char_limit)
    ratio = current / limit if limit > 0 else 1.0
    cfg = _promotion_config()
    if ratio >= cfg["critical_usage_ratio"]:
        level = "critical"
    elif ratio >= cfg["warn_usage_ratio"]:
        level = "warning"
    else:
        level = "ok"
    return {
        "current": current,
        "limit": limit,
        "remaining": max(0, limit - current),
        "ratio": ratio,
        "level": level,
        "entry_count": len(entries),
    }


def _topic_key(namespace: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", namespace).strip("-")
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:72] or 'topic'}-{digest}"


def _topic_paths(namespace: str) -> tuple[Path, Path]:
    root = get_hermes_home() / "memory" / "topics"
    key = _topic_key(namespace)
    return root / f"{key}.md", root / ".state" / f"{key}.json"


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_topic_state(path: Path, namespace: str) -> dict[str, Any]:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("namespace") == namespace:
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {"namespace": namespace, "entries": [], "sources": []}


def _archive_topic_unlocked(
    namespace: str,
    promoted_entries: list[str],
    *,
    review_task_id: str,
    promotion_id: str,
) -> dict[str, Any]:
    """Merge exact promoted facts into a Topic Markdown source and verify it."""
    markdown_path, state_path = _topic_paths(namespace)
    state = _load_topic_state(state_path, namespace)
    entries = [str(item).strip() for item in state.get("entries", []) if str(item).strip()]
    for item in promoted_entries:
        if item not in entries:
            entries.append(item)
    sources = [item for item in state.get("sources", []) if isinstance(item, dict)]
    source = {
        "promotion_id": promotion_id,
        "review_task_id": review_task_id,
        "promoted_at": int(time.time()),
    }
    if not any(item.get("promotion_id") == promotion_id for item in sources):
        sources.append(source)
    state = {
        "namespace": namespace,
        "entries": entries,
        "sources": sources,
        "updated_at": int(time.time()),
    }
    from utils import atomic_json_write

    atomic_json_write(state_path, state, mode=0o600)
    lines = [
        "# Grace Topic Memory",
        "",
        f"- Namespace: `{namespace}`",
        f"- Updated: {state['updated_at']}",
        "- Source: accepted Grace Kanban review promotions",
        "",
        "## Durable preferences and facts",
        "",
    ]
    lines.extend(f"- {item}" for item in entries)
    lines.append("")
    body = "\n".join(lines)
    _atomic_text_write(markdown_path, body)
    verified = markdown_path.read_text(encoding="utf-8") == body
    if not verified:
        raise RuntimeError("Topic Markdown readback did not match the atomic write")
    return {
        "archived": True,
        "archive_verified": True,
        "archive_path": str(markdown_path),
        "entry_count": len(entries),
        "topic_text": f"{namespace}：{'；'.join(entries)}",
    }


def _archive_topic(
    namespace: str,
    promoted_entries: list[str],
    *,
    review_task_id: str,
    promotion_id: str,
) -> dict[str, Any]:
    """Serialize the complete Topic read/merge/write across processes."""
    import fcntl

    _markdown_path, state_path = _topic_paths(namespace)
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return _archive_topic_unlocked(
                namespace,
                promoted_entries,
                review_task_id=review_task_id,
                promotion_id=promotion_id,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _split_clauses(text: str) -> list[str]:
    return [part.strip(" ；。") for part in re.split(r"[；\n]+", text) if part.strip(" ；。")]


def _bounded_topic_entry(
    namespace: str,
    promoted_entries: list[str],
    existing: Optional[str],
    max_chars: int,
) -> str:
    prefix = f"{namespace}："
    clauses: list[str] = []
    for item in promoted_entries:
        for clause in _split_clauses(item):
            if clause not in clauses:
                clauses.append(clause)
    if existing:
        existing_body = existing[len(prefix):] if existing.startswith(prefix) else existing
        for clause in _split_clauses(existing_body):
            if clause not in clauses:
                clauses.append(clause)
    value = prefix + "；".join(clauses)
    if len(value) <= max_chars:
        return value
    if max_chars <= len(prefix) + 3:
        return prefix[:max_chars]
    return value[: max_chars - 3].rstrip(" ；。") + "..."


def _sync_prompt_memory(namespace: str, promoted_entries: list[str]) -> dict[str, Any]:
    store = load_on_disk_store()
    entries = list(store.memory_entries)
    current = len(ENTRY_DELIMITER.join(entries)) if entries else 0
    limit = int(store.memory_char_limit)
    ratio = current / limit if limit > 0 else 1.0
    cfg = _promotion_config()
    prefix = f"{namespace}："
    matching = [entry for entry in entries if entry.startswith(prefix)]
    existing = matching[0] if len(matching) == 1 else None
    if len(matching) > 1:
        # Legacy Prompt Memory allowed one fact per entry, so the same Topic
        # namespace can appear many times.  Treat the whole namespace group as
        # the replaceable unit; unrelated entries remain outside the batch.
        existing = "；".join(
            entry[len(prefix):] if entry.startswith(prefix) else entry
            for entry in matching
        )
    if ratio >= cfg["critical_usage_ratio"] and existing is None:
        return {
            "prompt_synced": False,
            "prompt_deferred": True,
            "prompt_error": "critical capacity and no same-namespace entry to merge",
            "capacity": prompt_memory_capacity(),
        }
    if ratio >= cfg["critical_usage_ratio"]:
        target_chars = cfg["critical_merge_chars"]
    elif ratio >= cfg["warn_usage_ratio"]:
        target_chars = cfg["warn_prompt_chars"]
    else:
        target_chars = cfg["max_prompt_chars"]
    if existing is None:
        delimiter_chars = len(ENTRY_DELIMITER) if entries else 0
        target_chars = min(target_chars, limit - current - delimiter_chars)
        if target_chars < len(prefix) + 12:
            return {
                "prompt_synced": False,
                "prompt_deferred": True,
                "prompt_error": "insufficient capacity for a meaningful namespace entry",
                "capacity": prompt_memory_capacity(),
            }
    else:
        # A same-namespace merge must fit without evicting unrelated entries.
        # At critical capacity the configured merge size can be larger than
        # the entry being replaced, so cap it to the exact remaining budget.
        # Delimiters between the matching entries disappear when the legacy
        # group is collapsed into one entry.  Let the atomic batch enforce the
        # exact final budget; this conservative bound only prevents expansion.
        matching_chars = sum(len(entry) for entry in matching)
        matching_delimiters = len(ENTRY_DELIMITER) * max(0, len(matching) - 1)
        unrelated_chars = current - matching_chars - matching_delimiters
        target_chars = min(target_chars, max(0, limit - unrelated_chars))
        if target_chars < len(prefix) + 12:
            return {
                "prompt_synced": False,
                "prompt_deferred": True,
                "prompt_error": "insufficient capacity for a meaningful same-namespace merge",
                "capacity": prompt_memory_capacity(),
            }
    candidate = _bounded_topic_entry(
        namespace, promoted_entries, existing, target_chars
    )
    if not matching:
        write_result = store.add("memory", candidate)
    elif len(matching) == 1:
        write_result = store.replace("memory", prefix, candidate)
    else:
        operations = [
            {"action": "replace", "old_text": matching[0], "content": candidate},
            *(
                {"action": "remove", "old_text": entry}
                for entry in matching[1:]
            ),
        ]
        write_result = store.apply_batch("memory", operations)
    if not write_result.get("success"):
        return {
            "prompt_synced": False,
            "prompt_error": str(write_result.get("error") or "prompt write failed"),
            "capacity": prompt_memory_capacity(),
        }
    readback = load_on_disk_store()
    matching_readback = [
        entry for entry in readback.memory_entries if entry.startswith(prefix)
    ]
    verified = matching_readback == [candidate]
    return {
        "prompt_synced": verified,
        "prompt_verified": verified,
        "prompt_entry": candidate if verified else None,
        "prompt_error": None if verified else "prompt readback mismatch",
        "capacity": prompt_memory_capacity(),
    }


def _load_env_value(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    env_path = get_hermes_home() / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip().strip('"').strip("'")
    return ""


def _mem0_request(
    method: str,
    url: str,
    *,
    api_key: str,
    timeout: float,
    body: Optional[dict[str, Any]] = None,
) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8")) if payload else {}


def _unwrap_mem0_results(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("results", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _mem0_text(item: Mapping[str, Any]) -> str:
    return str(item.get("memory") or item.get("text") or item.get("content") or "")


def _sync_mem0(namespace: str, topic_text: str, archive_path: str) -> dict[str, Any]:
    config_path = get_hermes_home() / "mem0.json"
    if not config_path.exists():
        return {"mem0_synced": True, "mem0_required": False, "mem0_status": "not_configured"}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "mem0_synced": False,
            "mem0_required": True,
            "mem0_error": f"invalid Mem0 config: {exc}",
        }
    if config.get("mode") != "rest":
        return {
            "mem0_synced": False,
            "mem0_required": True,
            "mem0_error": f"unsupported Mem0 mode: {config.get('mode')!r}",
        }
    try:
        base_url = str(config["base_url"]).rstrip("/")
        memories_path = "/" + str(config.get("memories_path", "/memories")).strip("/")
        timeout = float(config.get("timeout", 10.0))
        api_key = _load_env_value("MEM0_API_KEY")
        user_id = str(config.get("user_id") or "kj")
        agent_id = str(config.get("agent_id") or "hermes-grace")
        namespace_digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:24]
        run_id = f"grace-topic:{namespace_digest}"
        query = urllib.parse.urlencode({"run_id": run_id, "top_k": 10})
        existing = _unwrap_mem0_results(
            _mem0_request(
                "GET",
                f"{base_url}{memories_path}?{query}",
                api_key=api_key,
                timeout=timeout,
            )
        )
        metadata = {
            "type": "grace_topic_memory",
            "source": "grace-kanban-acceptance",
            "namespace": namespace,
            "archive_path": archive_path,
        }
        if existing:
            memory_id = str(existing[0]["id"])
            escaped_id = urllib.parse.quote(memory_id, safe="")
            _mem0_request(
                "PUT",
                f"{base_url}{memories_path}/{escaped_id}",
                api_key=api_key,
                timeout=timeout,
                body={"text": topic_text, "metadata": metadata},
            )
            action = "updated"
        else:
            created = _mem0_request(
                "POST",
                f"{base_url}{memories_path}",
                api_key=api_key,
                timeout=timeout,
                body={
                    "messages": [{"role": "assistant", "content": topic_text}],
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "infer": False,
                    "metadata": metadata,
                },
            )
            created_rows = _unwrap_mem0_results(created)
            memory_id = str(created_rows[0].get("id") or "") if created_rows else ""
            action = "created"
        readback = _unwrap_mem0_results(
            _mem0_request(
                "GET",
                f"{base_url}{memories_path}?{query}",
                api_key=api_key,
                timeout=timeout,
            )
        )
        verified = any(_mem0_text(item) == topic_text for item in readback)
        return {
            "mem0_synced": verified,
            "mem0_required": True,
            "mem0_verified": verified,
            "mem0_action": action,
            "mem0_run_id": run_id,
            "mem0_memory_id": memory_id,
            "mem0_duplicate_count": max(0, len(readback) - 1),
            "mem0_error": None if verified else "Mem0 readback mismatch",
        }
    except (
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        socket.timeout,
        urllib.error.URLError,
    ) as exc:
        return {
            "mem0_synced": False,
            "mem0_required": True,
            "mem0_error": f"{type(exc).__name__}: {exc}",
        }


def promote_claimed_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Process one claimed outbox row and return its durable target status."""
    namespace = str(record.get("namespace") or "").strip()
    entries_value = record.get("entries")
    if isinstance(entries_value, str):
        entries_value = json.loads(entries_value)
    promoted_entries = [
        str(item).strip()
        for item in (entries_value or [])
        if str(item).strip()
    ]
    if not namespace or not promoted_entries:
        raise ValueError("promotion record requires namespace and non-empty entries")
    archive = _archive_topic(
        namespace,
        promoted_entries,
        review_task_id=str(record.get("review_task_id") or ""),
        promotion_id=str(record.get("id") or ""),
    )
    mem0 = _sync_mem0(namespace, archive["topic_text"], archive["archive_path"])
    prompt = _sync_prompt_memory(namespace, promoted_entries)
    pending_targets: list[str] = []
    if not mem0.get("mem0_synced"):
        pending_targets.append("mem0")
    if not prompt.get("prompt_synced"):
        pending_targets.append("prompt_memory")
    return {
        **archive,
        **mem0,
        **prompt,
        "pending_targets": pending_targets,
        "complete": not pending_targets,
        "processed_at": int(time.time()),
    }


def process_due_promotions(
    *,
    board: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Claim and process due outbox rows for one Kanban board."""
    from hermes_cli import kanban_db as kb

    owner = f"{os.getpid()}:{time.time_ns()}"
    results: list[dict[str, Any]] = []
    with kb.connect_closing(board=board) as conn:
        claimed = kb.claim_due_grace_memory_promotions(
            conn,
            task_id=task_id,
            limit=limit,
            lease_owner=owner,
        )
    for record in claimed:
        try:
            result = promote_claimed_record(record)
            error = None
        except Exception as exc:
            result = {
                "complete": False,
                "pending_targets": ["topic_archive", "mem0", "prompt_memory"],
                "processed_at": int(time.time()),
            }
            error = f"{type(exc).__name__}: {exc}"
        with kb.connect_closing(board=board) as conn:
            kb.finish_grace_memory_promotion(
                conn,
                str(record["id"]),
                lease_owner=owner,
                result=result,
                error=error,
                retry_seconds=_promotion_config()["retry_seconds"],
            )
        results.append({"promotion_id": record["id"], **result, "error": error})
    return results


def process_due_promotions_all_boards(*, limit_per_board: int = 10) -> list[dict[str, Any]]:
    """Retry pending promotions across every locally discovered Kanban board."""
    from hermes_cli import kanban_db as kb

    results: list[dict[str, Any]] = []
    for board_meta in kb.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or board_meta.get("id") or "default")
        results.extend(
            process_due_promotions(board=board, limit=limit_per_board)
        )
    return results
