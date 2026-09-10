"""Phase-one provenance only: never select rules or modify a Loop Contract.

Source hashes describe a bounded on-disk snapshot, NOT the code loaded by every
worker, nor a pinned behavior implementation. Unversioned fields stay explicit.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
import hashlib
import json
import logging
from pathlib import Path
import time


logger = logging.getLogger(__name__)
_sink = ContextVar("behavior_observation_sink", default=None)
VALIDATOR_SOURCES = (
    "proactive/loop_contract.py",
    "proactive/policy_registry.py",
    "proactive/domain_memory.py",
    "hermes_cli/facebook_group_preflight.py",
)
BEHAVIOR_SOURCES = VALIDATOR_SOURCES + (
    "proactive/grace_task_compiler.py",
    "proactive/hubops_routing.py",
    "proactive/openclaw_async_executor.py",
    "proactive/backend_poll_worker.py",
    "plugins/openclaw_bridge/clawops_delegate.py",
    "gateway/kanban_watchers.py",
    "hermes_cli/kanban_db.py",
    "hermes_cli/objective_workflow.py",
)


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


@lru_cache(maxsize=1)
def source_snapshot():
    """Hash listed sources once per observer process; missing files are visible."""
    root = Path(__file__).resolve().parents[1]
    sources = {}
    for name in BEHAVIOR_SOURCES:
        try:
            sources[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            sources[name] = None
    return int(time.time()), sources


def metadata(contract=None):
    contract = contract or {}
    snapshot_at, sources = source_snapshot()
    validators = {name: sources[name] for name in VALIDATOR_SOURCES}
    return {
        "mode": "observe_only",
        "behavior_profile_id": "legacy_shared",
        "behavior_profile_version": None,
        "contract_schema_version": contract.get("contract_version"),
        "validator_set_hash": digest(validators) if all(validators.values()) else None,
        "validator_set_scope": "listed_validator_sources_only",
        "behavior_bundle_hash": digest(sources) if all(sources.values()) else None,
        "safety_kernel_version": None,
        "policy_snapshot_hash": (
            digest(contract["policy_snapshots"]) if "policy_snapshots" in contract else None
        ),
        "policy_snapshot_verified": False,
        "policy_binding_snapshot_hash": (
            digest(contract["policy_binding_snapshot"])
            if "policy_binding_snapshot" in contract else None
        ),
        "source_evidence": "listed_files_on_disk_at_first_observation",
        "source_snapshot_observed_at": snapshot_at,
        "source_manifest": dict(sources),
        "runtime_version_verified": False,
    }


@contextmanager
def capture(sink):
    """Observe locally (e.g. replay JSONL); nested/parallel contexts stay separate."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def emit(*, rule_id, owner, phase, decision, contract=None, reason=None, **details):
    """Best effort. Never log raw contracts, policy content, or error messages."""
    try:
        identity = (contract or {}).get("identity") or {}
        objective = (contract or {}).get("objective_ref") or {}
        event = {
            **metadata(contract),
            "event_schema_version": 1,
            "observed_at": int(time.time()),
            "rule_id": rule_id, "owner": owner, "phase": phase,
            "decision": decision,
            "project": identity.get("project"),
            "platform": identity.get("platform"),
            "chat_id": identity.get("chat_id"),
            "thread_id": identity.get("thread_id"),
            "request_instance_id": identity.get("request_instance_id"),
            "objective_id": objective.get("objective_id"),
            "stage_key": objective.get("stage_key"),
            "input_hash": digest(contract) if contract is not None else None,
            "reason_hash": digest(str(reason)) if reason is not None else None,
            **details,
        }
        # Serialize before handing a copy to an optional sink: neither can mutate
        # execution inputs, and sink/logging failures cannot become task blockers.
        serialized = json.dumps(event, ensure_ascii=False, sort_keys=True)
        sink = _sink.get()
        if sink is not None:
            try:
                sink(json.loads(serialized))
            except Exception:
                pass
        logger.info("behavior_observation %s", serialized)
    except Exception:
        pass


def observe_contract(rule_id, *, phase):
    """Boundary receipt; specific validator failures have their own rule IDs."""
    def decorate(function):
        @wraps(function)
        def observed(contract, *args, **kwargs):
            try:
                result = function(contract, *args, **kwargs)
            except Exception as exc:
                emit(rule_id=rule_id, owner=function.__module__, phase=phase,
                     decision="raised", contract=contract, reason=exc,
                     error_type=type(exc).__name__)
                raise
            emit(rule_id=rule_id, owner=function.__module__, phase=phase,
                 decision="returned", contract=contract)
            return result
        return observed
    return decorate


def objective_state(conn, objective_id):
    try:
        cursor = conn.execute(
            "SELECT * FROM grace_objectives WHERE objective_id=?",
            (objective_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        values = dict(zip((column[0] for column in cursor.description), row))
        return {key: values.get(key) for key in ("status", "current_stage_key", "revision")}
    except Exception:
        return None


def observe_objective(rule_id):
    def decorate(function):
        @wraps(function)
        def observed(conn, *args, **kwargs):
            callback = kwargs.get("callback") or {}
            review_task_id = kwargs.get("review_task_id") or kwargs.get("task_id") or (args[0] if args else None)
            if not callback and review_task_id:
                try:
                    row = conn.execute(
                        "SELECT objective_id,stage_key FROM grace_loop_callbacks WHERE review_task_id=?",
                        (review_task_id,),
                    ).fetchone()
                    if row:
                        callback = {"objective_id": row[0], "stage_key": row[1]}
                except Exception:
                    pass
            objective_id = kwargs.get("objective_id") or callback.get("objective_id")
            before = objective_state(conn, objective_id)
            try:
                result = function(conn, *args, **kwargs)
            except Exception as exc:
                emit(rule_id=rule_id, owner=function.__module__, phase="objective",
                     decision="raised", reason=exc, objective_id=objective_id,
                     before=before, after=objective_state(conn, objective_id),
                     error_type=type(exc).__name__)
                raise
            emit(rule_id=rule_id, owner=function.__module__, phase="objective",
                 decision="returned", objective_id=objective_id,
                 reason=kwargs.get("reason") or (kwargs.get("payload") or {}).get("reason"),
                 stage_key=callback.get("stage_key"),
                 outcome_kind=kwargs.get("kind") or kwargs.get("outcome_kind"),
                 before=before, after=objective_state(conn, objective_id),
                 transaction_pending=conn.in_transaction)
            return result
        return observed
    return decorate


def record_created_objective(conn, objective_id):
    """Creation-time sidecar; never backfill/relabel an existing Objective.

    Run only after business commit: SQLite FULL/ROLLBACK can abort an entire
    transaction, even when its Python exception is caught. For caller-owned
    transactions retain only the pending log receipt, never risk their writes.
    Later Contract receipts join by objective_ref; no executable pin is implied.
    """
    try:
        if conn.in_transaction:
            emit(rule_id="objective.observation_storage", owner=__name__, phase="observation",
                 decision="not_persisted", objective_id=objective_id,
                 reason_code="caller_transaction_pending", transaction_pending=True)
            return
        value = metadata()
        conn.execute(
            "INSERT OR IGNORE INTO grace_objective_behavior_observations "
            "(objective_id,behavior_profile_id,behavior_profile_version,"
            "contract_schema_version,validator_set_hash,policy_snapshot_hash,"
            "behavior_bundle_hash,safety_kernel_version,observation,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (objective_id, value["behavior_profile_id"], value["behavior_profile_version"],
             value["contract_schema_version"], value["validator_set_hash"],
             value["policy_snapshot_hash"], value["behavior_bundle_hash"],
             value["safety_kernel_version"], json.dumps(value, sort_keys=True), int(time.time())),
        )
    except Exception as exc:
        emit(rule_id="objective.observation_storage", owner=__name__, phase="observation",
             decision="unavailable", objective_id=objective_id, error_type=type(exc).__name__)
