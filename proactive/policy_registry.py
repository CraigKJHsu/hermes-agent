"""Versioned policy registry and Topic bindings for Grace Loop Contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_default_hermes_root
from utils import atomic_json_write, atomic_replace

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


POLICY_MARKER = "GRACE_POLICY_SNAPSHOT:"
_POLICY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,127}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_POLICY_LOCK_TIMEOUT_SECONDS = 10.0
_policy_thread_lock = threading.RLock()
_UNSET = object()


class PolicyRegistryError(ValueError):
    """Raised when policy state is missing, conflicting, or stale."""


def _registry_root() -> Path:
    return get_default_hermes_root() / "policies"


def _validate_policy_id(policy_id: object) -> str:
    value = str(policy_id or "").strip().lower()
    if not _POLICY_ID_RE.fullmatch(value):
        raise PolicyRegistryError(
            "policy_id must be 3..128 lowercase letters, numbers, dots, dashes, or underscores"
        )
    return value


def _validate_version(version: object) -> str:
    value = str(version or "").strip()
    if not _VERSION_RE.fullmatch(value):
        raise PolicyRegistryError("policy version is invalid")
    return value


def _policy_dir(policy_id: str) -> Path:
    return _registry_root() / "registry" / _validate_policy_id(policy_id)


def _manifest_path(policy_id: str) -> Path:
    return _policy_dir(policy_id) / "manifest.json"


def _version_path(policy_id: str, version: str) -> Path:
    return _policy_dir(policy_id) / "versions" / f"{_validate_version(version)}.md"


def _topic_key(namespace: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", namespace).strip("-")
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:72] or 'topic'}-{digest}"


def _binding_path(namespace: str) -> Path:
    return _registry_root() / "topic-bindings" / f"{_topic_key(namespace)}.json"


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@contextmanager
def _registry_lock():
    """Serialize manifest read-check-write operations across threads and processes."""
    lock_path = _registry_root() / ".registry.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _policy_thread_lock:
        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")
        with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as handle:
            if fcntl is None and msvcrt is None:
                yield
                return
            deadline = time.monotonic() + _POLICY_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    if fcntl:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError, PermissionError):
                    if time.monotonic() >= deadline:
                        raise PolicyRegistryError("timed out waiting for policy registry lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def serialize_with_policy_registry(func):
    """Serialize a completion transition with policy and binding updates."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _registry_lock():
            return func(*args, **kwargs)

    return wrapped


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
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


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    value, _ = _load_json_snapshot(path, label=label)
    return value


def _load_json_snapshot(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise PolicyRegistryError(f"{label} not found: {path}") from exc
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyRegistryError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyRegistryError(f"invalid {label}: expected object")
    return value, hashlib.sha256(raw).hexdigest()


def create_policy_version(
    policy_id: str,
    version: str,
    content: str,
    *,
    owner_scope: str,
    owner_id: str,
    supersedes: str | None = None,
    activate: bool = False,
    expected_active_version: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Create one immutable policy version and optionally activate it."""
    with _registry_lock():
        return _create_policy_version_unlocked(
            policy_id,
            version,
            content,
            owner_scope=owner_scope,
            owner_id=owner_id,
            supersedes=supersedes,
            activate=activate,
            expected_active_version=expected_active_version,
        )


def _create_policy_version_unlocked(
    policy_id: str,
    version: str,
    content: str,
    *,
    owner_scope: str,
    owner_id: str,
    supersedes: str | None,
    activate: bool,
    expected_active_version: str | None | object,
) -> dict[str, Any]:
    clean_id = _validate_policy_id(policy_id)
    clean_version = _validate_version(version)
    clean_content = str(content or "")
    if not clean_content.strip():
        raise PolicyRegistryError("policy content must not be empty")
    if str(owner_scope or "").strip() not in {"global", "brand", "channel", "topic"}:
        raise PolicyRegistryError("owner_scope must be global, brand, channel, or topic")
    if not str(owner_id or "").strip():
        raise PolicyRegistryError("owner_id is required")

    manifest_path = _manifest_path(clean_id)
    if manifest_path.exists():
        manifest = _load_json(manifest_path, label="policy manifest")
        if manifest.get("policy_id") != clean_id:
            raise PolicyRegistryError("policy manifest identity mismatch")
        if manifest.get("owner_scope") != owner_scope or manifest.get("owner_id") != owner_id:
            raise PolicyRegistryError("policy ownership is immutable")
    else:
        manifest = {
            "policy_id": clean_id,
            "owner_scope": owner_scope,
            "owner_id": owner_id,
            "active_version": None,
            "versions": [],
            "updated_at": 0,
        }

    active_version = manifest.get("active_version")
    if expected_active_version is not _UNSET and active_version != expected_active_version:
        raise PolicyRegistryError(
            f"active version changed: expected {expected_active_version!r}, found {active_version!r}"
        )
    if supersedes is not None:
        supersedes = _validate_version(supersedes)
        if supersedes != active_version:
            raise PolicyRegistryError("supersedes must match the current active version")

    digest = _sha256_text(clean_content)
    version_path = _version_path(clean_id, clean_version)
    if version_path.exists():
        existing = version_path.read_text(encoding="utf-8")
        if existing != clean_content:
            raise PolicyRegistryError("policy versions are immutable")
    else:
        _atomic_text_write(version_path, clean_content)
    if version_path.read_text(encoding="utf-8") != clean_content:
        raise PolicyRegistryError("policy version readback mismatch")

    versions = [item for item in manifest.get("versions", []) if isinstance(item, dict)]
    record = next((item for item in versions if item.get("version") == clean_version), None)
    if record is None:
        record = {
            "version": clean_version,
            "sha256": digest,
            "supersedes": supersedes,
            "created_at": int(time.time()),
            "status": "draft",
        }
        versions.append(record)
    elif record.get("sha256") != digest:
        raise PolicyRegistryError("policy manifest digest mismatch")

    manifest["versions"] = versions
    manifest["updated_at"] = int(time.time())
    atomic_json_write(manifest_path, manifest, mode=0o600)
    if activate:
        return _activate_policy_unlocked(
            clean_id,
            clean_version,
            expected_active_version=expected_active_version,
        )
    return policy_status(clean_id)


def activate_policy(
    policy_id: str,
    version: str,
    *,
    expected_active_version: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Atomically switch a policy manifest to an existing verified version."""
    with _registry_lock():
        return _activate_policy_unlocked(
            policy_id,
            version,
            expected_active_version=expected_active_version,
        )


def _activate_policy_unlocked(
    policy_id: str,
    version: str,
    *,
    expected_active_version: str | None | object,
) -> dict[str, Any]:
    clean_id = _validate_policy_id(policy_id)
    clean_version = _validate_version(version)
    manifest_path = _manifest_path(clean_id)
    manifest = _load_json(manifest_path, label="policy manifest")
    active = manifest.get("active_version")
    if expected_active_version is not _UNSET and active != expected_active_version:
        raise PolicyRegistryError(
            f"active version changed: expected {expected_active_version!r}, found {active!r}"
        )
    versions = [item for item in manifest.get("versions", []) if isinstance(item, dict)]
    selected = next((item for item in versions if item.get("version") == clean_version), None)
    if selected is None:
        raise PolicyRegistryError(f"unknown policy version: {clean_version}")
    content = _version_path(clean_id, clean_version).read_text(encoding="utf-8")
    if _sha256_text(content) != selected.get("sha256"):
        raise PolicyRegistryError("policy version digest mismatch")
    for item in versions:
        item["status"] = "active" if item is selected else "superseded"
    manifest["active_version"] = clean_version
    manifest["versions"] = versions
    manifest["updated_at"] = int(time.time())
    atomic_json_write(manifest_path, manifest, mode=0o600)
    return policy_status(clean_id)


def policy_status(policy_id: str) -> dict[str, Any]:
    clean_id = _validate_policy_id(policy_id)
    manifest_path = _manifest_path(clean_id)
    manifest = _load_json(manifest_path, label="policy manifest")
    active = manifest.get("active_version")
    result = deepcopy(manifest)
    result["manifest_path"] = str(manifest_path)
    if active:
        path = _version_path(clean_id, str(active))
        content = path.read_text(encoding="utf-8")
        record = next(
            (
                item
                for item in manifest.get("versions", [])
                if isinstance(item, dict) and item.get("version") == active
            ),
            None,
        )
        if record is None or record.get("sha256") != _sha256_text(content):
            raise PolicyRegistryError("active policy digest does not match manifest")
        result["version_path"] = str(path)
        result["active_sha256"] = _sha256_text(content)
    return result


def resolve_active_policy(
    policy_id: str,
    *,
    sections: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the verified active policy content and immutable receipt.

    Runtime governance code should depend on a policy id, not on a mutable
    persona or memory file.  This public wrapper preserves the registry's
    digest/readback checks while keeping the lower-level requirement resolver
    private.
    """
    return _resolve_requirement(
        {
            "policy_id": policy_id,
            "resolution": "latest_active",
            "sections": list(sections),
        }
    )


def resolve_policy_snapshot(
    policy_id: str,
    version: str,
    *,
    sections: Sequence[str] = (),
) -> dict[str, Any]:
    """Return one immutable registry version after manifest and digest checks."""
    return _resolve_requirement(
        {
            "policy_id": policy_id,
            "resolution": "pinned_version",
            "version": version,
            "sections": list(sections),
        }
    )


def bind_topic_policies(
    namespace: str,
    requirements: Sequence[Mapping[str, Any]],
    *,
    expected_binding_sha256: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Replace one Topic's policy bindings after validating every target."""
    with _registry_lock():
        clean_namespace = str(namespace or "").strip()
        if not clean_namespace:
            raise PolicyRegistryError("Topic namespace is required")
        normalized = _normalize_requirements(requirements)
        for requirement in normalized:
            _resolve_requirement(requirement)
        path = _binding_path(clean_namespace)
        current_sha256 = _sha256_file(path)
        if expected_binding_sha256 is not _UNSET and current_sha256 != expected_binding_sha256:
            raise PolicyRegistryError(
                "Topic policy binding changed since it was read"
            )
        payload = {
            "namespace": clean_namespace,
            "requirements": normalized,
            "updated_at": int(time.time()),
        }
        atomic_json_write(path, payload, mode=0o600)
        readback = _load_json(path, label="Topic policy binding")
        if readback != payload:
            raise PolicyRegistryError("Topic policy binding readback mismatch")
        return {
            **payload,
            "binding_path": str(path),
            "binding_sha256": _sha256_file(path),
        }


def topic_policy_binding(namespace: str) -> dict[str, Any]:
    path = _binding_path(str(namespace or "").strip())
    if not path.exists():
        return {
            "namespace": str(namespace or "").strip(),
            "requirements": [],
            "binding_path": str(path),
            "binding_sha256": None,
        }
    result, binding_sha256 = _load_json_snapshot(path, label="Topic policy binding")
    if result.get("namespace") != str(namespace or "").strip():
        raise PolicyRegistryError("Topic policy binding namespace mismatch")
    result["binding_path"] = str(path)
    result["binding_sha256"] = binding_sha256
    return result


def topic_policy_binding_for_scope(
    platform: str,
    chat_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Return the unique binding owned by one trusted messaging Topic scope."""
    clean_platform = str(platform or "").strip().lower()
    clean_chat_id = str(chat_id or "").strip()
    clean_thread_id = str(thread_id or "").strip()
    if not clean_platform or not clean_chat_id or not clean_thread_id:
        raise PolicyRegistryError("platform, chat_id, and thread_id are required")

    prefix = f"{clean_platform}:{clean_chat_id}:{clean_thread_id}/"
    bindings_dir = _registry_root() / "topic-bindings"
    matches: list[dict[str, Any]] = []
    for path in sorted(bindings_dir.glob("*.json")):
        value, binding_sha256 = _load_json_snapshot(
            path, label="Topic policy binding"
        )
        namespace = str(value.get("namespace") or "")
        if namespace == prefix[:-1] or namespace.startswith(prefix):
            value["binding_path"] = str(path)
            value["binding_sha256"] = binding_sha256
            matches.append(value)
    if not matches:
        raise PolicyRegistryError(
            f"no managed policy binding for Topic scope {prefix[:-1]}"
        )
    if len(matches) != 1:
        raise PolicyRegistryError(
            f"ambiguous managed policy bindings for Topic scope {prefix[:-1]}"
        )
    return matches[0]


def resolve_topic_policies_for_scope(
    platform: str,
    chat_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Resolve current, hash-verified policy content for one trusted Topic scope."""
    binding = topic_policy_binding_for_scope(platform, chat_id, thread_id)
    policies = [
        _resolve_requirement(requirement)
        for requirement in binding.get("requirements", [])
    ]
    return {
        "namespace": binding["namespace"],
        "binding_path": binding["binding_path"],
        "binding_sha256": binding["binding_sha256"],
        "requirements": binding.get("requirements", []),
        "policies": policies,
    }


def resolve_task_policy_snapshots(task_body: str) -> dict[str, Any]:
    """Resolve and verify the immutable policy refs pinned into a task body."""
    binding, refs = _policy_context_from_task_body(task_body)
    if binding is None and not refs:
        raise PolicyRegistryError("task has no managed policy snapshot")

    if binding is not None:
        namespace = str(binding.get("namespace") or "")
        canonical_path = _binding_path(namespace)
        if str(binding.get("path") or "") != str(canonical_path):
            raise PolicyRegistryError("policy snapshot Topic binding path is invalid")
        expected_binding_sha256 = binding.get("sha256")
        if _sha256_file(canonical_path) != expected_binding_sha256:
            raise PolicyRegistryError("policy_stale: Topic policy binding changed")
        if expected_binding_sha256 is None:
            # A null digest pins the absence of a Topic binding. The missing file is
            # therefore the verified state, not a read failure.
            bound_requirements = []
        else:
            canonical_binding = _load_json(canonical_path, label="Topic policy binding")
            if canonical_binding.get("namespace") != namespace:
                raise PolicyRegistryError("policy snapshot Topic binding namespace mismatch")
            bound_requirements = _normalize_requirements(
                canonical_binding.get("requirements", [])
            )
        refs_by_id = {str(ref.get("policy_id") or ""): ref for ref in refs}
        if len(refs_by_id) != len(refs):
            raise PolicyRegistryError("policy snapshot refs contain invalid policy ids")
        for requirement in bound_requirements:
            ref = refs_by_id.get(requirement["policy_id"])
            if ref is None:
                raise PolicyRegistryError(
                    "policy snapshot set does not include all Topic requirements"
                )
            if (
                str(ref.get("resolution") or "latest_active")
                != requirement["resolution"]
                or list(ref.get("sections") or []) != requirement["sections"]
                or (
                    requirement["resolution"] == "fixed"
                    and ref.get("version") != requirement.get("version")
                )
            ):
                raise PolicyRegistryError(
                    f"policy snapshot requirement mismatch: {requirement['policy_id']}"
                )

    policies: list[dict[str, Any]] = []
    for ref in refs:
        resolution = str(ref.get("resolution") or "latest_active")
        requirement: dict[str, Any] = {
            "policy_id": ref.get("policy_id"),
            "resolution": resolution,
            "sections": list(ref.get("sections") or []),
        }
        if resolution == "fixed":
            requirement["version"] = ref.get("version")
        current = _resolve_requirement(requirement)
        for key in (
            "version",
            "sha256",
            "sections",
            "manifest_path",
            "version_path",
        ):
            if current.get(key) != ref.get(key):
                raise PolicyRegistryError(
                    f"policy_stale: task snapshot {ref.get('policy_id')}.{key} differs"
                )
        policies.append(current)

    return {
        "binding": dict(binding) if binding is not None else None,
        "policies": policies,
    }


def _normalize_requirements(requirements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(requirements, (list, tuple)):
        raise PolicyRegistryError("policy_requirements must be a list")
    merged: dict[str, dict[str, Any]] = {}
    for raw in requirements:
        if not isinstance(raw, Mapping):
            raise PolicyRegistryError("each policy requirement must be an object")
        policy_id = _validate_policy_id(raw.get("policy_id"))
        resolution = str(raw.get("resolution") or "latest_active").strip()
        if resolution not in {"latest_active", "fixed"}:
            raise PolicyRegistryError("policy resolution must be latest_active or fixed")
        version = raw.get("version")
        if resolution == "fixed":
            version = _validate_version(version)
        elif version not in {None, ""}:
            raise PolicyRegistryError("latest_active requirements must not set version")
        sections = raw.get("sections") or []
        if not isinstance(sections, list) or any(
            not isinstance(item, str) or not item.strip() for item in sections
        ):
            raise PolicyRegistryError("policy sections must be non-empty strings")
        candidate = {
            "policy_id": policy_id,
            "resolution": resolution,
            "sections": list(dict.fromkeys(item.strip() for item in sections)),
        }
        if resolution == "fixed":
            candidate["version"] = version
        existing = merged.get(policy_id)
        if existing and (
            existing["resolution"] != resolution
            or existing.get("version") != candidate.get("version")
        ):
            raise PolicyRegistryError(f"conflicting requirements for policy {policy_id}")
        if existing:
            existing["sections"] = list(
                dict.fromkeys([*existing["sections"], *candidate["sections"]])
            )
        else:
            merged[policy_id] = candidate
    return list(merged.values())


def _resolve_requirement(requirement: Mapping[str, Any]) -> dict[str, Any]:
    policy_id = _validate_policy_id(requirement.get("policy_id"))
    manifest_path = _manifest_path(policy_id)
    manifest = _load_json(manifest_path, label="policy manifest")
    resolution = str(requirement.get("resolution") or "latest_active")
    version = (
        manifest.get("active_version")
        if resolution == "latest_active"
        else requirement.get("version")
    )
    if not version:
        raise PolicyRegistryError(f"policy has no active version: {policy_id}")
    version = _validate_version(version)
    record = next(
        (
            item
            for item in manifest.get("versions", [])
            if isinstance(item, dict) and item.get("version") == version
        ),
        None,
    )
    if record is None:
        raise PolicyRegistryError(f"policy version missing from manifest: {policy_id}@{version}")
    version_path = _version_path(policy_id, version)
    try:
        content = version_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyRegistryError(f"policy version not readable: {version_path}") from exc
    digest = _sha256_text(content)
    if digest != record.get("sha256"):
        raise PolicyRegistryError(f"policy digest mismatch: {policy_id}@{version}")
    return {
        "policy_id": policy_id,
        "owner_scope": manifest.get("owner_scope"),
        "owner_id": manifest.get("owner_id"),
        "resolution": resolution,
        "version": version,
        "sha256": digest,
        "sections": list(requirement.get("sections") or []),
        "manifest_path": str(manifest_path),
        "version_path": str(version_path),
        "content": content,
    }


def resolve_contract_policies(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Attach immutable policy snapshots from explicit and Topic-bound requirements."""
    value = deepcopy(dict(contract or {}))
    memory = value.get("memory")
    namespace = str(memory.get("namespace") or "").strip() if isinstance(memory, Mapping) else ""
    binding = topic_policy_binding(namespace) if namespace else None
    expected_binding = (
        {
            "namespace": namespace,
            "path": binding["binding_path"],
            "sha256": binding["binding_sha256"],
        }
        if binding
        else None
    )
    if "policy_binding_snapshot" in value:
        if value.get("policy_binding_snapshot") != expected_binding:
            raise PolicyRegistryError("Topic policy binding snapshot is stale or altered")
    elif "policy_snapshots" in value:
        raise PolicyRegistryError("policy_binding_snapshot is required with policy snapshots")
    if expected_binding:
        value["policy_binding_snapshot"] = expected_binding
    else:
        value.pop("policy_binding_snapshot", None)
    bound = binding.get("requirements", []) if binding else []
    explicit = value.get("policy_requirements") or []
    requirements = _normalize_requirements([*bound, *explicit])
    if not requirements:
        value.pop("policy_requirements", None)
        value.pop("policy_snapshots", None)
        return value

    supplied = value.get("policy_snapshots")
    expected = [_resolve_requirement(item) for item in requirements]
    if binding:
        binding_ref = {
            "binding_namespace": namespace,
            "binding_path": binding["binding_path"],
            "binding_sha256": binding["binding_sha256"],
        }
        for snapshot in expected:
            snapshot.update(binding_ref)
    if supplied is not None:
        if not isinstance(supplied, list):
            raise PolicyRegistryError("policy_snapshots must be a list")
        if any(not isinstance(item, Mapping) for item in supplied):
            raise PolicyRegistryError("policy_snapshots must contain objects")
        expected_by_id = {item["policy_id"]: item for item in expected}
        supplied_by_id = {
            str(item.get("policy_id") or ""): item
            for item in supplied
        }
        if len(supplied_by_id) != len(supplied):
            raise PolicyRegistryError("policy_snapshots contain duplicate policy ids")
        if set(supplied_by_id) != set(expected_by_id):
            raise PolicyRegistryError("policy snapshot set does not match current requirements")
        for policy_id, current in expected_by_id.items():
            snapshot = supplied_by_id[policy_id]
            for key in (
                "version",
                "sha256",
                "content",
                "manifest_path",
                "version_path",
                "binding_namespace",
                "binding_path",
                "binding_sha256",
            ):
                if snapshot.get(key) != current.get(key):
                    raise PolicyRegistryError(
                        f"policy snapshot is stale or altered: {policy_id}.{key}"
                    )
    value["policy_requirements"] = requirements
    value["policy_snapshots"] = expected
    return value


def policy_snapshot_marker(contract: Mapping[str, Any]) -> str | None:
    snapshots = contract.get("policy_snapshots")
    binding = contract.get("policy_binding_snapshot")
    if not isinstance(snapshots, list):
        snapshots = []
    if not snapshots and not isinstance(binding, Mapping):
        return None
    refs = [
        {
            key: item.get(key)
            for key in (
                "policy_id",
                "resolution",
                "version",
                "sha256",
                "sections",
                "manifest_path",
                "version_path",
                "binding_namespace",
                "binding_path",
                "binding_sha256",
            )
        }
        for item in snapshots
        if isinstance(item, Mapping)
    ]
    payload = {
        "binding": dict(binding) if isinstance(binding, Mapping) else None,
        "policies": refs,
    }
    return f"{POLICY_MARKER} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"


def _policy_context_from_task_body(
    task_body: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    for line in str(task_body or "").splitlines():
        if not line.startswith(POLICY_MARKER):
            continue
        try:
            value = json.loads(line[len(POLICY_MARKER) :].strip())
        except (ValueError, json.JSONDecodeError) as exc:
            raise PolicyRegistryError("invalid policy snapshot marker") from exc
        if not isinstance(value, Mapping):
            raise PolicyRegistryError("policy snapshot marker must be an object")
        binding = value.get("binding")
        if binding is not None:
            if not isinstance(binding, Mapping):
                raise PolicyRegistryError("policy binding snapshot must be an object")
            binding = dict(binding)
            if {"namespace", "path", "sha256"} - set(binding):
                raise PolicyRegistryError("policy binding snapshot is incomplete")
        refs = value.get("policies")
        if not isinstance(refs, list):
            raise PolicyRegistryError("policy snapshot marker must contain a policy list")
        if any(not isinstance(item, Mapping) for item in refs):
            raise PolicyRegistryError("policy snapshot refs must be objects")
        return binding, [dict(item) for item in refs]
    return None, []


def policy_refs_from_task_body(task_body: str) -> list[dict[str, Any]]:
    _, refs = _policy_context_from_task_body(task_body)
    required = {
        "policy_id",
        "resolution",
        "version",
        "sha256",
        "manifest_path",
        "version_path",
    }
    if any(required - set(item) for item in refs):
        raise PolicyRegistryError("policy snapshot ref is incomplete")
    policy_ids = [str(item.get("policy_id") or "") for item in refs]
    if any(not policy_id for policy_id in policy_ids) or len(set(policy_ids)) != len(
        policy_ids
    ):
        raise PolicyRegistryError("policy snapshot refs contain invalid policy ids")
    return refs


def validate_policy_completion(
    task_body: str,
    metadata: Mapping[str, Any],
    *,
    role: str,
) -> None:
    """Require exact receipts and reject stale policy reviews at write time."""
    binding, expected = _policy_context_from_task_body(task_body)
    policy_refs_from_task_body(task_body)
    if binding is None and not expected:
        return
    receipts = metadata.get("policy_receipts")
    if not isinstance(receipts, list):
        raise PolicyRegistryError("metadata.policy_receipts must be a list")
    if any(not isinstance(item, Mapping) for item in receipts):
        raise PolicyRegistryError("metadata.policy_receipts must contain objects")
    role_receipts = [item for item in receipts if item.get("role") == role]
    by_id = {
        str(item.get("policy_id") or ""): item
        for item in role_receipts
    }
    if len(by_id) != len(role_receipts):
        raise PolicyRegistryError(f"duplicate {role} policy receipts")
    if set(by_id) != {str(item["policy_id"]) for item in expected}:
        raise PolicyRegistryError(f"{role} policy receipts do not match the task snapshot")
    binding_namespace = str((binding or {}).get("namespace") or "")
    binding_path = str((binding or {}).get("path") or "")
    binding_sha256 = (binding or {}).get("sha256")
    if role == "review" and binding_namespace:
        canonical_binding_path = _binding_path(binding_namespace)
        if binding_path != str(canonical_binding_path):
            raise PolicyRegistryError("policy snapshot Topic binding path is invalid")
        if _sha256_file(canonical_binding_path) != binding_sha256:
            raise PolicyRegistryError("policy_stale: Topic policy binding changed")
    for item in expected:
        receipt = by_id[str(item["policy_id"])]
        if (
            receipt.get("version") != item.get("version")
            or receipt.get("sha256") != item.get("sha256")
            or receipt.get("loaded") is not True
        ):
            raise PolicyRegistryError(
                f"{role} policy receipt mismatch: {item['policy_id']}"
            )
        version_path = Path(str(item.get("version_path") or ""))
        try:
            current_digest = _sha256_text(version_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyRegistryError(f"policy version unavailable: {version_path}") from exc
        if current_digest != item.get("sha256"):
            raise PolicyRegistryError(f"policy version changed: {item['policy_id']}")
        if role == "review" and item.get("resolution") == "latest_active":
            current = _resolve_requirement(
                {
                    "policy_id": item["policy_id"],
                    "resolution": "latest_active",
                    "sections": [],
                }
            )
            if current.get("version") != item.get("version"):
                raise PolicyRegistryError(
                    f"policy_stale: {item['policy_id']} active version is "
                    f"{current.get('version')!r}, task used {item.get('version')!r}"
                )
            for key in ("sha256", "manifest_path", "version_path"):
                if current.get(key) != item.get(key):
                    raise PolicyRegistryError(
                        f"policy_stale: current {item['policy_id']}.{key} differs"
                    )
            if receipt.get("latest_active_verified") is not True:
                raise PolicyRegistryError(
                    "review receipt must set latest_active_verified=true: "
                    f"{item['policy_id']}"
                )
