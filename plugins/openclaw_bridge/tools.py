from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from plugins.openclaw_bridge.schemas import validate_delegated_result, validate_delegated_task
from proactive.tool_policy import PolicyLevel, decide_action, load_tool_policy


DEFAULT_OPENCLAW_BRIDGE_PATH = "/api/plugins/hermes-bridge/tasks"
DEFAULT_OPENCLAW_TEMPLATE = "agents.ask_team"
_ZERO_EFFECT_ASYNC_CAPABILITY = object()
_READONLY_BROWSER_ALLOWED_URLS = frozenset(
    {
        "https://example.com/",
        "https://www.linkedin.com/in/craig-k-j-hsu-6012b815",
    }
)


@dataclass(frozen=True)
class OpenClawBridgeConfig:
    base_url: str
    gateway_token: str
    bridge_token: str
    task_template: str = DEFAULT_OPENCLAW_TEMPLATE
    timeout_seconds: int = 30


OPENCLAW_DELEGATE_PARAMETERS = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "context_refs": {"type": "array", "items": {"type": "string"}},
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "denied_tools": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string"},
        "requires_confirmation": {"type": "boolean"},
        "max_runtime_seconds": {"type": "integer"},
        "output_format": {"type": "string"},
        "audit_required": {"type": "boolean"},
        "requested_by": {"type": "string"},
        "openclaw_task_id": {"type": "string"},
        "target_url": {"type": "string"},
        "start_idempotency_key": {"type": "string"},
        "backend_run_id": {"type": "string"},
        "protocol_version": {"type": "string"},
        "delegation_id": {"type": "string"},
        "attempt_id": {"type": "string"},
        "contract_fingerprint": {"type": "string"},
        "project": {"type": "string"},
        "topic_id": {"type": "string"},
        "executor_backend": {"type": "string"},
        "executor_profile": {"type": "string"},
        "backend_agent_id": {"type": "string"},
        "approval_grant_id": {"type": "string"},
        "external_effect_budget": {"type": "integer"},
        "workspace_policy": {"type": "string"},
        "session_policy": {"type": "string"},
        "credential_refs": {"type": "array", "items": {"type": "string"}},
        "idempotency_key": {"type": "string"},
        "dry_run": {"type": "boolean"},
    },
    "required": ["objective"],
}

OPENCLAW_DELEGATE_SCHEMA = {
    "description": "Diagnostic OpenClaw bridge only; not a real execution fallback.",
    "parameters": OPENCLAW_DELEGATE_PARAMETERS,
}


def build_delegated_task(args: dict[str, Any]) -> dict[str, Any]:
    raw_protocol_version = args.get("protocol_version")
    protocol_version = (
        "1.0"
        if raw_protocol_version is None
        else str(raw_protocol_version).strip()
    )
    if protocol_version not in {"1.0", "2.0"}:
        raise ValueError(
            "protocol_version must be explicitly '1.0' or '2.0' when provided"
        )
    if protocol_version == "2.0":
        for field in ("requires_confirmation", "audit_required", "dry_run"):
            if field in args and not isinstance(args[field], bool):
                raise ValueError(f"Protocol v2 {field} must be a boolean")
        for field in (
            "context_refs",
            "allowed_tools",
            "denied_tools",
            "credential_refs",
        ):
            if field not in args:
                continue
            value = args[field]
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(
                    f"Protocol v2 {field} must be an array of strings"
                )
        if "external_effect_budget" in args:
            budget = args["external_effect_budget"]
            if (
                not isinstance(budget, int)
                or isinstance(budget, bool)
                or budget < 0
            ):
                raise ValueError(
                    "Protocol v2 external_effect_budget must be a "
                    "non-negative integer"
                )
        if "max_runtime_seconds" in args:
            runtime = args["max_runtime_seconds"]
            if (
                not isinstance(runtime, int)
                or isinstance(runtime, bool)
                or runtime <= 0
            ):
                raise ValueError(
                    "Protocol v2 max_runtime_seconds must be a positive integer"
                )
    risk = str(args.get("risk_level") or "medium").lower()
    requires_confirmation = bool(args.get("requires_confirmation", risk in {"high", "critical"}))
    task = {
        "task_id": str(args.get("task_id") or f"openclaw-{uuid.uuid4().hex[:12]}"),
        "requested_by": str(args.get("requested_by") or "hermes"),
        "objective": str(args.get("objective") or "").strip(),
        "context_refs": list(args.get("context_refs") or []),
        "allowed_tools": list(args.get("allowed_tools") or []),
        "denied_tools": list(args.get("denied_tools") or []),
        "risk_level": risk,
        "requires_confirmation": requires_confirmation,
        "max_runtime_seconds": int(args.get("max_runtime_seconds") or 300),
        "output_format": str(args.get("output_format") or "markdown"),
        "audit_required": bool(args.get("audit_required", True)),
    }
    if protocol_version == "2.0":
        required_v2 = (
            "delegation_id",
            "attempt_id",
            "contract_fingerprint",
            "project",
            "topic_id",
            "executor_profile",
            "backend_agent_id",
        )
        missing = [name for name in required_v2 if not str(args.get(name) or "").strip()]
        if missing:
            raise ValueError(
                "Protocol v2 requires field(s): " + ", ".join(missing)
            )
        task.update(
            {
                "protocol_version": "2.0",
                "delegation_id": str(args["delegation_id"]).strip(),
                "attempt_id": str(args["attempt_id"]).strip(),
                "contract_fingerprint": str(args["contract_fingerprint"]).strip(),
                "executor_backend": str(args.get("executor_backend") or "openclaw").strip(),
                "external_effect_budget": int(args.get("external_effect_budget") or 0),
                "workspace_policy": str(args.get("workspace_policy") or "dedicated").strip(),
                "session_policy": str(args.get("session_policy") or "ephemeral").strip(),
                "credential_refs": list(args.get("credential_refs") or []),
                "dry_run": bool(args.get("dry_run", True)),
                "idempotency_key": str(
                    args.get("idempotency_key") or args["attempt_id"]
                ).strip(),
            }
        )
        for field in (
            "project",
            "topic_id",
            "executor_profile",
            "backend_agent_id",
            "approval_grant_id",
            "openclaw_task_id",
            "target_url",
            "start_idempotency_key",
            "backend_run_id",
        ):
            value = str(args.get(field) or "").strip()
            if value:
                task[field] = value
    return validate_delegated_task(task)


def _blocked_result(task: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    return {
        "task_id": (task or {}).get("task_id", ""),
        "status": "blocked",
        "summary": reason,
        "artifacts": [],
        "tool_calls": [],
        "audit_log": [reason],
        "errors": [reason],
        "requires_human_review": True,
        "recommended_next_action": "Ask KJ for approval before delegating to OpenClaw.",
    }


def _blocked_capability_result(
    task: dict[str, Any] | None,
    reason: str,
    next_action: str,
) -> dict[str, Any]:
    result = _blocked_result(task, reason)
    result["recommended_next_action"] = next_action
    result["errors"] = ["external_capability_unavailable"]
    return result


def _requires_external_browser_capability(task: dict[str, Any]) -> bool:
    if (
        task.get("protocol_version") == "2.0"
        and task.get("openclaw_task_id") == "openclaw.browser.read_snapshot"
        and task.get("dry_run") is False
    ):
        return False
    haystack = " ".join(
        [
            str(task.get("objective") or ""),
            " ".join(str(item) for item in task.get("context_refs") or []),
            " ".join(str(item) for item in task.get("allowed_tools") or []),
        ]
    ).lower()
    external_targets = ("facebook", "fb marketplace", "marketplace", "社團")
    external_actions = (
        "post",
        "publish",
        "listing",
        "join",
        "inspect",
        "check",
        "facebook",
        "刊登",
        "發布",
        "發佈",
        "貼文",
        "檢查",
        "加入",
    )
    return any(target in haystack for target in external_targets) and any(
        action in haystack for action in external_actions
    )


def _is_explicit_openclaw_dry_run(task: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(task.get("requested_by") or ""),
            str(task.get("openclaw_task_id") or ""),
            " ".join(str(item) for item in task.get("context_refs") or []),
        ]
    ).lower()
    return "openclaw-dry-run" in haystack or "dry-run" in haystack


def _requires_clawops_runtime(task: dict[str, Any]) -> bool:
    """Return True for work that should enter the Hermes-owned ClawOps queue."""
    if _is_explicit_openclaw_dry_run(task):
        return False
    haystack = " ".join(
        [
            str(task.get("objective") or ""),
            " ".join(str(item) for item in task.get("context_refs") or []),
            " ".join(str(item) for item in task.get("allowed_tools") or []),
        ]
    ).lower()
    runtime_terms = (
        "clawops",
        "content creator",
        "marketing operator",
        "secondhand_commerce",
        "course_marketing",
        "ingrids_marketing",
        "content_draft",
        "campaign",
        "product_marketing",
        "生成圖片",
        "生圖",
        "圖片生成",
        "產出圖",
        "產生圖",
        "image generation",
        "image_generate",
        "generate image",
        "image_gen",
    )
    return any(term in haystack for term in runtime_terms)


def _default_transport(
    task: dict[str, Any],
    *,
    live_async_capability: object | None = None,
) -> dict[str, Any]:
    config = load_openclaw_bridge_config()
    if config is None:
        return {
            "task_id": task["task_id"],
            "status": "blocked",
            "summary": (
                "OpenClaw bridge transport is not configured. Set "
                "OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN, and "
                "OPENCLAW_HERMES_BRIDGE_TOKEN, or configure openclaw_bridge "
                "in Hermes config."
            ),
            "artifacts": [],
            "tool_calls": [],
            "audit_log": ["Hermes blocked delegation before OpenClaw because bridge config is incomplete."],
            "errors": ["openclaw_bridge_not_configured"],
            "requires_human_review": True,
            "recommended_next_action": "Configure OpenClaw bridge URL and tokens, then retry the dry-run.",
        }
    return post_to_openclaw_bridge(
        task,
        config,
        live_async_capability=live_async_capability,
    )


def _placeholder_transport(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": "queued",
        "summary": "DelegatedTask validated; no OpenClaw transport configured in this process.",
        "artifacts": [],
        "tool_calls": [],
        "audit_log": ["Hermes retained conversation authority; OpenClaw direct reply disabled."],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "Configure the OpenClaw bridge transport to execute this task.",
    }


def _config_get(mapping: dict[str, Any], *names: str) -> str:
    current: Any = mapping
    for name in names:
        if not isinstance(current, dict):
            return ""
        current = current.get(name)
    return str(current or "").strip()


def _read_env_file_value(path: str, key: str) -> str:
    if not path:
        return ""
    env_path = Path(path).expanduser()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{key}="
    export_prefix = f"export {key}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        value = ""
        if line.startswith(export_prefix):
            value = line[len(export_prefix) :]
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        return value.strip()
    return ""


def load_openclaw_bridge_config() -> OpenClawBridgeConfig | None:
    base_url = os.getenv("OPENCLAW_HERMES_BRIDGE_URL", "").strip()
    if not base_url:
        base_url = os.getenv("OPENCLAW_GATEWAY_URL", "").strip()
    gateway_token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
    bridge_token = os.getenv("OPENCLAW_HERMES_BRIDGE_TOKEN", "").strip()
    task_template = os.getenv("OPENCLAW_HERMES_TASK_TEMPLATE", "").strip() or DEFAULT_OPENCLAW_TEMPLATE
    timeout_seconds = int(os.getenv("OPENCLAW_HERMES_TIMEOUT_SECONDS", "30") or "30")

    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        bridge_cfg = cfg.get("openclaw_bridge") if isinstance(cfg, dict) else {}
        openclaw_cfg = cfg.get("openclaw") if isinstance(cfg, dict) else {}
        if isinstance(bridge_cfg, dict):
            base_url = base_url or _config_get(bridge_cfg, "url") or _config_get(bridge_cfg, "base_url")
            gateway_token = gateway_token or _config_get(bridge_cfg, "gateway_token")
            bridge_token = bridge_token or _config_get(bridge_cfg, "bridge_token")
            task_template = _config_get(bridge_cfg, "task_template") or task_template
            env_file = _config_get(bridge_cfg, "env_file")
            if env_file:
                base_url = base_url or _read_env_file_value(env_file, "OPENCLAW_GATEWAY_URL")
                gateway_token = gateway_token or _read_env_file_value(env_file, "OPENCLAW_GATEWAY_TOKEN")
                bridge_token = bridge_token or _read_env_file_value(env_file, "OPENCLAW_HERMES_BRIDGE_TOKEN")
            if bridge_cfg.get("timeout_seconds"):
                timeout_seconds = int(bridge_cfg["timeout_seconds"])
        if isinstance(openclaw_cfg, dict):
            base_url = base_url or _config_get(openclaw_cfg, "gateway_url") or _config_get(openclaw_cfg, "url")
    except Exception:
        pass

    if not (base_url and gateway_token and bridge_token):
        return None
    if base_url.startswith("ws://"):
        base_url = "http://" + base_url[len("ws://") :]
    elif base_url.startswith("wss://"):
        base_url = "https://" + base_url[len("wss://") :]
    return OpenClawBridgeConfig(
        base_url=base_url.rstrip("/"),
        gateway_token=gateway_token,
        bridge_token=bridge_token,
        task_template=task_template,
        timeout_seconds=min(max(int(timeout_seconds), 1), 30),
    )


def _openclaw_payload(
    task: dict[str, Any],
    config: OpenClawBridgeConfig,
    *,
    live_async_capability: object | None = None,
) -> dict[str, Any]:
    template = str(task.get("openclaw_task_id") or config.task_template or DEFAULT_OPENCLAW_TEMPLATE)
    is_live_read_snapshot = (
        task.get("protocol_version") == "2.0"
        and template
        in {
            "openclaw.browser.read_snapshot",
            "openclaw.browser.read_snapshot_poll",
            "openclaw.browser.read_snapshot_cancel",
        }
        and task.get("dry_run") is False
        and task.get("executor_backend") == "openclaw"
        and task.get("executor_profile") == "browser-readonly"
        and task.get("backend_agent_id") == "missioncrew-browser-readonly"
        and task.get("external_effect_budget") == 0
        and task.get("workspace_policy") == "dedicated"
        and task.get("session_policy") == "ephemeral"
        and task.get("credential_refs") == []
        and task.get("requires_confirmation") is False
        and task.get("allowed_tools") == ["browser.read"]
        and str(task.get("target_url") or "").strip()
        in _READONLY_BROWSER_ALLOWED_URLS
        and bool(str(task.get("project") or "").strip())
        and bool(str(task.get("topic_id") or "").strip())
        and bool(str(task.get("idempotency_key") or "").strip())
        and (
            template == "openclaw.browser.read_snapshot"
            or (
                bool(str(task.get("start_idempotency_key") or "").strip())
                and bool(str(task.get("backend_run_id") or "").strip())
            )
        )
    )
    is_live_zero_effect_async = (
        live_async_capability is _ZERO_EFFECT_ASYNC_CAPABILITY
        and
        task.get("protocol_version") == "2.0"
        and template
        in {
            "openclaw.agent.zero_effect_async_start",
            "openclaw.agent.zero_effect_async_poll",
            "openclaw.agent.zero_effect_async_cancel",
        }
        and task.get("dry_run") is False
        and task.get("executor_backend") == "openclaw"
        and task.get("executor_profile") == "zero-effect-async"
        and task.get("backend_agent_id") == "missioncrew-browser-readonly"
        and task.get("external_effect_budget") == 0
        and task.get("workspace_policy") == "dedicated"
        and task.get("session_policy") == "ephemeral"
        and task.get("credential_refs") == []
        and task.get("requires_confirmation") is False
        and task.get("allowed_tools") == []
        and bool(str(task.get("project") or "").strip())
        and bool(str(task.get("topic_id") or "").strip())
        and bool(str(task.get("idempotency_key") or "").strip())
        and (
            template == "openclaw.agent.zero_effect_async_start"
            or (
                bool(str(task.get("start_idempotency_key") or "").strip())
                and bool(str(task.get("backend_run_id") or "").strip())
            )
        )
    )
    if (
        task.get("dry_run") is False
        and not is_live_read_snapshot
        and not is_live_zero_effect_async
    ):
        raise ValueError(
            "Only the Protocol v2 zero-effect OpenClaw templates may set "
            "dry_run=false."
        )
    payload = {
        "taskId": template,
        "requestedBy": task["requested_by"],
        "intent": task["objective"],
        "priority": "normal",
        "requiresConfirmation": bool(task["requires_confirmation"]),
        "allowedTools": list(task.get("allowed_tools") or []),
        "input": {
            "objective": task["objective"],
            "contextRefs": list(task.get("context_refs") or []),
            "delegatedTaskId": task["task_id"],
            "outputFormat": task["output_format"],
            **(
                {"url": str(task["target_url"]).strip()}
                if task.get("target_url")
                else {}
            ),
            **(
                {
                    "startIdempotencyKey": str(
                        task["start_idempotency_key"]
                    ).strip()
                }
                if task.get("start_idempotency_key")
                else {}
            ),
            **(
                {"backendRunId": str(task["backend_run_id"]).strip()}
                if task.get("backend_run_id")
                else {}
            ),
        },
        "dryRun": bool(task.get("dry_run", True)),
        "idempotencyKey": str(task.get("idempotency_key") or task["task_id"]),
    }
    if task.get("protocol_version") == "2.0":
        payload.update(
            {
                "protocolVersion": "2.0",
                "identity": {
                    "delegationId": task["delegation_id"],
                    "attemptId": task["attempt_id"],
                    "contractFingerprint": task["contract_fingerprint"],
                    "project": task.get("project"),
                    "topicId": task.get("topic_id"),
                },
                "routing": {
                    "executorBackend": task.get("executor_backend"),
                    "executorProfile": task.get("executor_profile"),
                    "backendAgentId": task.get("backend_agent_id"),
                },
                "policy": {
                    "approvalGrantId": task.get("approval_grant_id"),
                    "externalEffectBudget": task.get("external_effect_budget", 0),
                    "workspacePolicy": task.get("workspace_policy"),
                    "sessionPolicy": task.get("session_policy"),
                    "credentialRefs": list(task.get("credential_refs") or []),
                },
            }
        )
    return payload


def _with_protocol_failure_correlation(
    task: Mapping[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Preserve Protocol v2 request identity without claiming a response echo."""
    if task.get("protocol_version") == "2.0":
        result.update(
            {
                "protocol_version": "2.0",
                "delegation_id": task["delegation_id"],
                "attempt_id": task["attempt_id"],
                "contract_fingerprint": task["contract_fingerprint"],
                "identity_correlated": False,
                "protocol_correlated": False,
            }
        )
    return result


def post_to_openclaw_bridge(
    task: dict[str, Any],
    config: OpenClawBridgeConfig,
    *,
    live_async_capability: object | None = None,
) -> dict[str, Any]:
    payload = _openclaw_payload(
        task,
        config,
        live_async_capability=live_async_capability,
    )
    url = urljoin(config.base_url + "/", DEFAULT_OPENCLAW_BRIDGE_PATH.lstrip("/"))
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.gateway_token}",
            "x-openclaw-hermes-token": config.bridge_token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
            error_payload = json.loads(error_body)
        except Exception:
            # Error responses are best-effort diagnostics. A truncated body
            # must not replace the stable HTTP status fallback with a second
            # exception from the handler itself.
            error_payload = {}
        if not isinstance(error_payload, dict):
            error_payload = {}
        bridge_error = error_payload.get("error")
        bridge_message = (
            str(bridge_error.get("message") or "")
            if isinstance(bridge_error, dict)
            else ""
        )
        return _with_protocol_failure_correlation(task, {
            "task_id": task["task_id"],
            "status": "failed",
            "summary": str(
                error_payload.get("summary")
                or bridge_message
                or f"OpenClaw bridge HTTP error: {exc.code}"
            ),
            "artifacts": [],
            "tool_calls": [{"name": "openclaw_bridge_http", "url": url, "status": exc.code}],
            "audit_log": ["Hermes sent DelegatedTask to OpenClaw bridge; OpenClaw returned HTTP error."],
            "errors": [
                f"http_{exc.code}",
                *([bridge_message] if bridge_message else []),
            ],
            "requires_human_review": True,
            "recommended_next_action": "Check OpenClaw gateway/plugin status and bridge token configuration.",
        })
    except URLError as exc:
        return _with_protocol_failure_correlation(task, {
            "task_id": task["task_id"],
            "status": "failed",
            "summary": f"OpenClaw bridge connection failed: {exc.reason}",
            "artifacts": [],
            "tool_calls": [{"name": "openclaw_bridge_http", "url": url, "status": "connection_failed"}],
            "audit_log": ["Hermes attempted OpenClaw bridge delegation but could not connect."],
            "errors": ["connection_failed"],
            "requires_human_review": True,
            "recommended_next_action": "Start OpenClaw gateway and confirm OPENCLAW_GATEWAY_URL.",
        })
    except TimeoutError:
        return _with_protocol_failure_correlation(task, {
            "task_id": task["task_id"],
            "status": "failed",
            "summary": "OpenClaw bridge request timed out.",
            "artifacts": [],
            "tool_calls": [{"name": "openclaw_bridge_http", "url": url, "status": "timeout"}],
            "audit_log": ["Hermes attempted OpenClaw bridge delegation but timed out."],
            "errors": ["timeout"],
            "requires_human_review": True,
            "recommended_next_action": "Check OpenClaw gateway health and task runtime.",
        })

    try:
        openclaw_result = json.loads(raw)
    except json.JSONDecodeError:
        openclaw_result = {"ok": False, "status": "failed", "summary": raw}
    if not isinstance(openclaw_result, dict):
        openclaw_result = {
            "ok": False,
            "status": "failed",
            "summary": "OpenClaw bridge returned a non-object JSON response.",
        }

    ok_present = "ok" in openclaw_result
    raw_ok = openclaw_result.get("ok")
    ok_type_valid = not ok_present or isinstance(raw_ok, bool)
    ok = (
        raw_ok
        if ok_present and ok_type_valid
        else (
            openclaw_result.get("status") == "succeeded"
            if not ok_present
            else False
        )
    )
    status = str(openclaw_result.get("status") or ("succeeded" if ok else "failed"))
    if status == "accepted":
        status = "queued"
    if status not in {"queued", "running", "succeeded", "failed", "blocked"}:
        status = "succeeded" if ok else "failed"
    if ok_present and not ok:
        status = "failed"
    if status in {"failed", "blocked"}:
        ok = False
    audit = openclaw_result.get("auditLog") or openclaw_result.get("audit_log") or []
    expected_protocol = str(task.get("protocol_version") or "1.0")
    raw_returned_protocol = openclaw_result.get("protocolVersion")
    returned_protocol = (
        raw_returned_protocol.strip()
        if isinstance(raw_returned_protocol, str)
        else ""
    )
    protocol_correlated = (
        expected_protocol != "2.0" or returned_protocol == expected_protocol
    )
    protocol_errors: list[str] = []
    if not ok_type_valid:
        protocol_errors.append("OpenClaw response ok must be a boolean.")
    if expected_protocol == "2.0" and returned_protocol != "2.0":
        protocol_errors.append(
            "OpenClaw response did not explicitly echo Protocol v2."
        )
    execution_identity = openclaw_result.get("executionIdentity")
    identity_correlated = expected_protocol != "2.0"
    if expected_protocol == "2.0":
        expected_identity = {
            "delegationId": str(task.get("delegation_id") or ""),
            "attemptId": str(task.get("attempt_id") or ""),
            "contractFingerprint": str(task.get("contract_fingerprint") or ""),
        }
        returned_identity = (
            execution_identity if isinstance(execution_identity, dict) else {}
        )
        identity_correlated = True
        for field, expected_value in expected_identity.items():
            returned_value = returned_identity.get(field)
            if (
                not isinstance(returned_value, str)
                or not returned_value.strip()
                or returned_value != expected_value
            ):
                identity_correlated = False
                protocol_errors.append(
                    f"OpenClaw response executionIdentity.{field} did not match the request."
                )
    backend = openclaw_result.get("backendExecution")
    claimed_success = ok and status == "succeeded"
    if expected_protocol == "2.0" and claimed_success:
        returned_backend = backend if isinstance(backend, dict) else {}
        for field in ("backendRunId", "backendAgentId", "sessionKey"):
            value = returned_backend.get(field)
            if not isinstance(value, str) or not value.strip():
                protocol_errors.append(
                    f"OpenClaw Protocol v2 success response omitted backendExecution.{field}."
                )
        expected_agent_id = str(task.get("backend_agent_id") or "")
        returned_agent_id = returned_backend.get("backendAgentId")
        if (
            isinstance(returned_agent_id, str)
            and returned_agent_id.strip()
            and returned_agent_id != expected_agent_id
        ):
            protocol_errors.append(
                "OpenClaw response backendExecution.backendAgentId did not match the request."
            )
    if protocol_errors:
        ok = False
        status = "failed"
    active = status in {"queued", "running"}
    raw_artifacts = openclaw_result.get("artifacts")
    if raw_artifacts is None:
        returned_artifacts = []
    elif isinstance(raw_artifacts, list):
        returned_artifacts = list(raw_artifacts)
    else:
        returned_artifacts = []
        protocol_errors.append("OpenClaw response artifacts must be an array.")
        ok = False
        status = "failed"
        active = False
    if "output" in openclaw_result:
        returned_artifacts.append(
            {
                "type": "openclaw_result",
                "value": openclaw_result["output"],
            }
        )
    delegated_result = {
        "task_id": task["task_id"],
        "status": status,
        "summary": str(openclaw_result.get("summary") or "OpenClaw bridge returned a result."),
        "artifacts": returned_artifacts,
        "tool_calls": [{"name": "openclaw_bridge_http", "url": url, "template": payload["taskId"]}],
        "audit_log": audit if isinstance(audit, list) else [audit],
        "errors": (
            protocol_errors
            if protocol_errors
            else (
                []
                if ok or active
                else [
                    str(
                        (openclaw_result.get("error") or {}).get("message")
                        if isinstance(openclaw_result.get("error"), dict)
                        else openclaw_result.get("error")
                        or "openclaw_bridge_failed"
                    )
                ]
            )
        ),
        "requires_human_review": bool(
            openclaw_result.get(
                "requiresHumanReview",
                status in {"failed", "blocked"},
            )
        )
        or (ok_present and not ok)
        or bool(protocol_errors),
        "recommended_next_action": str(openclaw_result.get("recommendedNextAction") or ("Review OpenClaw result." if not ok else "Return summarized result to KJ.")),
    }
    if expected_protocol == "2.0":
        delegated_result.update(
            {
                "protocol_version": "2.0",
                "delegation_id": str(task.get("delegation_id") or ""),
                "attempt_id": str(task.get("attempt_id") or ""),
                "contract_fingerprint": str(task.get("contract_fingerprint") or ""),
                "identity_correlated": identity_correlated,
                "protocol_correlated": protocol_correlated,
            }
        )
    if isinstance(backend, dict):
        for result_field, backend_field in (
            ("backend_run_id", "backendRunId"),
            ("backend_agent_id", "backendAgentId"),
            ("backend_session_key", "sessionKey"),
        ):
            value = backend.get(backend_field)
            if isinstance(value, str) and value.strip():
                delegated_result[result_field] = value.strip()
    return delegated_result


def delegate_to_openclaw(
    args: dict[str, Any],
    *,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    policy_path: str | None = None,
    _live_async_capability: object | None = None,
) -> dict[str, Any]:
    task = build_delegated_task(args)
    risk = task["risk_level"]
    if task["requires_confirmation"] or risk in {"high", "critical"}:
        return _blocked_result(task, f"Delegated task risk_level={risk} requires approval.")
    if _requires_clawops_runtime(task):
        return _blocked_capability_result(
            task,
            "This work belongs in the Hermes-owned ClawOps runtime queue, not the OpenClaw dry-run bridge.",
            "Create a /clawops task so HubOps routing can assign the appropriate ClawOps worker/agent.",
        )
    if _requires_external_browser_capability(task):
        return _blocked_capability_result(
            task,
            "Facebook/external browser work cannot be delegated to the OpenClaw dry-run bridge.",
            "Route this task to a browser-capable executor with explicit audit evidence.",
        )
    if (
        task.get("dry_run") is False
        and task.get("openclaw_task_id")
        in {
            "openclaw.agent.zero_effect_async_start",
            "openclaw.agent.zero_effect_async_poll",
            "openclaw.agent.zero_effect_async_cancel",
        }
        and _live_async_capability is not _ZERO_EFFECT_ASYNC_CAPABILITY
    ):
        return _blocked_capability_result(
            task,
            "Live asynchronous OpenClaw work requires the durable ClawOps "
            "admission and polling path.",
            "Create the zero-effect task through the ClawOps execution backend "
            "router.",
        )

    policy = load_tool_policy(policy_path)
    for action in task["allowed_tools"]:
        decision = decide_action(action, policy)
        if decision.level is PolicyLevel.DENY:
            return _blocked_result(task, f"Tool policy denied delegated action: {action}")
        if decision.level is PolicyLevel.CONFIRM_FIRST and risk != "low":
            return _blocked_result(task, f"Delegated action requires confirmation: {action}")

    result = (
        transport(task)
        if transport is not None
        else _default_transport(
            task,
            live_async_capability=_live_async_capability,
        )
    )
    if "task_id" not in result:
        result = {"task_id": task["task_id"], **result}
    return validate_delegated_result(result)


def delegate_zero_effect_async_to_openclaw(
    args: dict[str, Any],
    *,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    policy_path: str | None = None,
) -> dict[str, Any]:
    """Use the internal capability held only by the durable async adapter."""
    return delegate_to_openclaw(
        args,
        transport=transport,
        policy_path=policy_path,
        _live_async_capability=_ZERO_EFFECT_ASYNC_CAPABILITY,
    )


def handle_openclaw_delegate(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    merged_args = dict(args or {})
    merged_args.update({k: v for k, v in kwargs.items() if v is not None})
    return json.dumps(delegate_to_openclaw(merged_args), ensure_ascii=False)


def _objective_from_raw_args(raw_args: str) -> str:
    raw = (raw_args or "").strip()
    if not raw:
        return "OpenClaw bridge dry-run"
    return raw


def _notification_summary(result: dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    task_id = result.get("task_id", "")
    summary = result.get("summary", "")
    review = "yes" if result.get("requires_human_review") else "no"
    next_action = result.get("recommended_next_action", "")
    return (
        "OpenClaw bridge result\n"
        f"- task_id: {task_id}\n"
        f"- status: {status}\n"
        f"- human_review: {review}\n"
        f"- summary: {summary}\n"
        f"- next_action: {next_action}"
    )


def _create_kanban_task_for_delegation(objective: str, result: dict[str, Any]) -> str:
    from hermes_cli.kanban import run_slash

    title = f"OpenClaw delegated task: {objective[:80]}"
    body = (
        "Hermes-created ClawOps/OpenClaw bridge task.\n\n"
        "OpenClaw remains execution-only; all user-facing decisions stay with Hermes.\n\n"
        "Delegated result:\n"
        f"```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```"
    )
    command = (
        "create "
        f"{shlex.quote(title)} "
        f"--body {shlex.quote(body)} "
        "--created-by hermes-openclaw-bridge "
        "--workspace scratch"
    )
    return run_slash(command)


def handle_openclaw_dry_run(raw_args: str) -> str:
    """Slash command handler for `/openclaw-dry-run`.

    Default mode validates the Hermes-side bridge only and does not enqueue a
    runtime task. Prefix with `kanban` to explicitly create a kanban task after
    validation, which gives KJ a visible runtime queue record without letting
    OpenClaw own the conversation.
    """
    raw = (raw_args or "").strip()
    create_kanban = False
    if raw.lower().startswith("kanban "):
        create_kanban = True
        raw = raw[7:].strip()
    use_placeholder = False
    if raw.lower().startswith("local "):
        use_placeholder = True
        raw = raw[6:].strip()

    objective = _objective_from_raw_args(raw)
    result = delegate_to_openclaw(
        {
            "objective": objective,
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["telegram:openclaw-dry-run"],
            "max_runtime_seconds": 60,
            "output_format": "markdown",
            "audit_required": True,
        },
        transport=_placeholder_transport if use_placeholder else None,
    )

    response = _notification_summary(result)
    if create_kanban:
        kanban_output = _create_kanban_task_for_delegation(objective, result)
        response = f"{response}\n\nKanban enqueue result:\n{kanban_output}"
    return response


_TOPIC_POLITE_PREFIX = r"(?:請\s*)?(?:(?:幫我|麻煩你?|可以(?:請你)?)\s*)*"
_CURRENT_TOPIC_TARGET = (
    r"(?:(?:這個|目前|當前|本|this|current)\s*)?"
    r"(?:telegram\s*)?(?:topic|主題|話題)(?:\s*(?:名稱|名字|標題))?"
)
_TOPIC_POLITE_SUFFIX = (
    r"(?:，?\s*(?:嗎|好嗎|可以嗎|麻煩你了|謝謝你?|謝謝))?"
    r"[？?！!。．.]*"
)
_DIRECT_TOPIC_RENAME_PATTERNS = (
    re.compile(
        rf"^{_TOPIC_POLITE_PREFIX}"
        rf"(?:更改|修改|重新命名|更名|命名)\s*{_CURRENT_TOPIC_TARGET}"
        rf"\s*(?:為|成|叫做)\s*[:：]?\s*(?P<title>.+?)"
        rf"{_TOPIC_POLITE_SUFFIX}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_TOPIC_POLITE_PREFIX}(?:把\s*)?{_CURRENT_TOPIC_TARGET}\s*"
        rf"(?:改成|改為|更名為|命名為|設定為|設為|叫做)\s*[:：]?\s*"
        rf"(?P<title>.+?){_TOPIC_POLITE_SUFFIX}$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:please\s+|can\s+you\s+)*rename\s+"
        r"(?:this|current)\s+(?:telegram\s*)?topic(?:\s+(?:name|title))?\s+to\s+"
        r"(?P<title>.+?)(?:\s+please)?[?!.]*$",
        re.IGNORECASE,
    ),
)
_FULL_QUOTED_TITLE_RE = re.compile(
    r"^(?:「(?P<corner>[^「」『』\"“”]+)」|"
    r"『(?P<double_corner>[^「」『』\"“”]+)』|"
    r'"(?P<ascii>[^「」『』\"“”]+)"|'
    r"“(?P<curly>[^「」『』\"“”]+)”)$"
)
_NON_IMPERATIVE_TOPIC_RE = re.compile(
    r"(?:為什麼|為何|如何|怎麼(?:樣)?|教我|說明|"
    r"how\s+(?:do|can|should|would)\b|why\b|what\s+if\b)",
    re.IGNORECASE,
)
_UNQUOTED_COMPOUND_TITLE_RE = re.compile(
    r"(?:並且|然後|同時|以及|順便|再幫我|接著|"
    r"並(?=(?:告訴|幫|列|回|整理|摘要|刪|新增|查|說明))|"
    r"\b(?:and|then|also|while|because|after|before|when)\b)",
    re.IGNORECASE,
)
_TITLE_QUOTE_DELIMITER_RE = re.compile(r"[「」『』\"“”]")
_UNQUOTED_TITLE_ATOM_RE = re.compile(r"^[\w\s'’\-]+$", re.UNICODE)
_LINE_BOUNDARY_RE = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def _native_topic_title_rewrite(event: Any) -> str | None:
    """Translate an unambiguous current-Topic rename request to ``/title``.

    The gateway still performs its normal sender authorization after this
    rewrite.  Explanations, hypothetical questions and compound requests stay
    with Grace so this narrow convenience path cannot silently drop intent.
    """
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    original_text = str(getattr(event, "text", "") or "")
    if platform != "telegram" or not thread_id or thread_id == "general":
        return None
    if _LINE_BOUNDARY_RE.search(original_text):
        return None
    raw_text = original_text.strip()
    if not raw_text or raw_text.startswith("/"):
        return None
    if _NON_IMPERATIVE_TOPIC_RE.search(raw_text):
        return None
    text = raw_text
    match = next(
        (candidate for pattern in _DIRECT_TOPIC_RENAME_PATTERNS if (candidate := pattern.fullmatch(text))),
        None,
    )
    if match is None:
        return None

    title_segment = match.group("title").strip()
    quoted_title = _FULL_QUOTED_TITLE_RE.fullmatch(title_segment)
    title = (
        next(value for value in quoted_title.groups() if value is not None).strip()
        if quoted_title
        else title_segment
    )
    if not title or (
        not quoted_title
        and (
            _TITLE_QUOTE_DELIMITER_RE.search(title)
            or not _UNQUOTED_TITLE_ATOM_RE.fullmatch(title)
            or _UNQUOTED_COMPOUND_TITLE_RE.search(title)
        )
    ):
        return None
    return f"/title {title}"


def pre_gateway_dispatch(*, event: Any, **_kwargs: Any) -> dict[str, str] | None:
    """Route narrow native commands; other natural language reaches Grace."""
    text = str(getattr(event, "text", "") or "").strip()
    if not text or text.startswith("/"):
        return None

    native_title = _native_topic_title_rewrite(event)
    if native_title:
        return {"action": "rewrite", "text": native_title}

    lowered = text.lower()
    openclaw_prefixes = ("openclaw:", "openclaw ")
    if lowered.startswith(openclaw_prefixes):
        _, _, rest = text.partition(" ")
        if ":" in text.split(" ", 1)[0]:
            rest = text.split(":", 1)[1]
        rest = rest.strip() or "OpenClaw bridge dry-run"
        return {"action": "rewrite", "text": f"/openclaw-dry-run {rest}"}

    return None


def _should_route_external_browser_work_to_clawops(text: str) -> bool:
    """Conservatively route real external browser work away from Hermes."""
    lowered = (text or "").lower()
    if not lowered:
        return False
    # Keep explanatory questions with Hermes.  They may mention Facebook and
    # posting without asking for browser execution.
    explanation_markers = (
        "說明",
        "為何",
        "為什麼",
        "怎麼",
        "如何",
        "風險",
        "建議",
        "解釋",
        "what is",
        "why",
        "how ",
    )
    if any(marker in lowered for marker in explanation_markers):
        return False

    external_targets = (
        "facebook",
        "fb marketplace",
        "marketplace",
        "社團",
        "交流團",
        "group",
        "商品表單",
    )
    execution_actions = (
        "繼續",
        "執行",
        "刊登",
        "發布",
        "發佈",
        "貼文",
        "上傳",
        "照片",
        "點 next",
        "next",
        "檢查",
        "核對",
        "只讀",
        "post",
        "publish",
        "submit",
        "upload",
        "join",
    )
    return any(target in lowered for target in external_targets) and any(
        action in lowered for action in execution_actions
    )
