"""Deterministic MissionCrew model routing and execution receipt checks."""

from __future__ import annotations

import json
import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_default_hermes_root
from proactive.policy_registry import (
    policy_status,
    resolve_active_policy,
    resolve_policy_snapshot,
)


POLICY_ID = "missioncrew-model-routing-v1"
MODEL_SOL = "gpt-5.6-sol"
MODEL_TERRA = "gpt-5.6-terra"
MODEL_LUNA = "gpt-5.6-luna"
MODEL_MECHANICAL = "gpt-5.5"
MODEL_SPARK = "gpt-5.3-codex-spark"

_EFFORT_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}
ROUTING_ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_REASONING_EFFORT",
    "HERMES_DISABLE_MODEL_FALLBACK",
    "HERMES_MODEL_ROUTING_RECEIPT",
)


class ModelRoutingError(ValueError):
    """Raised when routing policy or a formal execution receipt is invalid."""


def _load_policy() -> tuple[dict[str, Any], dict[str, str]]:
    manifest_path = (
        get_default_hermes_root()
        / "policies"
        / "registry"
        / POLICY_ID
        / "manifest.json"
    )
    status = policy_status(POLICY_ID) if manifest_path.exists() else None
    if status and status.get("active_version"):
        snapshot = resolve_active_policy(POLICY_ID)
        raw_content = snapshot["content"]
        policy_source = "managed_active"
        version = str(snapshot["version"])
        digest = str(snapshot["sha256"])
    else:
        # Bootstrap ordinary routing from the tracked policy artifact so a
        # fresh install can create worker tasks before activation. Formal
        # acceptance rejects this source below; it requires an active registry
        # snapshot and therefore still fails closed.
        policy_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "managed-policies"
            / "missioncrew-model-routing-v1.json"
        )
        raw_content = policy_path.read_text(encoding="utf-8")
        policy_source = "bundled_bootstrap"
        version = "v1"
        digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    try:
        content = json.loads(raw_content)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ModelRoutingError("active model-routing policy is not valid JSON") from exc
    if not isinstance(content, dict) or content.get("policy_id") != POLICY_ID:
        raise ModelRoutingError("active model-routing policy identity mismatch")
    if content.get("pro_mode_enabled") is not False:
        raise ModelRoutingError("Pro mode must remain disabled by policy")
    receipt = {
        "policy_snapshot_id": f"{POLICY_ID}@{version}",
        "policy_id": POLICY_ID,
        "policy_version": version,
        "policy_sha256": digest,
        "policy_source": policy_source,
    }
    return content, receipt


def _decision_fields(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    result = {
        "task_risk": str(raw.get("task_risk") or "low").strip().lower(),
        "ambiguity": str(raw.get("ambiguity") or "low").strip().lower(),
        "memory_impact": str(raw.get("memory_impact") or "none").strip().lower(),
        "external_action": bool(raw.get("external_action", False)),
        "policy_conflict": bool(raw.get("policy_conflict", False)),
        "financial_or_security_impact": bool(
            raw.get("financial_or_security_impact", False)
        ),
        "destructive_or_irreversible": bool(
            raw.get("destructive_or_irreversible", False)
        ),
    }
    if result["task_risk"] not in {"low", "medium", "high", "critical"}:
        raise ModelRoutingError("task_risk is invalid")
    if result["ambiguity"] not in {"low", "medium", "high"}:
        raise ModelRoutingError("ambiguity is invalid")
    if result["memory_impact"] not in {"none", "session", "durable"}:
        raise ModelRoutingError("memory_impact is invalid")
    return result


def classify_grace_message(user_message: str) -> dict[str, Any]:
    """Conservatively classify pre-model risk for one interactive Grace turn."""
    text = str(user_message or "").lower()
    financial = any(
        token in text
        for token in (
            "付款", "轉帳", "匯款", "下單", "交易", "資金", "財務",
            "payment", "transfer", "trade", "financial", "funds",
        )
    )
    security = any(
        token in text
        for token in (
            "密碼", "金鑰", "憑證", "權限", "資安", "安全",
            "password", "api key", "credential", "permission", "security",
        )
    )
    destructive = any(
        token in text
        for token in (
            "刪除", "清空", "覆寫", "重設", "不可逆",
            "delete", "remove", "purge", "overwrite", "reset", "irreversible",
        )
    )
    policy_conflict = any(
        token in text
        for token in (
            "政策衝突", "授權不明", "未授權", "policy conflict",
            "unclear authorization", "unauthorized",
        )
    )
    external_action = any(
        token in text
        for token in (
            "發布", "寄出", "傳送", "上線", "部署",
            "publish", "send", "deploy", "post",
        )
    )
    critical = financial or security or destructive or policy_conflict
    return _decision_fields(
        {
            "task_risk": "high" if critical else "low",
            "ambiguity": "medium" if "?" in text or "？" in text else "low",
            "memory_impact": "none",
            "external_action": external_action,
            "policy_conflict": policy_conflict,
            "financial_or_security_impact": financial or security,
            "destructive_or_irreversible": destructive,
        }
    )


def _route(
    *,
    model: str,
    effort: str,
    reason: str,
    fallback_allowed: bool,
    fields: Mapping[str, Any],
    policy_receipt: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "requested_model": model,
        "effective_model": model,
        "reasoning_effort": effort,
        "effective_reasoning_effort": effort,
        "reasoning_mode": "standard",
        "routing_reason": reason,
        "fallback_allowed": fallback_allowed,
        "fallback_applied": False,
        **dict(policy_receipt),
        "decision_fields": dict(fields),
    }


def _configured_route(
    policy: Mapping[str, Any],
    section: str,
    *,
    default_model: str,
    default_effort: str,
) -> tuple[str, str]:
    value = policy.get(section)
    if not isinstance(value, Mapping):
        raise ModelRoutingError(f"model-routing policy section is missing: {section}")
    model = str(value.get("model") or default_model).strip()
    effort = str(value.get("reasoning") or default_effort).strip().lower()
    if not model or effort not in _EFFORT_RANK:
        raise ModelRoutingError(f"model-routing policy section is invalid: {section}")
    return model, effort


def _configured_grace_route(
    policy: Mapping[str, Any],
    reasoning_key: str,
    default_effort: str,
) -> tuple[str, str]:
    grace = policy.get("grace")
    if not isinstance(grace, Mapping):
        raise ModelRoutingError("model-routing policy grace section is missing")
    model = str(grace.get("model") or MODEL_SOL).strip()
    effort = str(grace.get(reasoning_key) or default_effort).strip().lower()
    if model != MODEL_SOL or effort not in _EFFORT_RANK:
        raise ModelRoutingError("model-routing Grace configuration is invalid")
    return model, effort


def route_grace(
    purpose: str,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Route a Grace decision. Formal decisions fail closed without policy."""
    policy, receipt = _load_policy()
    decision = _decision_fields(fields)
    purpose = str(purpose or "planning").strip().lower()
    critical = (
        decision["task_risk"] in {"high", "critical"}
        or decision["policy_conflict"]
        or decision["financial_or_security_impact"]
        or decision["destructive_or_irreversible"]
    )
    if critical or purpose in {"policy_conflict", "critical_action"}:
        if receipt.get("policy_source") != "managed_active":
            raise ModelRoutingError(
                "formal Grace routing requires an active managed policy"
            )
        model, effort = _configured_grace_route(
            policy, "critical_reasoning", "xhigh"
        )
        return _route(
            model=model,
            effort=effort,
            reason="grace_high_risk_or_policy_conflict",
            fallback_allowed=False,
            fields=decision,
            policy_receipt=receipt,
        )
    if (
        purpose in {"evidence_review", "acceptance_review", "memory_adjudication"}
        or decision["memory_impact"] == "durable"
        or decision["ambiguity"] == "high"
    ):
        if receipt.get("policy_source") != "managed_active":
            raise ModelRoutingError(
                "formal Grace routing requires an active managed policy"
            )
        model, effort = _configured_grace_route(
            policy, "formal_review_reasoning", "high"
        )
        return _route(
            model=model,
            effort=effort,
            reason="grace_formal_review_or_durable_memory",
            fallback_allowed=False,
            fields=decision,
            policy_receipt=receipt,
        )
    model, effort = _configured_grace_route(
        policy, "default_reasoning", "medium"
    )
    return _route(
        model=model,
        effort=effort,
        reason="grace_conversation_or_planning",
        fallback_allowed=True,
        fields=decision,
        policy_receipt=receipt,
    )


def route_worker(
    task_type: str,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Route delegated work; Spark is reserved for focused coding/tool work."""
    policy, receipt = _load_policy()
    decision = _decision_fields(fields)
    workers = policy.get("workers")
    if not isinstance(workers, Mapping):
        raise ModelRoutingError("model-routing policy workers section is missing")
    if decision["policy_conflict"]:
        raise ModelRoutingError("policy conflicts must return to Grace before execution")
    kind = str(task_type or "general").strip().lower().replace("-", "_")
    if (
        decision["task_risk"] in {"high", "critical"}
        or decision["financial_or_security_impact"]
        or decision["destructive_or_irreversible"]
    ):
        model, effort = _configured_route(
            workers,
            "complex",
            default_model=MODEL_MECHANICAL,
            default_effort="high",
        )
        reason = "high_risk_worker_requires_independent_grace_gate"
    elif kind in {
        "focused_code",
        "code_edit",
        "code_format",
        "ui_iteration",
        "tool_driven_coding",
    }:
        model, effort = _configured_route(
            workers,
            "focused_coding",
            default_model=MODEL_SPARK,
            default_effort="low",
        )
        reason = "focused_coding_or_tool_work"
    elif kind in {
        "browser_readonly",
        "browser_read_only",
        "commerce_group_status",
        "data_extraction",
        "facebook_group_readonly",
        "facebook_group_read_only",
        "facebook_marketplace_group_readonly",
        "facebook_marketplace_group_read_only",
        "facebook_marketplace_readonly",
        "facebook_marketplace_read_only",
        "format_conversion",
        "group_status",
        "read_only",
        "readonly",
        "secondhand_commerce_group_status",
        "status_check",
        "classification",
    }:
        model, effort = _configured_route(
            workers,
            "mechanical",
            default_model=MODEL_MECHANICAL,
            default_effort="low",
        )
        reason = "mechanical_high_volume_work"
    elif kind in {
        "code",
        "coding",
        "engineering",
        "software_development",
        "devops",
        "complex_research",
        "complex_code",
        "review",
    }:
        model, effort = _configured_route(
            workers,
            "complex",
            default_model=MODEL_MECHANICAL,
            default_effort="high",
        )
        reason = "complex_worker_or_independent_review"
    else:
        model, effort = _configured_route(
            workers,
            "default",
            default_model=MODEL_MECHANICAL,
            default_effort="medium",
        )
        reason = "general_worker"
    return _route(
        model=model,
        effort=effort,
        reason=reason,
        fallback_allowed=True,
        fields=decision,
        policy_receipt=receipt,
    )


def validate_grace_acceptance_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    expected_route: Mapping[str, Any] | None,
    expected_task_id: str,
) -> None:
    """Require verified Sol High+ and no fallback for formal Grace acceptance."""
    value = dict(receipt or {})
    expected = dict(expected_route or {})
    if value.get("runtime_attested") is not True:
        raise ModelRoutingError("Grace acceptance requires a runtime-attested receipt")
    if not expected_task_id or value.get("task_id") != expected_task_id:
        raise ModelRoutingError("Grace acceptance receipt is not bound to this task")
    if value.get("policy_id") != POLICY_ID or not value.get("policy_sha256"):
        raise ModelRoutingError("Grace acceptance requires a managed-policy receipt")
    if value.get("policy_source") != "managed_active":
        raise ModelRoutingError("Grace acceptance requires an active managed policy")
    for key in ("policy_id", "policy_version", "policy_sha256", "policy_snapshot_id"):
        if not expected.get(key) or value.get(key) != expected.get(key):
            raise ModelRoutingError(
                "Grace acceptance receipt does not match the task policy snapshot"
            )
    snapshot = resolve_policy_snapshot(
        POLICY_ID, str(value.get("policy_version") or "")
    )
    if (
        value.get("policy_version") != snapshot.get("version")
        or value.get("policy_sha256") != snapshot.get("sha256")
    ):
        raise ModelRoutingError("Grace acceptance policy snapshot is not verified")
    if value.get("effective_model") != MODEL_SOL:
        raise ModelRoutingError("Grace acceptance requires gpt-5.6-sol")
    effort = str(value.get("effective_reasoning_effort") or "").lower()
    expected_effort = str(expected.get("reasoning_effort") or "").lower()
    required_rank = max(
        _EFFORT_RANK["high"],
        _EFFORT_RANK.get(expected_effort, len(_EFFORT_RANK)),
    )
    if _EFFORT_RANK.get(effort, -1) < required_rank:
        raise ModelRoutingError(
            "Grace acceptance reasoning is below the task route requirement"
        )
    if value.get("fallback_applied") is not False:
        raise ModelRoutingError("Grace acceptance prohibits fallback")
    if value.get("reasoning_mode") != "standard":
        raise ModelRoutingError("Grace acceptance must use standard reasoning mode")


def routing_env(
    route: Mapping[str, Any],
    *,
    task_id: str | None = None,
) -> dict[str, str]:
    """Compile an immutable per-process route into worker environment values."""
    value = deepcopy(dict(route))
    value["task_id"] = str(task_id or "").strip()
    value["effective_model"] = None
    value["effective_reasoning_effort"] = None
    value["runtime_attested"] = False
    return {
        "HERMES_KANBAN_TASK": value["task_id"],
        "HERMES_REASONING_EFFORT": str(value["reasoning_effort"]),
        "HERMES_DISABLE_MODEL_FALLBACK": "0" if value["fallback_allowed"] else "1",
        "HERMES_MODEL_ROUTING_RECEIPT": json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }


def clear_routing_env(env: dict[str, str]) -> None:
    for key in ROUTING_ENV_KEYS:
        env.pop(key, None)


def attest_runtime_execution(
    *,
    model: str,
    reasoning_effort: str,
    api_mode: str,
) -> dict[str, Any] | None:
    """Attest the actual model request at the Codex transport boundary."""
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw = os.environ.get("HERMES_MODEL_ROUTING_RECEIPT")
    if not task_id or not raw:
        return None
    receipt = execution_receipt_from_env(raw)
    if receipt is None or receipt.get("task_id") != task_id:
        raise ModelRoutingError("runtime model receipt is not bound to the Kanban task")
    receipt["effective_model"] = str(model or "").strip()
    receipt["effective_reasoning_effort"] = str(reasoning_effort or "").strip().lower()
    receipt["effective_api_mode"] = str(api_mode or "").strip()
    receipt["runtime_attested"] = True
    os.environ["HERMES_MODEL_ROUTING_RECEIPT"] = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return receipt


def execution_receipt_from_env(env_value: str | None) -> dict[str, Any] | None:
    if not env_value:
        return None
    try:
        value = json.loads(env_value)
    except json.JSONDecodeError as exc:
        raise ModelRoutingError("model-routing execution receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ModelRoutingError("model-routing execution receipt must be an object")
    return value
