"""Lease, poll, and resume asynchronous execution-backend runs."""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Callable, Iterable, Mapping, Optional
import uuid

from hermes_cli import kanban_db as kb
from proactive.execution_backends import (
    TERMINAL_BACKEND_STATUSES,
    next_poll_delay_seconds,
)


BackendPollAdapter = Callable[[kb.Run], Mapping[str, Any]]
BackendTerminalHandler = Callable[[kb.Run, Mapping[str, Any]], Mapping[str, Any]]


def _read_poll_control(metadata: Mapping[str, Any], key: str) -> int:
    value = metadata.get(key)
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-negative integer.") from exc
    if parsed < 0 or str(value).strip() != str(parsed):
        raise ValueError(f"{key} must be a non-negative integer.")
    return parsed


@dataclass(frozen=True)
class BackendPollWorkerResult:
    claimed: int
    observed: int
    terminal: int
    retried: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "observed": self.observed,
            "terminal": self.terminal,
            "retried": self.retried,
            "errors": list(self.errors),
        }


def default_poll_owner() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex}"


def poll_due_backend_runs(
    *,
    adapters: Mapping[str, BackendPollAdapter],
    terminal_handlers: Optional[Mapping[str, BackendTerminalHandler]] = None,
    executor_profiles: Optional[Iterable[str]] = None,
    board: Optional[str] = None,
    owner: Optional[str] = None,
    limit: int = 20,
    lease_seconds: int = 30,
    now: Optional[int] = None,
) -> BackendPollWorkerResult:
    """Poll due runs once without holding a database transaction over I/O."""
    poll_owner = str(owner or default_poll_owner())
    remaining = int(limit)
    if remaining <= 0:
        raise ValueError("poll limit must be positive")

    claimed_count = 0
    observed = 0
    terminal = 0
    retried = 0
    errors: list[str] = []
    handlers = dict(terminal_handlers or {})
    claimed_run_ids: set[int] = set()

    while remaining > 0:
        with kb.connect_closing(board=board) as conn:
            claimed = kb.claim_due_backend_polls(
                conn,
                owner=poll_owner,
                executor_backends=tuple(adapters),
                executor_profiles=executor_profiles,
                exclude_run_ids=claimed_run_ids,
                limit=1,
                lease_seconds=lease_seconds,
                now=now,
            )
        if not claimed:
            break
        run = claimed[0]
        claimed_run_ids.add(run.id)
        claimed_count += 1
        remaining -= 1
        terminal_observed = False
        metadata = dict(run.metadata or {})
        try:
            max_poll_iterations = _read_poll_control(
                metadata,
                "max_poll_iterations",
            )
            no_progress_limit = _read_poll_control(
                metadata,
                "no_progress_error_limit",
            )
        except ValueError as exc:
            error = f"Invalid backend poll-control metadata: {exc}"
            with kb.connect_closing(board=board) as conn:
                kb.block_task(
                    conn,
                    run.task_id,
                    reason=error,
                    kind="capability",
                    expected_run_id=run.id,
                )
            errors.append(error)
            continue
        stored_terminal_observation = metadata.get(
            "backend_terminal_observation"
        )
        replay_terminal_observation = (
            dict(stored_terminal_observation)
            if (
                run.backend_status in TERMINAL_BACKEND_STATUSES
                and isinstance(stored_terminal_observation, Mapping)
            )
            else None
        )
        stop_cleanup_pending = bool(
            metadata.get("stop_rule_cleanup_pending")
        )
        cleanup_attempt_count = int(
            metadata.get("cleanup_attempt_count") or 0
        )
        cleanup_attempt_limit = int(
            metadata.get("cleanup_attempt_limit") or 3
        )
        cleanup_deadline_at = int(
            metadata.get("cleanup_deadline_at") or 0
        )
        current_time = int(kb.time.time())
        if (
            stop_cleanup_pending
            and (
                cleanup_attempt_count >= cleanup_attempt_limit
                or (
                    cleanup_deadline_at > 0
                    and current_time >= cleanup_deadline_at
                )
            )
        ):
            error = (
                "Backend cancellation cleanup exhausted its bounded policy; "
                f"backend_run_id={run.backend_run_id or 'unknown'} remains "
                "retained for operator recovery."
            )
            with kb.connect_closing(board=board) as conn:
                kb.block_task(
                    conn,
                    run.task_id,
                    reason=error,
                    kind="capability",
                    expected_run_id=run.id,
                )
            errors.append(error)
            continue
        if (
            not stop_cleanup_pending
            and
            run.backend_status in {"queued", "running"}
            and max_poll_iterations > 0
            and run.backend_poll_count >= max_poll_iterations
        ):
            error = (
                "Loop Contract max_iterations reached before backend terminal "
                f"state: {run.backend_poll_count}/{max_poll_iterations}."
            )
            with kb.connect_closing(board=board) as conn:
                if run.backend_run_id:
                    with kb.write_txn(conn):
                        kb.merge_active_run_metadata(
                            conn,
                            run.task_id,
                            expected_run_id=run.id,
                            metadata={
                                "stop_rule_cleanup_pending": True,
                                "stop_rule_reason": error,
                                "cleanup_attempt_count": 0,
                                "cleanup_attempt_limit": 3,
                                "cleanup_deadline_at": (
                                    current_time
                                    + max(90, int(lease_seconds) * 3)
                                ),
                            },
                        )
                        kb.release_backend_poll_claim(
                            conn,
                            run_id=run.id,
                            owner=poll_owner,
                            retry_seconds=max(lease_seconds, 1),
                            error=error,
                            increment_poll_count=False,
                            now=now,
                        )
                    retried += 1
                else:
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=error,
                        kind="capability",
                        expected_run_id=run.id,
                    )
            errors.append(error)
            continue
        if (
            not stop_cleanup_pending
            and
            run.backend_status in {"queued", "running"}
            and run.max_runtime_seconds is not None
            and int(kb.time.time())
            >= int(run.started_at) + int(run.max_runtime_seconds)
        ):
            error = (
                "Loop Contract max_runtime_seconds reached before backend "
                f"terminal state: {run.max_runtime_seconds}s."
            )
            with kb.connect_closing(board=board) as conn:
                if run.backend_run_id:
                    with kb.write_txn(conn):
                        kb.merge_active_run_metadata(
                            conn,
                            run.task_id,
                            expected_run_id=run.id,
                            metadata={
                                "stop_rule_cleanup_pending": True,
                                "stop_rule_reason": error,
                                "cleanup_attempt_count": 0,
                                "cleanup_attempt_limit": 3,
                                "cleanup_deadline_at": (
                                    current_time
                                    + max(90, int(lease_seconds) * 3)
                                ),
                            },
                        )
                        kb.release_backend_poll_claim(
                            conn,
                            run_id=run.id,
                            owner=poll_owner,
                            retry_seconds=max(lease_seconds, 1),
                            error=error,
                            increment_poll_count=False,
                            now=now,
                        )
                    retried += 1
                else:
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=error,
                        kind="capability",
                        expected_run_id=run.id,
                    )
            errors.append(error)
            continue
        io_lease_seconds = max(
            int(lease_seconds),
            int(run.max_runtime_seconds or lease_seconds) + 30,
        )
        lease_now = int(kb.time.time())
        with kb.connect_closing(board=board) as conn:
            if not kb.renew_external_backend_claim(
                conn,
                run.task_id,
                expected_run_id=run.id,
                ttl_seconds=io_lease_seconds,
            ):
                error = (
                    f"External backend run lost its active Kanban claim: "
                    f"task={run.task_id} run={run.id}."
                )
                released = kb.release_backend_poll_claim(
                    conn,
                    run_id=run.id,
                    owner=poll_owner,
                    retry_seconds=max(lease_seconds, 1),
                    error=error,
                    increment_poll_count=False,
                    now=now,
                )
                errors.append(error)
                if released:
                    retried += 1
                continue
            if (
                replay_terminal_observation is None
                and not stop_cleanup_pending
            ):
                circuit_state = kb.backend_circuit_states(
                    conn,
                    now=lease_now,
                ).get(run.executor_backend, "closed")
                probe_claimed = (
                    circuit_state == "half_open"
                    and kb.claim_backend_circuit_probe(
                        conn,
                        run.executor_backend,
                        lease_seconds=io_lease_seconds,
                        now=lease_now,
                    )
                )
                if circuit_state == "open" or (
                    circuit_state == "half_open" and not probe_claimed
                ):
                    kb.release_backend_poll_claim(
                        conn,
                        run_id=run.id,
                        owner=poll_owner,
                        retry_seconds=max(lease_seconds, 1),
                        error=(
                            f"Backend circuit is {circuit_state}; "
                            "poll deferred."
                        ),
                        increment_poll_count=False,
                        now=now,
                    )
                    retried += 1
                    continue
            if not kb.renew_backend_poll_claim(
                conn,
                run_id=run.id,
                owner=poll_owner,
                lease_seconds=io_lease_seconds,
            ):
                error = (
                    f"Poll lease expired before backend I/O: "
                    f"task={run.task_id} run={run.id}."
                )
                errors.append(error)
                continue
            if stop_cleanup_pending and not kb.merge_active_run_metadata(
                conn,
                run.task_id,
                expected_run_id=run.id,
                metadata={
                    "cleanup_attempt_count": cleanup_attempt_count + 1,
                },
            ):
                errors.append(
                    "Cancellation cleanup attempt could not be persisted."
                )
                continue
            circuit_generation = (
                int(replay_terminal_observation["circuit_generation"])
                if replay_terminal_observation is not None
                else int(
                    kb.backend_circuit_snapshot(
                        conn,
                        run.executor_backend,
                    )["generation"]
                )
            )
        adapter = adapters.get(run.executor_backend)
        if adapter is None and replay_terminal_observation is None:
            error = f"No poll adapter registered for backend={run.executor_backend}."
            with kb.connect_closing(board=board) as conn:
                kb.release_backend_poll_claim(
                    conn,
                    run_id=run.id,
                    owner=poll_owner,
                    retry_seconds=next_poll_delay_seconds(
                        run.backend_poll_count + 1
                    ),
                    error=error,
                    now=now,
                )
            errors.append(error)
            retried += 1
            continue
        poll_lease_lost = False
        try:
            if replay_terminal_observation is None:
                assert adapter is not None
            observation = (
                replay_terminal_observation
                if replay_terminal_observation is not None
                else dict(adapter(run))
            )
            status = str(observation.get("status") or "").strip().lower()
            if status not in {"queued", "running", *TERMINAL_BACKEND_STATUSES}:
                raise ValueError(f"Unsupported backend poll status={status!r}.")
            backend_run_id = str(
                observation.get("backend_run_id") or run.backend_run_id or ""
            ).strip()
            backend_agent_id = str(
                observation.get("backend_agent_id") or run.backend_agent_id or ""
            ).strip()
            protocol_version = str(
                observation.get("protocol_version") or run.protocol_version or ""
            ).strip()
            if status in TERMINAL_BACKEND_STATUSES:
                observation["circuit_generation"] = circuit_generation
            with kb.connect_closing(board=board) as conn:
                with kb.write_txn(conn):
                    if not kb.renew_backend_poll_claim(
                        conn,
                        run_id=run.id,
                        owner=poll_owner,
                        lease_seconds=max(int(lease_seconds), 120),
                    ):
                        poll_lease_lost = True
                        raise RuntimeError(
                            f"Poll lease expired during backend I/O: "
                            f"task={run.task_id} run={run.id}."
                        )
                    recorded = kb.record_backend_lifecycle(
                        conn,
                        run.task_id,
                        expected_run_id=run.id,
                        status=status,
                        backend_run_id=backend_run_id,
                        backend_agent_id=backend_agent_id,
                        protocol_version=protocol_version,
                        workspace_ref=run.workspace_ref,
                        result_digest=(
                            str(
                                observation.get("result_digest") or ""
                            ).strip()
                            or None
                        ),
                        next_poll_seconds=(
                            next_poll_delay_seconds(
                                run.backend_poll_count + 1
                            )
                            if status in {"queued", "running"}
                            else None
                        ),
                        poll_owner=poll_owner,
                        terminal_observation=(
                            observation
                            if status in TERMINAL_BACKEND_STATUSES
                            else None
                        ),
                    )
                    recovered_session_key = str(
                        observation.get("backend_session_key") or ""
                    ).strip()
                    if recorded and recovered_session_key:
                        recorded = kb.merge_active_run_metadata(
                            conn,
                            run.task_id,
                            expected_run_id=run.id,
                            metadata={
                                "backend_session_key": (
                                    recovered_session_key
                                ),
                            },
                        )
                    if recorded:
                        refreshed_run = kb.get_run(conn, run.id)
                        if refreshed_run is not None:
                            run = refreshed_run
                    if recorded and status in {"queued", "running"}:
                        progress_metadata: dict[str, Any] = {
                            "last_poll_error": None,
                            "same_poll_error_count": 0,
                        }
                        if (
                            stop_cleanup_pending
                            and observation.get("cleanup_in_progress") is True
                        ):
                            progress_metadata["cleanup_attempt_count"] = (
                                cleanup_attempt_count
                            )
                        kb.merge_active_run_metadata(
                            conn,
                            run.task_id,
                            expected_run_id=run.id,
                            metadata=progress_metadata,
                        )
                        kb.record_backend_circuit_outcome(
                            conn,
                            run.executor_backend,
                            succeeded=True,
                            expected_generation=circuit_generation,
                        )
            if not recorded:
                raise RuntimeError(
                    f"Poll lease or lifecycle changed for task={run.task_id} run={run.id}."
                )
            observed += 1
            if status in TERMINAL_BACKEND_STATUSES:
                terminal_observed = True
                handler = handlers.get(run.executor_backend)
                if handler is None:
                    with kb.connect_closing(board=board) as conn:
                        kb.block_task(
                            conn,
                            run.task_id,
                            reason=(
                                "Backend reached terminal state without a registered "
                                "terminal evidence handler."
                            ),
                            kind="capability",
                            expected_run_id=run.id,
                        )
                else:
                    with kb.connect_closing(board=board) as conn:
                        if not kb.renew_backend_poll_claim(
                            conn,
                            run_id=run.id,
                            owner=poll_owner,
                            lease_seconds=max(int(lease_seconds), 120),
                        ):
                            poll_lease_lost = True
                            raise RuntimeError(
                                "Poll lease expired before terminal evidence "
                                f"handling: task={run.task_id} run={run.id}."
                            )
                    handler(run, observation)
                terminal += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(error)
            if poll_lease_lost:
                continue
            with kb.connect_closing(board=board) as conn:
                if not kb.renew_backend_poll_claim(
                    conn,
                    run_id=run.id,
                    owner=poll_owner,
                    lease_seconds=max(int(lease_seconds), 120),
                ):
                    poll_lease_lost = True
            if poll_lease_lost:
                continue
            if terminal_observed:
                with kb.connect_closing(board=board) as conn:
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=(
                            "Backend terminated, but its terminal evidence "
                            f"handler failed: {error}"
                        ),
                        kind="capability",
                        expected_run_id=run.id,
                    )
            else:
                with kb.connect_closing(board=board) as conn:
                    with kb.write_txn(conn):
                        previous_error = str(
                            metadata.get("last_poll_error") or ""
                        )
                        same_error_count = (
                            int(metadata.get("same_poll_error_count") or 0) + 1
                            if previous_error == error
                            else 1
                        )
                        kb.merge_active_run_metadata(
                            conn,
                            run.task_id,
                            expected_run_id=run.id,
                            metadata={
                                "last_poll_error": error,
                                "same_poll_error_count": same_error_count,
                            },
                        )
                        should_stop = (
                            no_progress_limit > 0
                            and same_error_count >= no_progress_limit
                        )
                        if should_stop:
                            stop_error = (
                                "Loop Contract no_progress rule reached after "
                                f"{same_error_count} identical poll errors: {error}"
                            )
                            if run.backend_run_id:
                                kb.merge_active_run_metadata(
                                    conn,
                                    run.task_id,
                                    expected_run_id=run.id,
                                    metadata={
                                        "stop_rule_cleanup_pending": True,
                                        "stop_rule_reason": stop_error,
                                        "cleanup_attempt_count": 0,
                                        "cleanup_attempt_limit": 3,
                                        "cleanup_deadline_at": (
                                            int(kb.time.time())
                                            + max(
                                                90,
                                                int(lease_seconds) * 3,
                                            )
                                        ),
                                    },
                                )
                                released = kb.release_backend_poll_claim(
                                    conn,
                                    run_id=run.id,
                                    owner=poll_owner,
                                    retry_seconds=max(lease_seconds, 1),
                                    error=stop_error,
                                    now=now,
                                )
                            else:
                                kb.record_backend_circuit_outcome(
                                    conn,
                                    run.executor_backend,
                                    succeeded=False,
                                    error=error,
                                    expected_generation=circuit_generation,
                                )
                                kb.block_task(
                                    conn,
                                    run.task_id,
                                    reason=stop_error,
                                    kind="capability",
                                    expected_run_id=run.id,
                                )
                                released = False
                        else:
                            released = kb.release_backend_poll_claim(
                                conn,
                                run_id=run.id,
                                owner=poll_owner,
                                retry_seconds=next_poll_delay_seconds(
                                    run.backend_poll_count + 1
                                ),
                                error=error,
                                now=now,
                            )
                            if released:
                                kb.record_backend_circuit_outcome(
                                    conn,
                                    run.executor_backend,
                                    succeeded=False,
                                    error=error,
                                    expected_generation=circuit_generation,
                                )
                if released:
                    retried += 1

    return BackendPollWorkerResult(
        claimed=claimed_count,
        observed=observed,
        terminal=terminal,
        retried=retried,
        errors=tuple(errors),
    )


def poll_due_openclaw_runs(
    *,
    board: Optional[str] = None,
    owner: Optional[str] = None,
    limit: int = 20,
    lease_seconds: int = 30,
    now: Optional[int] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> BackendPollWorkerResult:
    """Poll the production OpenClaw adapter with its mandatory evidence gate."""
    from proactive.openclaw_async_executor import (
        make_loop_contract_poll_adapter,
        make_loop_contract_terminal_handler,
        make_zero_effect_async_poll_adapter,
        make_zero_effect_async_terminal_handler,
    )
    from proactive.openclaw_executor import (
        make_readonly_browser_poll_adapter,
        make_readonly_browser_terminal_handler,
    )

    async_poll = make_zero_effect_async_poll_adapter(
        transport=transport,
        policy_path=policy_path,
    )
    loop_poll = make_loop_contract_poll_adapter(
        transport=transport,
        policy_path=policy_path,
    )
    browser_poll = make_readonly_browser_poll_adapter(
        transport=transport,
        policy_path=policy_path,
    )
    async_terminal = make_zero_effect_async_terminal_handler(board=board)
    loop_terminal = make_loop_contract_terminal_handler(board=board)
    browser_terminal = make_readonly_browser_terminal_handler(board=board)

    def openclaw_profile(run: kb.Run) -> str:
        profile = str(
            (run.metadata or {}).get("executor_profile") or ""
        ).strip().lower().replace("_", "-")
        if not profile:
            profile = str(run.profile or "").strip().lower().replace("_", "-")
            if not profile:
                return {
                    "clawops-ops": "zero-effect-async",
                    "clawops-browser": "browser-readonly",
                }.get(profile, "zero-effect-async")

        profile = {
            "zero_effect_async": "zero-effect-async",
            "browser_readonly": "browser-readonly",
            "zero-effect-async": "zero-effect-async",
            "browser-readonly": "browser-readonly",
            "loop-contract": "loop-contract",
        }.get(profile, profile)

        if profile in ("zero-effect-async", "browser-readonly", "loop-contract"):
            return profile

        return {
            "clawops-ops": "zero-effect-async",
            "clawops-browser": "browser-readonly",
        }.get(str(run.profile or "").strip().lower().replace("_", "-"), "zero-effect-async")

    def poll_openclaw(run: kb.Run) -> Mapping[str, Any]:
        profile = openclaw_profile(run)
        if profile == "zero-effect-async":
            return async_poll(run)
        if profile == "browser-readonly":
            return browser_poll(run)
        if profile == "loop-contract":
            return loop_poll(run)
        return async_poll(run)  # fallback to safe no-effect path when profile metadata is unexpected

    def handle_openclaw_terminal(
        run: kb.Run,
        observation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        profile = openclaw_profile(run)
        if profile == "zero-effect-async":
            return async_terminal(run, observation)
        if profile == "browser-readonly":
            return browser_terminal(run, observation)
        if profile == "loop-contract":
            return loop_terminal(run, observation)
        return async_terminal(run, observation)  # fallback to safe terminal handling for unexpected profile

    return poll_due_backend_runs(
        adapters={"openclaw": poll_openclaw},
        terminal_handlers={"openclaw": handle_openclaw_terminal},
        executor_profiles=("zero-effect-async", "browser-readonly", "loop-contract"),
        board=board,
        owner=owner,
        limit=limit,
        lease_seconds=lease_seconds,
        now=now,
    )
