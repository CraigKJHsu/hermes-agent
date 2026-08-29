from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import json
from typing import Any, Iterable, Mapping

import yaml


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
DEFAULT_HUB_OPS_DIR = Path(__file__).resolve().parents[2] / "docs" / "projects" / "hub-ops"
TASK_TYPE_ALIASES = {
    "legal_review": "legal_compliance",
    "compliance_review": "legal_compliance",
    "legal_and_compliance": "legal_compliance",
    "legal_compliance_review": "legal_compliance",
    "contract_review": "legal_compliance",
    "privacy_review": "legal_compliance",
    "platform_compliance": "legal_compliance",
    "listing": "browser_publish",
    "relisting": "browser_publish",
    "re_listing": "browser_publish",
    "republish": "browser_publish",
    "browser_republish": "browser_publish",
    "cross_platform_listing": "browser_publish",
    "secondhand_commerce_cross_platform_listing": "browser_publish",
    "secondhand_commerce_group_status": "research",
    "facebook_existing_listing_group_distribution": "browser_publish",
    "group_distribution": "browser_publish",
}
ALWAYS_APPROVAL_TASK_TYPES = {
    "browser_publish",
    "browser_ops",
    "facebook_page_api_publish",
}
TASK_REQUIRED_CALLABLE_TOOLS = {
    "facebook_page_publish_preflight": frozenset(
        {"facebook_page_publish_preflight"}
    ),
    "facebook_page_api_publish": frozenset(
        {
            "facebook_page_graph_status",
            "facebook_page_graph_publish",
        }
    ),
}
OPENCLAW_FACEBOOK_PAGE_PROFILE = "missioncrew-facebook-page-operator"


def _probe_openclaw_facebook_page_tools(required_tools: set[str]) -> dict[str, Any]:
    """Verify the dedicated agent and bridge plugin declare the exact tools."""
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        agents = ((config.get("agents") or {}).get("list") or [])
        agent = next(
            item
            for item in agents
            if isinstance(item, Mapping)
            and str(item.get("id") or "") == OPENCLAW_FACEBOOK_PAGE_PROFILE
        )
        agent_tools = {
            str(item or "").strip()
            for item in ((agent.get("tools") or {}).get("allow") or [])
        }
        plugin = (
            ((config.get("plugins") or {}).get("entries") or {}).get("hermes-bridge")
            or {}
        )
        plugin_tools = {
            str(item or "").strip()
            for item in ((plugin.get("config") or {}).get("allowedTools") or [])
        }
        available = agent_tools & plugin_tools
        missing = sorted(required_tools - available)
        return {
            "ok": not missing and plugin.get("enabled") is True,
            "available_tools": sorted(available),
            "missing_required_tools": missing,
            "probe_error": "" if not missing else "OpenClaw agent/plugin tool allowlist mismatch",
        }
    except (FileNotFoundError, OSError, ValueError, StopIteration, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "available_tools": [],
            "missing_required_tools": sorted(required_tools),
            "probe_error": f"{type(exc).__name__}: {exc}",
        }


def normalize_clawops_task_type(value: str) -> str:
    """Return a registered task type or a safe legacy alias candidate."""
    normalized = "_".join((value or "").strip().lower().replace("-", "_").split())
    return TASK_TYPE_ALIASES.get(normalized, normalized)


def registered_worker_task_types(
    hub_ops_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Read the canonical model-visible task types from active HubOps routing."""
    docs_dir = Path(hub_ops_dir) if hub_ops_dir else DEFAULT_HUB_OPS_DIR
    rules = _read_yaml(docs_dir / "routing-rules.yaml")
    return _worker_task_types(rules)


def resolved_route_binding(route: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable route fields that affect execution authority."""
    return deepcopy(
        {
            key: route.get(key)
            for key in (
                "project",
                "task_type",
                "risk_level",
                "agent_assignment",
                "assignment",
                "backend_role_card",
                "approval_checklist",
                "output_schema",
            )
        }
    )


def route_requires_owner_approval(route: Mapping[str, Any]) -> bool:
    """Fail closed for every route with controlled capabilities."""
    assignment = route.get("assignment")
    return bool(
        str(route.get("risk_level") or "").lower() in {"high", "critical"}
        or str(route.get("task_type") or "") in ALWAYS_APPROVAL_TASK_TYPES
        or (
            isinstance(assignment, Mapping)
            and assignment.get("approval_required")
        )
    )


def route_clawops_objective(
    objective: str,
    *,
    project: str = "hub_ops",
    task_type: str = "ops",
    risk_level: str = "low",
    approved: bool = False,
    contract_fingerprint: str = "",
    hub_ops_dir: str | Path | None = None,
    runtime_callable_tools: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    clean_objective = " ".join((objective or "").split())
    if not clean_objective:
        return _blocked("objective is required", objective="", project=project, task_type=task_type, risk_level=risk_level)

    docs_dir = Path(hub_ops_dir) if hub_ops_dir else DEFAULT_HUB_OPS_DIR
    try:
        registry = _read_yaml(docs_dir / "agent-registry.yaml")
        rules = _read_yaml(docs_dir / "routing-rules.yaml")
    except (OSError, ValueError) as exc:
        return _blocked(str(exc), objective=clean_objective, project=project, task_type=task_type, risk_level=risk_level)

    worker_routes = rules.get("worker_routes")
    worker_profiles = registry.get("worker_profiles")
    agent_routes = rules.get("routes")
    agents = registry.get("agents")
    if not isinstance(worker_routes, list) or not isinstance(worker_profiles, Mapping):
        return _blocked(
            "HubOps worker routing is not configured.",
            objective=clean_objective,
            project=project,
            task_type=task_type,
            risk_level=risk_level,
        )

    requested_task_type = (task_type or "").strip()
    canonical_task_type = normalize_clawops_task_type(requested_task_type)
    allowed_task_types = _worker_task_types(rules)
    if canonical_task_type not in allowed_task_types:
        return _blocked(
            f"Unsupported task_type={requested_task_type or '<empty>'}. "
            f"Allowed task types: {', '.join(allowed_task_types)}.",
            objective=clean_objective,
            project=project,
            task_type=requested_task_type,
            risk_level=risk_level,
        )

    route = _match_worker_route(
        worker_routes,
        project=project,
        task_type=canonical_task_type,
        risk_level=risk_level,
    )
    if route is None:
        return _blocked(
            "No HubOps worker route matched this task.",
            objective=clean_objective,
            project=project,
            task_type=canonical_task_type,
            risk_level=risk_level,
        )

    assign = route.get("assign") if isinstance(route, Mapping) else None
    worker_id = str((assign or {}).get("worker") or "").strip() if isinstance(assign, Mapping) else ""
    worker = worker_profiles.get(worker_id) if worker_id else None
    if not worker_id or not isinstance(worker, Mapping):
        return _blocked(
            f"HubOps worker profile is missing: {worker_id or '<empty>'}",
            objective=clean_objective,
            project=project,
            task_type=canonical_task_type,
            risk_level=risk_level,
        )

    runtime_profile = str(
        worker.get("runtime_profile") or worker_id.replace(".", "-")
    ).strip()
    required_callable_tools = {
        str(tool or "").strip()
        for tool in worker.get("required_callable_tools") or []
        if str(tool or "").strip()
    }
    required_callable_tools.update(
        TASK_REQUIRED_CALLABLE_TOOLS.get(canonical_task_type, ())
    )
    if required_callable_tools:
        if runtime_callable_tools is None:
            if runtime_profile == OPENCLAW_FACEBOOK_PAGE_PROFILE:
                capability = _probe_openclaw_facebook_page_tools(
                    required_callable_tools
                )
            else:
                from hermes_cli.kanban_db import probe_profile_callable_tools

                capability = probe_profile_callable_tools(
                    profile=runtime_profile,
                    required_tools=sorted(required_callable_tools),
                )
            available_callable_tools = {
                str(tool or "").strip()
                for tool in capability.get("available_tools") or []
                if str(tool or "").strip()
            }
        else:
            capability = {"ok": True}
            available_callable_tools = {
                str(tool or "").strip()
                for tool in runtime_callable_tools.get(runtime_profile, ())
                if str(tool or "").strip()
            }
        missing_callable_tools = sorted(
            required_callable_tools - available_callable_tools
        )
        if missing_callable_tools:
            probe_error = str(capability.get("probe_error") or "").strip()
            return _blocked(
                "Runtime capability admission failed for "
                f"profile={runtime_profile}: missing callable tools: "
                f"{', '.join(missing_callable_tools)}."
                + (f" Probe error: {probe_error}" if probe_error else ""),
                objective=clean_objective,
                project=project,
                task_type=canonical_task_type,
                risk_level=risk_level,
                worker_id=worker_id,
                worker=worker,
                approval_required=True,
            )

    risk = _normalize_risk(risk_level)
    risk_limit = _normalize_risk(str(worker.get("risk_level_limit") or "low"))
    contract_risk_limit = _normalize_risk(
        str(worker.get("contract_risk_level_limit") or risk_limit)
    )
    approval_required = bool((assign or {}).get("approval_required", worker.get("approval_required", False)))
    if risk in {"high", "critical"} and not approved:
        return _blocked(
            f"Human approval is required before routing risk_level={risk} ClawOps work.",
            objective=clean_objective,
            project=project,
            task_type=canonical_task_type,
            risk_level=risk,
            worker_id=worker_id,
            worker=worker,
            approval_required=True,
        )
    risk_authorization: dict[str, Any] = {}
    if RISK_ORDER[risk] > RISK_ORDER[risk_limit]:
        clean_fingerprint = (contract_fingerprint or "").strip()
        if not clean_fingerprint:
            return _blocked(
                f"Task risk_level={risk} exceeds worker risk_level_limit={risk_limit}; "
                "a validated single-Loop-Contract authorization is required.",
                objective=clean_objective,
                project=project,
                task_type=canonical_task_type,
                risk_level=risk,
                worker_id=worker_id,
                worker=worker,
                approval_required=True,
            )
        if RISK_ORDER[risk] > RISK_ORDER[contract_risk_limit]:
            return _blocked(
                f"Task risk_level={risk} exceeds worker contract_risk_level_limit={contract_risk_limit}.",
                objective=clean_objective,
                project=project,
                task_type=canonical_task_type,
                risk_level=risk,
                worker_id=worker_id,
                worker=worker,
                approval_required=True,
            )
        risk_authorization = {
            "mode": "single_loop_contract",
            "issued_by": "Hermes",
            "contract_fingerprint": clean_fingerprint,
            "risk_level": risk,
            "human_approved": True,
            "worker_risk_level_limit": risk_limit,
            "contract_risk_level_limit": contract_risk_limit,
            # This is the authoritative ceiling for this one compiled
            # contract.  The worker_risk_level_limit above remains useful
            # audit context, but must not be mistaken for the effective
            # ceiling after Hermes has issued a scoped elevation.
            "effective_risk_level_limit": risk,
            "reusable": False,
        }
    agent_id = _match_agent_id(
        agent_routes if isinstance(agent_routes, list) else [],
        agents if isinstance(agents, Mapping) else {},
        project=project,
        task_type=canonical_task_type,
        risk_level=risk,
    )

    agent_assignment = _agent_assignment(
        agent_id,
        (agents or {}).get(agent_id) if isinstance(agents, Mapping) else None,
    )
    assignment = _assignment(
        worker_id,
        worker,
        approval_required=approval_required,
        effective_risk_level_limit=(risk if risk_authorization else risk_limit),
    )
    approval_checklist = str(
        (assign or {}).get("approval_checklist") or worker.get("approval_checklist") or ""
    )
    output_schema = worker.get("output_schema") or {}

    return {
        "status": "routed",
        "objective": clean_objective,
        "project": project,
        "task_type": canonical_task_type,
        "requested_task_type": requested_task_type,
        "risk_level": risk,
        "agent_assignment": agent_assignment,
        "assignment": assignment,
        "backend_role_card": _backend_role_card(
            agent_assignment=agent_assignment,
            assignment=assignment,
            worker=worker,
            approval_checklist=approval_checklist,
            output_schema=output_schema,
            task_type=canonical_task_type,
            risk_level=risk,
        ),
        "risk_authorization": risk_authorization,
        "approval_checklist": approval_checklist,
        "output_schema": output_schema,
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"HubOps YAML must be an object: {path}")
    return raw


def _worker_task_types(rules: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    routes = rules.get("worker_routes")
    if not isinstance(routes, list):
        return ()
    for route in routes:
        match = route.get("match") if isinstance(route, Mapping) else None
        task_type = str((match or {}).get("task_type") or "").strip()
        if task_type and task_type not in result:
            result.append(task_type)
    return tuple(result)


def _match_worker_route(
    routes: list[Any],
    *,
    project: str,
    task_type: str,
    risk_level: str,
) -> Mapping[str, Any] | None:
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        match = route.get("match")
        if isinstance(match, Mapping) and _matches(match, project=project, task_type=task_type, risk_level=risk_level):
            return route
    return None


def _match_agent_id(
    routes: list[Any],
    agents: Mapping[str, Any],
    *,
    project: str,
    task_type: str,
    risk_level: str,
) -> str:
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        match = route.get("match")
        if isinstance(match, Mapping) and _matches(match, project=project, task_type=task_type, risk_level=risk_level):
            assign = route.get("assign")
            agent_id = str((assign or {}).get("agent") or "").strip() if isinstance(assign, Mapping) else ""
            if agent_id in agents:
                return agent_id
    return ""


def _matches(match: Mapping[str, Any], *, project: str, task_type: str, risk_level: str) -> bool:
    expected = {
        "project": project,
        "task_type": task_type,
        "risk_level": _normalize_risk(risk_level),
    }
    for key, value in match.items():
        if str(value) != expected.get(str(key), value):
            return False
    return True


def _assignment(
    worker_id: str,
    worker: Mapping[str, Any],
    *,
    approval_required: bool,
    effective_risk_level_limit: str = "",
) -> dict[str, Any]:
    risk_level_limit = _normalize_risk(str(worker.get("risk_level_limit") or "low"))
    return {
        "assigned_worker": worker_id,
        "runtime_profile": str(worker.get("runtime_profile") or worker_id.replace(".", "-")),
        "display_name": str(worker.get("display_name") or worker_id),
        "allowed_tools": list(worker.get("allowed_tools") or []),
        "required_callable_tools": list(
            worker.get("required_callable_tools") or []
        ),
        "risk_level_limit": risk_level_limit,
        "effective_risk_level_limit": _normalize_risk(
            effective_risk_level_limit or risk_level_limit
        ),
        "approval_required": approval_required,
        "approval_required_actions": list(worker.get("approval_required_actions") or []),
        "timeout_seconds": int(worker.get("timeout_seconds") or 900),
        "retry_policy": worker.get("retry_policy") or {"max_attempts": 1, "backoff_seconds": 0},
    }


def _agent_assignment(agent_id: str, agent: Any) -> dict[str, Any]:
    agent = agent if isinstance(agent, Mapping) else {}
    return {
        "assigned_agent": agent_id,
        "display_name": str(agent.get("display_name") or agent_id),
        "role": str(agent.get("role") or ""),
        "primary_model": str(agent.get("primary_model") or ""),
        "fallback_model": str(agent.get("fallback_model") or ""),
        "allowed_projects": list(agent.get("allowed_projects") or []),
        "approval_required": bool(agent.get("approval_required", False)),
    }


def _backend_role_card(
    *,
    agent_assignment: Mapping[str, Any],
    assignment: Mapping[str, Any],
    worker: Mapping[str, Any],
    approval_checklist: str,
    output_schema: Any,
    task_type: str,
    risk_level: str,
) -> dict[str, Any]:
    output = output_schema if isinstance(output_schema, Mapping) else {}
    return {
        "agent_id": str(agent_assignment.get("assigned_agent") or ""),
        "agent_display_name": str(agent_assignment.get("display_name") or ""),
        "agent_role": str(agent_assignment.get("role") or ""),
        "worker_id": str(assignment.get("assigned_worker") or ""),
        "worker_display_name": str(assignment.get("display_name") or ""),
        "worker_role": str(worker.get("role") or ""),
        "runtime_profile": str(assignment.get("runtime_profile") or ""),
        "task_type": task_type,
        "risk_level": risk_level,
        "risk_level_limit": str(assignment.get("risk_level_limit") or ""),
        "effective_risk_level_limit": str(
            assignment.get("effective_risk_level_limit") or ""
        ),
        "approval_required": bool(assignment.get("approval_required")),
        "approval_required_actions": list(
            assignment.get("approval_required_actions") or []
        ),
        "approval_checklist": approval_checklist,
        "output_format": str(output.get("format") or ""),
        "required_sections": list(output.get("required_sections") or []),
        "primary_model": str(agent_assignment.get("primary_model") or ""),
        "fallback_model": str(agent_assignment.get("fallback_model") or ""),
    }


def _blocked(
    reason: str,
    *,
    objective: str,
    project: str,
    task_type: str,
    risk_level: str,
    worker_id: str = "",
    worker: Mapping[str, Any] | None = None,
    approval_required: bool = True,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "objective": objective,
        "project": project,
        "task_type": task_type,
        "risk_level": _normalize_risk(risk_level),
        "blocked_reason": reason,
        "agent_assignment": _agent_assignment("", {}),
        "assignment": _assignment(worker_id, worker or {}, approval_required=approval_required),
        "approval_checklist": str((worker or {}).get("approval_checklist") or ""),
        "output_schema": (worker or {}).get("output_schema") or {},
    }


def _normalize_risk(value: str) -> str:
    risk = (value or "low").strip().lower()
    return risk if risk in RISK_ORDER else "low"
