"""ClawOps control-plane adapter for explicitly allowlisted OpenClaw executors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as kb
from plugins.openclaw_bridge.tools import delegate_to_openclaw
from proactive.execution_backends import (
    ExecutionRequirements,
    next_poll_delay_seconds,
    route_execution_backend,
)
from proactive.loop_contract import contract_fingerprint, validate_loop_contract


READONLY_BROWSER_AGENT = "missioncrew-browser-readonly"
READONLY_BROWSER_WORKSPACE = (
    Path.home()
    / "my_agent_team"
    / "openclaw-workspace"
    / "agents"
    / READONLY_BROWSER_AGENT
)


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


def _ambiguous_transport_exception(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _canonical_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _terminal_evidence_digest(result: Mapping[str, Any]) -> str:
    """Hash immutable terminal identity and backend evidence only."""
    return _canonical_digest(
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


def _result_output(result: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for artifact in result.get("artifacts") or []:
        if (
            isinstance(artifact, Mapping)
            and artifact.get("type") == "openclaw_result"
            and isinstance(artifact.get("value"), Mapping)
        ):
            return artifact["value"]
    return None


def _result_identity_matches(
    result: Mapping[str, Any],
    *,
    expected_delegation_id: str,
    expected_attempt_id: str,
    expected_contract_fingerprint: str,
) -> bool:
    return (
        result.get("identity_correlated") is True
        and result.get("delegation_id") == expected_delegation_id
        and result.get("attempt_id") == expected_attempt_id
        and result.get("contract_fingerprint") == expected_contract_fingerprint
    )


def _review_readonly_result(
    result: Mapping[str, Any],
    *,
    expected_url: str,
    expected_delegation_id: str,
    expected_attempt_id: str,
    expected_contract_fingerprint: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if result.get("status") != "succeeded":
        errors.append("OpenClaw result status is not succeeded.")
    if result.get("protocol_version") != "2.0":
        errors.append("OpenClaw result did not explicitly return Protocol v2.")
    if result.get("protocol_correlated") is not True:
        errors.append("OpenClaw result is not correlated to Protocol v2.")
    if result.get("requires_human_review") is not False:
        errors.append("OpenClaw result requires human review.")
    if result.get("errors"):
        errors.append("OpenClaw result contains bridge errors.")
    if not _result_identity_matches(
        result,
        expected_delegation_id=expected_delegation_id,
        expected_attempt_id=expected_attempt_id,
        expected_contract_fingerprint=expected_contract_fingerprint,
    ):
        errors.append("OpenClaw result identity does not match the delegated attempt.")
    for key in ("backend_run_id", "backend_agent_id", "backend_session_key"):
        if not str(result.get(key) or "").strip():
            errors.append(f"Missing backend evidence: {key}.")
    output = _result_output(result)
    if output is None:
        errors.append("Missing structured OpenClaw output artifact.")
        return False, errors
    evidence = output.get("evidence")
    if not isinstance(evidence, Mapping):
        errors.append("Missing read-only evidence object.")
    else:
        if evidence.get("requestedUrl") != expected_url:
            errors.append("Returned evidence URL does not match the delegated URL.")
        if evidence.get("sideEffectsPerformed") is not False:
            errors.append("Result does not prove sideEffectsPerformed=false.")
        if evidence.get("externalEffectBudget") != 0:
            errors.append("Result does not preserve externalEffectBudget=0.")
    result_text = output.get("resultText")
    snapshot_result: Optional[Mapping[str, Any]] = None
    if not isinstance(result_text, str) or not result_text.strip():
        errors.append("Missing structured browser snapshot resultText.")
    else:
        try:
            parsed_result = json.loads(result_text)
        except json.JSONDecodeError:
            parsed_result = None
        if not isinstance(parsed_result, Mapping):
            errors.append("Browser snapshot resultText is not a JSON object.")
        else:
            snapshot_result = parsed_result
    if snapshot_result is not None:
        if snapshot_result.get("url") != expected_url:
            errors.append("Snapshot payload URL does not match the delegated URL.")
        snapshot_excerpt = snapshot_result.get("snapshotExcerpt")
        if not isinstance(snapshot_excerpt, str) or not snapshot_excerpt.strip():
            errors.append("Snapshot payload does not contain page content.")
        if snapshot_result.get("sideEffectsPerformed") is not False:
            errors.append("Snapshot payload does not prove sideEffectsPerformed=false.")
        if isinstance(evidence, Mapping) and isinstance(snapshot_excerpt, str):
            if evidence.get("browserSnapshotChars") != len(snapshot_excerpt):
                errors.append("Snapshot evidence length does not match the returned payload.")
    if str(result.get("backend_agent_id") or "") != READONLY_BROWSER_AGENT:
        errors.append("Unexpected OpenClaw backend agent.")
    return not errors, errors


def _snapshot_review_metadata(
    result: Mapping[str, Any],
    *,
    expected_url: str,
) -> dict[str, Any]:
    output = _result_output(result)
    if output is None or not isinstance(output.get("resultText"), str):
        raise ValueError("Validated OpenClaw result is missing resultText.")
    snapshot = json.loads(str(output["resultText"]))
    if not isinstance(snapshot, Mapping):
        raise ValueError("Validated OpenClaw resultText is not an object.")
    excerpt = str(snapshot.get("snapshotExcerpt") or "")
    return {
        "requested_url": expected_url,
        "snapshot_validated": True,
        "snapshot_chars": len(excerpt),
        "snapshot_result_digest": hashlib.sha256(
            str(output["resultText"]).encode("utf-8")
        ).hexdigest(),
    }


def _complete_or_resume_grace_review_locked(
    conn,
    *,
    execution_task_id: str,
    review_task_id: str,
    execution_run: kb.Run,
    expected_url: str,
) -> tuple[bool, list[str]]:
    review_task = kb.get_task(conn, review_task_id)
    if review_task is not None and review_task.status == "done":
        if review_task.result == "accepted":
            return True, []
        return False, ["Grace review task is done without an accepted result."]

    metadata = execution_run.metadata or {}
    checks = [
        (
            execution_run.status == "done"
            and execution_run.outcome == "completed"
            and execution_run.backend_run_id
            and execution_run.backend_agent_id == READONLY_BROWSER_AGENT
            and execution_run.protocol_version == "2.0"
            and execution_run.result_digest
        ),
        metadata.get("snapshot_validated") is True,
        metadata.get("requested_url") == expected_url,
        metadata.get("external_effect_budget") == 0,
        metadata.get("side_effects_performed") is False,
        metadata.get("result_digest") == execution_run.result_digest,
        int(metadata.get("snapshot_chars") or 0) > 0,
    ]
    if not all(checks):
        return False, ["Completed execution does not contain resumable review evidence."]

    review = kb.get_task(conn, review_task_id)
    if review is None:
        return False, ["Grace review task is missing."]
    if review is not None and review.status == "done":
        return (
            review.result == "accepted",
            [] if review.result == "accepted" else [
                "Grace review completed without acceptance."
            ],
        )
    return True, []


def _complete_or_resume_grace_review(
    conn,
    *,
    execution_task_id: str,
    review_task_id: str,
    execution_run: kb.Run,
    expected_url: str,
) -> tuple[bool, list[str]]:
    """Claim and finish a recovery review under one writer transaction."""
    with kb.write_txn(conn):
        return _complete_or_resume_grace_review_locked(
            conn,
            execution_task_id=execution_task_id,
            review_task_id=review_task_id,
            execution_run=execution_run,
            expected_url=expected_url,
        )


def _finalize_readonly_terminal_locked(
    conn,
    *,
    execution_task_id: str,
    review_task_id: str,
    execution_run: kb.Run,
    delegated_result: Mapping[str, Any],
    expected_url: str,
    expected_delegation_id: str,
    expected_attempt_id: str,
    expected_contract_fingerprint: str,
    expected_circuit_generation: int,
) -> tuple[bool, list[str], str]:
    """Bind, review, and close one already-persisted terminal result."""
    result_digest = _terminal_evidence_digest(delegated_result)
    current_task = kb.get_task(conn, execution_task_id)
    current_run = kb.get_run(conn, execution_run.id)
    if (
        current_task is not None
        and current_task.status == "done"
        and current_run is not None
        and current_run.status == "done"
        and current_run.outcome == "completed"
        and current_run.result_digest == result_digest
        and (current_run.metadata or {}).get("contract_fingerprint")
        == expected_contract_fingerprint
    ):
        accepted, errors = _complete_or_resume_grace_review_locked(
            conn,
            execution_task_id=execution_task_id,
            review_task_id=review_task_id,
            execution_run=current_run,
            expected_url=expected_url,
        )
        return accepted, errors, result_digest
    if current_run is not None:
        execution_run = current_run
    review_ok, review_errors = _review_readonly_result(
        delegated_result,
        expected_url=expected_url,
        expected_delegation_id=expected_delegation_id,
        expected_attempt_id=expected_attempt_id,
        expected_contract_fingerprint=expected_contract_fingerprint,
    )
    backend_run_id = str(delegated_result.get("backend_run_id") or "").strip()
    backend_agent_id = str(
        delegated_result.get("backend_agent_id") or ""
    ).strip()
    returned_protocol = str(
        delegated_result.get("protocol_version") or ""
    ).strip()
    identity_matches = _result_identity_matches(
        delegated_result,
        expected_delegation_id=expected_delegation_id,
        expected_attempt_id=expected_attempt_id,
        expected_contract_fingerprint=expected_contract_fingerprint,
    )
    lifecycle_identity_valid = (
        returned_protocol == "2.0"
        and delegated_result.get("protocol_correlated") is True
        and identity_matches
    )
    if (
        lifecycle_identity_valid
        and backend_run_id
        and backend_agent_id
        and not kb.bind_backend_run(
            conn,
            execution_task_id,
            expected_run_id=execution_run.id,
            backend_run_id=backend_run_id,
            backend_agent_id=backend_agent_id,
            protocol_version=returned_protocol,
            workspace_ref=str(READONLY_BROWSER_WORKSPACE),
            result_digest=result_digest,
        )
    ):
        review_errors = [
            "Backend run could not be uniquely bound to the active Kanban attempt."
        ]
        review_ok = False
    if not review_ok:
        kb.record_backend_circuit_outcome(
            conn,
            "openclaw",
            succeeded=False,
            error="; ".join(review_errors),
            expected_generation=expected_circuit_generation,
        )
        kb.block_task(
            conn,
            execution_task_id,
            reason="; ".join(review_errors),
            kind="capability",
            expected_run_id=execution_run.id,
        )
        return False, review_errors, result_digest

    snapshot_metadata = _snapshot_review_metadata(
        delegated_result,
        expected_url=expected_url,
    )
    if not kb.complete_task(
        conn,
        execution_task_id,
        result=str(delegated_result["summary"]),
        summary=(
            "OpenClaw completed a real read-only browser snapshot and "
            "returned zero-effect evidence."
        ),
        metadata={
            **(execution_run.metadata or {}),
            "executor_backend": "openclaw",
            "backend_run_id": backend_run_id,
            "backend_agent_id": backend_agent_id,
            "backend_session_key": delegated_result["backend_session_key"],
            "protocol_version": "2.0",
            "contract_fingerprint": expected_contract_fingerprint,
            "result_digest": result_digest,
            "external_effect_budget": 0,
            "side_effects_performed": False,
            **snapshot_metadata,
        },
        expected_run_id=execution_run.id,
    ):
        raise RuntimeError("Execution task changed before completion was recorded.")
    kb.record_backend_circuit_outcome(
        conn,
        "openclaw",
        succeeded=True,
        expected_generation=expected_circuit_generation,
    )
    completed_run = kb.latest_run(conn, execution_task_id)
    if completed_run is None:
        raise RuntimeError("Completed execution task has no run evidence.")
    return True, [], result_digest


def _finalize_readonly_terminal(
    conn,
    *,
    execution_task_id: str,
    review_task_id: str,
    execution_run: kb.Run,
    delegated_result: Mapping[str, Any],
    expected_url: str,
    expected_delegation_id: str,
    expected_attempt_id: str,
    expected_contract_fingerprint: str,
    expected_circuit_generation: int,
) -> tuple[bool, list[str], str]:
    """Finalize execution and its Grace review under one writer lock."""
    with kb.write_txn(conn):
        return _finalize_readonly_terminal_locked(
            conn,
            execution_task_id=execution_task_id,
            review_task_id=review_task_id,
            execution_run=execution_run,
            delegated_result=delegated_result,
            expected_url=expected_url,
            expected_delegation_id=expected_delegation_id,
            expected_attempt_id=expected_attempt_id,
            expected_contract_fingerprint=expected_contract_fingerprint,
            expected_circuit_generation=expected_circuit_generation,
        )


def execute_readonly_browser_snapshot(
    url: str,
    *,
    contract: Mapping[str, Any],
    board: Optional[str] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> dict[str, Any]:
    """Execute and review one zero-effect browser snapshot through OpenClaw.

    Hermes/Grace retains the Loop Contract and Kanban records. OpenClaw owns
    only the dedicated backend session. The backend run is bound to the exact
    claimed Kanban attempt before either the execution or review task can
    complete.
    """
    normalized_contract = validate_loop_contract(contract, effect_budget_mode="zero")
    fingerprint = contract_fingerprint(normalized_contract)
    identity = normalized_contract["identity"]
    objective = normalized_contract["goal"]["objective"]
    allowed_scope = normalized_contract["scope"]["allowed"]
    forbidden_scope = normalized_contract["scope"]["forbidden"]
    if url not in allowed_scope or url in forbidden_scope:
        raise ValueError(
            "Read-only browser URL must be explicitly allowed and not forbidden "
            "by the Loop Contract scope."
        )
    routing = normalized_contract.get("routing")
    resolved = routing.get("resolved") if isinstance(routing, Mapping) else {}
    assignment = resolved.get("assignment") if isinstance(resolved, Mapping) else {}
    route_allowed_urls = [
        str(value or "").strip()
        for value in (
            assignment.get("allowed_urls")
            if isinstance(assignment, Mapping)
            else []
        ) or []
        if str(value or "").strip()
    ]
    if url not in route_allowed_urls:
        raise ValueError(
            "Read-only browser URL must be authorized by the "
            "resolved route assignment."
        )
    request_instance_id = str(identity["request_instance_id"])
    idempotency_key = hashlib.sha256(
        (
            "openclaw-readonly:"
            f"{identity['project']}:{fingerprint}:{request_instance_id}:{url}"
        ).encode("utf-8")
    ).hexdigest()
    replaying_delegation = False
    circuit_generation: int | None = None

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
            (idempotency_key,),
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
                (f"{idempotency_key}:review",),
            ).fetchone()
            existing_review_task_id = (
                str(review_row["id"]) if review_row is not None else ""
            )
            if existing is not None and existing.status == "done":
                if (
                    existing_run is None
                    or existing.project_namespace != str(identity["project"])
                    or (existing_run.metadata or {}).get("contract_fingerprint")
                    != fingerprint
                ):
                    return {
                        "execution_task_id": existing_task_id,
                        "review_task_id": existing_review_task_id,
                        "status": "blocked",
                        "deduplicated": True,
                        "review_errors": [
                            "Deduplicated execution lacks matching run evidence."
                        ],
                    }
                accepted, errors = _complete_or_resume_grace_review(
                    conn,
                    execution_task_id=existing_task_id,
                    review_task_id=existing_review_task_id,
                    execution_run=existing_run,
                    expected_url=url,
                )
                return {
                    "execution_task_id": existing_task_id,
                    "review_task_id": existing_review_task_id,
                    "status": "succeeded" if accepted else "blocked",
                    "deduplicated": True,
                    "backend_run_id": existing_run.backend_run_id,
                    **({"review_errors": errors} if errors else {}),
                }
            if existing is not None and existing.status == "blocked":
                return {
                    "execution_task_id": existing_task_id,
                    "review_task_id": existing_review_task_id,
                    "status": "blocked",
                    "deduplicated": True,
                    "backend_run_id": (
                        existing_run.backend_run_id if existing_run else None
                    ),
                    "review_errors": [
                        existing.last_failure_error
                        or "Earlier OpenClaw execution is durably blocked."
                    ],
                }
            if (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and existing_run.backend_status == "queued"
                and not existing_run.backend_run_id
                and (existing_run.metadata or {}).get(
                    "admission_ambiguous"
                )
                is True
            ):
                replay_metadata = existing_run.metadata or {}
                stored_generation = replay_metadata.get(
                    "circuit_generation"
                )
                if (
                    isinstance(stored_generation, int)
                    and not isinstance(stored_generation, bool)
                ):
                    circuit_generation = int(stored_generation)
                    replaying_delegation = True
            elif (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and existing_run.backend_status is not None
            ):
                existing_metadata = existing_run.metadata or {}
                if existing_run.backend_status in {
                    "succeeded",
                    "failed",
                    "blocked",
                }:
                    terminal_observation = existing_metadata.get(
                        "backend_terminal_observation"
                    )
                    stored_circuit_generation = existing_metadata.get(
                        "circuit_generation"
                    )
                    delegated_result = (
                        terminal_observation.get("delegated_result")
                        if isinstance(terminal_observation, Mapping)
                        else None
                    )
                    if (
                        not isinstance(delegated_result, Mapping)
                        or not isinstance(stored_circuit_generation, int)
                        or isinstance(stored_circuit_generation, bool)
                    ):
                        kb.block_task(
                            conn,
                            existing_task_id,
                            reason=(
                                "Persisted terminal OpenClaw run is missing "
                                "durable delegated-result evidence."
                            ),
                            kind="capability",
                            expected_run_id=existing_run.id,
                        )
                        return {
                            "execution_task_id": existing_task_id,
                            "review_task_id": existing_review_task_id,
                            "status": "blocked",
                            "deduplicated": True,
                            "review_errors": [
                                "Persisted terminal result evidence is missing."
                            ],
                        }
                    accepted, errors, persisted_digest = (
                        _finalize_readonly_terminal(
                            conn,
                            execution_task_id=existing_task_id,
                            review_task_id=existing_review_task_id,
                            execution_run=existing_run,
                            delegated_result=delegated_result,
                            expected_url=url,
                            expected_delegation_id=f"grace:{existing_task_id}",
                            expected_attempt_id=(
                                f"{existing_task_id}:run:{existing_run.id}"
                            ),
                            expected_contract_fingerprint=fingerprint,
                            expected_circuit_generation=int(
                                stored_circuit_generation
                            ),
                        )
                    )
                    return {
                        "execution_task_id": existing_task_id,
                        "review_task_id": existing_review_task_id,
                        "status": "succeeded" if accepted else "blocked",
                        "deduplicated": True,
                        "backend_run_id": existing_run.backend_run_id,
                        "result_digest": persisted_digest,
                        **({"review_errors": errors} if errors else {}),
                    }
                return {
                    "execution_task_id": existing_task_id,
                    "review_task_id": existing_review_task_id,
                    "status": existing_run.backend_status,
                    "deduplicated": True,
                    "backend_run_id": existing_run.backend_run_id,
                }
            elif (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and existing_run.backend_status is None
            ):
                replay_metadata = existing_run.metadata or {}
                stored_generation = replay_metadata.get(
                    "circuit_generation"
                )
                if (
                    isinstance(stored_generation, int)
                    and not isinstance(stored_generation, bool)
                ):
                    circuit_generation = int(stored_generation)
                    replaying_delegation = True
        circuit_states = (
            {}
            if replaying_delegation
            else kb.backend_circuit_states(conn)
        )
        if (
            not replaying_delegation
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
                capabilities=(
                    "browser_read",
                    "isolated_session",
                    "isolated_workspace",
                ),
                semantic_class="browser_readonly",
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
            raise RuntimeError(
                "No eligible OpenClaw backend: "
                f"{routing_decision['selection_reason']}"
            )
        from proactive.model_routing import route_grace, route_worker

        route_fields = {
            "task_risk": "low",
            "memory_impact": (
                "durable"
                if normalized_contract.get("memory", {}).get(
                    "promote_on_acceptance"
                )
                else "none"
            ),
            "external_action": False,
        }
        worker_model_route = route_worker("browser_readonly", route_fields)
        review_model_route = route_grace("acceptance_review", route_fields)
        routing_decision = dict(routing_decision)
        routing_decision["model_route"] = worker_model_route
        with kb.write_txn(conn):
            execution_task_id = kb.create_task(
                conn,
                title=f"ClawOps OpenClaw read-only snapshot: {url}",
                body=json.dumps(normalized_contract, ensure_ascii=False, indent=2),
                assignee="clawops-browser",
                created_by="grace",
                workspace_kind="dir",
                workspace_path=str(READONLY_BROWSER_WORKSPACE),
                max_runtime_seconds=int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                idempotency_key=idempotency_key,
                executor_backend="openclaw",
                executor_profile="browser-readonly",
                project_namespace=str(identity["project"]),
                routing_decision=routing_decision,
                model_override=worker_model_route["requested_model"],
            )
            review_task_id = kb.create_task(
                conn,
                title=f"Grace review: {url}",
                body=(
                    "Verify backend run identity, URL equality, zero external-effect "
                    "budget, sideEffectsPerformed=false, and structured snapshot output."
                ),
                assignee="default",
                created_by="grace",
                parents=[execution_task_id],
                idempotency_key=f"{idempotency_key}:review",
                executor_backend="hermes",
                executor_profile="grace-policy-review",
                project_namespace=str(identity["project"]),
                routing_decision={
                    "selected_backend": "hermes",
                    "model_route": review_model_route,
                },
                model_override=review_model_route["requested_model"],
            )
            task = kb.get_task(conn, execution_task_id)
            if task is not None and task.status == "done":
                run = kb.latest_run(conn, execution_task_id)
                if run is None:
                    return {
                        "execution_task_id": execution_task_id,
                        "review_task_id": review_task_id,
                        "status": "blocked",
                        "deduplicated": True,
                        "review_errors": ["Completed execution task has no run evidence."],
                    }
                if (
                    task.project_namespace != str(identity["project"])
                    or (run.metadata or {}).get("contract_fingerprint") != fingerprint
                ):
                    return {
                        "execution_task_id": execution_task_id,
                        "review_task_id": review_task_id,
                        "status": "blocked",
                        "deduplicated": True,
                        "review_errors": [
                            "Deduplicated execution does not match project and contract identity."
                        ],
                    }
                accepted, errors = _complete_or_resume_grace_review(
                    conn,
                    execution_task_id=execution_task_id,
                    review_task_id=review_task_id,
                    execution_run=run,
                    expected_url=url,
                )
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": "succeeded" if accepted else "blocked",
                    "deduplicated": True,
                    "backend_run_id": run.backend_run_id,
                    **({"review_errors": errors} if errors else {}),
                }
            if task is not None and task.status == "blocked":
                run = kb.latest_run(conn, execution_task_id)
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": "blocked",
                    "deduplicated": True,
                    "backend_run_id": run.backend_run_id if run else None,
                    "review_errors": [
                        task.last_failure_error
                        or "Earlier OpenClaw execution is durably blocked."
                    ],
                }
            if task is not None and task.status == "running":
                run = kb.latest_run(conn, execution_task_id)
                metadata = run.metadata if run else {}
                admission_replayable = bool(
                    run is not None
                    and run.backend_status == "queued"
                    and not run.backend_run_id
                    and isinstance(metadata, Mapping)
                    and metadata.get("admission_ambiguous") is True
                )
                if (
                    run is None
                    or (
                        run.backend_status is not None
                        and not admission_replayable
                    )
                    or not isinstance(metadata, Mapping)
                    or metadata.get("contract_fingerprint") != fingerprint
                    or metadata.get("delegation_id")
                    != f"grace:{execution_task_id}"
                    or metadata.get("attempt_id")
                    != f"{execution_task_id}:run:{run.id}"
                    or metadata.get("idempotency_key")
                    != f"{execution_task_id}:run:{run.id}"
                    or not isinstance(
                        metadata.get("circuit_generation"), int
                    )
                    or isinstance(
                        metadata.get("circuit_generation"), bool
                    )
                ):
                    return {
                        "execution_task_id": execution_task_id,
                        "review_task_id": review_task_id,
                        "status": "blocked",
                        "deduplicated": True,
                        "backend_run_id": run.backend_run_id if run else None,
                        "review_errors": [
                            "Existing OpenClaw execution cannot be safely resumed."
                        ],
                    }
                run_id = run.id
                circuit_generation = int(metadata["circuit_generation"])
                replaying_delegation = True
            else:
                claimed = kb.claim_task(
                    conn, execution_task_id, claimer="clawops-openclaw-router"
                )
                if claimed is None or claimed.current_run_id is None:
                    raise RuntimeError(
                        "OpenClaw execution task could not be claimed by the ClawOps router."
                    )
                run_id = int(claimed.current_run_id)
                circuit_generation = int(
                    kb.backend_circuit_snapshot(
                        conn,
                        "openclaw",
                    )["generation"]
                )
                if not kb.merge_active_run_metadata(
                    conn,
                    execution_task_id,
                    expected_run_id=run_id,
                    metadata={
                        "delegation_id": f"grace:{execution_task_id}",
                        "attempt_id": f"{execution_task_id}:run:{run_id}",
                        "idempotency_key": f"{execution_task_id}:run:{run_id}",
                        "start_idempotency_key": (
                            f"{execution_task_id}:run:{run_id}"
                        ),
                        "contract_fingerprint": fingerprint,
                        "review_task_id": review_task_id,
                        "circuit_generation": circuit_generation,
                        "requested_url": url,
                        "allowed_urls": [url],
                        "project": str(identity["project"]),
                        "topic_id": str(
                            identity.get("thread_id")
                            or identity["topic_name"]
                        ),
                        "executor_profile": "browser-readonly",
                        "model_route": worker_model_route,
                    },
                ):
                    raise RuntimeError(
                        "OpenClaw execution correlation could not be saved."
                    )
    if circuit_generation is None:
        raise RuntimeError("OpenClaw execution lacks a circuit generation.")
    expected_delegation_id = f"grace:{execution_task_id}"
    expected_attempt_id = f"{execution_task_id}:run:{run_id}"

    try:
        delegated_result = delegate_to_openclaw(
            {
                "task_id": execution_task_id,
                "objective": objective,
                "context_refs": [
                    f"kanban:{execution_task_id}",
                    f"loop-contract:{fingerprint}",
                ],
                "allowed_tools": ["browser.read"],
                "denied_tools": [
                    "browser.click",
                    "browser.type",
                    "browser.upload",
                    "message.send",
                    "shell",
                    "filesystem.write",
                ],
                "risk_level": "low",
                "requires_confirmation": False,
                "max_runtime_seconds": int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                "output_format": "json",
                "audit_required": True,
                "requested_by": "hermes",
                "protocol_version": "2.0",
                "delegation_id": expected_delegation_id,
                "attempt_id": expected_attempt_id,
                "contract_fingerprint": fingerprint,
                "project": str(identity["project"]),
                "topic_id": str(
                    identity.get("thread_id") or identity["topic_name"]
                ),
                "executor_backend": "openclaw",
                "executor_profile": "browser-readonly",
                "backend_agent_id": READONLY_BROWSER_AGENT,
                "model_route": worker_model_route,
                "external_effect_budget": 0,
                "workspace_policy": "dedicated",
                "session_policy": "ephemeral",
                "credential_refs": [],
                "idempotency_key": f"{execution_task_id}:run:{run_id}",
                "openclaw_task_id": "openclaw.browser.read_snapshot",
                "target_url": url,
                "allowed_urls": [url],
                "dry_run": False,
            },
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
                    error=f"OpenClaw delegation raised: {exc}",
                    expected_generation=circuit_generation,
                )
                if not _ambiguous_transport_exception(exc):
                    kb.block_task(
                        conn,
                        execution_task_id,
                        reason=f"OpenClaw delegation raised: {exc}",
                        kind="capability",
                        expected_run_id=run_id,
                    )
        return {
            "execution_task_id": execution_task_id,
            "review_task_id": review_task_id,
            "status": (
                "retrying"
                if _ambiguous_transport_exception(exc)
                else "blocked"
            ),
            "deduplicated": replaying_delegation,
            "review_errors": [f"OpenClaw delegation raised: {exc}"],
        }
    result_digest = _terminal_evidence_digest(delegated_result)
    delegated_status = str(delegated_result.get("status") or "").strip().lower()
    identity_matches = _result_identity_matches(
        delegated_result,
        expected_delegation_id=expected_delegation_id,
        expected_attempt_id=expected_attempt_id,
        expected_contract_fingerprint=fingerprint,
    )
    with kb.connect_closing(board=board) as conn:
        if _ambiguous_transport_result(delegated_result):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error=(
                    "OpenClaw read-only response was ambiguous; retrying "
                    "the same idempotency key."
                ),
                expected_generation=circuit_generation,
            )
            return {
                "execution_task_id": execution_task_id,
                "review_task_id": review_task_id,
                "status": "retrying",
                "deduplicated": replaying_delegation,
                "routing_decision": routing_decision,
                "delegated_result": delegated_result,
            }
        backend_run_id = str(delegated_result.get("backend_run_id") or "").strip()
        backend_agent_id = str(
            delegated_result.get("backend_agent_id") or ""
        ).strip()
        returned_protocol = str(
            delegated_result.get("protocol_version") or ""
        ).strip()
        admission_output = next(
            (
                artifact.get("value")
                for artifact in delegated_result.get("artifacts") or []
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
            delegated_status in {"queued", "running"}
            and returned_protocol == "2.0"
            and delegated_result.get("protocol_correlated") is True
            and identity_matches
            and not backend_run_id
            and isinstance(admission_evidence, Mapping)
            and admission_evidence.get("admissionPending") is True
            and admission_evidence.get("terminal") is False
        )
        if admission_pending:
            claim_renewed = False
            with kb.write_txn(conn):
                recorded = kb.merge_active_run_metadata(
                    conn,
                    execution_task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                ) and kb.record_backend_lifecycle(
                    conn,
                    execution_task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    workspace_ref=str(READONLY_BROWSER_WORKSPACE),
                    result_digest=_canonical_digest(delegated_result),
                    next_poll_seconds=2,
                )
                if recorded:
                    claim_renewed = kb.renew_external_backend_claim(
                        conn,
                        execution_task_id,
                        expected_run_id=run_id,
                    )
            if not recorded:
                kb.block_task(
                    conn,
                    execution_task_id,
                    reason=(
                        "OpenClaw browser admission could not be durably "
                        "scheduled for reconciliation."
                    ),
                    kind="capability",
                    expected_run_id=run_id,
                )
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": "blocked",
                    "delegated_result": delegated_result,
                }
            return {
                "execution_task_id": execution_task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": "retrying",
                "routing_decision": routing_decision,
                "delegated_result": delegated_result,
                "deduplicated": replaying_delegation,
                "claim_renewed": claim_renewed,
            }
        lifecycle_identity_valid = (
            returned_protocol == "2.0"
            and delegated_result.get("protocol_correlated") is True
            and identity_matches
            and backend_run_id
            and backend_agent_id == READONLY_BROWSER_AGENT
            and str(
                delegated_result.get("backend_session_key") or ""
            ).strip()
        )
        if (
            lifecycle_identity_valid
            and delegated_status in {"queued", "running"}
        ):
            backend_session_key = str(
                delegated_result["backend_session_key"]
            ).strip()
            with kb.write_txn(conn):
                recorded = kb.merge_active_run_metadata(
                    conn,
                    execution_task_id,
                    expected_run_id=run_id,
                    metadata={
                        "backend_session_key": backend_session_key,
                    },
                ) and kb.record_backend_lifecycle(
                    conn,
                    execution_task_id,
                    expected_run_id=run_id,
                    status=delegated_status,
                    backend_run_id=backend_run_id,
                    backend_agent_id=backend_agent_id,
                    protocol_version=returned_protocol,
                    workspace_ref=str(READONLY_BROWSER_WORKSPACE),
                    result_digest=_canonical_digest(delegated_result),
                    next_poll_seconds=next_poll_delay_seconds(1),
                )
                if recorded:
                    recorded = kb.renew_external_backend_claim(
                        conn,
                        execution_task_id,
                        expected_run_id=run_id,
                    )
                if recorded:
                    kb.record_backend_circuit_outcome(
                        conn,
                        "openclaw",
                        succeeded=True,
                        expected_generation=circuit_generation,
                    )
            if not recorded:
                lifecycle_error = (
                    "Active browser backend lifecycle could not be durably "
                    "renewed; exact cancellation cleanup is pending."
                )
                with kb.write_txn(conn):
                    recovery_saved = kb.merge_active_run_metadata(
                        conn,
                        execution_task_id,
                        expected_run_id=run_id,
                        metadata={
                            "backend_session_key": backend_session_key,
                            "stop_rule_cleanup_pending": True,
                            "stop_rule_reason": lifecycle_error,
                            "cleanup_attempt_count": 0,
                            "cleanup_attempt_limit": 3,
                            "cleanup_deadline_at": (
                                int(kb.time.time()) + 120
                            ),
                        },
                    ) and kb.record_backend_lifecycle(
                        conn,
                        execution_task_id,
                        expected_run_id=run_id,
                        status=delegated_status,
                        backend_run_id=backend_run_id,
                        backend_agent_id=backend_agent_id,
                        protocol_version=returned_protocol,
                        workspace_ref=str(READONLY_BROWSER_WORKSPACE),
                        result_digest=_canonical_digest(
                            delegated_result
                        ),
                        next_poll_seconds=1,
                    )
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": (
                        "running" if recovery_saved else "retrying"
                    ),
                    "review_errors": [lifecycle_error],
                    "delegated_result": delegated_result,
                }
            return {
                "execution_task_id": execution_task_id,
                "review_task_id": review_task_id,
                "status": delegated_status,
                "deduplicated": replaying_delegation,
                "backend_run_id": backend_run_id,
                "backend_agent_id": backend_agent_id,
                "backend_session_key": backend_session_key,
                "routing_decision": routing_decision,
                "delegated_result": delegated_result,
            }
        if not kb.record_backend_lifecycle(
            conn,
            execution_task_id,
            expected_run_id=run_id,
            status=(
                delegated_status
                if (
                    lifecycle_identity_valid
                    and delegated_status in {"succeeded", "failed", "blocked"}
                )
                else "failed"
            ),
            backend_run_id=(backend_run_id if lifecycle_identity_valid else None),
            backend_agent_id=(
                backend_agent_id if lifecycle_identity_valid else None
            ),
            protocol_version=(
                returned_protocol if lifecycle_identity_valid else None
            ),
            workspace_ref=(
                str(READONLY_BROWSER_WORKSPACE)
                if lifecycle_identity_valid
                else None
            ),
            result_digest=result_digest,
            terminal_observation={
                "status": (
                    delegated_status
                    if delegated_status in {"succeeded", "failed", "blocked"}
                    else "failed"
                ),
                "delegated_result": delegated_result,
            },
        ):
            completed_task = kb.get_task(conn, execution_task_id)
            completed_run = kb.get_run(conn, run_id)
            completed_metadata = (
                completed_run.metadata if completed_run is not None else {}
            )
            matching_concurrent_completion = (
                completed_task is not None
                and completed_task.status == "done"
                and completed_run is not None
                and completed_run.status == "done"
                and completed_run.outcome == "completed"
                and completed_run.backend_status == "succeeded"
                and completed_run.result_digest == result_digest
                and isinstance(completed_metadata, Mapping)
                and completed_metadata.get("contract_fingerprint")
                == fingerprint
                and completed_metadata.get("delegation_id")
                == expected_delegation_id
                and completed_metadata.get("attempt_id")
                == expected_attempt_id
            )
            if matching_concurrent_completion:
                accepted, errors = _complete_or_resume_grace_review(
                    conn,
                    execution_task_id=execution_task_id,
                    review_task_id=review_task_id,
                    execution_run=completed_run,
                    expected_url=url,
                )
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": "succeeded" if accepted else "blocked",
                    "deduplicated": True,
                    "backend_run_id": completed_run.backend_run_id,
                    "backend_agent_id": completed_run.backend_agent_id,
                    "backend_session_key": completed_metadata.get(
                        "backend_session_key"
                    ),
                    "result_digest": completed_run.result_digest,
                    "routing_decision": routing_decision,
                    "delegated_result": delegated_result,
                    **({"review_errors": errors} if errors else {}),
                }
            terminal_observation = (
                completed_metadata.get("backend_terminal_observation")
                if isinstance(completed_metadata, Mapping)
                else None
            )
            persisted_result = (
                terminal_observation.get("delegated_result")
                if isinstance(terminal_observation, Mapping)
                else None
            )
            matching_pending_completion = (
                completed_task is not None
                and completed_task.status == "running"
                and completed_run is not None
                and completed_run.status == "running"
                and completed_run.backend_status == "succeeded"
                and isinstance(persisted_result, Mapping)
                and _terminal_evidence_digest(persisted_result)
                == result_digest
                and completed_metadata.get("contract_fingerprint")
                == fingerprint
                and completed_metadata.get("delegation_id")
                == expected_delegation_id
                and completed_metadata.get("attempt_id")
                == expected_attempt_id
            )
            if matching_pending_completion:
                accepted, errors, persisted_digest = (
                    _finalize_readonly_terminal(
                        conn,
                        execution_task_id=execution_task_id,
                        review_task_id=review_task_id,
                        execution_run=completed_run,
                        delegated_result=persisted_result,
                        expected_url=url,
                        expected_delegation_id=expected_delegation_id,
                        expected_attempt_id=expected_attempt_id,
                        expected_contract_fingerprint=fingerprint,
                        expected_circuit_generation=circuit_generation,
                    )
                )
                return {
                    "execution_task_id": execution_task_id,
                    "review_task_id": review_task_id,
                    "status": "succeeded" if accepted else "blocked",
                    "deduplicated": True,
                    "backend_run_id": completed_run.backend_run_id,
                    "backend_agent_id": completed_run.backend_agent_id,
                    "backend_session_key": persisted_result.get(
                        "backend_session_key"
                    ),
                    "result_digest": persisted_digest,
                    "routing_decision": routing_decision,
                    "delegated_result": persisted_result,
                    **({"review_errors": errors} if errors else {}),
                }
            lifecycle_error = (
                "Backend lifecycle could not be uniquely bound to the active "
                "Kanban attempt."
            )
            kb.block_task(
                conn,
                execution_task_id,
                reason=lifecycle_error,
                kind="capability",
                expected_run_id=run_id,
            )
            return {
                "execution_task_id": execution_task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "review_errors": [lifecycle_error],
                "delegated_result": delegated_result,
            }
        execution_run = kb.latest_run(conn, execution_task_id)
        if execution_run is None:
            raise RuntimeError("Terminal execution task has no run evidence.")
        accepted, errors, result_digest = _finalize_readonly_terminal(
            conn,
            execution_task_id=execution_task_id,
            review_task_id=review_task_id,
            execution_run=execution_run,
            delegated_result=delegated_result,
            expected_url=url,
            expected_delegation_id=expected_delegation_id,
            expected_attempt_id=expected_attempt_id,
            expected_contract_fingerprint=fingerprint,
            expected_circuit_generation=circuit_generation,
        )
        if not accepted:
            return {
                "execution_task_id": execution_task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "review_errors": errors,
                "delegated_result": delegated_result,
            }

    return {
        "execution_task_id": execution_task_id,
        "review_task_id": review_task_id,
        "status": "succeeded",
        "deduplicated": replaying_delegation,
        "backend_run_id": delegated_result["backend_run_id"],
        "backend_agent_id": delegated_result["backend_agent_id"],
        "backend_session_key": delegated_result["backend_session_key"],
        "result_digest": result_digest,
        "routing_decision": routing_decision,
        "delegated_result": delegated_result,
    }


def _readonly_browser_delegation_args(
    run: kb.Run,
    *,
    openclaw_task_id: str,
    idempotency_key: str,
    start_idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    metadata = run.metadata or {}
    requested_url = str(metadata.get("requested_url") or "").strip()
    if not requested_url:
        raise ValueError("Browser run is missing its durable requested URL.")
    if metadata.get("allowed_urls") != [requested_url]:
        raise ValueError("Browser run is missing its exact durable URL authority.")
    args: dict[str, Any] = {
        "task_id": run.task_id,
        "objective": (
            "Cancel and clean up the exact read-only browser snapshot run."
            if openclaw_task_id
            == "openclaw.browser.read_snapshot_cancel"
            else "Resume the exact read-only browser snapshot run."
        ),
        "context_refs": [
            f"kanban:{run.task_id}",
            f"loop-contract:{metadata['contract_fingerprint']}",
        ],
        "allowed_tools": ["browser.read"],
        "denied_tools": [
            "browser.click",
            "browser.type",
            "browser.upload",
            "message.send",
            "shell",
            "filesystem.write",
        ],
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
        "executor_profile": "browser-readonly",
        "backend_agent_id": READONLY_BROWSER_AGENT,
        "model_route": dict(metadata.get("model_route") or {}),
        "external_effect_budget": 0,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": [],
        "idempotency_key": idempotency_key,
        "openclaw_task_id": openclaw_task_id,
        "target_url": requested_url,
        "allowed_urls": [requested_url],
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


def make_readonly_browser_poll_adapter(
    *,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> Callable[[kb.Run], Mapping[str, Any]]:
    """Replay or cancel an admitted browser run from durable correlation."""

    def poll(run: kb.Run) -> Mapping[str, Any]:
        metadata = run.metadata or {}
        start_key = str(
            metadata.get("start_idempotency_key")
            or metadata.get("idempotency_key")
            or ""
        ).strip()
        if not start_key:
            raise ValueError(
                "Browser run is missing durable start correlation."
            )
        if (
            not run.backend_run_id
            and metadata.get("admission_ambiguous") is True
        ):
            result = delegate_to_openclaw(
                _readonly_browser_delegation_args(
                    run,
                    openclaw_task_id="openclaw.browser.read_snapshot",
                    idempotency_key=start_key,
                ),
                transport=transport,
                policy_path=policy_path,
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
                    f"Unexpected OpenClaw browser admission status={status!r}."
                )
            if not _result_identity_matches(
                result,
                expected_delegation_id=str(
                    metadata.get("delegation_id") or ""
                ),
                expected_attempt_id=str(
                    metadata.get("attempt_id") or ""
                ),
                expected_contract_fingerprint=str(
                    metadata.get("contract_fingerprint") or ""
                ),
            ):
                raise ValueError(
                    "OpenClaw browser admission replay identity did not match."
                )
            returned_run_id = str(
                result.get("backend_run_id") or ""
            ).strip()
            returned_agent_id = str(
                result.get("backend_agent_id") or ""
            ).strip()
            returned_session_key = str(
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
                and not returned_run_id
                and result.get("protocol_version") == "2.0"
                and result.get("protocol_correlated") is True
                and isinstance(admission_evidence, Mapping)
                and admission_evidence.get("admissionPending") is True
                and admission_evidence.get("terminal") is False
            ):
                return {
                    "status": "queued",
                    "protocol_version": "2.0",
                    "result_digest": _canonical_digest(result),
                    "delegated_result": result,
                }
            if (
                status in {"failed", "blocked"}
                and not returned_run_id
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
                result.get("protocol_version") != "2.0"
                or result.get("protocol_correlated") is not True
                or not returned_run_id
                or returned_agent_id != READONLY_BROWSER_AGENT
                or not returned_session_key
            ):
                raise ValueError(
                    "OpenClaw browser admission replay returned incomplete "
                    "backend identity."
                )
            return {
                "status": status,
                "backend_run_id": returned_run_id,
                "backend_agent_id": returned_agent_id,
                "backend_session_key": returned_session_key,
                "protocol_version": "2.0",
                "result_digest": (
                    _terminal_evidence_digest(result)
                    if status in {"succeeded", "failed", "blocked"}
                    else _canonical_digest(result)
                ),
                "delegated_result": result,
            }
        if not run.backend_run_id:
            raise ValueError(
                "Browser run is missing durable backend correlation."
            )
        stop_cleanup_pending = bool(
            metadata.get("stop_rule_cleanup_pending")
        )
        result = delegate_to_openclaw(
            _readonly_browser_delegation_args(
                run,
                openclaw_task_id=(
                    "openclaw.browser.read_snapshot_cancel"
                    if stop_cleanup_pending
                    else "openclaw.browser.read_snapshot_poll"
                ),
                idempotency_key=(
                    (
                        f"{start_key}:cancel:"
                        f"{run.backend_poll_count + 1}"
                    )
                    if stop_cleanup_pending
                    else (
                        f"{start_key}:poll:"
                        f"{run.backend_poll_count + 1}"
                    )
                ),
                start_idempotency_key=(
                    start_key
                ),
            ),
            transport=transport,
            policy_path=policy_path,
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
                f"Unexpected OpenClaw browser poll status={status!r}."
            )
        if not _result_identity_matches(
            result,
            expected_delegation_id=str(metadata.get("delegation_id") or ""),
            expected_attempt_id=str(metadata.get("attempt_id") or ""),
            expected_contract_fingerprint=str(
                metadata.get("contract_fingerprint") or ""
            ),
        ):
            raise ValueError(
                "OpenClaw browser replay identity did not match its Kanban run."
            )
        returned_run_id = str(result.get("backend_run_id") or "").strip()
        returned_agent_id = str(
            result.get("backend_agent_id") or ""
        ).strip()
        returned_session_key = str(
            result.get("backend_session_key") or ""
        ).strip()
        expected_session_key = str(
            metadata.get("backend_session_key") or ""
        ).strip()
        if (
            result.get("protocol_version") != "2.0"
            or result.get("protocol_correlated") is not True
            or returned_run_id != run.backend_run_id
            or returned_agent_id != READONLY_BROWSER_AGENT
            or returned_session_key != expected_session_key
        ):
            raise ValueError(
                "OpenClaw browser replay returned different backend identity."
            )
        cleanup_in_progress = (
            stop_cleanup_pending
            and status in {"queued", "running"}
        )
        if (
            stop_cleanup_pending
            and not cleanup_in_progress
            and not _browser_cleanup_evidence_valid(
                result,
                run=run,
                metadata=metadata,
            )
        ):
            raise ValueError(
                "OpenClaw browser cancellation did not prove exact terminal "
                "resource cleanup."
            )
        lifecycle_status = (
            "running"
            if cleanup_in_progress and run.backend_status == "running"
            else status
        )
        return {
            "status": lifecycle_status,
            "backend_run_id": returned_run_id,
            "backend_agent_id": returned_agent_id,
            "backend_session_key": returned_session_key,
            "protocol_version": "2.0",
            "result_digest": (
                _terminal_evidence_digest(result)
                if status in {"succeeded", "failed", "blocked"}
                else _canonical_digest(result)
            ),
            "delegated_result": result,
            "stop_cleanup_pending": stop_cleanup_pending,
            "cleanup_in_progress": cleanup_in_progress,
        }

    return poll


def _browser_cleanup_evidence_valid(
    result: Mapping[str, Any],
    *,
    run: kb.Run,
    metadata: Mapping[str, Any],
) -> bool:
    output = _result_output(result)
    evidence = (
        output.get("evidence") if isinstance(output, Mapping) else None
    )
    return (
        result.get("status") == "blocked"
        and result.get("protocol_version") == "2.0"
        and result.get("protocol_correlated") is True
        and _result_identity_matches(
            result,
            expected_delegation_id=str(
                metadata.get("delegation_id") or ""
            ),
            expected_attempt_id=str(metadata.get("attempt_id") or ""),
            expected_contract_fingerprint=str(
                metadata.get("contract_fingerprint") or ""
            ),
        )
        and result.get("backend_run_id") == run.backend_run_id
        and result.get("backend_agent_id") == READONLY_BROWSER_AGENT
        and result.get("backend_session_key")
        == metadata.get("backend_session_key")
        and isinstance(evidence, Mapping)
        and evidence.get("externalEffectBudget") == 0
        and evidence.get("sideEffectsPerformed") is False
        and evidence.get("terminal") is True
        and evidence.get("cancellationRequested") is True
        and evidence.get("terminationProven") is True
        and evidence.get("sessionCleaned") is True
        and evidence.get("browserTabsCleaned") is True
    )


def make_readonly_browser_terminal_handler(
    *,
    board: Optional[str] = None,
) -> Callable[[kb.Run, Mapping[str, Any]], Mapping[str, Any]]:
    """Close a browser run only after terminal or cancellation evidence."""

    def handle(run: kb.Run, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = run.metadata or {}
        result = observation.get("delegated_result")
        if not isinstance(result, Mapping):
            raise ValueError(
                "Browser terminal observation is missing delegated evidence."
            )
        circuit_value = observation.get("circuit_generation")
        circuit_generation = (
            int(circuit_value) if circuit_value is not None else None
        )
        if metadata.get("stop_rule_cleanup_pending"):
            cleanup_valid = (
                observation.get("status") == "blocked"
                and _browser_cleanup_evidence_valid(
                    result,
                    run=run,
                    metadata=metadata,
                )
            )
            reason = str(
                metadata.get("stop_rule_reason")
                or "Browser execution was cancelled by its stop rule."
            )
            with kb.connect_closing(board=board) as conn:
                with kb.write_txn(conn):
                    kb.record_backend_circuit_outcome(
                        conn,
                        "openclaw",
                        succeeded=cleanup_valid,
                        error=(
                            None
                            if cleanup_valid
                            else "Browser cancellation evidence failed review."
                        ),
                        expected_generation=circuit_generation,
                    )
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=(
                            reason
                            if cleanup_valid
                            else (
                                "Browser cancellation did not prove exact "
                                "terminal resource cleanup."
                            )
                        ),
                        kind=(
                            "transient"
                            if cleanup_valid
                            else "capability"
                        ),
                        expected_run_id=run.id,
                    )
            return {"accepted": False, "cleanup_verified": cleanup_valid}

        review_task_id = str(metadata.get("review_task_id") or "")
        requested_url = str(metadata.get("requested_url") or "")
        with kb.connect_closing(board=board) as conn:
            accepted, errors, digest = _finalize_readonly_terminal(
                conn,
                execution_task_id=run.task_id,
                review_task_id=review_task_id,
                execution_run=run,
                delegated_result=result,
                expected_url=requested_url,
                expected_delegation_id=str(
                    metadata.get("delegation_id") or ""
                ),
                expected_attempt_id=str(
                    metadata.get("attempt_id") or ""
                ),
                expected_contract_fingerprint=str(
                    metadata.get("contract_fingerprint") or ""
                ),
                expected_circuit_generation=int(circuit_generation or 0),
            )
        return {
            "accepted": accepted,
            "review_errors": errors,
            "result_digest": digest,
        }

    return handle
