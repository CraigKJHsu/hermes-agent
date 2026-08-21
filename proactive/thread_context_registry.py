"""Conversation-lane project registry used by delegated execution.

Every authenticated Telegram Topic owns an implicit lightweight main project.
The project is materialized lazily and deterministically the first time the
gateway sees the Topic, so Grace never needs a special user phrase before it
can keep related work together.  Explicit registry entries and subprojects
remain available for durable names, routing and memory isolation.
"""

from __future__ import annotations

import os
import re
import tempfile
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home


class ThreadContextError(ValueError):
    """Raised when a conversation lane has no safe project binding."""


def registry_path() -> Path:
    override = os.getenv("HERMES_THREAD_CONTEXT_REGISTRY", "").strip()
    return (
        Path(override).expanduser()
        if override
        else get_hermes_home() / "thread_context_registry.yaml"
    )


def load_thread_context_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"version": 1, "contexts": []}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise ThreadContextError(f"invalid thread context registry: {path}")
    return dict(loaded)


def _normalized_lane(*, platform: str, chat_id: str, thread_id: str) -> tuple[str, str, str]:
    platform = str(platform or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    thread_id = str(thread_id or "").strip()
    return platform, chat_id, thread_id


def _matching_contexts(
    registry: Mapping[str, Any],
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in registry.get("contexts", []):
        if not isinstance(item, Mapping):
            continue
        if (
            str(item.get("platform") or "").strip().lower() == platform
            and str(item.get("chat_id") or "").strip() == chat_id
            and str(item.get("thread_id") or "").strip() == thread_id
        ):
            matches.append(dict(item))
    return matches


def _safe_project_component(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return normalized or fallback


def _lightweight_context(*, platform: str, chat_id: str, thread_id: str) -> dict[str, Any]:
    chat_component = _safe_project_component(chat_id.lstrip("-"), fallback="chat")
    thread_component = _safe_project_component(thread_id, fallback="topic")
    lane_digest = sha256(
        f"{platform}\0{chat_id}\0{thread_id}".encode("utf-8")
    ).hexdigest()[:12]
    project = f"{platform}_{chat_component}_{thread_component}_{lane_digest}"
    topic_name = f"Topic {thread_id}"
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "topic_name": topic_name,
        "project": project,
        "project_name": topic_name,
        "project_kind": "lightweight_main",
        "auto_created": True,
        "memory_namespace": f"{platform}:{chat_id}:{thread_id}/{project}",
        "aliases": [topic_name, project],
        "subprojects": [],
    }


@contextmanager
def _registry_write_lock(path: Path):
    """Serialize registry materialization across gateway/cron processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            fcntl = None  # type: ignore[assignment]
        try:
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _write_registry(path: Path, registry: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                dict(registry),
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary_name, path.stat().st_mode)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def ensure_thread_context(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Return an exact lane, materializing a lightweight Telegram main project.

    This deliberately does not create a filesystem workspace, Kanban card or
    subproject.  It only establishes the stable Topic identity and isolated
    memory namespace that later work can use.
    """
    platform, chat_id, thread_id = _normalized_lane(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if platform != "telegram" or not chat_id or not thread_id or thread_id == "general":
        raise ThreadContextError(
            f"unregistered conversation lane: {platform}/{chat_id}/thread/{thread_id}"
        )

    path = registry_path()
    with _registry_write_lock(path):
        registry = load_thread_context_registry()
        matches = _matching_contexts(
            registry,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if len(matches) > 1:
            raise ThreadContextError(
                "conversation lane has duplicate project registry entries"
            )
        if matches:
            result = matches[0]
            if not str(result.get("project") or "").strip():
                raise ThreadContextError("registered lane is missing project")
            return result

        result = _lightweight_context(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        contexts = registry.get("contexts")
        if not isinstance(contexts, list):
            contexts = []
        registry["version"] = int(registry.get("version") or 1)
        registry["contexts"] = [*contexts, result]
        _write_registry(path, registry)
        return result


def _normalized_match_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_generated_topic_placeholder(topic_name: object, thread_id: object) -> bool:
    """Return true only for the exact label created by materialization."""
    expected = f"Topic {str(thread_id or '').strip()}"
    return str(topic_name or "") == expected


def _project_aliases(project: Mapping[str, Any]) -> set[str]:
    values = {
        str(project.get("project") or ""),
        str(project.get("id") or ""),
        str(project.get("name") or ""),
        str(project.get("project_name") or ""),
        *[str(value) for value in list(project.get("aliases") or [])],
    }
    return {
        normalized
        for value in values
        if (normalized := _normalized_match_text(value))
    }


def _hint_explicitly_names_alias(hint: str, alias: str) -> bool:
    """Match ASCII identifiers on token boundaries and CJK names literally."""
    if not re.search(r"[\u3400-\u9fff]", alias):
        return bool(
            re.search(
                rf"(?<![a-z0-9_.-]){re.escape(alias)}(?![a-z0-9_.-])",
                hint,
            )
        )
    return len(alias) >= 2 and alias in hint


def _select_project_context(context: Mapping[str, Any], work_hint: str) -> dict[str, Any]:
    """Select a clearly named subproject or retain the lightweight main project.

    Grace only needs a user decision when two or more active subprojects exist
    and the work does not identify exactly one of them.  A single child does
    not steal unrelated work from the Topic's main project.
    """
    result = dict(context)
    subprojects = [
        dict(item)
        for item in list(context.get("subprojects") or [])
        if isinstance(item, Mapping)
        and str(item.get("status") or "active").strip().lower()
        not in {"archived", "closed"}
    ]
    if not subprojects:
        return result

    hint = _normalized_match_text(work_hint)
    matched = []
    if hint:
        for project in subprojects:
            if any(
                _hint_explicitly_names_alias(hint, alias)
                for alias in _project_aliases(project)
            ):
                matched.append(project)
    if len(matched) == 1:
        selected = matched[0]
        selected_id = str(selected.get("project") or selected.get("id") or "").strip()
        if not selected_id:
            raise ThreadContextError("registered subproject is missing project id")
        result["project"] = selected_id
        result["project_name"] = str(selected.get("name") or selected_id)
        result["memory_namespace"] = str(
            selected.get("memory_namespace")
            or f"{context.get('memory_namespace', '')}/subproject/{selected_id}"
        )
        result["selected_subproject"] = selected_id
        return result
    if len(subprojects) >= 2:
        names = [
            str(item.get("name") or item.get("project") or item.get("id") or "未命名")
            for item in subprojects
        ]
        raise ThreadContextError(
            "ambiguous project placement in this Topic; active subprojects: "
            + ", ".join(names)
        )
    return result


def resolve_thread_context(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    auto_create: bool = False,
    work_hint: str = "",
) -> dict[str, Any]:
    """Resolve an exact lane without falling back to another Topic/project."""
    platform, chat_id, thread_id = _normalized_lane(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    matches = _matching_contexts(
        load_thread_context_registry(),
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if len(matches) > 1:
        raise ThreadContextError(
            "conversation lane has duplicate project registry entries"
        )
    if matches:
        result = matches[0]
        if not str(result.get("project") or "").strip():
            raise ThreadContextError("registered lane is missing project")
        return _select_project_context(result, work_hint)
    if auto_create:
        return _select_project_context(
            ensure_thread_context(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
            work_hint,
        )
    raise ThreadContextError(
        f"unregistered conversation lane: {platform}/{chat_id}/thread/{thread_id}; "
        "automatic lightweight project creation is unavailable for this lane"
    )


def update_thread_context_topic_name(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    topic_name: str,
) -> dict[str, Any]:
    """Synchronize a successful Topic rename with its main project label."""
    clean_name = str(topic_name or "").strip()
    if not clean_name:
        raise ThreadContextError("topic_name is required")
    context = ensure_thread_context(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    path = registry_path()
    with _registry_write_lock(path):
        registry = load_thread_context_registry()
        changed = None
        for item in registry.get("contexts", []):
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("platform") or "").strip().lower() == str(platform).strip().lower()
                and str(item.get("chat_id") or "").strip() == str(chat_id).strip()
                and str(item.get("thread_id") or "").strip() == str(thread_id).strip()
            ):
                item["topic_name"] = clean_name
                if item.get("auto_created") or item.get("project_kind") == "lightweight_main":
                    item["project_name"] = clean_name
                aliases = [str(value) for value in list(item.get("aliases") or [])]
                if clean_name not in aliases:
                    aliases.append(clean_name)
                item["aliases"] = aliases
                changed = dict(item)
                break
        if changed is None:
            return context
        _write_registry(path, registry)
        return changed


def seed_thread_context_topic_name(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    topic_name: str,
) -> dict[str, Any]:
    """Atomically fill a placeholder without overwriting an authoritative rename."""
    platform, chat_id, thread_id = _normalized_lane(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    clean_name = str(topic_name or "").strip()
    if platform != "telegram" or not chat_id or not thread_id or thread_id == "general":
        raise ThreadContextError(
            f"unregistered conversation lane: {platform}/{chat_id}/thread/{thread_id}"
        )

    path = registry_path()
    with _registry_write_lock(path):
        registry = load_thread_context_registry()
        matches = _matching_contexts(
            registry,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if len(matches) > 1:
            raise ThreadContextError(
                "conversation lane has duplicate project registry entries"
            )

        contexts = registry.get("contexts")
        if not isinstance(contexts, list):
            contexts = []
        registry_changed = False
        if not matches:
            created = _lightweight_context(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            contexts.append(created)
            registry["version"] = int(registry.get("version") or 1)
            registry["contexts"] = contexts
            registry_changed = True

        changed = None
        for item in contexts:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("platform") or "").strip().lower() == platform
                and str(item.get("chat_id") or "").strip() == chat_id
                and str(item.get("thread_id") or "").strip() == thread_id
            ):
                current_name = str(item.get("topic_name") or "").strip()
                if clean_name and (
                    not current_name
                    or is_generated_topic_placeholder(current_name, thread_id)
                ):
                    item["topic_name"] = clean_name
                    if item.get("auto_created") or item.get("project_kind") == "lightweight_main":
                        item["project_name"] = clean_name
                    aliases = [str(value) for value in list(item.get("aliases") or [])]
                    if clean_name not in aliases:
                        aliases.append(clean_name)
                    item["aliases"] = aliases
                    registry_changed = True
                changed = dict(item)
                break
        if changed is None:
            raise ThreadContextError("registered lane is missing project")
        if registry_changed:
            _write_registry(path, registry)
        return changed


def resolve_thread_context_alias(alias: str) -> dict[str, Any]:
    """Resolve an explicit scheduler alias without guessing a conversation lane."""
    alias = str(alias or "").strip()
    if not alias:
        raise ThreadContextError("scheduled delegation is missing context_alias")
    matches = []
    for item in load_thread_context_registry().get("contexts", []):
        if not isinstance(item, Mapping):
            continue
        aliases = item.get("aliases") or []
        if alias == str(item.get("project") or "").strip() or alias in {
            str(value).strip() for value in aliases
        }:
            matches.append(dict(item))
    if len(matches) != 1:
        raise ThreadContextError(
            f"context_alias must resolve to exactly one registered lane: {alias}"
        )
    return matches[0]


def assert_contract_matches_context(contract: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    identity = contract.get("identity") if isinstance(contract, Mapping) else None
    identity = identity if isinstance(identity, Mapping) else {}
    for key in ("project", "topic_name", "thread_id"):
        if str(identity.get(key) or "").strip() != str(context.get(key) or "").strip():
            raise ThreadContextError(
                f"contract {key} does not match registered Topic context"
            )
