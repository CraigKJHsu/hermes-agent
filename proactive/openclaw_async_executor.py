"""Real zero-effect asynchronous OpenClaw execution through ClawOps."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.telegram_message_path import (
    actor,
    append_hop,
    backend_projection,
    begin_backend_attempt,
    bind_message_path,
    dumps_message_path,
    merge_message_paths,
    normalize_message_path,
)
from plugins.openclaw_bridge.tools import (
    delegate_loop_contract_to_openclaw,
    delegate_zero_effect_async_to_openclaw,
)
from proactive.execution_backends import (
    ExecutionRequirements,
    route_execution_backend,
)
from proactive.loop_contract import (
    contract_fingerprint,
    is_internal_only_target as _internal_only_external_target,
    validate_loop_contract,
)


ZERO_EFFECT_AGENT = "missioncrew-browser-readonly"
ZERO_EFFECT_WORKSPACE = (
    Path.home()
    / "my_agent_team"
    / "openclaw-workspace"
    / "agents"
    / ZERO_EFFECT_AGENT
)
ZERO_EFFECT_RESULT_TEXT = (
    '{"result":"zero-effect async completed","sideEffectsPerformed":false}'
)
LOOP_CONTRACT_AGENT = "missioncrew-executor"
LOOP_CONTRACT_WORKSPACE = (
    Path.home() / "my_agent_team" / "openclaw-workspace" / "agents" / LOOP_CONTRACT_AGENT
)
LOOP_CONTRACT_AGENT_BY_WORKER = {
    "clawops.browser_readonly": "missioncrew-browser-readonly",
    "clawops.facebook_marketplace_readonly": "missioncrew-browser-readonly",
    "clawops.research": "missioncrew-research",
    "clawops.content": "missioncrew-content",
    "clawops.ops": "missioncrew-ops",
    "clawops.facebook_page_api": "missioncrew-ops",
    "clawops.dev": "missioncrew-devops",
    "clawops.browser": "missioncrew-browser-operator",
    "clawops.facebook_marketplace_group": "missioncrew-browser-operator",
    "clawops.review": "missioncrew-review",
}
LOOP_CONTRACT_AGENT_BY_TASK_TYPE = {
    "browser_readonly": "missioncrew-browser-readonly",
    "facebook_marketplace_readonly": "missioncrew-browser-readonly",
    "secondhand_commerce_group_status": "missioncrew-browser-readonly",
    "research": "missioncrew-research",
    "analytics": "missioncrew-research",
    "content_draft": "missioncrew-content",
    "campaign": "missioncrew-content",
    "product_marketing": "missioncrew-content",
    "ops": "missioncrew-ops",
    "devops": "missioncrew-devops",
    "local_code": "missioncrew-devops",
    "implementation": "missioncrew-devops",
    "deployment": "missioncrew-devops",
    "browser_publish": "missioncrew-browser-operator",
    "browser_ops": "missioncrew-browser-operator",
    "facebook_marketplace_group_publish": "missioncrew-browser-operator",
    "facebook_marketplace_price_update": "missioncrew-browser-operator",
    "risk_review": "missioncrew-review",
    "legal_compliance": "missioncrew-review",
}

_READ_ONLY_EXTERNAL_TARGET_TASK_TYPES = frozenset(
    {
        "content_draft",
        "facebook_marketplace_readonly",
        "secondhand_commerce_group_status",
    }
)


def _openclaw_result_payload(output: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    result = output.get("result")
    if isinstance(result, Mapping):
        return result
    result_text = output.get("resultText")
    if not isinstance(result_text, str) or not result_text.strip():
        return None
    try:
        parsed = json.loads(result_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _loop_external_effect_budget(
    contract: Mapping[str, Any],
    *,
    task_type: str,
) -> int:
    """Keep read-only or local-draft external scope separate from mutation authority."""
    if task_type in _READ_ONLY_EXTERNAL_TARGET_TASK_TYPES:
        return 0
    return sum(
        1
        for target in (contract.get("external_targets") or [])
        if not _internal_only_external_target(target)
    )


def _contract_runtime_tools(contract: Mapping[str, Any]) -> list[str]:
    routing = contract.get("routing")
    resolved = routing.get("resolved") if isinstance(routing, Mapping) else None
    assignment = resolved.get("assignment") if isinstance(resolved, Mapping) else None
    declared = assignment.get("allowed_tools") if isinstance(assignment, Mapping) else []
    if not isinstance(declared, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in declared:
        name = str(item or "").strip()
        if name in {"image_generate"} and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _loop_allowed_tools(
    task_type: str,
    *,
    external_effects: bool,
    contract_tools: Optional[list[str]] = None,
) -> list[str]:
    if external_effects:
        return ["browser"]
    direct_tools = list(contract_tools or [])
    if task_type == "zero_effect_smoke":
        return []
    if task_type in {"browser_publish", "browser_ops", "facebook_page_api_publish"}:
        return ["browser"]
    if task_type in {"local_code", "implementation", "deployment"}:
        return ["read", "write", "edit", "apply_patch", "exec", "process"]
    if task_type in {
        "research",
        "facebook_marketplace_readonly",
            "secondhand_commerce_group_status",
    }:
        return ["read", "web_search", "browser"]
    allowed = ["read", "write", "web_search"]
    for tool in direct_tools:
        if tool not in allowed:
            allowed.append(tool)
    return allowed


def _loop_backend_agent_id(
    contract: Mapping[str, Any],
    *,
    task_type: str,
    external_effects: bool,
) -> str:
    if external_effects:
        return "missioncrew-browser-operator"
    routing = contract.get("routing")
    resolved = routing.get("resolved") if isinstance(routing, Mapping) else None
    assignment = resolved.get("assignment") if isinstance(resolved, Mapping) else None
    if isinstance(assignment, Mapping):
        worker_id = str(assignment.get("assigned_worker") or "").strip()
        if worker_id in LOOP_CONTRACT_AGENT_BY_WORKER:
            return _existing_loop_agent_or_executor(
                LOOP_CONTRACT_AGENT_BY_WORKER[worker_id]
            )
    return _existing_loop_agent_or_executor(
        LOOP_CONTRACT_AGENT_BY_TASK_TYPE.get(task_type, LOOP_CONTRACT_AGENT)
    )


def _loop_workspace(agent_id: str) -> Path:
    return Path.home() / "my_agent_team" / "openclaw-workspace" / "agents" / agent_id


def _existing_loop_agent_or_executor(agent_id: str) -> str:
    """Use specialized OpenClaw agents only when their workspace is installed."""
    clean = str(agent_id or "").strip() or LOOP_CONTRACT_AGENT
    if clean == LOOP_CONTRACT_AGENT or _loop_workspace(clean).exists():
        return clean
    if LOOP_CONTRACT_WORKSPACE.exists():
        return LOOP_CONTRACT_AGENT
    return clean


def _loop_delegation_args(
    run: kb.Run,
    *,
    openclaw_task_id: str,
    idempotency_key: str,
    objective: str,
    start_idempotency_key: str = "",
) -> dict[str, Any]:
    metadata = run.metadata or {}
    contract = dict(metadata.get("loop_contract") or {})
    external_effect_budget = int(metadata.get("external_effect_budget") or 0)
    args = {
        "task_id": run.task_id,
        "objective": objective,
        "context_refs": [f"kanban:{run.task_id}"],
        "allowed_tools": list(metadata.get("allowed_tools") or []),
        "denied_tools": ["message", "gateway", "nodes"],
        "risk_level": str(metadata.get("risk_level") or "medium"),
        "requires_confirmation": False,
        "max_runtime_seconds": int(run.max_runtime_seconds or 900),
        "output_format": "json",
        "audit_required": True,
        "requested_by": "hermes",
        "protocol_version": "2.0",
        "delegation_id": str(metadata["delegation_id"]),
        "attempt_id": str(metadata["attempt_id"]),
        "contract_fingerprint": str(metadata["contract_fingerprint"]),
        "project": str(metadata["project"]),
        "topic_id": str(metadata["topic_id"]),
        "task_type": str(metadata.get("task_type") or "analysis"),
        "executor_backend": "openclaw",
        "executor_profile": "loop-contract",
        "backend_agent_id": str(metadata.get("backend_agent_id") or LOOP_CONTRACT_AGENT),
        "approval_grant_id": str(metadata.get("approval_grant_id") or ""),
        "external_effect_budget": external_effect_budget,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": list(metadata.get("credential_refs") or []),
        "idempotency_key": idempotency_key,
        "openclaw_task_id": openclaw_task_id,
        "dry_run": False,
        "loop_contract": contract,
    }
    message_path = metadata.get("backend_telegram_message_path")
    if isinstance(message_path, Mapping):
        args["message_path"] = dict(message_path)
    if start_idempotency_key:
        args.update({
            "start_idempotency_key": start_idempotency_key,
            "backend_run_id": str(run.backend_run_id or ""),
            "backend_session_key": str(metadata.get("backend_session_key") or ""),
        })
    return args


def _ambiguous_transport_result(result: Mapping[str, Any]) -> bool:
    errors = {
        str(error).strip().lower()
        for error in (result.get("errors") or [])
    }
    return (
        result.get("identity_correlated") is not True
        and (
            bool(errors & {"connection_failed", "timeout"})
            or any(error.startswith("http_5") for error in errors)
        )
    )


def _loop_admission_pending(result: Mapping[str, Any]) -> bool:
    output = next(
        (
            artifact.get("value")
            for artifact in (result.get("artifacts") or [])
            if (
                isinstance(artifact, Mapping)
                and artifact.get("type") == "openclaw_result"
                and isinstance(artifact.get("value"), Mapping)
            )
        ),
        None,
    )
    evidence = output.get("evidence") if isinstance(output, Mapping) else None
    return (
        str(result.get("status") or "").strip().lower() in {"queued", "running"}
        and result.get("identity_correlated") is True
        and result.get("protocol_correlated") is True
        and str(result.get("protocol_version") or "").strip() == "2.0"
        and not str(result.get("backend_run_id") or "").strip()
        and isinstance(evidence, Mapping)
        and evidence.get("admissionPending") is True
        and evidence.get("terminal") is False
    )


def _ambiguous_transport_exception(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _worker_safe_loop_contract(
    contract: Mapping[str, Any],
    *,
    telegram_message_path: Optional[Mapping[str, Any]] = None,
    external_effect_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Keep the approval fingerprint while withholding raw user wording."""
    safe = json.loads(json.dumps(dict(contract), ensure_ascii=False))
    if external_effect_budget == 0:
        safe.pop("external_targets", None)
    else:
        external_targets = [
            target
            for target in (safe.get("external_targets") or [])
            if not _internal_only_external_target(target)
        ]
        if external_targets:
            safe["external_targets"] = external_targets
        else:
            safe.pop("external_targets", None)
    original = str(safe.pop("original_request", "") or "")
    audit = safe.setdefault("audit", {})
    audit["original_request_sha256"] = hashlib.sha256(
        original.encode("utf-8")
    ).hexdigest()
    audit["original_request_location"] = (
        "Grace session history only; not disclosed to OpenClaw"
    )
    projected_path = backend_projection(telegram_message_path)
    if projected_path:
        safe["trace"] = {
            "telegram_message_path": projected_path,
            "visibility": "backend-visible-audit-metadata",
            "raw_user_message": "not_disclosed",
        }
    return safe


def _ensure_loop_contract_routing(
    contract: Mapping[str, Any],
    *,
    task_type: str,
    risk_level: str,
) -> dict[str, Any]:
    normalized = validate_loop_contract(contract)
    routing = normalized.get("routing")
    resolved = routing.get("resolved") if isinstance(routing, Mapping) else None
    role_card = (
        resolved.get("backend_role_card")
        if isinstance(resolved, Mapping)
        else None
    )
    if isinstance(role_card, Mapping):
        return normalized

    from proactive.hubops_routing import (
        resolved_route_binding,
        route_clawops_objective,
    )

    preview = route_clawops_objective(
        str(normalized["goal"]["objective"]),
        project=str(normalized["identity"]["project"]),
        task_type=task_type,
        risk_level=risk_level,
        approved=True,
        contract_fingerprint=contract_fingerprint(normalized),
    )
    if preview.get("status") != "routed":
        return normalized
    enriched = json.loads(json.dumps(normalized, ensure_ascii=False))
    enriched_routing = enriched.setdefault("routing", {})
    if not isinstance(enriched_routing, dict):
        enriched_routing = {}
        enriched["routing"] = enriched_routing
    enriched_routing["resolved"] = resolved_route_binding(preview)
    return validate_loop_contract(enriched)


def _terminal_evidence_digest(result: Mapping[str, Any]) -> str:
    """Hash only immutable terminal identity and backend evidence."""
    return _digest(
        {
            "status": result.get("status"),
            "protocol_version": result.get("protocol_version"),
            "delegation_id": result.get("delegation_id"),
            "attempt_id": result.get("attempt_id"),
            "contract_fingerprint": result.get("contract_fingerprint"),
            "backend_run_id": result.get("backend_run_id"),
            "backend_agent_id": result.get("backend_agent_id"),
            "backend_session_key": result.get("backend_session_key"),
            "artifacts": result.get("artifacts"),
            "requires_human_review": result.get("requires_human_review"),
        }
    )


def _identity_matches(
    result: Mapping[str, Any],
    *,
    delegation_id: str,
    attempt_id: str,
    fingerprint: str,
) -> bool:
    return (
        result.get("identity_correlated") is True
        and result.get("delegation_id") == delegation_id
        and result.get("attempt_id") == attempt_id
        and result.get("contract_fingerprint") == fingerprint
    )


def _delegation_args(
    run: kb.Run,
    *,
    openclaw_task_id: str,
    idempotency_key: str,
    objective: str,
    start_idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    metadata = run.metadata or {}
    args: dict[str, Any] = {
        "task_id": run.task_id,
        "objective": objective,
        "context_refs": [f"kanban:{run.task_id}"],
        "allowed_tools": [],
        "denied_tools": ["*"],
        "risk_level": "low",
        "requires_confirmation": False,
        "max_runtime_seconds": int(run.max_runtime_seconds or 300),
        "output_format": "json",
        "audit_required": True,
        "requested_by": "hermes",
        "protocol_version": "2.0",
        "delegation_id": str(metadata["delegation_id"]),
        "attempt_id": str(metadata["attempt_id"]),
        "contract_fingerprint": str(metadata["contract_fingerprint"]),
        "project": str(metadata["project"]),
        "topic_id": str(metadata["topic_id"]),
        "executor_backend": "openclaw",
        "executor_profile": "zero-effect-async",
        "backend_agent_id": ZERO_EFFECT_AGENT,
        "external_effect_budget": 0,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": [],
        "idempotency_key": idempotency_key,
        "openclaw_task_id": openclaw_task_id,
        "dry_run": False,
    }
    if start_idempotency_key:
        args.update(
            {
                "start_idempotency_key": start_idempotency_key,
                "backend_run_id": str(run.backend_run_id or ""),
            }
        )
    return args


def start_zero_effect_async_acceptance(
    *,
    contract: Mapping[str, Any],
    board: Optional[str] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create, route, and admit one zero-effect OpenClaw async acceptance run."""
    normalized_contract = validate_loop_contract(contract)
    fingerprint = contract_fingerprint(normalized_contract)
    identity = normalized_contract["identity"]
    replaying_admission = False
    run: kb.Run | None = None
    circuit_generation: int | None = None
    task_idempotency_key = (
        f"openclaw-zero-effect:{identity['project']}:"
        f"{identity['request_instance_id']}:{fingerprint}"
    )
    with kb.connect_closing(board=board) as conn:
        existing_row = conn.execute(
            """
            SELECT id
              FROM tasks
             WHERE idempotency_key = ?
               AND status != 'archived'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (task_idempotency_key,),
        ).fetchone()
        if existing_row is not None:
            existing_task_id = str(existing_row["id"])
            existing = kb.get_task(conn, existing_task_id)
            existing_run = kb.latest_run(conn, existing_task_id)
            review_row = conn.execute(
                """
                SELECT id
                  FROM tasks
                 WHERE idempotency_key = ?
                   AND status != 'archived'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (f"{existing_task_id}:zero-effect-review",),
            ).fetchone()
            existing_review_task_id = (
                str(review_row["id"]) if review_row is not None else ""
            )
            metadata = existing_run.metadata if existing_run else {}
            if (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and existing_run.backend_status
                in {"succeeded", "failed", "blocked"}
                and isinstance(metadata, Mapping)
                and isinstance(
                    metadata.get("backend_terminal_observation"),
                    Mapping,
                )
            ):
                observation = dict(
                    metadata["backend_terminal_observation"]
                )
                observation["result_digest"] = existing_run.result_digest
                observation["circuit_generation"] = metadata.get(
                    "circuit_generation"
                )
                handled = make_zero_effect_async_terminal_handler(
                    board=board
                )(existing_run, observation)
                return {
                    "execution_task_id": existing_task_id,
                    "review_task_id": existing_review_task_id,
                    "run_id": existing_run.id,
                    "status": (
                        "succeeded"
                        if handled.get("accepted") is True
                        else "blocked"
                    ),
                    "backend_run_id": existing_run.backend_run_id,
                    "routing_decision": existing_run.routing_decision,
                    "deduplicated": True,
                }
            can_replay_admission = (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and (
                    existing_run.backend_status is None
                    or (
                        existing_run.backend_status == "queued"
                        and not existing_run.backend_run_id
                        and metadata.get("admission_ambiguous") is True
                    )
                )
                and isinstance(metadata, Mapping)
                and isinstance(metadata.get("circuit_generation"), int)
                and not isinstance(metadata.get("circuit_generation"), bool)
                and all(
                    str(metadata.get(key) or "").strip()
                    for key in (
                        "delegation_id",
                        "attempt_id",
                        "contract_fingerprint",
                        "start_idempotency_key",
                    )
                )
            )
            if existing is not None and existing.status not in {"ready", "todo"}:
                if can_replay_admission:
                    run = existing_run
                    run_id = existing_run.id
                    delegation_id = str(metadata["delegation_id"])
                    attempt_id = str(metadata["attempt_id"])
                    start_idempotency_key = str(metadata["start_idempotency_key"])
                    circuit_generation = int(metadata["circuit_generation"])
                    replaying_admission = True
                else:
                    return {
                        "execution_task_id": existing_task_id,
                        "review_task_id": existing_review_task_id,
                        "run_id": existing_run.id if existing_run else None,
                        "status": (
                            existing_run.backend_status
                            if (
                                existing_run is not None
                                and existing_run.backend_status
                                in {
                                    "queued",
                                    "running",
                                    "succeeded",
                                    "failed",
                                    "blocked",
                                }
                            )
                            else (
                                "succeeded"
                                if existing.status == "done"
                                else "blocked"
                            )
                        ),
                        "backend_run_id": (
                            existing_run.backend_run_id if existing_run else None
                        ),
                        "routing_decision": (
                            existing_run.routing_decision
                            if existing_run and existing_run.routing_decision
                            else None
                        ),
                        "deduplicated": True,
                    }
        circuit_states = (
            {}
            if replaying_admission
            else kb.backend_circuit_states(conn)
        )
        if (
            not replaying_admission
            and circuit_states.get("openclaw") == "half_open"
        ):
            if kb.claim_backend_circuit_probe(
                conn,
                "openclaw",
                lease_seconds=(
                    int(
                        normalized_contract["stop_rules"][
                            "max_runtime_seconds"
                        ]
                    )
                    + 30
                ),
            ):
                circuit_states["openclaw"] = "closed"
        routing_decision = route_execution_backend(
            ExecutionRequirements.build(
                capabilities=("isolated_session", "long_running"),
                semantic_class="isolated_long_running",
                risk_level="low",
                credential_policy="agent_scoped",
                workspace_policy="dedicated",
                session_policy="ephemeral",
                max_runtime_seconds=int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                preferred_backend="openclaw",
            ),
            circuit_states=circuit_states,
        )
        if routing_decision["selected_backend"] != "openclaw":
            raise RuntimeError(routing_decision["selection_reason"])
        with kb.write_txn(conn):
            task_id = kb.create_task(
                conn,
                title="OpenClaw zero-effect asynchronous acceptance",
                body=json.dumps(normalized_contract, ensure_ascii=False, indent=2),
                assignee="clawops-ops",
                created_by="grace",
                workspace_kind="dir",
                workspace_path=str(ZERO_EFFECT_WORKSPACE),
                max_runtime_seconds=int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                idempotency_key=task_idempotency_key,
                executor_backend="openclaw",
                executor_profile="zero-effect-async",
                project_namespace=str(identity["project"]),
                routing_decision=routing_decision,
            )
            review_task_id = kb.create_task(
                conn,
                title="Grace review: OpenClaw zero-effect asynchronous acceptance",
                body=(
                    "Verify exact backend identity, terminal transcript, zero tools, "
                    "zero external effects, and ephemeral-session cleanup."
                ),
                assignee="clawops-review",
                created_by="grace",
                parents=[task_id],
                idempotency_key=f"{task_id}:zero-effect-review",
                executor_backend="hermes",
                executor_profile="grace-policy-review",
                project_namespace=str(identity["project"]),
            )
            existing = kb.get_task(conn, task_id)
            if existing is not None and existing.status not in {"ready", "todo"}:
                existing_run = kb.latest_run(conn, task_id)
                metadata = existing_run.metadata if existing_run else {}
                can_replay_admission = (
                    existing.status == "running"
                    and existing_run is not None
                    and existing_run.backend_status is None
                    and isinstance(metadata, Mapping)
                    and isinstance(metadata.get("circuit_generation"), int)
                    and not isinstance(
                        metadata.get("circuit_generation"), bool
                    )
                    and all(
                        str(metadata.get(key) or "").strip()
                        for key in (
                            "delegation_id",
                            "attempt_id",
                            "contract_fingerprint",
                            "start_idempotency_key",
                        )
                    )
                )
                if can_replay_admission:
                    run = existing_run
                    run_id = existing_run.id
                    delegation_id = str(metadata["delegation_id"])
                    attempt_id = str(metadata["attempt_id"])
                    start_idempotency_key = str(metadata["start_idempotency_key"])
                    circuit_generation = int(metadata["circuit_generation"])
                    replaying_admission = True
                else:
                    return {
                        "execution_task_id": task_id,
                        "review_task_id": review_task_id,
                        "run_id": existing_run.id if existing_run else None,
                        "status": (
                            existing_run.backend_status
                            if (
                                existing_run is not None
                                and existing_run.backend_status
                                in {
                                    "queued",
                                    "running",
                                    "succeeded",
                                    "failed",
                                    "blocked",
                                }
                            )
                            else (
                                "succeeded"
                                if existing.status == "done"
                                else "blocked"
                            )
                        ),
                        "backend_run_id": (
                            existing_run.backend_run_id if existing_run else None
                        ),
                        "routing_decision": (
                            existing_run.routing_decision
                            if existing_run and existing_run.routing_decision
                            else routing_decision
                        ),
                        "deduplicated": True,
                    }
            if run is None:
                claimed = kb.claim_task(
                    conn, task_id, claimer="clawops-openclaw-router"
                )
                if claimed is None or claimed.current_run_id is None:
                    raise RuntimeError("Zero-effect OpenClaw task could not be claimed.")
                run_id = int(claimed.current_run_id)
                delegation_id = f"grace:{task_id}"
                attempt_id = f"{task_id}:run:{run_id}"
                start_idempotency_key = f"{attempt_id}:async-start"
                circuit_generation = int(
                    kb.backend_circuit_snapshot(
                        conn,
                        "openclaw",
                    )["generation"]
                )
                if not kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={
                        "delegation_id": delegation_id,
                        "attempt_id": attempt_id,
                        "contract_fingerprint": fingerprint,
                        "project": str(identity["project"]),
                        "topic_id": str(
                            identity.get("thread_id") or identity["topic_name"]
                        ),
                        "executor_profile": "zero-effect-async",
                        "start_idempotency_key": start_idempotency_key,
                        "review_task_id": review_task_id,
                        "circuit_generation": circuit_generation,
                        "external_effect_budget": 0,
                        # Agent/content iterations and transport polls are
                        # different budgets. Runtime_seconds is the durable
                        # wall-clock bound for asynchronous polling.
                        "max_poll_iterations": 0,
                        "no_progress_error_limit": (
                            2
                            if normalized_contract["stop_rules"]["no_progress"]
                            else None
                        ),
                    },
                ):
                    raise RuntimeError(
                        "Zero-effect OpenClaw run correlation could not be saved."
                    )
                run = kb.get_run(conn, run_id)
            if run is None:
                raise RuntimeError(
                    "Zero-effect OpenClaw run disappeared before admission."
                )
            if circuit_generation is None:
                raise RuntimeError(
                    "Zero-effect OpenClaw admission lacks a circuit generation."
                )

    try:
        result = delegate_zero_effect_async_to_openclaw(
            _delegation_args(
                run,
                openclaw_task_id="openclaw.agent.zero_effect_async_start",
                idempotency_key=start_idempotency_key,
                objective="Start the fixed zero-effect asynchronous acceptance task.",
            ),
            transport=transport,
            policy_path=policy_path,
        )
    except Exception as exc:
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error=f"OpenClaw async admission raised: {exc}",
                    expected_generation=circuit_generation,
                )
                if not _ambiguous_transport_exception(exc):
                    kb.block_task(
                        conn,
                        task_id,
                        reason=f"OpenClaw async admission raised: {exc}",
                        kind="capability",
                        expected_run_id=run_id,
                    )
                else:
                    kb.merge_active_run_metadata(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        metadata={"admission_ambiguous": True},
                    )
                    kb.record_backend_lifecycle(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        status="queued",
                        protocol_version="2.0",
                        next_poll_seconds=2,
                    )
                    kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
        return {
            "execution_task_id": task_id,
            "review_task_id": review_task_id,
            "status": (
                "retrying"
                if _ambiguous_transport_exception(exc)
                else "blocked"
            ),
            "deduplicated": replaying_admission,
            "review_errors": [f"OpenClaw async admission raised: {exc}"],
        }
    status = str(result.get("status") or "").strip().lower()
    identity_ok = _identity_matches(
        result,
        delegation_id=delegation_id,
        attempt_id=attempt_id,
        fingerprint=fingerprint,
    )
    backend_run_id = str(result.get("backend_run_id") or "").strip()
    backend_agent_id = str(result.get("backend_agent_id") or "").strip()
    backend_session_key = str(result.get("backend_session_key") or "").strip()
    protocol_version = str(result.get("protocol_version") or "").strip()
    with kb.connect_closing(board=board) as conn:
        if _ambiguous_transport_result(result):
            with kb.write_txn(conn):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error=(
                        "OpenClaw async admission response was ambiguous; "
                        "retrying the same idempotency key."
                    ),
                    expected_generation=circuit_generation,
                )
                kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                )
                kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                kb.renew_external_backend_claim(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": "retrying",
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
            }
        admission_output = next(
            (
                artifact.get("value")
                for artifact in result.get("artifacts") or []
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("type") == "openclaw_result"
                    and isinstance(artifact.get("value"), Mapping)
                )
            ),
            None,
        )
        admission_evidence = (
            admission_output.get("evidence")
            if isinstance(admission_output, Mapping)
            else None
        )
        admission_pending = (
            status in {"queued", "running"}
            and identity_ok
            and not backend_run_id
            and protocol_version == "2.0"
            and result.get("protocol_correlated") is True
            and isinstance(admission_evidence, Mapping)
            and admission_evidence.get("admissionPending") is True
            and admission_evidence.get("terminal") is False
        )
        if admission_pending:
            claim_renewed = False
            with kb.write_txn(conn):
                persisted = kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                ) and kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                if persisted:
                    claim_renewed = kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
            if not persisted:
                kb.block_task(
                    conn,
                    task_id,
                    reason=(
                        "OpenClaw pending admission could not be durably "
                        "scheduled for reconciliation."
                    ),
                    kind="capability",
                    expected_run_id=run_id,
                )
                return {
                    "execution_task_id": task_id,
                    "review_task_id": review_task_id,
                    "status": "blocked",
                    "delegated_result": result,
                }
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": "retrying",
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
                "claim_renewed": claim_renewed,
            }
        if (
            status
            not in {"queued", "running", "succeeded", "failed", "blocked"}
            or not identity_ok
            or not backend_run_id
            or backend_agent_id != ZERO_EFFECT_AGENT
            or not backend_session_key
            or protocol_version != "2.0"
            or result.get("protocol_correlated") is not True
            or not kb.merge_active_run_metadata(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={"backend_session_key": backend_session_key},
            )
            or not kb.record_backend_lifecycle(
                conn,
                task_id,
                expected_run_id=run_id,
                status=status,
                backend_run_id=backend_run_id,
                backend_agent_id=backend_agent_id,
                protocol_version=protocol_version,
                workspace_ref=str(ZERO_EFFECT_WORKSPACE),
                result_digest=(
                    _terminal_evidence_digest(result)
                    if status in {"succeeded", "failed", "blocked"}
                    else _digest(result)
                ),
                next_poll_seconds=(
                    0 if status in {"queued", "running"} else None
                ),
                terminal_observation=(
                    {
                        "status": status,
                        "delegated_result": result,
                    }
                    if status in {"succeeded", "failed", "blocked"}
                    else None
                ),
                terminal_handler_pending=(
                    status in {"succeeded", "failed", "blocked"}
                ),
            )
        ):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error=(
                    "OpenClaw async admission did not return exact correlated "
                    "active evidence."
                ),
                expected_generation=circuit_generation,
            )
            kb.block_task(
                conn,
                task_id,
                reason="OpenClaw async admission did not return exact correlated active evidence.",
                kind="capability",
                expected_run_id=run_id,
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "delegated_result": result,
            }
        if status in {"queued", "running"} and not kb.renew_external_backend_claim(
            conn,
            task_id,
            expected_run_id=run_id,
        ):
            kb.block_task(
                conn,
                task_id,
                reason="OpenClaw async run could not renew its Kanban claim.",
                kind="capability",
                expected_run_id=run_id,
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "delegated_result": result,
            }
        if status in {"queued", "running"}:
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=True,
                expected_generation=circuit_generation,
            )
        if status in {"succeeded", "failed", "blocked"}:
            terminal_run = kb.get_run(conn, run_id)
            if terminal_run is None:
                raise RuntimeError(
                    "Immediate terminal async run disappeared before review."
                )
            terminal_handler = make_zero_effect_async_terminal_handler(
                board=board
            )
            accepted = terminal_handler(
                terminal_run,
                {
                    "status": status,
                    "result_digest": _terminal_evidence_digest(result),
                    "delegated_result": result,
                    "circuit_generation": circuit_generation,
                },
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": (
                    "succeeded"
                    if accepted.get("accepted") is True
                    else "blocked"
                ),
                "backend_run_id": backend_run_id,
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
            }
    return {
        "execution_task_id": task_id,
        "review_task_id": review_task_id,
        "run_id": run_id,
        "status": status,
        "backend_run_id": backend_run_id,
        "routing_decision": routing_decision,
        "delegated_result": result,
        "deduplicated": replaying_admission,
    }


def make_zero_effect_async_poll_adapter(
    *,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> Callable[[kb.Run], Mapping[str, Any]]:
    """Build a restart-safe poll adapter from correlation saved on the run."""

    def poll(run: kb.Run) -> Mapping[str, Any]:
        metadata = run.metadata or {}
        stop_cleanup_pending = bool(
            metadata.get("stop_rule_cleanup_pending")
        )
        start_key = str(metadata.get("start_idempotency_key") or "").strip()
        if not start_key:
            raise ValueError("Async run is missing durable start correlation.")
        if not run.backend_run_id and metadata.get("admission_ambiguous") is True:
            result = delegate_zero_effect_async_to_openclaw(
                _delegation_args(
                    run,
                    openclaw_task_id=(
                        "openclaw.agent.zero_effect_async_start"
                    ),
                    idempotency_key=start_key,
                    objective=(
                        "Reconcile the ambiguous zero-effect asynchronous "
                        "OpenClaw admission."
                    ),
                ),
                transport=transport,
                policy_path=policy_path,
            )
            if _ambiguous_transport_result(result):
                raise TimeoutError(
                    "OpenClaw async admission remains ambiguous."
                )
            status = str(result.get("status") or "").strip().lower()
            if status not in {
                "queued",
                "running",
                "succeeded",
                "failed",
                "blocked",
            }:
                raise ValueError(
                    f"Unexpected OpenClaw admission status={status!r}."
                )
            if not _identity_matches(
                result,
                delegation_id=str(metadata.get("delegation_id") or ""),
                attempt_id=str(metadata.get("attempt_id") or ""),
                fingerprint=str(
                    metadata.get("contract_fingerprint") or ""
                ),
            ):
                raise ValueError(
                    "OpenClaw admission replay identity did not match."
                )
            backend_run_id = str(
                result.get("backend_run_id") or ""
            ).strip()
            backend_agent_id = str(
                result.get("backend_agent_id") or ""
            ).strip()
            backend_session_key = str(
                result.get("backend_session_key") or ""
            ).strip()
            admission_output = next(
                (
                    artifact.get("value")
                    for artifact in result.get("artifacts") or []
                    if (
                        isinstance(artifact, Mapping)
                        and artifact.get("type") == "openclaw_result"
                        and isinstance(artifact.get("value"), Mapping)
                    )
                ),
                None,
            )
            admission_evidence = (
                admission_output.get("evidence")
                if isinstance(admission_output, Mapping)
                else None
            )
            if (
                status in {"queued", "running"}
                and not backend_run_id
                and result.get("protocol_version") == "2.0"
                and result.get("protocol_correlated") is True
                and isinstance(admission_evidence, Mapping)
                and admission_evidence.get("admissionPending") is True
                and admission_evidence.get("terminal") is False
            ):
                return {
                    "status": "queued",
                    "protocol_version": "2.0",
                    "result_digest": _digest(result),
                    "delegated_result": result,
                }
            if (
                status in {"failed", "blocked"}
                and not backend_run_id
                and result.get("protocol_version") == "2.0"
                and result.get("protocol_correlated") is True
            ):
                return {
                    "status": status,
                    "protocol_version": "2.0",
                    "result_digest": _terminal_evidence_digest(result),
                    "delegated_result": result,
                }
            if (
                result.get("protocol_correlated") is not True
                or not backend_run_id
                or backend_agent_id != ZERO_EFFECT_AGENT
                or not backend_session_key
            ):
                raise ValueError(
                    "OpenClaw admission replay returned unauthorized or "
                    "incomplete backend evidence."
                )
            return {
                "status": status,
                "backend_run_id": backend_run_id,
                "backend_agent_id": backend_agent_id,
                "backend_session_key": backend_session_key,
                "protocol_version": result.get("protocol_version"),
                "result_digest": (
                    _terminal_evidence_digest(result)
                    if status in {"succeeded", "failed", "blocked"}
                    else _digest(result)
                ),
                "delegated_result": result,
            }
        if not run.backend_run_id:
            raise ValueError("Async run is missing durable backend run identity.")
        poll_key = f"{start_key}:poll:{run.backend_poll_count + 1}"
        result = delegate_zero_effect_async_to_openclaw(
            _delegation_args(
                run,
                openclaw_task_id=(
                    "openclaw.agent.zero_effect_async_cancel"
                    if stop_cleanup_pending
                    else "openclaw.agent.zero_effect_async_poll"
                ),
                idempotency_key=poll_key,
                objective=(
                    "Cancel and clean up the exact zero-effect asynchronous "
                    "OpenClaw run."
                    if stop_cleanup_pending
                    else (
                        "Poll the exact zero-effect asynchronous OpenClaw run."
                    )
                ),
                start_idempotency_key=start_key,
            ),
            transport=transport,
            policy_path=policy_path,
        )
        status = str(result.get("status") or "").strip().lower()
        if status not in {"queued", "running", "succeeded", "failed", "blocked"}:
            raise ValueError(f"Unexpected OpenClaw async poll status={status!r}.")
        if not _identity_matches(
            result,
            delegation_id=str(metadata.get("delegation_id") or ""),
            attempt_id=str(metadata.get("attempt_id") or ""),
            fingerprint=str(metadata.get("contract_fingerprint") or ""),
        ):
            raise ValueError("OpenClaw async poll identity did not match its Kanban run.")
        returned_backend_run_id = str(result.get("backend_run_id") or "")
        returned_session_key = str(result.get("backend_session_key") or "")
        expected_session_key = str(metadata.get("backend_session_key") or "")
        if (
            returned_backend_run_id
            and returned_backend_run_id != run.backend_run_id
        ) or (
            status in {"queued", "running", "succeeded"}
            and returned_backend_run_id != run.backend_run_id
        ):
            raise ValueError("OpenClaw async poll returned a different backend run.")
        if (
            returned_session_key
            and returned_session_key != expected_session_key
        ) or (
            status in {"queued", "running", "succeeded"}
            and returned_session_key != expected_session_key
        ):
            raise ValueError("OpenClaw async poll returned a different backend session.")
        return {
            "status": status,
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": result.get("backend_agent_id"),
            "protocol_version": result.get("protocol_version"),
            "result_digest": (
                _terminal_evidence_digest(result)
                if status in {"succeeded", "failed", "blocked"}
                else _digest(result)
            ),
            "delegated_result": result,
            "stop_cleanup_pending": stop_cleanup_pending,
        }

    return poll


def make_zero_effect_async_terminal_handler(
    *,
    board: Optional[str] = None,
) -> Callable[[kb.Run, Mapping[str, Any]], Mapping[str, Any]]:
    """Build the evidence gate that closes execution and Grace review tasks."""

    def handle(run: kb.Run, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        circuit_generation_value = observation.get("circuit_generation")
        circuit_generation = (
            int(circuit_generation_value)
            if circuit_generation_value is not None
            else None
        )
        result = observation.get("delegated_result")
        if not isinstance(result, Mapping):
            raise ValueError("Terminal observation is missing delegated result evidence.")
        output = next(
            (
                artifact.get("value")
                for artifact in result.get("artifacts") or []
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("type") == "openclaw_result"
                    and isinstance(artifact.get("value"), Mapping)
                )
            ),
            None,
        )
        evidence = output.get("evidence") if isinstance(output, Mapping) else None
        valid = (
            observation.get("status") == "succeeded"
            and result.get("errors") in (None, [])
            and result.get("requires_human_review") is False
            and result.get("protocol_correlated") is True
            and result.get("backend_session_key")
            == (run.metadata or {}).get("backend_session_key")
            and isinstance(evidence, Mapping)
            and evidence.get("externalEffectBudget") == 0
            and evidence.get("sideEffectsPerformed") is False
            and evidence.get("toolsAllowed") == []
            and evidence.get("terminal") is True
            and evidence.get("sessionCleaned") is True
            and output.get("resultText") == ZERO_EFFECT_RESULT_TEXT
        )
        metadata = run.metadata or {}
        review_task_id = str(metadata.get("review_task_id") or "")
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                current_run = kb.get_run(conn, run.id)
                if current_run is None:
                    raise RuntimeError(
                        "Async execution run disappeared before terminal review."
                    )
                current_metadata = current_run.metadata or metadata
                if not valid:
                    kb.record_backend_circuit_outcome(
                        conn,
                        "openclaw",
                        succeeded=False,
                        error="Zero-effect async terminal evidence failed review.",
                        expected_generation=circuit_generation,
                    )
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=(
                            "Zero-effect async terminal evidence failed Grace review."
                        ),
                        kind="capability",
                        expected_run_id=run.id,
                    )
                    return {"accepted": False}
                completed = kb.complete_task(
                    conn,
                    run.task_id,
                    result="OpenClaw zero-effect asynchronous acceptance passed.",
                    summary=(
                        "OpenClaw progressed asynchronously to terminal, returned "
                        "zero-tool/zero-effect evidence, and cleaned its session."
                    ),
                    metadata={
                        **current_metadata,
                        "backend_run_id": run.backend_run_id,
                        "result_digest": observation.get("result_digest"),
                        "side_effects_performed": False,
                        "terminal": True,
                    },
                    expected_run_id=run.id,
                )
                if not completed:
                    raise RuntimeError(
                        "Async execution changed before terminal completion."
                    )
                review = kb.claim_task(
                    conn,
                    review_task_id,
                    claimer="grace-policy-review",
                )
                if review is None or review.current_run_id is None:
                    raise RuntimeError("Grace async review task could not be claimed.")
                if not kb.complete_task(
                    conn,
                    review_task_id,
                    result="accepted",
                    summary=(
                        "Grace accepted exact backend identity, zero tools, zero "
                        "external effects, terminal evidence, and cleanup."
                    ),
                    metadata={
                        "reviewed_execution_task_id": run.task_id,
                        "backend_run_id": run.backend_run_id,
                        "checks": [
                            "backend_identity",
                            "zero_tools",
                            "zero_external_effects",
                            "terminal_transcript",
                            "ephemeral_cleanup",
                        ],
                    },
                    expected_run_id=int(review.current_run_id),
                ):
                    raise RuntimeError("Grace async review changed before completion.")
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=True,
                    expected_generation=circuit_generation,
                )
        return {"accepted": True, "review_task_id": review_task_id}

    return handle


def _admit_loop_contract_run(
    *,
    run: kb.Run,
    review_task_id: str,
    objective: str,
    routing_decision: Mapping[str, Any],
    board: Optional[str],
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]],
    policy_path: Optional[str],
    deduplicated: bool,
) -> dict[str, Any]:
    """Admit or idempotently replay one already-correlated Loop Contract run."""
    metadata = run.metadata or {}
    task_id = run.task_id
    run_id = run.id
    delegation_id = str(metadata["delegation_id"])
    attempt_id = str(metadata["attempt_id"])
    fingerprint = str(metadata["contract_fingerprint"])
    start_key = str(metadata["start_idempotency_key"])
    try:
        result = delegate_loop_contract_to_openclaw(
            _loop_delegation_args(
                run,
                openclaw_task_id="openclaw.agent.loop_contract_start",
                idempotency_key=start_key,
                objective=objective,
            ),
            transport=transport,
            policy_path=policy_path,
        )
    except Exception as exc:
        ambiguous = _ambiguous_transport_exception(exc)
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                if ambiguous:
                    kb.merge_active_run_metadata(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        metadata={"admission_ambiguous": True},
                    )
                    kb.record_backend_lifecycle(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        status="queued",
                        protocol_version="2.0",
                        next_poll_seconds=2,
                    )
                    kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
                else:
                    kb.block_task(
                        conn,
                        task_id,
                        reason=f"OpenClaw Loop Contract admission raised: {exc}",
                        kind="capability",
                        expected_run_id=run_id,
                    )
        return {
            "execution_task_id": task_id,
            "review_task_id": review_task_id,
            "run_id": run_id,
            "status": "retrying" if ambiguous else "blocked",
            "deduplicated": deduplicated,
        }
    if _ambiguous_transport_result(result) or _loop_admission_pending(result):
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                )
                kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                kb.renew_external_backend_claim(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                )
        return {
            "execution_task_id": task_id,
            "review_task_id": review_task_id,
            "run_id": run_id,
            "status": "retrying",
            "routing_decision": dict(routing_decision),
            "delegated_result": result,
            "deduplicated": deduplicated,
        }
    status = str(result.get("status") or "").strip().lower()
    backend_run_id = str(result.get("backend_run_id") or "").strip()
    backend_agent_id = str(result.get("backend_agent_id") or "").strip()
    backend_session_key = str(result.get("backend_session_key") or "").strip()
    protocol_version = str(result.get("protocol_version") or "").strip()
    valid = (
        status in {"queued", "running", "succeeded"}
        and _identity_matches(
            result,
            delegation_id=delegation_id,
            attempt_id=attempt_id,
            fingerprint=fingerprint,
        )
        and backend_run_id
        and backend_agent_id == str(metadata.get("backend_agent_id") or LOOP_CONTRACT_AGENT)
        and backend_session_key
        and protocol_version == "2.0"
        and result.get("protocol_correlated") is True
    )
    with kb.connect_closing(board=board) as conn:
        with kb.write_txn(conn):
            if not valid:
                kb.block_task(
                    conn,
                    task_id,
                    reason="OpenClaw Loop Contract admission returned incomplete or uncorrelated evidence.",
                    kind="capability",
                    expected_run_id=run_id,
                )
                return {
                    "execution_task_id": task_id,
                    "review_task_id": review_task_id,
                    "status": "blocked",
                    "delegated_result": result,
                }
            admitted_path = bind_message_path(
                merge_message_paths(
                    kb.telegram_message_path_for_task(conn, task_id),
                    metadata.get("telegram_message_path"),
                ),
                openclaw_backend_agent_id=backend_agent_id,
                openclaw_backend_run_id=backend_run_id,
                openclaw_backend_session_key=backend_session_key,
            )
            if admitted_path:
                admitted_path = append_hop(
                    admitted_path,
                    stage="openclaw_execution",
                    from_actor=actor("clawops", "clawops"),
                    to_actor=actor(backend_agent_id, "openclaw_backend"),
                    status="observed",
                    identifiers={
                        "backend_run_id": backend_run_id,
                        "backend_session_key": backend_session_key,
                    },
                )
                conn.execute(
                    "UPDATE grace_delegations SET telegram_message_path = ?, updated_at = ? "
                    "WHERE delegation_id = ?",
                    (
                        dumps_message_path(admitted_path),
                        int(time.time()),
                        delegation_id,
                    ),
                )
            kb.merge_active_run_metadata(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "backend_session_key": backend_session_key,
                    "backend_agent_id": backend_agent_id,
                    "admission_ambiguous": False,
                    **(
                        {
                            "telegram_message_path": admitted_path,
                            "backend_telegram_message_path": backend_projection(
                                admitted_path
                            ),
                        }
                        if admitted_path
                        else {}
                    ),
                },
            )
            kb.record_backend_lifecycle(
                conn,
                task_id,
                expected_run_id=run_id,
                status=status,
                backend_run_id=backend_run_id,
                backend_agent_id=backend_agent_id,
                protocol_version=protocol_version,
                workspace_ref=str(_loop_workspace(backend_agent_id)),
                result_digest=_digest(result),
                next_poll_seconds=0,
            )
            kb.renew_external_backend_claim(conn, task_id, expected_run_id=run_id)
    terminal_review: Mapping[str, Any] | None = None
    if status == "succeeded":
        with kb.connect_closing(board=board) as conn:
            terminal_run = kb.get_run(conn, run_id)
        if terminal_run is None:
            raise RuntimeError("Synchronous OpenClaw run disappeared before validation.")
        terminal_review = make_loop_contract_terminal_handler(board=board)(
            terminal_run,
            {
                "status": status,
                "backend_run_id": backend_run_id,
                "backend_agent_id": backend_agent_id,
                "protocol_version": protocol_version,
                "result_digest": _terminal_evidence_digest(result),
                "delegated_result": result,
            },
        )
        if terminal_review.get("accepted") is not True:
            status = "blocked"
    return {
        "execution_task_id": task_id,
        "review_task_id": review_task_id,
        "run_id": run_id,
        "status": status,
        "backend_run_id": backend_run_id,
        "routing_decision": dict(routing_decision),
        "delegated_result": result,
        "deduplicated": deduplicated,
        **({"terminal_review": dict(terminal_review)} if terminal_review else {}),
    }


def start_loop_contract_execution(
    *,
    contract: Mapping[str, Any],
    task_type: str,
    risk_level: str,
    approved: bool,
    delegation_id: str,
    delegation_build_owner: str = "",
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
    user_id: str = "",
    session_key: str = "",
    session_id: str = "",
    message_id: str = "",
    notifier_profile: str = "",
    callback_lease_owner: str = "",
    telegram_message_path: Optional[Mapping[str, Any]] = None,
    board: Optional[str] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create and admit the canonical Grace execution card in OpenClaw."""
    task_type = str(task_type or "analysis").strip()
    risk_level = str(risk_level or "medium").strip().lower()
    normalized = _ensure_loop_contract_routing(
        contract,
        task_type=task_type,
        risk_level=risk_level,
    )
    fingerprint = contract_fingerprint(normalized)
    identity = normalized["identity"]
    external_effect_budget = _loop_external_effect_budget(
        normalized,
        task_type=task_type,
    )
    if external_effect_budget and not approved:
        raise ValueError("External-effect OpenClaw execution requires scoped approval.")
    contract_tools = _contract_runtime_tools(normalized)
    allowed_tools = _loop_allowed_tools(
        task_type,
        external_effects=external_effect_budget > 0,
        contract_tools=contract_tools,
    )
    backend_agent_id = _loop_backend_agent_id(
        normalized,
        task_type=task_type,
        external_effects=external_effect_budget > 0,
    )
    # Imported lazily to avoid the compiler/executor module cycle while keeping
    # one canonical worker-safe contract and Grace acceptance prompt.
    from proactive.grace_task_compiler import render_execution_body, render_review_body
    idempotency_key = f"openclaw-loop:{delegation_id}:{fingerprint}"
    with kb.connect_closing(board=board) as conn:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            existing_task_id = str(existing["id"])
            existing_task = kb.get_task(conn, existing_task_id)
            existing_run = kb.latest_run(conn, existing_task_id)
            review = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? LIMIT 1",
                (f"{idempotency_key}:review",),
            ).fetchone()
            existing_review_task_id = str(review["id"]) if review else ""
            existing_metadata = existing_run.metadata if existing_run else {}
            replayable = bool(
                existing_task
                and existing_task.status == "running"
                and existing_run
                and not existing_run.backend_run_id
                and isinstance(existing_metadata, Mapping)
                and existing_metadata.get("start_idempotency_key")
                and existing_metadata.get("attempt_id")
                and existing_metadata.get("contract_fingerprint") == fingerprint
                and existing_metadata.get("delegation_id") == delegation_id
            )
            if replayable:
                return _admit_loop_contract_run(
                    run=existing_run,
                    review_task_id=existing_review_task_id,
                    objective=normalized["goal"]["objective"],
                    routing_decision=existing_run.routing_decision or {},
                    board=board,
                    transport=transport,
                    policy_path=policy_path,
                    deduplicated=True,
                )
            return {
                "execution_task_id": existing_task_id,
                "review_task_id": existing_review_task_id,
                "run_id": existing_run.id if existing_run else None,
                "status": (
                    "succeeded"
                    if existing_task and existing_task.status == "done"
                    else (existing_task.status if existing_task else "blocked")
                ),
                "backend_run_id": existing_run.backend_run_id if existing_run else None,
                "deduplicated": True,
            }
    with kb.connect_closing(board=board) as conn:
        with kb.write_txn(conn):
            existing = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                task_id = str(existing["id"])
                task = kb.get_task(conn, task_id)
                run = kb.latest_run(conn, task_id)
                review = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ? LIMIT 1",
                    (f"{idempotency_key}:review",),
                ).fetchone()
                return {
                    "execution_task_id": task_id,
                    "review_task_id": str(review["id"]) if review else "",
                    "run_id": run.id if run else None,
                    "status": "succeeded" if task and task.status == "done" else (task.status if task else "blocked"),
                    "backend_run_id": run.backend_run_id if run else None,
                    "deduplicated": True,
                }
            routing_decision = route_execution_backend(
                ExecutionRequirements.build(
                    capabilities=tuple(
                        ["isolated_session", "long_running"]
                        + contract_tools
                        + (["browser_write"] if external_effect_budget else [])
                    ),
                    semantic_class=("browser_write" if external_effect_budget else "isolated_long_running"),
                    risk_level=risk_level,
                    credential_policy="agent_scoped",
                    workspace_policy="dedicated",
                    session_policy="ephemeral",
                    max_runtime_seconds=int(normalized["stop_rules"]["max_runtime_seconds"]),
                    preferred_backend="openclaw",
                )
            )
            if routing_decision.get("selected_backend") != "openclaw":
                raise RuntimeError(
                    json.dumps(routing_decision, ensure_ascii=False, sort_keys=True)
                )
            task_id = kb.create_task(
                conn,
                title=f"OpenClaw: {normalized['goal']['objective'][:90]}",
                body=render_execution_body(normalized),
                assignee="openclaw",
                created_by="grace-loop-compiler",
                workspace_kind="dir",
                workspace_path=str(LOOP_CONTRACT_WORKSPACE),
                max_runtime_seconds=int(normalized["stop_rules"]["max_runtime_seconds"]),
                goal_mode=True,
                goal_max_turns=min(
                    20, int(normalized["stop_rules"]["max_iterations"])
                ),
                idempotency_key=idempotency_key,
                executor_backend="openclaw",
                executor_profile="loop-contract",
                project_namespace=str(identity["project"]),
                routing_decision=routing_decision,
            )
            review_task_id = kb.create_task(
                conn,
                title=f"Grace review: {normalized['goal']['objective'][:78]}",
                body=render_review_body(normalized, task_id),
                assignee="default",
                created_by="grace-loop-compiler",
                parents=[task_id],
                workspace_kind="scratch",
                max_runtime_seconds=min(
                    1800, int(normalized["stop_rules"]["max_runtime_seconds"])
                ),
                goal_mode=True,
                goal_max_turns=min(
                    8, int(normalized["stop_rules"]["max_iterations"])
                ),
                session_id=f"grace-loop:{delegation_id}:review",
                idempotency_key=f"{idempotency_key}:review",
                executor_backend="hermes",
                executor_profile="grace-policy-review",
                project_namespace=str(identity["project"]),
            )
            if platform and chat_id:
                for notification_task_id in (task_id, review_task_id):
                    kb.add_notify_sub(
                        conn,
                        task_id=notification_task_id,
                        platform=platform.strip().lower(),
                        chat_id=chat_id.strip(),
                        thread_id=thread_id.strip() or None,
                        user_id=user_id.strip() or None,
                        notifier_profile=notifier_profile.strip() or None,
                    )
                kb.add_grace_loop_callback(
                    conn,
                    review_task_id=review_task_id,
                    execution_task_id=task_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    session_key=session_key,
                    session_id=session_id,
                    message_id=message_id,
                    notifier_profile=notifier_profile.strip() or None,
                    contract_fingerprint=fingerprint,
                    completion_mode=normalized["completion_mode"],
                    objective_id=str(
                        (normalized.get("objective_ref") or {}).get("objective_id") or ""
                    ),
                    stage_key=str(
                        (normalized.get("objective_ref") or {}).get("stage_key") or ""
                    ),
                )
                if not delegation_build_owner:
                    raise RuntimeError(
                        "OpenClaw control-plane admission requires its delegation build owner."
                    )
                kb.mark_grace_delegation_queued(
                    conn,
                    delegation_id=delegation_id,
                    build_owner=delegation_build_owner,
                    execution_task_id=task_id,
                    review_task_id=review_task_id,
                    callback_lease_owner=callback_lease_owner,
                )
            claimed = kb.claim_task(conn, task_id, claimer="openclaw-loop-router")
            if claimed is None or claimed.current_run_id is None:
                raise RuntimeError("OpenClaw Loop Contract card could not be claimed.")
            run_id = int(claimed.current_run_id)
            attempt_id = f"{task_id}:run:{run_id}"
            start_key = f"{attempt_id}:start"
            metadata = {
                "delegation_id": delegation_id,
                "attempt_id": attempt_id,
                "contract_fingerprint": fingerprint,
                "project": str(identity["project"]),
                "topic_id": str(identity.get("thread_id") or identity["topic_name"]),
                "task_type": task_type,
                "risk_level": risk_level,
                "executor_profile": "loop-contract",
                "approval_grant_id": delegation_id if approved else "",
                "external_effect_budget": external_effect_budget,
                "allowed_tools": allowed_tools,
                "backend_agent_id": backend_agent_id,
                "credential_refs": (["hermes-controlled-browser"] if external_effect_budget else []),
                "loop_contract": {},
                "start_idempotency_key": start_key,
                "review_task_id": review_task_id,
                # Do not cancel a healthy asynchronous run merely because it
                # needed more transport polls than agent/content iterations.
                "max_poll_iterations": 0,
            }
            canonical_path = (
                kb.telegram_message_path_for_task(conn, task_id)
                or normalize_message_path(telegram_message_path)
            )
            if canonical_path:
                canonical_path = bind_message_path(
                    canonical_path,
                    delegation_id=delegation_id,
                    execution_task_id=task_id,
                    review_task_id=review_task_id,
                    run_id=run_id,
                    openclaw_backend_agent_id=backend_agent_id,
                )
                canonical_path = append_hop(
                    canonical_path,
                    stage="openclaw_admission_attempt",
                    from_actor=actor("clawops", "clawops"),
                    to_actor=actor(backend_agent_id, "openclaw_backend"),
                    status="attempted",
                    identifiers={
                        "execution_task_id": task_id,
                        "run_id": run_id,
                    },
                )
                metadata["telegram_message_path"] = canonical_path
                metadata["backend_telegram_message_path"] = backend_projection(
                    canonical_path
                )
                conn.execute(
                    "UPDATE grace_delegations SET telegram_message_path = ?, updated_at = ? "
                    "WHERE delegation_id = ?",
                    (dumps_message_path(canonical_path), int(time.time()), delegation_id),
                )
            metadata["loop_contract"] = _worker_safe_loop_contract(
                normalized,
                telegram_message_path=canonical_path,
                external_effect_budget=external_effect_budget,
            )
            if not kb.merge_active_run_metadata(
                conn, task_id, expected_run_id=run_id, metadata=metadata
            ):
                raise RuntimeError("OpenClaw Loop Contract correlation could not be saved.")
            run = kb.get_run(conn, run_id)
    if run is None:
        raise RuntimeError("OpenClaw Loop Contract run disappeared before admission.")
    return _admit_loop_contract_run(
        run=run,
        review_task_id=review_task_id,
        objective=normalized["goal"]["objective"],
        routing_decision=routing_decision,
        board=board,
        transport=transport,
        policy_path=policy_path,
        deduplicated=False,
    )


def _loop_contract_from_execution_body(body: str) -> dict[str, Any]:
    """Read the canonical contract embedded by ``render_execution_body``."""
    marker = "```json"
    start = str(body or "").find(marker)
    if start < 0:
        raise ValueError("OpenClaw correction card has no embedded Loop Contract.")
    start += len(marker)
    end = str(body).find("```", start)
    if end < 0:
        raise ValueError("OpenClaw correction card has an unterminated Loop Contract.")
    parsed = json.loads(str(body)[start:end].strip())
    if not isinstance(parsed, Mapping):
        raise ValueError("OpenClaw correction Loop Contract must be an object.")
    return dict(parsed)


def retry_ready_loop_contract_execution(
    task_id: str,
    *,
    board: Optional[str] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> dict[str, Any]:
    """Re-admit one Grace-rejected OpenClaw card without creating a new card."""
    with kb.connect_closing(board=board) as conn:
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if task is None:
                raise ValueError(f"Unknown OpenClaw correction task: {task_id}")
            replaying_admission = False
            if task.status not in {"ready", "running"}:
                run = kb.latest_run(conn, task_id)
                return {
                    "execution_task_id": task_id,
                    "run_id": run.id if run else None,
                    "status": task.status,
                    "deduplicated": True,
                }
            if task.executor_backend != "openclaw" or task.executor_profile != "loop-contract":
                raise ValueError(f"Task {task_id} is not an OpenClaw Loop Contract card.")
            previous_run = kb.latest_run(conn, task_id)
            previous_metadata = previous_run.metadata if previous_run else {}
            if not isinstance(previous_metadata, Mapping):
                previous_metadata = {}
            if task.status == "running":
                replaying_admission = bool(
                    previous_run
                    and previous_run.id == task.current_run_id
                    and not previous_run.backend_run_id
                    and previous_metadata.get("correction_admission") is True
                    and previous_metadata.get("start_idempotency_key")
                )
                if not replaying_admission:
                    return {
                        "execution_task_id": task_id,
                        "run_id": previous_run.id if previous_run else None,
                        "status": task.status,
                        "deduplicated": True,
                    }
            correction = conn.execute(
                """
                SELECT payload
                  FROM task_events
                 WHERE task_id = ? AND kind = 'grace_correction_requested'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if correction is None:
                raise ValueError(f"Task {task_id} has no Grace correction request.")
            correction_payload = json.loads(correction["payload"] or "{}")
            external_effect_budget = int(
                previous_metadata.get("external_effect_budget") or 0
            )
            if external_effect_budget:
                raise ValueError(
                    "Grace correction auto-retry is limited to zero-effect Loop Contracts; "
                    "external-effect work requires a fresh scoped approval."
                )
            correction_reason = str(correction_payload.get("reason") or "").strip()
            if not correction_reason:
                raise ValueError("OpenClaw correction requires non-empty Grace evidence.")
            embedded_contract = _loop_contract_from_execution_body(task.body)
            parent_contract = previous_metadata.get(
                "parent_loop_contract",
                previous_metadata.get("loop_contract"),
            )
            latest_contract = previous_metadata.get("loop_contract")
            if not isinstance(parent_contract, Mapping):
                raise ValueError("OpenClaw correction lacks its admitted Loop Contract.")
            if not isinstance(latest_contract, Mapping):
                latest_contract = parent_contract
            embedded_audit = embedded_contract.get("audit")
            embedded_request_digest = (
                str(embedded_audit.get("original_request_sha256") or "")
                if isinstance(embedded_audit, Mapping)
                else ""
            )
            parent_audit = parent_contract.get("audit")
            parent_request_digest = (
                str(parent_audit.get("original_request_sha256") or "")
                if isinstance(parent_audit, Mapping)
                else ""
            )
            parent_fingerprint = str(
                previous_metadata.get(
                    "parent_contract_fingerprint",
                    previous_metadata.get("contract_fingerprint"),
                )
                or ""
            )
            if embedded_request_digest and parent_request_digest != embedded_request_digest:
                for historical in conn.execute(
                    "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall():
                    historical_metadata = json.loads(historical["metadata"] or "{}")
                    candidate = historical_metadata.get(
                        "parent_loop_contract",
                        historical_metadata.get("loop_contract"),
                    )
                    if not isinstance(candidate, Mapping):
                        continue
                    candidate_audit = candidate.get("audit")
                    if (
                        isinstance(candidate_audit, Mapping)
                        and str(candidate_audit.get("original_request_sha256") or "")
                        == embedded_request_digest
                    ):
                        parent_contract = candidate
                        parent_fingerprint = str(
                            historical_metadata.get(
                                "parent_contract_fingerprint",
                                historical_metadata.get("contract_fingerprint"),
                            )
                            or ""
                        )
                        break
            embedded_scope = _worker_safe_loop_contract(embedded_contract)
            admitted_scope = dict(parent_contract)
            embedded_scope.pop("audit", None)
            admitted_scope.pop("audit", None)
            # Correlation metadata is bound only after the card body is
            # rendered. It is not execution authority and must not make the
            # immutable scope comparison fail during a Grace correction.
            embedded_scope.pop("trace", None)
            admitted_scope.pop("trace", None)
            if embedded_scope != admitted_scope:
                raise ValueError("OpenClaw correction card scope changed after admission.")
            if not parent_fingerprint:
                raise ValueError("OpenClaw correction lacks its contract fingerprint.")
            normalized = json.loads(
                json.dumps(dict(latest_contract), ensure_ascii=False)
            )
            if correction_reason:
                verification = normalized.setdefault("verification", {})
                feedback = [
                    str(item).strip()
                    for item in verification.get("review_feedback", [])
                    if str(item).strip()
                ]
                if correction_reason not in feedback:
                    feedback.append(correction_reason)
                verification["review_feedback"] = feedback
            fingerprint_scope = json.loads(
                json.dumps(normalized, ensure_ascii=False)
            )
            fingerprint_scope.pop("trace", None)
            fingerprint = contract_fingerprint(fingerprint_scope)
            if fingerprint == parent_fingerprint:
                raise ValueError("OpenClaw correction must produce a fresh fingerprint.")
            task_type = str(previous_metadata.get("task_type") or "analysis").strip()
            backend_agent_id = _loop_backend_agent_id(
                normalized,
                task_type=task_type,
                external_effects=external_effect_budget > 0,
            )
            if replaying_admission:
                run_id = int(previous_run.id)
                attempt_id = str(previous_metadata["attempt_id"])
                start_key = str(previous_metadata["start_idempotency_key"])
                metadata = dict(previous_metadata)
                metadata["backend_agent_id"] = backend_agent_id
            else:
                claimed = kb.claim_task(
                    conn,
                    task_id,
                    claimer="openclaw-loop-correction-router",
                )
                if claimed is None or claimed.current_run_id is None:
                    raise RuntimeError(
                        f"OpenClaw correction card {task_id} could not be claimed."
                    )
                run_id = int(claimed.current_run_id)
                attempt_id = f"{task_id}:run:{run_id}"
                start_key = f"{attempt_id}:start"
                metadata = {
                    "delegation_id": str(previous_metadata["delegation_id"]),
                    "attempt_id": attempt_id,
                    "contract_fingerprint": fingerprint,
                    "project": str(previous_metadata["project"]),
                    "topic_id": str(previous_metadata["topic_id"]),
                    "task_type": task_type,
                    "risk_level": str(previous_metadata.get("risk_level") or "medium"),
                    "executor_profile": "loop-contract",
                    "approval_grant_id": "",
                    "external_effect_budget": external_effect_budget,
                    "allowed_tools": _loop_allowed_tools(
                        task_type,
                        external_effects=external_effect_budget > 0,
                    ),
                    "backend_agent_id": backend_agent_id,
                    "credential_refs": (
                        ["hermes-controlled-browser"] if external_effect_budget else []
                    ),
                    "loop_contract": _worker_safe_loop_contract(
                        normalized,
                        external_effect_budget=external_effect_budget,
                    ),
                    "parent_loop_contract": dict(parent_contract),
                    "parent_contract_fingerprint": parent_fingerprint,
                    "start_idempotency_key": start_key,
                    "review_task_id": str(previous_metadata.get("review_task_id") or ""),
                    "correction_reason": correction_reason,
                    "correction_admission": True,
                    "max_poll_iterations": 0,
                }
                correction_path = begin_backend_attempt(
                    merge_message_paths(
                        kb.telegram_message_path_for_task(conn, task_id),
                        previous_metadata.get("telegram_message_path"),
                    ),
                    run_id=run_id,
                    backend_agent_id=backend_agent_id,
                )
                if correction_path:
                    correction_path = append_hop(
                        correction_path,
                        stage="openclaw_correction_attempt",
                        from_actor=actor("grace-review", "grace_review"),
                        to_actor=actor(
                            str(metadata["backend_agent_id"]),
                            "openclaw_backend",
                        ),
                        status="attempted",
                        identifiers={"run_id": run_id},
                    )
                    metadata["telegram_message_path"] = correction_path
                    metadata["backend_telegram_message_path"] = backend_projection(
                        correction_path
                    )
                    correction_contract = _worker_safe_loop_contract(
                        normalized,
                        external_effect_budget=external_effect_budget,
                    )
                    correction_contract["trace"] = {
                        "telegram_message_path": backend_projection(
                            correction_path
                        ),
                        "visibility": "backend-visible-audit-metadata",
                        "raw_user_message": "not_disclosed",
                    }
                    metadata["loop_contract"] = correction_contract
                    conn.execute(
                        "UPDATE grace_delegations SET telegram_message_path = ?, updated_at = ? "
                        "WHERE delegation_id = ?",
                        (
                            dumps_message_path(correction_path),
                            int(time.time()),
                            str(metadata["delegation_id"]),
                        ),
                    )
            if not kb.merge_active_run_metadata(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata=metadata,
            ):
                raise RuntimeError("OpenClaw correction correlation could not be saved.")
            run = kb.get_run(conn, run_id)
    if run is None:
        raise RuntimeError("OpenClaw correction run disappeared before admission.")
    objective = normalized["goal"]["objective"]
    try:
        result = delegate_loop_contract_to_openclaw(
            _loop_delegation_args(
                run,
                openclaw_task_id="openclaw.agent.loop_contract_start",
                idempotency_key=start_key,
                objective=objective,
            ),
            transport=transport,
            policy_path=policy_path,
        )
    except Exception as exc:
        ambiguous = _ambiguous_transport_exception(exc)
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                if ambiguous:
                    kb.merge_active_run_metadata(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        metadata={"admission_ambiguous": True},
                    )
                    kb.record_backend_lifecycle(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        status="queued",
                        protocol_version="2.0",
                        next_poll_seconds=2,
                    )
                    kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
                else:
                    kb.block_task(
                        conn,
                        task_id,
                        reason=f"OpenClaw Loop Contract correction admission raised: {exc}",
                        kind="capability",
                        expected_run_id=run_id,
                    )
        return {
            "execution_task_id": task_id,
            "run_id": run_id,
            "status": "retrying" if ambiguous else "blocked",
            "deduplicated": replaying_admission,
        }
    status = str(result.get("status") or "").strip().lower()
    backend_run_id = str(result.get("backend_run_id") or "").strip()
    backend_agent_id = str(result.get("backend_agent_id") or "").strip()
    backend_session_key = str(result.get("backend_session_key") or "").strip()
    protocol_version = str(result.get("protocol_version") or "").strip()
    if _ambiguous_transport_result(result) or _loop_admission_pending(result):
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                )
                kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                kb.renew_external_backend_claim(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                )
        return {
            "execution_task_id": task_id,
            "run_id": run_id,
            "status": "retrying",
            "deduplicated": replaying_admission,
            "delegated_result": result,
        }
    valid = (
        status in {"queued", "running", "succeeded"}
        and _identity_matches(
            result,
            delegation_id=str(metadata["delegation_id"]),
            attempt_id=attempt_id,
            fingerprint=fingerprint,
        )
        and backend_run_id
        and backend_agent_id == str(metadata.get("backend_agent_id") or LOOP_CONTRACT_AGENT)
        and backend_session_key
        and protocol_version == "2.0"
        and result.get("protocol_correlated") is True
    )
    with kb.connect_closing(board=board) as conn:
        with kb.write_txn(conn):
            if not valid:
                kb.block_task(
                    conn,
                    task_id,
                    reason=(
                        "OpenClaw Loop Contract correction admission returned "
                        "incomplete or uncorrelated evidence."
                    ),
                    kind="capability",
                    expected_run_id=run_id,
                )
                return {
                    "execution_task_id": task_id,
                    "status": "blocked",
                    "delegated_result": result,
                }
            corrected_path = bind_message_path(
                merge_message_paths(
                    kb.telegram_message_path_for_task(conn, task_id),
                    metadata.get("telegram_message_path"),
                ),
                run_id=run_id,
                openclaw_backend_agent_id=backend_agent_id,
                openclaw_backend_run_id=backend_run_id,
                openclaw_backend_session_key=backend_session_key,
            )
            if corrected_path:
                corrected_path = append_hop(
                    corrected_path,
                    stage="openclaw_correction",
                    from_actor=actor("grace-review", "grace_review"),
                    to_actor=actor(backend_agent_id, "openclaw_backend"),
                    status="observed",
                    identifiers={
                        "run_id": run_id,
                        "backend_run_id": backend_run_id,
                        "backend_session_key": backend_session_key,
                    },
                )
                conn.execute(
                    "UPDATE grace_delegations SET telegram_message_path = ?, updated_at = ? "
                    "WHERE delegation_id = ?",
                    (
                        dumps_message_path(corrected_path),
                        int(time.time()),
                        str(metadata["delegation_id"]),
                    ),
                )
            kb.merge_active_run_metadata(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={
                    "backend_session_key": backend_session_key,
                    "backend_agent_id": backend_agent_id,
                    **(
                        {
                            "telegram_message_path": corrected_path,
                            "backend_telegram_message_path": backend_projection(
                                corrected_path
                            ),
                        }
                        if corrected_path
                        else {}
                    ),
                },
            )
            kb.record_backend_lifecycle(
                conn,
                task_id,
                expected_run_id=run_id,
                status=status,
                backend_run_id=backend_run_id,
                backend_agent_id=backend_agent_id,
                protocol_version=protocol_version,
                workspace_ref=str(_loop_workspace(backend_agent_id)),
                result_digest=_digest(result),
                next_poll_seconds=0,
            )
            kb.renew_external_backend_claim(conn, task_id, expected_run_id=run_id)
    terminal_review: Mapping[str, Any] | None = None
    if status == "succeeded":
        with kb.connect_closing(board=board) as conn:
            terminal_run = kb.get_run(conn, run_id)
        if terminal_run is None:
            raise RuntimeError(
                "Synchronous OpenClaw correction disappeared before validation."
            )
        terminal_review = make_loop_contract_terminal_handler(board=board)(
            terminal_run,
            {
                "status": status,
                "backend_run_id": backend_run_id,
                "backend_agent_id": backend_agent_id,
                "protocol_version": protocol_version,
                "result_digest": _terminal_evidence_digest(result),
                "delegated_result": result,
            },
        )
        if terminal_review.get("accepted") is not True:
            status = "blocked"
    return {
        "execution_task_id": task_id,
        "run_id": run_id,
        "status": status,
        "backend_run_id": backend_run_id,
        "delegated_result": result,
        **({"terminal_review": dict(terminal_review)} if terminal_review else {}),
    }


def start_ready_loop_contract_corrections(
    *,
    board: Optional[str] = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Recover ready OpenClaw cards reopened by a rejected Grace review."""
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute(
            """
            SELECT task.id
              FROM tasks AS task
             WHERE (
                    (task.status = 'ready' AND task.claim_lock IS NULL)
                    OR (
                        task.status = 'running'
                        AND task.current_run_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                              FROM task_runs AS active_run
                             WHERE active_run.id = task.current_run_id
                               AND active_run.backend_run_id IS NULL
                               AND json_extract(
                                   active_run.metadata,
                                   '$.correction_admission'
                               ) = 1
                        )
                    )
               )
               AND task.executor_backend = 'openclaw'
               AND task.executor_profile = 'loop-contract'
               AND COALESCE(
                   (
                       SELECT json_extract(latest.metadata, '$.external_effect_budget')
                         FROM task_runs AS latest
                        WHERE latest.task_id = task.id
                        ORDER BY latest.id DESC
                        LIMIT 1
                   ),
                   0
               ) = 0
               AND EXISTS (
                   SELECT 1
                     FROM task_events AS event
                    WHERE event.task_id = task.id
                      AND event.kind = 'grace_correction_requested'
               )
             ORDER BY task.priority DESC, task.created_at, task.id
             LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["id"])
        try:
            results.append(retry_ready_loop_contract_execution(task_id, board=board))
        except Exception as exc:
            with kb.connect_closing(board=board) as conn:
                kb.block_task(
                    conn,
                    task_id,
                    reason=f"OpenClaw correction admission quarantined: {exc}",
                    kind="capability",
                )
            results.append({
                "execution_task_id": task_id,
                "status": "blocked",
                "error": str(exc),
            })
    return results


def make_loop_contract_poll_adapter(
    *,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> Callable[[kb.Run], Mapping[str, Any]]:
    def poll(run: kb.Run) -> Mapping[str, Any]:
        metadata = run.metadata or {}
        start_key = str(metadata.get("start_idempotency_key") or "")
        if not run.backend_run_id or not start_key:
            raise ValueError("OpenClaw Loop Contract run lacks durable correlation.")
        result = delegate_loop_contract_to_openclaw(
            _loop_delegation_args(
                run,
                openclaw_task_id=(
                    "openclaw.agent.loop_contract_cancel"
                    if metadata.get("stop_rule_cleanup_pending")
                    else "openclaw.agent.loop_contract_poll"
                ),
                idempotency_key=f"{start_key}:poll:{run.backend_poll_count + 1}",
                objective="Poll the exact OpenClaw Loop Contract run.",
                start_idempotency_key=start_key,
            ),
            transport=transport,
            policy_path=policy_path,
        )
        status = str(result.get("status") or "").strip().lower()
        if status not in {"queued", "running", "succeeded", "failed", "blocked"}:
            raise ValueError(f"Unexpected OpenClaw Loop Contract status={status!r}.")
        if not _identity_matches(
            result,
            delegation_id=str(metadata.get("delegation_id") or ""),
            attempt_id=str(metadata.get("attempt_id") or ""),
            fingerprint=str(metadata.get("contract_fingerprint") or ""),
        ):
            raise ValueError("OpenClaw Loop Contract poll identity mismatch.")
        expected_backend_agent = str(metadata.get("backend_agent_id") or "")
        expected_backend_session = str(metadata.get("backend_session_key") or "")
        if (
            str(result.get("backend_run_id") or "") != str(run.backend_run_id)
            or str(result.get("backend_agent_id") or "") != expected_backend_agent
            or str(result.get("backend_session_key") or "")
            != expected_backend_session
            or str(result.get("protocol_version") or "") != "2.0"
            or result.get("protocol_correlated") is not True
        ):
            raise ValueError("OpenClaw Loop Contract poll backend correlation mismatch.")
        return {
            "status": status,
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": expected_backend_agent,
            "protocol_version": "2.0",
            "result_digest": _terminal_evidence_digest(result) if status in {"succeeded", "failed", "blocked"} else _digest(result),
            "delegated_result": result,
        }
    return poll


def make_loop_contract_terminal_handler(
    *, board: Optional[str] = None,
) -> Callable[[kb.Run, Mapping[str, Any]], Mapping[str, Any]]:
    def handle(run: kb.Run, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        result = observation.get("delegated_result")
        output = next(
            (
                artifact.get("value")
                for artifact in (result.get("artifacts") if isinstance(result, Mapping) else []) or []
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("type") == "openclaw_result"
                    and isinstance(artifact.get("value"), Mapping)
                )
            ),
            None,
        )
        evidence = output.get("evidence") if isinstance(output, Mapping) else None
        audited_result = (
            _openclaw_result_payload(output)
            if isinstance(output, Mapping)
            else None
        )
        metadata = run.metadata or {}
        external_effects = (
            audited_result.get("externalEffects")
            if isinstance(audited_result, Mapping)
            else None
        )
        external_effect_budget = int(metadata.get("external_effect_budget") or 0)
        valid = (
            observation.get("status") == "succeeded"
            and isinstance(result, Mapping)
            and _identity_matches(
                result,
                delegation_id=str(metadata.get("delegation_id") or ""),
                attempt_id=str(metadata.get("attempt_id") or ""),
                fingerprint=str(metadata.get("contract_fingerprint") or ""),
            )
            and result.get("protocol_correlated") is True
            and result.get("identity_correlated") is True
            and str(result.get("backend_run_id") or "")
            == str(run.backend_run_id or "")
            and str(result.get("backend_agent_id") or "")
            == str(metadata.get("backend_agent_id") or "")
            and str(result.get("protocol_version") or "") == "2.0"
            and result.get("errors") in (None, [])
            and result.get("requires_human_review") is False
            and result.get("backend_session_key") == metadata.get("backend_session_key")
            and isinstance(evidence, Mapping)
            and evidence.get("terminal") is True
            and evidence.get("resultContractValid") is True
            and evidence.get("externalEffectBudget") == metadata.get("external_effect_budget")
            and isinstance(audited_result, Mapping)
            and audited_result.get("status") == "succeeded"
            and isinstance(external_effects, list)
            and len(external_effects) <= external_effect_budget
        )
        validation_error = (
            str(evidence.get("resultContractError") or "").strip()
            if isinstance(evidence, Mapping)
            else ""
        )
        worker_summary = (
            str(audited_result.get("summary") or "").strip()
            if isinstance(audited_result, Mapping)
            else ""
        )
        validation_reason = "OpenClaw Loop Contract terminal evidence failed validation."
        details = [item for item in (validation_error, worker_summary) if item]
        if details:
            validation_reason = (
                "OpenClaw Loop Contract was blocked before verified completion: "
                + " Worker report: ".join(details)
            )
        if not external_effects:
            validation_reason += " No external effect was verified or recorded."
        validation_reason = validation_reason[:2000]
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                if not valid:
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=validation_reason,
                        kind="capability",
                        expected_run_id=run.id,
                    )
                    return {
                        "accepted": False,
                        "reason": validation_reason,
                    }
                summary = str(audited_result.get("summary") or "OpenClaw Loop Contract completed.")
                if not kb.complete_task(
                    conn,
                    run.task_id,
                    result=json.dumps(dict(result), ensure_ascii=False),
                    summary=summary,
                    metadata={
                        **metadata,
                        "terminal": True,
                        "result_digest": observation.get("result_digest"),
                        "acceptance_evidence": audited_result.get("acceptanceEvidence"),
                        "external_effects": audited_result.get("externalEffects"),
                        "policy_receipts": audited_result.get("policyReceipts"),
                    },
                    expected_run_id=run.id,
                ):
                    raise RuntimeError("OpenClaw execution changed before completion.")
                # The existing parent link releases the normal Grace review card;
                # Grace remains the independent acceptance authority.
        return {"accepted": True, "review_task_id": str(metadata.get("review_task_id") or "")}
    return handle
