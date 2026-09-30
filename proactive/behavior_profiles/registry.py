"""Durable, exact-Topic behavior selection. Registry writes are operator-only.

Profiles contain code, not aliases to the current shared implementation. A
missing legacy pin is deliberately different from a newly created v1 pin.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import re
import sqlite3
import time


ROOT = Path(__file__).resolve().parent
CODE_ROOT = Path(__file__).resolve().parents[2]
PIN_FIELDS = (
    "behavior_profile_id", "behavior_profile_version", "project_namespace", "contract_schema_version",
    "validator_set_hash", "behavior_bundle_hash", "policy_snapshot_hash",
    "safety_kernel_version", "safety_kernel_hash", "generation",
)


class BehaviorProfileError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS grace_behavior_selections (
        platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        project TEXT NOT NULL, profile_id TEXT, profile_version TEXT,
        revision INTEGER NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(platform,chat_id,thread_id,project))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS grace_objective_behavior_pins (
        objective_id TEXT PRIMARY KEY REFERENCES grace_objectives(objective_id),
        pin TEXT NOT NULL, policy TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS grace_behavior_migrations (
        objective_id TEXT NOT NULL, generation INTEGER NOT NULL,
        previous_pin TEXT, previous_policy TEXT, next_pin TEXT NOT NULL,
        reason TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY(objective_id,generation))""")


def profile(profile_id, version):
    # A fixed catalog prevents an untrusted contract from importing arbitrary code.
    if profile_id not in {"ai_bizweek", "secondhand_commerce"} or not re.fullmatch(r"v[1-9][0-9]*", str(version)):
        raise BehaviorProfileError("behavior.profile_unknown: profile is not installed")
    path = ROOT / f"{profile_id}@{version}.json"
    if not path.is_file():
        raise BehaviorProfileError("behavior.profile_unknown: profile is not installed")
    manifest = json.loads(path.read_text())
    if (manifest.get("profile_id"), manifest.get("version")) != (profile_id, version):
        raise BehaviorProfileError("behavior.manifest_identity")
    if not re.fullmatch(r"v[1-9][0-9]*", str(manifest.get("bundle"))):
        raise BehaviorProfileError("behavior.bundle_name")
    components = {"contract", "compiler", "domain", "review", "workflow", "preflight", "routing", "prompt"}
    required_files = {f"{manifest['bundle']}/{c}.py" for c in components}
    required_files.update(f"{manifest['route_snapshot']}/{name}" for name in ("routing-rules.yaml", "agent-registry.yaml"))
    if not required_files.issubset(manifest["files"]):
        raise BehaviorProfileError("behavior.bundle_incomplete")
    for path, expected in manifest["files"].items():
        if not (ROOT / path).resolve().is_relative_to(ROOT):
            raise BehaviorProfileError("behavior.bundle_path")
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        if actual != expected:
            raise BehaviorProfileError(f"behavior.bundle_changed: {profile_id}@{version}/{path}")
    return manifest


def get_pin(conn, objective_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_behavior_pins'").fetchone():
        return None
    row = conn.execute("SELECT pin FROM grace_objective_behavior_pins WHERE objective_id=?",
                       (objective_id,)).fetchone()
    return json.loads(row[0]) if row else None


def closure_recovery_migration_matches(
    conn, *, objective_id, review_task_id, review_event_id, historical_pin,
):
    """Verify the durable receipt for one accepted terminal-closure re-pin."""
    current = get_pin(conn, objective_id)
    if current is None:
        return False
    row = conn.execute(
        "SELECT previous_pin,next_pin,reason FROM grace_behavior_migrations "
        "WHERE objective_id=? AND generation=?",
        (objective_id, current["generation"]),
    ).fetchone()
    try:
        previous = json.loads(row["previous_pin"]) if row else None
        next_pin = json.loads(row["next_pin"]) if row else None
        receipt = json.loads(row["reason"]) if row else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        previous == historical_pin
        and next_pin == current
        and isinstance(receipt, dict)
        and receipt.get("kind") == "terminal_closure_recovery"
        and receipt.get("review_task_id") == review_task_id
        and type(review_event_id) is int
        and review_event_id > 0
        and receipt.get("review_event_id") == review_event_id
        and receipt.get("previous_pin_hash") == digest(historical_pin)
        and receipt.get("next_generation") == current["generation"]
        and isinstance(receipt.get("reason"), str)
        and receipt["reason"].strip()
    )


def _terminal_closure_recovery_recorded(
    conn, *, objective_id, review_task_id, review_event_id,
):
    """Return whether this exact parked callback event was already re-pinned."""
    rows = conn.execute(
        "SELECT reason FROM grace_behavior_migrations WHERE objective_id=?",
        (objective_id,),
    ).fetchall()
    for row in rows:
        try:
            receipt = json.loads(row["reason"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("kind") == "terminal_closure_recovery"
            and receipt.get("review_task_id") == review_task_id
            and receipt.get("review_event_id") == review_event_id
        ):
            return True
    return False


@contextmanager
def _read_board(identity):
    from hermes_cli import kanban_db as kb
    path = kb.kanban_db_path(board=identity.get("board"))
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _read_objective_board(objective_id):
    """Find one Objective by durable id without trusting contract routing fields."""
    from hermes_cli import kanban_db as kb

    paths = [kb.kanban_db_path(board=kb.DEFAULT_BOARD)]
    try:
        # Active Objectives cannot live on archived boards; the active board
        # registry is therefore the complete authoritative search set.
        boards = kb.list_boards(include_archived=False)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise BehaviorProfileError("behavior.objective_unavailable") from exc
    paths.extend(
        Path(item["db_path"])
        for item in boards
        if item.get("db_path")
    )
    matches = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            # Registry metadata can outlive a deleted board file. There is no
            # database to inspect or Objective to admit at this path.
            continue
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise BehaviorProfileError("behavior.objective_unavailable") from exc
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='grace_objectives'"
            ).fetchone()
            objective = (
                conn.execute(
                    "SELECT 1 FROM grace_objectives WHERE objective_id=?",
                    (objective_id,),
                ).fetchone()
                if exists else None
            )
        except sqlite3.Error as exc:
            conn.close()
            for matched in matches:
                matched.close()
            raise BehaviorProfileError("behavior.objective_unavailable") from exc
        if objective is None:
            conn.close()
            continue
        matches.append(conn)
    if len(matches) != 1:
        for conn in matches:
            conn.close()
        if matches:
            raise BehaviorProfileError("behavior.objective_ambiguous")
        yield None
        return
    try:
        yield matches[0]
    finally:
        matches[0].close()


def set_selection(conn, *, platform, chat_id, thread_id, project, profile_id,
                  version, expected_revision, reason):
    """CAS a new-Objective pointer. None disables it; existing pins never change."""
    from hermes_cli import kanban_db as kb
    if not all(isinstance(v, str) and v.strip() for v in (platform, chat_id, thread_id, project, reason)):
        raise BehaviorProfileError("behavior.selection_scope: exact Topic, project and reason required")
    if profile_id is not None:
        verify_pin(_make_pin(profile_id, version, {}, project_namespace=project))
    elif version is not None:
        raise BehaviorProfileError("behavior.selection_version: disabled pointer has no version")
    scope = (platform, chat_id, thread_id, project)
    with kb.write_txn(conn):
        current = conn.execute("SELECT revision FROM grace_behavior_selections WHERE platform=? AND chat_id=? AND thread_id=? AND project=?", scope).fetchone()
        revision = current[0] if current else 0
        if revision != expected_revision:
            raise BehaviorProfileError("behavior.selection_cas: pointer changed")
        conn.execute("INSERT INTO grace_behavior_selections VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(platform,chat_id,thread_id,project) DO UPDATE SET profile_id=excluded.profile_id,profile_version=excluded.profile_version,revision=excluded.revision,reason=excluded.reason",
                     (*scope, profile_id, version, revision + 1, reason))
    return {"revision": revision + 1, "profile_id": profile_id, "version": version}


def _policy_snapshot(platform, chat_id, thread_id):
    from proactive.policy_registry import resolve_topic_policies_for_scope
    resolved = resolve_topic_policies_for_scope(platform, chat_id, thread_id)
    # Preserve complete verified bytes, including the original binding, at birth.
    return {
        "namespace": resolved["namespace"],
        "policy_requirements": resolved["requirements"],
        "policy_snapshots": resolved["policies"],
        "policy_binding_snapshot": {"namespace": resolved["namespace"],
                                    "path": resolved["binding_path"],
                                    "sha256": resolved["binding_sha256"]},
    }


def _make_pin(profile_id, version, policy, generation=1, *, project_namespace=None):
    manifest = profile(profile_id, version)
    return {"behavior_profile_id": profile_id, "behavior_profile_version": version,
            "project_namespace": project_namespace or profile_id,
            "contract_schema_version": manifest["contract_schema_version"],
            "validator_set_hash": manifest["validator_set_hash"],
            "behavior_bundle_hash": digest(manifest),
            "policy_snapshot_hash": digest(policy),
            "safety_kernel_version": manifest["safety_kernel_version"],
            "safety_kernel_hash": manifest["safety_kernel_hash"],
            "generation": generation}


def inspect_pin(pin):
    """Read all compatibility failures without changing a pin or its history."""
    result = {"ok": False, "errors": [], "kernel_mismatches": [], "manifest": None}
    errors = result["errors"]
    try:
        manifest = profile(pin["behavior_profile_id"], pin["behavior_profile_version"])
        result["manifest"] = manifest
        expected = {
            "contract_schema_version": manifest["contract_schema_version"],
            "validator_set_hash": manifest["validator_set_hash"],
            "behavior_bundle_hash": digest(manifest),
            "safety_kernel_version": manifest["safety_kernel_version"],
            "safety_kernel_hash": manifest["safety_kernel_hash"],
        }
        if any(pin.get(k) != v for k, v in expected.items()):
            errors.append("behavior.manifest_mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    try:
        kernel_version = str(pin["safety_kernel_version"])
        if not re.fullmatch(r"[1-9][0-9]*", kernel_version):
            raise BehaviorProfileError("behavior.kernel_version")
        kernel = json.loads((ROOT / f"kernel-v{kernel_version}.json").read_text())
        if digest(kernel) != pin["safety_kernel_hash"]:
            errors.append("behavior.kernel_migration_required")
        for path, expected in kernel.items():
            source = CODE_ROOT / path
            if not source.resolve().is_relative_to(CODE_ROOT):
                raise BehaviorProfileError("behavior.kernel_path")
            try:
                actual = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError:
                actual = None
            if actual != expected:
                result["kernel_mismatches"].append({
                    "path": path, "expected_sha256": expected, "actual_sha256": actual,
                })
                errors.append(f"behavior.kernel_migration_required: {path}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    result["runtime"] = runtime_health()
    errors.extend(result["runtime"]["errors"])
    result["ok"] = not errors
    return result


def runtime_health():
    """Attest only this process; a fresh CLI cannot attest a running gateway."""
    from hermes_cli import kanban_db as kb
    runtime = {"scope": "calling_process", "loaded_sha256": kb._REVIEW_RUNTIME_SHA256.hex(),
               "disk_sha256": None, "ok": False, "errors": []}
    try:
        runtime["disk_sha256"] = kb._review_runtime_digest().hex()
        runtime["ok"] = runtime["disk_sha256"] == runtime["loaded_sha256"]
        if not runtime["ok"]:
            runtime["errors"].append("behavior.runtime_reload_required")
    except (OSError, ValueError, TypeError) as exc:
        runtime["errors"].append(f"behavior.runtime_unavailable: {exc}")
    return runtime


def verify_pin(pin):
    result = inspect_pin(pin)
    if not result["ok"]:
        raise BehaviorProfileError(result["errors"][0])
    return result["manifest"]


def guard_objective(conn, objective_id):
    pin = get_pin(conn, objective_id)
    if pin:
        verify_pin(pin)
    return pin


def guard_task(conn, task_id):
    row = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    markers = [line[len("GRACE_BEHAVIOR_PIN: "):] for line in (row[0] or "").splitlines()
               if line.startswith("GRACE_BEHAVIOR_PIN: ")]
    linked = conn.execute("SELECT objective_id FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?",
                          (task_id, task_id)).fetchall()
    pinned_ids = {r[0] for r in linked if r[0] and get_pin(conn, r[0])}
    if not markers:
        if pinned_ids:
            raise BehaviorProfileError("behavior.task_pin_missing")
        return None
    if len(markers) != 1:
        raise BehaviorProfileError("behavior.marker_ambiguous")
    contract = json.loads(markers[0])
    if pinned_ids and pinned_ids != {contract["objective_ref"]["objective_id"]}:
        raise BehaviorProfileError("behavior.task_lineage_mismatch")
    bound = bind_contract(conn, contract)
    pin = bound.get("behavior_pin")
    if pin is None or contract.get("behavior_pin") != pin:
        raise BehaviorProfileError("behavior.task_pin_mismatch")
    verify_pin(pin)
    return pin


def validate_timeout_review_historical_contract(
    conn, *, review_task_id, execution_task_id, historical_contract,
):
    """Admit one sealed predecessor after an exact same-version timeout re-pin.

    The predecessor remains historical input: this only proves that its sealed
    contract is the one bridged by the authenticated retry receipt.  It never
    makes the retired pin executable or current again.
    """
    from hermes_cli import kanban_db as kb

    review = conn.execute(
        "SELECT body,status,current_run_id,max_runtime_seconds,max_retries "
        "FROM tasks WHERE id=?",
        (review_task_id,),
    ).fetchone()
    if review is None or review["status"] != "running" or review["current_run_id"] is None:
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")
    current_pin = guard_task(conn, review_task_id)
    current_contract = kb._grace_compiled_contract(review["body"] or "")
    historical_pin = (
        historical_contract.get("behavior_pin")
        if isinstance(historical_contract, dict)
        else None
    )
    if not all(isinstance(value, dict) for value in (
        current_pin, current_contract, historical_pin,
    )):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")
    if current_contract.get("behavior_pin") != historical_pin:
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    stable_fields = (
        "behavior_profile_id", "behavior_profile_version",
        "project_namespace", "contract_schema_version",
        "validator_set_hash", "policy_snapshot_hash", "safety_kernel_version",
    )
    if (
        any(current_pin.get(key) != historical_pin.get(key) for key in stable_fields)
        or type(current_pin.get("generation")) is not int
        or type(historical_pin.get("generation")) is not int
        or current_pin["generation"] != historical_pin["generation"] + 1
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    current_marker = next((
        json.loads(line[len("GRACE_BEHAVIOR_PIN: "):])
        for line in str(review["body"] or "").splitlines()
        if line.startswith("GRACE_BEHAVIOR_PIN: ")
    ), None)
    objective_ref = current_contract.get("objective_ref") or {}
    identity = current_contract.get("identity") or {}
    if (
        not isinstance(current_marker, dict)
        or current_marker.get("behavior_pin") != current_pin
        or (current_marker.get("objective_ref") or {}) != objective_ref
        or (current_marker.get("identity") or {}) != identity
        or (historical_contract.get("objective_ref") or {}) != objective_ref
        or (historical_contract.get("identity") or {}) != identity
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    objective_id = str(objective_ref.get("objective_id") or "")
    stage_key = str(objective_ref.get("stage_key") or "")
    lineage = conn.execute(
        "SELECT d.objective_id,d.stage_key,o.status,o.current_stage_key,o.revision,"
        "s.status stage_status "
        "FROM grace_delegations d "
        "JOIN grace_objectives o ON o.objective_id=d.objective_id "
        "JOIN grace_objective_stages s ON s.objective_id=d.objective_id "
        "AND s.stage_key=d.stage_key "
        "WHERE d.execution_task_id=? AND d.review_task_id=?",
        (execution_task_id, review_task_id),
    ).fetchall()
    if not (
        len(lineage) == 1
        and lineage[0]["objective_id"] == objective_id
        and lineage[0]["stage_key"] == stage_key
        and lineage[0]["status"] == "active"
        and lineage[0]["current_stage_key"] == stage_key
        and lineage[0]["stage_status"] == "queued"
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    migrations = conn.execute(
        "SELECT previous_pin,next_pin,reason FROM grace_behavior_migrations "
        "WHERE objective_id=? AND generation=?",
        (objective_id, current_pin["generation"]),
    ).fetchall()
    if not (
        len(migrations) == 1
        and migrations[0]["previous_pin"]
        and migrations[0]["next_pin"]
        and json.loads(migrations[0]["previous_pin"]) == historical_pin
        and json.loads(migrations[0]["next_pin"]) == current_pin
        and migrations[0]["reason"]
        == "Authenticated same-card recovery after exactly two formal-review cold-start timeouts."
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    receipts = conn.execute(
        "SELECT id,payload FROM task_events WHERE task_id=? "
        "AND kind='grace_review_retry_authorized' ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchall()
    try:
        receipt = json.loads(receipts[0]["payload"] or "{}") if len(receipts) == 1 else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        receipt = {}
    receipt_event_id = int(receipts[0]["id"]) if len(receipts) == 1 else 0
    timeout_run_ids = receipt.get("timeout_run_ids")
    previous_runtime = receipt.get("previous_max_runtime_seconds")
    repaired_runtime = receipt.get("max_runtime_seconds")
    gave_up_event_id = receipt.get("gave_up_event_id")
    timeout_runs = conn.execute(
        "SELECT id,status,outcome,max_runtime_seconds,error FROM task_runs "
        "WHERE task_id=? AND outcome='timed_out' ORDER BY id",
        (review_task_id,),
    ).fetchall()
    gave_up_event = (
        conn.execute(
            "SELECT id,payload FROM task_events WHERE id=? AND task_id=? "
            "AND kind='gave_up' AND id<?",
            (gave_up_event_id, review_task_id, receipt_event_id),
        ).fetchone()
        if type(gave_up_event_id) is int
        else None
    )
    try:
        gave_up = json.loads(gave_up_event["payload"] or "{}") if gave_up_event else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        gave_up = {}
    if not isinstance(gave_up, dict):
        gave_up = {}
    claimed = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed' "
        "AND run_id=? AND id>? LIMIT 1",
        (review_task_id, review["current_run_id"], receipt_event_id),
    ).fetchone()
    if not (
        receipt.get("repaired_fault") == "formal_review_cold_start_budget"
        and receipt.get("review_task_id") == review_task_id
        and receipt.get("execution_task_id") == execution_task_id
        and receipt.get("behavior_repin_previous_sha256") == digest(historical_pin)
        and receipt.get("behavior_repin_next_sha256") == digest(current_pin)
        and type(receipt.get("behavior_repin_previous_generation")) is int
        and receipt.get("behavior_repin_previous_generation")
        == historical_pin["generation"]
        and type(receipt.get("behavior_repin_generation")) is int
        and receipt.get("behavior_repin_generation") == current_pin["generation"]
        and type(receipt.get("behavior_repin_objective_revision")) is int
        and receipt.get("behavior_repin_objective_revision")
        == lineage[0]["revision"]
        and type(previous_runtime) is int
        and type(repaired_runtime) is int
        and previous_runtime > 0
        and repaired_runtime > previous_runtime
        and repaired_runtime == review["max_runtime_seconds"]
        and type(receipt.get("max_retries")) is int
        and receipt.get("max_retries") == review["max_retries"] == 1
        and isinstance(timeout_run_ids, list)
        and len(timeout_run_ids) == 2
        and all(type(run_id) is int for run_id in timeout_run_ids)
        and timeout_run_ids == [int(run["id"]) for run in timeout_runs]
        and all(
            run["status"] == "timed_out"
            and run["outcome"] == "timed_out"
            and run["max_runtime_seconds"] == previous_runtime
            and _timeout_error_matches(run["error"], previous_runtime)
            for run in timeout_runs
        )
        and gave_up_event is not None
        and gave_up.get("trigger_outcome") == "timed_out"
        and type(gave_up.get("failures")) is int
        and gave_up.get("failures") == 2
        and type(gave_up.get("effective_limit")) is int
        and gave_up.get("effective_limit") == 2
        and conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=? "
            "AND kind='gave_up' AND id<?",
            (review_task_id, receipt_event_id),
        ).fetchone()[0] == gave_up_event_id
        and claimed is not None
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")

    run_row = conn.execute(
        "SELECT metadata FROM task_runs WHERE id=? AND task_id=? AND ended_at IS NULL",
        (review["current_run_id"], review_task_id),
    ).fetchone()
    try:
        run_metadata = json.loads(run_row["metadata"] or "{}") if run_row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        run_metadata = {}
    review_source = run_metadata.get("workflow_review_source")
    if (
        not isinstance(review_source, dict)
        or review_source.get("behavior_pin") != current_pin
        or review_source != kb._workflow_review_source(conn, review_task_id)
    ):
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")
    if conn.execute(
        "SELECT 1 FROM task_external_effects WHERE task_id IN (?,?) LIMIT 1",
        (execution_task_id, review_task_id),
    ).fetchone():
        raise BehaviorProfileError("behavior.timeout_recovery_history_invalid")
    return current_pin


def pin_new_objective(conn, objective, project):
    """Called only inside the transaction inserting a new Objective."""
    selected = conn.execute("SELECT profile_id,profile_version FROM grace_behavior_selections WHERE platform=? AND chat_id=? AND thread_id=? AND project=?",
                            (objective["platform"], objective["chat_id"], objective["thread_id"], project)).fetchone()
    if not selected or selected[0] is None:
        return None
    policy = _policy_snapshot(objective["platform"], objective["chat_id"], objective["thread_id"])
    pin = _make_pin(selected[0], selected[1], policy, project_namespace=project)
    verify_pin(pin)
    conn.execute("INSERT INTO grace_objective_behavior_pins VALUES (?,?,?)",
                 (objective["objective_id"], json.dumps(pin), json.dumps(policy, ensure_ascii=False)))
    return pin


def bind_contract(conn, contract):
    """Controller attachment, before fingerprinting or approval creation."""
    value = deepcopy(dict(contract))
    objective_id = (value.get("objective_ref") or {}).get("objective_id")
    pin = get_pin(conn, objective_id) if objective_id else None
    supplied = value.get("behavior_pin")
    if supplied is not None and supplied != pin:
        raise BehaviorProfileError("behavior.pin_mismatch: supplied pin differs from Objective")
    if pin:
        objective = conn.execute("SELECT platform,chat_id,thread_id FROM grace_objectives WHERE objective_id=?", (objective_id,)).fetchone()
        identity = value.get("identity") or {}
        if tuple(objective) != tuple(identity.get(k, "") for k in ("platform", "chat_id", "thread_id")):
            raise BehaviorProfileError("behavior.topic_mismatch")
        if identity.get("project") != pin["project_namespace"]:
            raise BehaviorProfileError("behavior.project_mismatch")
        from hermes_cli import kanban_db as kb
        database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        if database:
            path = Path(database).resolve()
            board = None
            if path == (kb.kanban_home() / "kanban.db").resolve():
                board = kb.DEFAULT_BOARD
            elif path.name == "kanban.db" and path.parent.parent == kb.boards_root().resolve():
                board = path.parent.name
            if board is not None:
                if identity.get("board") not in (None, "", board):
                    raise BehaviorProfileError("behavior.board_mismatch")
                identity["board"] = board
        value["behavior_pin"] = pin
    return value


def attach_contract(contract):
    """Read-only admission attachment; invalid inputs must not create a board."""
    objective_id = (contract.get("objective_ref") or {}).get("objective_id")
    if not objective_id and contract.get("behavior_pin") is None:
        return deepcopy(dict(contract))
    if not objective_id:
        raise BehaviorProfileError("behavior.pin_unbound")
    with _read_objective_board(objective_id) as conn:
        if conn is None:
            raise BehaviorProfileError("behavior.objective_missing")
        return bind_contract(conn, contract)


def checked_pin(contract):
    """Read the authoritative board, never trust version metadata from a worker."""
    objective_id = (contract.get("objective_ref") or {}).get("objective_id")
    supplied = contract.get("behavior_pin")
    if not objective_id:
        if supplied is None:
            return None
        raise BehaviorProfileError("behavior.pin_unbound")
    with _read_objective_board(objective_id) as conn:
        if conn is None:
            raise BehaviorProfileError("behavior.objective_missing")
        stored = get_pin(conn, objective_id)
        if stored is None and supplied is None:
            return None
        if stored is not None and supplied is None:
            raise BehaviorProfileError("behavior.pin_missing")
        bound = bind_contract(conn, contract)
    if bound.get("behavior_pin") != supplied:
        raise BehaviorProfileError("behavior.pin_mismatch")
    verify_pin(supplied)
    return supplied


def task_marker(contract):
    value = {key: deepcopy(contract[key]) for key in ("identity", "objective_ref", "behavior_pin", "memory")}
    value["memory"] = {"namespace": value["memory"]["namespace"]}
    return "GRACE_BEHAVIOR_PIN: " + json.dumps(value, ensure_ascii=False, sort_keys=True)


def task_policy(body, *, historical=True):
    markers = [line[len("GRACE_BEHAVIOR_PIN: "):] for line in body.splitlines()
               if line.startswith("GRACE_BEHAVIOR_PIN: ")]
    if not markers:
        return None
    if len(markers) != 1:
        raise BehaviorProfileError("behavior.marker_ambiguous")
    contract = json.loads(markers[0])
    value = resolve_pinned_policies(contract, historical=historical)
    return {"binding": value["policy_binding_snapshot"], "policies": value["policy_snapshots"]}


def validate_task_policy_completion(body, metadata, role):
    policy = task_policy(body, historical=False)
    if policy is None:
        return False
    receipts = metadata.get("policy_receipts") if isinstance(metadata, dict) else None
    if not isinstance(receipts, list) or any(not isinstance(r, dict) for r in receipts):
        raise BehaviorProfileError("behavior.policy_receipts_missing")
    receipts = [r for r in receipts if r.get("role") == role]
    expected = policy["policies"]
    by_id = {r.get("policy_id"): r for r in receipts}
    if len(receipts) != len(by_id) or set(by_id) != {p["policy_id"] for p in expected}:
        raise BehaviorProfileError("behavior.policy_receipts_mismatch")
    for p in expected:
        r = by_id[p["policy_id"]]
        if r.get("loaded") is not True or any(r.get(k) != p[k] for k in ("version", "sha256")):
            raise BehaviorProfileError("behavior.policy_receipt_mismatch")
        if role == "review" and r.get("pinned_version_verified") is not True:
            raise BehaviorProfileError("behavior.pinned_version_not_verified")
    return True


def implementation(contract, component):
    pin = checked_pin(contract)
    if pin is None:
        return None
    if component not in {"contract", "compiler", "domain", "review", "workflow", "preflight", "routing"}:
        raise BehaviorProfileError("behavior.component_unknown")
    manifest = profile(pin["behavior_profile_id"], pin["behavior_profile_version"])
    return _load_component(manifest, component)


def _load_component(manifest, component):
    module = importlib.import_module(f"proactive.behavior_profiles.{manifest['bundle']}.{component}")
    expected = manifest["files"][f"{manifest['bundle']}/{component}.py"]
    if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
        raise BehaviorProfileError("behavior.loaded_module_mismatch")
    return module


def route_directory(contract):
    pin = checked_pin(contract)
    manifest = profile(pin["behavior_profile_id"], pin["behavior_profile_version"])
    directory = manifest["route_snapshot"]
    if any(directory + "/" + name not in manifest["files"] for name in ("routing-rules.yaml", "agent-registry.yaml")):
        raise BehaviorProfileError("behavior.route_snapshot_missing")
    return ROOT / directory


def implementation_for_body(body, component):
    for line in str(body or "").splitlines():
        if line.startswith("GRACE_BEHAVIOR_PIN: "):
            return implementation(json.loads(line[len("GRACE_BEHAVIOR_PIN: "):]), component)
    return None


def workflow_implementation(conn, objective_id):
    pin = guard_objective(conn, objective_id)
    if pin is None:
        return None
    manifest = profile(pin["behavior_profile_id"], pin["behavior_profile_version"])
    return _load_component(manifest, "workflow")


def resolve_pinned_policies(contract, *, historical=False):
    if historical:
        # Audit reads may use retired generations; admission/completion still
        # require the current pin. Never reinterpret old evidence as current.
        pin = contract.get("behavior_pin")
        if not pin:
            raise BehaviorProfileError("behavior.policy_unbound")
        with _read_objective_board(
            contract["objective_ref"]["objective_id"]
        ) as conn:
            if conn is None:
                raise BehaviorProfileError("behavior.objective_missing")
            oid = contract["objective_ref"]["objective_id"]
            objective = conn.execute("SELECT platform,chat_id,thread_id FROM grace_objectives WHERE objective_id=?", (oid,)).fetchone()
            identity = contract["identity"]
            if objective is None or tuple(objective) != tuple(identity.get(k, "") for k in ("platform", "chat_id", "thread_id")) or identity.get("project") != pin.get("project_namespace"):
                raise BehaviorProfileError("behavior.topic_mismatch")
            current = get_pin(conn, oid)
            if current != pin:
                rows = conn.execute("SELECT previous_pin,previous_policy FROM grace_behavior_migrations WHERE objective_id=?", (oid,)).fetchall()
                matches = [json.loads(r[1]) for r in rows if r[0] and json.loads(r[0]) == pin]
                if not matches:
                    raise BehaviorProfileError("behavior.historical_pin_unknown")
                return _apply_policy(contract, pin, matches[0])
    pin = checked_pin(contract)
    if pin is None:
        raise BehaviorProfileError("behavior.policy_unbound")
    with _read_objective_board(
        contract["objective_ref"]["objective_id"]
    ) as conn:
        if conn is None:
            raise BehaviorProfileError("behavior.objective_missing")
        row = conn.execute("SELECT policy FROM grace_objective_behavior_pins WHERE objective_id=?",
                           (contract["objective_ref"]["objective_id"],)).fetchone()
    policy = json.loads(row[0])
    return _apply_policy(contract, pin, policy)


def _apply_policy(contract, pin, policy):
    if digest(policy) != pin["policy_snapshot_hash"]:
        raise BehaviorProfileError("behavior.policy_corrupt")
    value = deepcopy(dict(contract))
    if (value.get("memory") or {}).get("namespace") != policy["namespace"]:
        raise BehaviorProfileError("behavior.policy_namespace_mismatch")
    for key in ("policy_requirements", "policy_snapshots", "policy_binding_snapshot"):
        if key in value and value[key] != policy[key]:
            raise BehaviorProfileError(f"behavior.policy_mismatch: {key}")
        value[key] = deepcopy(policy[key])
    return value


@contextmanager
def _read_transaction(conn):
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def _timeout_error_matches(error, expected_limit):
    if type(expected_limit) is not int or expected_limit <= 0:
        return False
    match = re.fullmatch(
        r"elapsed (\d+)s > limit (\d+)s", str(error or ""),
    )
    if match is None:
        return False
    elapsed, limit = (int(value) for value in match.groups())
    return elapsed > limit and limit == expected_limit


def _exact_timeout_review_repin_allowed(
    conn, *, objective, callback, review_task_id, previous_pin,
):
    """Admit only the parked review that motivated a same-version re-pin."""
    from hermes_cli import kanban_db as kb

    if callback["review_task_id"] != review_task_id:
        return False
    row = conn.execute(
        """
        SELECT d.state delegation_state,d.contract_fingerprint,
               e.status execution_status,r.status review_status,
               r.current_run_id,r.consecutive_failures,r.block_kind,r.body,
               s.status stage_status,s.review_task_id stage_review_task_id
          FROM grace_delegations d
          JOIN tasks e ON e.id=d.execution_task_id
          JOIN tasks r ON r.id=d.review_task_id
          JOIN grace_objective_stages s
            ON s.objective_id=d.objective_id AND s.stage_key=d.stage_key
         WHERE d.objective_id=? AND d.review_task_id=?
           AND d.execution_task_id=? AND d.stage_key=?
        """,
        (
            objective["objective_id"], review_task_id,
            callback["execution_task_id"], callback["stage_key"],
        ),
    ).fetchone()
    latest = conn.execute(
        "SELECT id,kind,payload FROM task_events WHERE task_id=? "
        "AND kind IN ('completed','blocked','gave_up','cancelled') "
        "ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchone()
    try:
        gave_up = json.loads(latest["payload"] or "{}") if latest else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        gave_up = {}
    if not isinstance(gave_up, dict):
        gave_up = {}
    runs = conn.execute(
        "SELECT id,status,outcome,max_runtime_seconds,error FROM task_runs "
        "WHERE task_id=? ORDER BY id",
        (review_task_id,),
    ).fetchall()
    timeout_run_ids = [
        int(event["run_id"] or 0)
        for event in conn.execute(
            "SELECT run_id FROM task_events WHERE task_id=? AND kind='timed_out' "
            "ORDER BY id",
            (review_task_id,),
        )
    ]
    markers = [
        line[len("GRACE_BEHAVIOR_PIN: "):]
        for line in str(row["body"] if row else "").splitlines()
        if line.startswith("GRACE_BEHAVIOR_PIN: ")
    ]
    try:
        marker = json.loads(markers[0]) if len(markers) == 1 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        marker = None
    compiled = kb._grace_compiled_contract(str(row["body"] if row else ""))
    now = int(time.time())
    previous_runtime = int(runs[0]["max_runtime_seconds"] or 0) if runs else 0
    return bool(
        row is not None
        and row["delegation_state"] == "queued"
        and row["contract_fingerprint"] == callback["contract_fingerprint"]
        and row["execution_status"] == "done"
        and row["review_status"] == "blocked"
        and row["current_run_id"] is None
        and row["consecutive_failures"] == 2
        and row["block_kind"] in {None, ""}
        and row["stage_status"] == "queued"
        and row["stage_review_task_id"] == review_task_id
        and objective["status"] == "active"
        and objective["current_stage_key"] == callback["stage_key"]
        and callback["state"] in {"pending", "delivering"}
        and callback["outcome_event_id"] is None
        and callback["outcome_kind"] is None
        and callback["outcome_payload"] is None
        and (
            callback["lease_expires"] is None
            or int(callback["lease_expires"]) <= now
        )
        and latest is not None
        and latest["kind"] == "gave_up"
        and int(callback["last_event_id"] or 0) < int(latest["id"])
        and gave_up.get("trigger_outcome") == "timed_out"
        and type(gave_up.get("failures")) is int
        and gave_up.get("failures") == 2
        and type(gave_up.get("effective_limit")) is int
        and gave_up.get("effective_limit") == 2
        and len(runs) == 2
        and timeout_run_ids == [int(run["id"]) for run in runs]
        and all(
            run["status"] == "timed_out"
            and run["outcome"] == "timed_out"
            and int(run["max_runtime_seconds"] or 0) == previous_runtime
            and _timeout_error_matches(run["error"], previous_runtime)
            for run in runs
        )
        and isinstance(marker, dict)
        and marker.get("behavior_pin") == previous_pin
        and isinstance(compiled, dict)
        and compiled.get("behavior_pin") == previous_pin
        and (marker.get("objective_ref") or {}).get("objective_id")
        == objective["objective_id"]
        and (marker.get("objective_ref") or {}).get("stage_key")
        == callback["stage_key"]
        and not conn.execute(
            "SELECT 1 FROM task_external_effects WHERE task_id IN (?,?) LIMIT 1",
            (callback["execution_task_id"], review_task_id),
        ).fetchone()
    )


def _exact_terminal_closure_repin_allowed(
    conn, *, objective, callback, review_task_id,
):
    """Admit one accepted terminal review parked by the closure invariant."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        """
        SELECT d.state delegation_state,e.status execution_status,
               r.status review_status,s.status stage_status,
               s.execution_task_id,s.review_task_id
          FROM grace_delegations d
          JOIN tasks e ON e.id=d.execution_task_id
          JOIN tasks r ON r.id=d.review_task_id
          JOIN grace_objective_stages s
            ON s.objective_id=d.objective_id AND s.stage_key=d.stage_key
         WHERE d.objective_id=? AND d.review_task_id=?
           AND d.execution_task_id=? AND d.stage_key=?
        """,
        (
            objective["objective_id"], review_task_id,
            callback["execution_task_id"], callback["stage_key"],
        ),
    ).fetchone()
    event = conn.execute(
        "SELECT id,run_id FROM task_events WHERE task_id=? AND kind='completed' "
        "ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchone()
    review_run = (
        kb.get_run(conn, int(event["run_id"]))
        if event is not None and event["run_id"] is not None
        else None
    )
    incomplete_predecessor = conn.execute(
        "SELECT 1 FROM grace_objective_stages WHERE objective_id=? "
        "AND stage_key<>? AND (status<>'done' OR outcome_kind='cancelled') "
        "LIMIT 1",
        (objective["objective_id"], callback["stage_key"]),
    ).fetchone()
    return bool(
        callback["review_task_id"] == review_task_id
        and objective["status"] == "active"
        and objective["current_stage_key"] == callback["stage_key"]
        and objective["terminal_stage_key"] == callback["stage_key"]
        and str(callback["completion_mode"] or "terminal") == "terminal"
        and callback["state"] == "pending"
        and callback["lease_event_id"] is None
        and callback["lease_owner"] is None
        and callback["lease_expires"] is None
        and callback["outcome_event_id"] is None
        and callback["outcome_kind"] is None
        and callback["outcome_payload"] is None
        and event is not None
        and int(callback["attempt_event_id"] or 0) == int(event["id"])
        and int(callback["attempts"] or 0) > 0
        and "cannot close before required stages complete"
        in str(callback["last_error"] or "")
        and row is not None
        and row["delegation_state"] == "queued"
        and row["execution_status"] == "done"
        and row["review_status"] == "done"
        and row["stage_status"] != "done"
        and row["execution_task_id"] == callback["execution_task_id"]
        and row["review_task_id"] == review_task_id
        and review_run is not None
        and review_run.task_id == review_task_id
        and review_run.outcome == "completed"
        and kb.grace_review_accepted(review_run.metadata)
        and incomplete_predecessor is None
    )


def _kernel_repair_snapshot_lineage_valid(
    conn, *, objective_id, review_task_id, snapshot_pin, current_pin,
):
    """Verify every immutable kernel-repair receipt from snapshot to current."""
    if snapshot_pin == current_pin:
        return True
    if not (
        isinstance(snapshot_pin, dict)
        and isinstance(current_pin, dict)
        and type(snapshot_pin.get("generation")) is int
        and type(current_pin.get("generation")) is int
        and snapshot_pin["generation"] < current_pin["generation"]
    ):
        return False
    expected_pin = snapshot_pin
    migrations = conn.execute(
        "SELECT generation,previous_pin,next_pin,reason "
        "FROM grace_behavior_migrations WHERE objective_id=? "
        "AND generation>? AND generation<=? ORDER BY generation",
        (
            objective_id,
            snapshot_pin["generation"],
            current_pin["generation"],
        ),
    ).fetchall()
    for migration in migrations:
        try:
            recorded_previous = json.loads(migration["previous_pin"])
            recorded_next = json.loads(migration["next_pin"])
            receipt = json.loads(migration["reason"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not (
            recorded_previous == expected_pin
            and isinstance(recorded_next, dict)
            and migration["generation"]
            == expected_pin["generation"] + 1
            and recorded_next.get("generation") == migration["generation"]
            and isinstance(receipt, dict)
            and receipt.get("kind") == "kernel_repair_review_repin"
            and receipt.get("review_task_id") == review_task_id
            and receipt.get("previous_pin_hash") == digest(expected_pin)
            and receipt.get("next_generation") == migration["generation"]
        ):
            return False
        expected_pin = recorded_next
    return expected_pin == current_pin


def _exact_kernel_repair_review_repin_allowed(
    conn, *, objective, callback, review_task_id, previous_pin,
):
    """Admit one exact review stranded at a verified kernel boundary."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        """
        SELECT d.state delegation_state,e.status execution_status,
               r.status review_status,r.current_run_id,r.consecutive_failures,
               s.status stage_status,s.outcome_kind stage_outcome_kind,
               s.delegation_id,s.execution_task_id,
               s.review_task_id,r.body,d.contract_fingerprint,
               d.contract_snapshot
          FROM grace_delegations d
          JOIN tasks e ON e.id=d.execution_task_id
          JOIN tasks r ON r.id=d.review_task_id
          JOIN grace_objective_stages s
            ON s.objective_id=d.objective_id AND s.stage_key=d.stage_key
         WHERE d.objective_id=? AND d.review_task_id=?
           AND d.execution_task_id=? AND d.stage_key=?
        """,
        (
            objective["objective_id"], review_task_id,
            callback["execution_task_id"], callback["stage_key"],
        ),
    ).fetchone()
    latest_event = conn.execute(
        "SELECT id,kind,run_id,payload FROM task_events WHERE task_id=? "
        "AND kind IN ('completed','dependency_wait','blocked','gave_up','cancelled') "
        "ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchone()
    latest_run_row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchone()
    review_run = (
        kb.get_run(conn, int(latest_run_row["id"]))
        if latest_run_row is not None else None
    )
    protocol_event = (
        conn.execute(
            "SELECT id,run_id,payload FROM task_events WHERE task_id=? "
            "AND kind='protocol_violation' AND id<? "
            "ORDER BY id DESC LIMIT 1",
            (review_task_id, int(latest_event["id"])),
        ).fetchone()
        if latest_event is not None else None
    )
    try:
        gave_up = json.loads(latest_event["payload"] or "{}") if latest_event else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        gave_up = {}
    metadata = review_run.metadata if review_run is not None else {}
    blocker = metadata.get("goal_loop_blocker") if isinstance(metadata, dict) else None
    blocker_text = " ".join(
        str((blocker or {}).get(key) or "")
        for key in ("judge_reason", "last_response_excerpt")
    )
    comments = "\n".join(
        str(item[0] or "")
        for item in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id",
            (review_task_id,),
        )
    )
    markers = [
        line[len("GRACE_BEHAVIOR_PIN: "):]
        for line in str(row["body"] if row else "").splitlines()
        if line.startswith("GRACE_BEHAVIOR_PIN: ")
    ]
    try:
        marker = json.loads(markers[0]) if len(markers) == 1 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        marker = None
    compiled = kb._grace_compiled_contract(str(row["body"] if row else ""))
    try:
        snapshot = json.loads(row["contract_snapshot"] or "") if row else None
    except (TypeError, ValueError, json.JSONDecodeError):
        snapshot = None
    marker_identity = marker.get("identity") if isinstance(marker, dict) else None
    marker_ref = marker.get("objective_ref") if isinstance(marker, dict) else None
    marker_namespace = (
        (marker.get("memory") or {}).get("namespace")
        if isinstance(marker, dict) else None
    )
    compiled_namespace = (
        (compiled.get("memory") or {}).get("namespace")
        if isinstance(compiled, dict) else None
    )
    snapshot_namespace = (
        (snapshot.get("memory") or {}).get("namespace")
        if isinstance(snapshot, dict) else None
    )
    snapshot_fingerprint = None
    if isinstance(snapshot, dict):
        from proactive.loop_contract import contract_fingerprint
        snapshot_fingerprint = contract_fingerprint(snapshot)
    compiled_without_audit = deepcopy(compiled) if isinstance(compiled, dict) else None
    if isinstance(compiled_without_audit, dict):
        compiled_without_audit.pop("audit", None)
    if row is None or latest_event is None or review_run is None:
        return False
    snapshot_pin = (
        snapshot.get("behavior_pin") if isinstance(snapshot, dict) else None
    )
    compiled_at_snapshot_pin = deepcopy(compiled_without_audit)
    if isinstance(compiled_at_snapshot_pin, dict):
        compiled_at_snapshot_pin["behavior_pin"] = snapshot_pin
    snapshot_lineage_valid = _kernel_repair_snapshot_lineage_valid(
        conn,
        objective_id=objective["objective_id"],
        review_task_id=review_task_id,
        snapshot_pin=snapshot_pin,
        current_pin=previous_pin,
    )
    lease_finalize_repair = bool(
        callback["state"] == "attention"
        and str(callback["last_error"] or "").startswith(
            "Grace callback delivery failed: RuntimeError: Grace callback lease expired, "
            "ownership changed, or its trigger event was superseded before finalization."
        )
    )
    common_state = bool(
        callback["review_task_id"] == review_task_id
        and (
            objective["status"] == "active"
            or (objective["status"] == "blocked" and lease_finalize_repair)
        )
        and objective["current_stage_key"] == callback["stage_key"]
        and callback["lease_event_id"] is None
        and callback["lease_owner"] is None
        and callback["lease_expires"] is None
        and callback["outcome_event_id"] is None
        and callback["outcome_kind"] is None
        and callback["outcome_payload"] is None
        and latest_event is not None
        and not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND id>? LIMIT 1",
            (review_task_id, int(latest_event["id"])),
        ).fetchone()
        and row is not None
        and row["delegation_state"] == "queued"
        and row["execution_status"] in {"done", "blocked"}
        and row["current_run_id"] is None
        and (
            row["stage_status"] == "queued"
            or (
                lease_finalize_repair
                and row["stage_status"] == "done"
                and row["stage_outcome_kind"] == "intermediate_blocked"
            )
        )
        and row["execution_task_id"] == callback["execution_task_id"]
        and row["review_task_id"] == review_task_id
        and review_run is not None
        and review_run.task_id == review_task_id
    )
    protocol_repair = bool(
        callback["state"] == "attention"
        and (
            str(callback["last_error"] or "").startswith(
                "control_plane_repair_in_progress:"
            )
            or lease_finalize_repair
        )
        and int(callback["attempts"] or 0) >= 3
        and latest_event["kind"] == "gave_up"
        and int(callback["attempt_event_id"] or 0) == int(latest_event["id"])
        and isinstance(gave_up, dict)
        and gave_up.get("trigger_outcome") == "crashed"
        and type(gave_up.get("failures")) is int
        and gave_up.get("failures") == int(row["consecutive_failures"] or 0)
        and row["review_status"] == "blocked"
        and int(row["consecutive_failures"] or 0) >= 1
        and (
            latest_event["run_id"] is None
            or int(latest_event["run_id"]) == review_run.id
        )
        and review_run.status == "crashed"
        and review_run.outcome == "crashed"
        and "protocol violation" in str(review_run.error or "").lower()
        and protocol_event is not None
        and protocol_event["run_id"] == review_run.id
        and not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND id>? AND id<? "
            "LIMIT 1",
            (
                review_task_id,
                int(protocol_event["id"]),
                int(latest_event["id"]),
            ),
        ).fetchone()
        and isinstance(blocker, dict)
        and blocker.get("class") == "finalize_protocol_violation"
        and "behavior.kernel_migration_required" in blocker_text
        and "behavior.kernel_migration_required" in comments
    )
    dependency_wait_repair = bool(
        callback["state"] == "pending"
        and callback["last_error"] is None
        and int(callback["attempts"] or 0) == 0
        and callback["attempt_event_id"] is None
        and latest_event["kind"] == "dependency_wait"
        and int(latest_event["run_id"] or 0) == review_run.id
        and row["review_status"] == "todo"
        and int(row["consecutive_failures"] or 0) == 0
        and review_run.status == "blocked"
        and review_run.outcome == "blocked"
    )
    identity_valid = bool(
        row is not None
        and row["execution_task_id"] == callback["execution_task_id"]
        and row["review_task_id"] == review_task_id
        and isinstance(marker, dict)
        and marker.get("behavior_pin") == previous_pin
        and isinstance(compiled, dict)
        and compiled.get("behavior_pin") == previous_pin
        and isinstance(snapshot, dict)
        and snapshot_lineage_valid
        and compiled_at_snapshot_pin == snapshot
        and callback["contract_fingerprint"] == row["contract_fingerprint"]
        and snapshot_fingerprint == row["contract_fingerprint"]
        and compiled.get("identity") == marker_identity
        and snapshot.get("identity") == marker_identity
        and compiled.get("objective_ref") == marker_ref
        and snapshot.get("objective_ref") == marker_ref
        and isinstance(marker_namespace, str)
        and marker_namespace.strip()
        and compiled_namespace == marker_namespace
        and snapshot_namespace == marker_namespace
        and (marker.get("objective_ref") or {}).get("objective_id")
        == objective["objective_id"]
        and (marker.get("objective_ref") or {}).get("stage_key")
        == callback["stage_key"]
        and not conn.execute(
            "SELECT 1 FROM task_external_effects "
            "WHERE task_id IN (?,?) LIMIT 1",
            (callback["execution_task_id"], review_task_id),
        ).fetchone()
    )
    return common_state and (protocol_repair or dependency_wait_repair) and identity_valid


def _kernel_repair_review_repin_recorded(
    conn, *, objective_id, review_task_id, review_run_id,
    previous_pin_hash=None,
):
    """Return whether the exact review run already consumed this repair edge."""
    for row in conn.execute(
        "SELECT reason FROM grace_behavior_migrations WHERE objective_id=?",
        (objective_id,),
    ):
        try:
            receipt = json.loads(row["reason"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("kind") == "kernel_repair_review_repin"
            and receipt.get("review_task_id") == review_task_id
            and receipt.get("review_run_id") == review_run_id
            and (
                previous_pin_hash is None
                or receipt.get("previous_pin_hash") == previous_pin_hash
            )
        ):
            return True
    return False


def _kernel_repair_profiles_compatible(previous_pin, next_manifest):
    """Require an explicit, byte-identical review-runtime compatibility edge."""
    previous_version = previous_pin["behavior_profile_version"]
    if previous_version not in next_manifest.get(
        "inflight_review_compatible_from", []
    ):
        return False
    previous_manifest = profile(
        previous_pin["behavior_profile_id"], previous_version,
    )
    if any(
        previous_manifest.get(key) != next_manifest.get(key)
        for key in ("bundle", "contract_schema_version", "validator_set_hash")
    ):
        return False
    bundle = str(next_manifest["bundle"])
    review_components = {
        f"{bundle}/{name}.py"
        for name in (
            "contract", "compiler", "domain", "review",
            "workflow", "preflight", "routing", "prompt",
        )
    }
    return all(
        previous_manifest["files"].get(path)
        == next_manifest["files"].get(path)
        for path in review_components
    )


def _replace_behavior_marker(body, *, previous_pin, next_pin):
    lines = str(body or "").splitlines()
    indexes = [
        index for index, line in enumerate(lines)
        if line.startswith("GRACE_BEHAVIOR_PIN: ")
    ]
    if len(indexes) != 1:
        raise BehaviorProfileError("behavior.migration_review_marker")
    index = indexes[0]
    try:
        marker = json.loads(lines[index][len("GRACE_BEHAVIOR_PIN: "):])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BehaviorProfileError("behavior.migration_review_marker") from exc
    if not isinstance(marker, dict) or marker.get("behavior_pin") != previous_pin:
        raise BehaviorProfileError("behavior.migration_review_marker")
    marker["behavior_pin"] = next_pin
    lines[index] = "GRACE_BEHAVIOR_PIN: " + json.dumps(
        marker, ensure_ascii=False, sort_keys=True,
    )
    return "\n".join(lines) + ("\n" if str(body or "").endswith("\n") else "")


def _replace_compiled_behavior_pin(body, *, previous_pin, next_pin):
    """Replace the one sealed Loop Contract pin in a review task body."""
    candidates = []
    for match in re.finditer(r"```json\n(.*?)\n```", str(body or ""), re.DOTALL):
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and isinstance(value.get("objective_ref"), dict)
            and "behavior_pin" in value
        ):
            candidates.append((match, value))
    if len(candidates) != 1:
        raise BehaviorProfileError("behavior.migration_review_contract")
    match, contract = candidates[0]
    if contract.get("behavior_pin") != previous_pin:
        raise BehaviorProfileError("behavior.migration_review_contract")
    contract["behavior_pin"] = next_pin
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (
        str(body or "")[:match.start(1)]
        + encoded
        + str(body or "")[match.end(1):]
    )


def migrate_objective(conn, *, objective_id, platform, chat_id, thread_id,
                      profile_id, version, expected_revision, expected_pin_hash,
                      reason, policy_mode="retain", rollback_generation=None,
                      project_namespace=None, apply=False,
                      recovery_review_task_id=None,
                      closure_review_task_id=None,
                      kernel_repair_review_task_id=None):
    """Explicit idle migration, plus one exact parked-review re-pin boundary."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.objective_workflow import has_in_flight_delegation
    if not isinstance(reason, str) or not reason.strip():
        raise BehaviorProfileError("behavior.migration_reason_required")
    if policy_mode not in {"retain", "current"}:
        raise BehaviorProfileError("behavior.migration_policy_mode")
    with kb.write_txn(conn) if apply else _read_transaction(conn):
        objective = kb.get_grace_objective(conn, objective_id)
        if objective is None or objective["status"] not in kb._ACTIVE_GRACE_OBJECTIVE_STATUSES:
            raise BehaviorProfileError("behavior.migration_inactive")
        if tuple(objective[k] for k in ("platform", "chat_id", "thread_id")) != (platform, chat_id, thread_id):
            raise BehaviorProfileError("behavior.migration_topic_mismatch")
        previous = get_pin(conn, objective_id)
        if objective["revision"] != expected_revision or digest(previous) != expected_pin_hash:
            raise BehaviorProfileError("behavior.migration_cas: inspect the current Objective")
        if previous and previous["behavior_profile_id"] != profile_id:
            raise BehaviorProfileError("behavior.migration_project_mismatch")
        recovery_review_id = str(recovery_review_task_id or "").strip()
        closure_review_id = str(closure_review_task_id or "").strip()
        kernel_repair_review_id = str(
            kernel_repair_review_task_id or ""
        ).strip()
        if recovery_review_task_id is not None and (
            not recovery_review_id
            or recovery_review_id != recovery_review_task_id
            or previous is None
            or policy_mode != "retain"
            or rollback_generation is not None
            or previous["behavior_profile_id"] != profile_id
            or previous["behavior_profile_version"] != version
        ):
            raise BehaviorProfileError("behavior.migration_timeout_recovery_invalid")
        if closure_review_task_id is not None and (
            not closure_review_id
            or closure_review_id != closure_review_task_id
            or recovery_review_id
            or previous is None
            or policy_mode != "retain"
            or rollback_generation is not None
            or previous["behavior_profile_id"] != profile_id
            or previous["behavior_profile_version"] != version
        ):
            raise BehaviorProfileError("behavior.migration_closure_recovery_invalid")
        if kernel_repair_review_task_id is not None and (
            not kernel_repair_review_id
            or kernel_repair_review_id != kernel_repair_review_task_id
            or recovery_review_id
            or closure_review_id
            or previous is None
            or policy_mode != "retain"
            or rollback_generation is not None
            or previous["behavior_profile_id"] != profile_id
            or previous["behavior_profile_version"] == version
        ):
            raise BehaviorProfileError("behavior.migration_kernel_repair_invalid")
        if kernel_repair_review_id and not _kernel_repair_profiles_compatible(
            previous, profile(profile_id, version),
        ):
            raise BehaviorProfileError(
                "behavior.migration_kernel_repair_incompatible"
            )
        if has_in_flight_delegation(conn, objective_id) or conn.execute(
            "SELECT 1 FROM grace_delegations d JOIN task_runs r ON r.task_id IN (d.execution_task_id,d.review_task_id) WHERE d.objective_id=? AND r.ended_at IS NULL LIMIT 1",
            (objective_id,),
        ).fetchone():
            raise BehaviorProfileError("behavior.migration_inflight: finish or cancel the active work first")
        # Every terminal callback must agree with its exact completed stage.
        # The sole unfinished exception is an accepted intermediate callback
        # parked by the controller's authenticated session-reset handoff.
        callbacks = conn.execute(
            "SELECT * FROM grace_loop_callbacks WHERE objective_id=?",
            (objective_id,),
        ).fetchall()
        timeout_recovery_admitted = False
        closure_recovery_admitted = False
        closure_recovery_event_id = None
        kernel_repair_admitted = False
        kernel_repair_run_id = None
        for callback in callbacks:
            stage = conn.execute(
                "SELECT status,outcome_kind,delegation_id "
                "FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
                (objective_id, callback["stage_key"]),
            ).fetchone()
            settled = bool(
                callback["lease_owner"] is None
                and stage is not None
                and stage["status"] == "done"
                and stage["outcome_kind"] == callback["outcome_kind"]
                and (
                    callback["state"] in {"delivered", "attention"}
                    or (
                        callback["state"] == "cancelled"
                        and stage["outcome_kind"] in {
                            "superseded_by_retry", "cancelled",
                            "intermediate_blocked",
                        }
                    )
                )
            )
            if settled:
                continue
            if (
                recovery_review_id
                and _exact_timeout_review_repin_allowed(
                    conn,
                    objective=objective,
                    callback=callback,
                    review_task_id=recovery_review_id,
                    previous_pin=previous,
                )
            ):
                timeout_recovery_admitted = True
                continue
            if (
                closure_review_id
                and _exact_terminal_closure_repin_allowed(
                    conn,
                    objective=objective,
                    callback=callback,
                    review_task_id=closure_review_id,
                )
            ):
                closure_recovery_admitted = True
                closure_recovery_event_id = int(callback["attempt_event_id"])
                continue
            if (
                kernel_repair_review_id
                and _exact_kernel_repair_review_repin_allowed(
                    conn,
                    objective=objective,
                    callback=callback,
                    review_task_id=kernel_repair_review_id,
                    previous_pin=previous,
                )
            ):
                kernel_repair_admitted = True
                run = conn.execute(
                    "SELECT id FROM task_runs WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (kernel_repair_review_id,),
                ).fetchone()
                kernel_repair_run_id = int(run["id"])
                continue
            exact_reset_handoff = bool(
                callback["state"] == "attention"
                and callback["last_error"] == kb._GRACE_SESSION_RESET_HANDOFF_ERROR
                and str(callback["completion_mode"] or "terminal") == "intermediate"
                and callback["lease_event_id"] is None
                and callback["lease_owner"] is None
                and callback["outcome_event_id"] is None
                and callback["outcome_kind"] is None
                and callback["user_report_delivered_at"] is None
                and stage is not None
                and stage["status"] != "done"
                and stage["outcome_kind"] is None
                and objective["current_stage_key"] == callback["stage_key"]
                and tuple(callback[key] for key in (
                    "platform", "chat_id", "thread_id",
                )) == (platform, chat_id, thread_id)
            )
            delegation = None
            event = None
            review_run = None
            if exact_reset_handoff:
                delegations = conn.execute(
                    "SELECT d.state,e.status execution_status,r.status review_status "
                    "FROM grace_delegations d "
                    "JOIN tasks e ON e.id=d.execution_task_id "
                    "JOIN tasks r ON r.id=d.review_task_id "
                    "WHERE d.execution_task_id=? AND d.review_task_id=? "
                    "AND d.objective_id=? AND d.stage_key=? "
                    "AND d.delegation_id=?",
                    (
                        callback["execution_task_id"], callback["review_task_id"],
                        objective_id, callback["stage_key"], stage["delegation_id"],
                    ),
                ).fetchall()
                delegation = delegations[0] if len(delegations) == 1 else None
                event = conn.execute(
                    """SELECT e.id,e.run_id FROM task_events e
                         WHERE e.task_id=? AND e.id>? AND e.kind='completed'
                           AND NOT EXISTS (
                               SELECT 1 FROM task_events later
                                WHERE later.task_id=e.task_id AND later.id>e.id
                                  AND later.kind IN (
                                      'unblocked','promoted','claimed','spawned',
                                      'completed','backend_retry_scheduled'
                                  )
                           )
                         ORDER BY e.id DESC LIMIT 1""",
                    (callback["review_task_id"], int(callback["last_event_id"] or 0)),
                ).fetchone()
                review_run = (
                    kb.get_run(conn, int(event["run_id"]))
                    if event is not None and event["run_id"] is not None
                    else None
                )
            if (
                exact_reset_handoff
                and delegation is not None
                and delegation["state"] == "queued"
                and delegation["execution_status"] == "done"
                and delegation["review_status"] == "done"
                and review_run is not None
                and review_run.task_id == callback["review_task_id"]
                and review_run.outcome == "completed"
                and kb.grace_review_accepted(review_run.metadata)
                and not conn.execute(
                    "SELECT 1 FROM task_external_effects "
                    "WHERE task_id IN (?,?) LIMIT 1",
                    (callback["execution_task_id"], callback["review_task_id"]),
                ).fetchone()
            ):
                continue
            raise BehaviorProfileError("behavior.migration_callback_pending")
        if recovery_review_id and not timeout_recovery_admitted:
            raise BehaviorProfileError("behavior.migration_timeout_recovery_invalid")
        if closure_review_id and not closure_recovery_admitted:
            raise BehaviorProfileError("behavior.migration_closure_recovery_invalid")
        if kernel_repair_review_id and not kernel_repair_admitted:
            raise BehaviorProfileError("behavior.migration_kernel_repair_invalid")
        if (
            kernel_repair_review_id
            and _kernel_repair_review_repin_recorded(
                conn,
                objective_id=objective_id,
                review_task_id=kernel_repair_review_id,
                review_run_id=kernel_repair_run_id,
                previous_pin_hash=digest(previous),
            )
        ):
            raise BehaviorProfileError(
                "behavior.migration_kernel_repair_already_applied"
            )
        if closure_review_id and _terminal_closure_recovery_recorded(
            conn,
            objective_id=objective_id,
            review_task_id=closure_review_id,
            review_event_id=closure_recovery_event_id,
        ):
            raise BehaviorProfileError(
                "behavior.migration_closure_recovery_already_applied"
            )
        # Cover a partially built card before its saga has attached task IDs.
        for task in conn.execute("SELECT id,body,status,current_run_id FROM tasks WHERE status NOT IN ('done','blocked','archived') AND instr(body,'GRACE_BEHAVIOR_PIN: ')>0"):
            for line in task["body"].splitlines():
                if line.startswith("GRACE_BEHAVIOR_PIN: "):
                    marker = json.loads(line[len("GRACE_BEHAVIOR_PIN: "):])
                    if (marker.get("objective_ref") or {}).get("objective_id") == objective_id:
                        if (
                            kernel_repair_admitted
                            and task["id"] == kernel_repair_review_id
                            and task["status"] == "todo"
                            and task["current_run_id"] is None
                        ):
                            break
                        dependency_finished = (
                            task["current_run_id"] is None
                            and conn.execute(
                                """SELECT 1 FROM grace_objective_stages s
                                   WHERE s.objective_id=? AND s.review_task_id=?
                                     AND s.status='done'
                                     AND s.completed_at IS NOT NULL
                                     AND s.delegation_id IS NOT NULL
                                     AND s.execution_task_id IS NOT NULL
                                     AND s.review_task_id IS NOT NULL
                                     AND s.outcome_kind IN (
                                         'continued','intermediate_blocked',
                                         'terminal_blocked',
                                         'superseded_by_retry','cancelled'
                                     )
                                     AND (
                                         s.outcome_kind='intermediate_blocked'
                                         OR ?='todo'
                                     )
                                     AND EXISTS (
                                         SELECT 1 FROM grace_delegations d
                                          WHERE d.delegation_id=s.delegation_id
                                            AND d.objective_id=s.objective_id
                                            AND d.stage_key=s.stage_key
                                            AND d.execution_task_id=s.execution_task_id
                                            AND d.review_task_id=s.review_task_id
                                     )
                                     AND NOT EXISTS (
                                         SELECT 1 FROM tasks bound_task
                                          WHERE bound_task.id IN (
                                              s.execution_task_id,
                                              s.review_task_id
                                          )
                                            AND bound_task.current_run_id IS NOT NULL
                                     )
                                     AND NOT EXISTS (
                                         SELECT 1 FROM task_runs active_run
                                          WHERE active_run.task_id IN (
                                              s.execution_task_id,
                                              s.review_task_id
                                          )
                                            AND active_run.ended_at IS NULL
                                     )
                                     AND EXISTS (
                                         SELECT 1 FROM grace_loop_callbacks c
                                          WHERE c.review_task_id=s.review_task_id
                                            AND c.objective_id=s.objective_id
                                            AND c.stage_key=s.stage_key
                                            AND c.state IN ('delivered','attention','cancelled')
                                            AND c.outcome_kind=s.outcome_kind
                                            AND c.lease_owner IS NULL
                                     )""",
                                (objective_id, task["id"], task["status"]),
                            ).fetchone()
                        )
                        if dependency_finished:
                            break
                        raise BehaviorProfileError("behavior.migration_inflight: unfinished versioned card")
        row = conn.execute("SELECT policy FROM grace_objective_behavior_pins WHERE objective_id=?", (objective_id,)).fetchone() if previous else None
        previous_policy = json.loads(row[0]) if row else None
        generation = previous["generation"] + 1 if previous else 1
        if rollback_generation is not None:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_behavior_migrations'").fetchone():
                raise BehaviorProfileError("behavior.rollback_target_unavailable")
            record = conn.execute("SELECT previous_pin,previous_policy FROM grace_behavior_migrations WHERE objective_id=? AND generation=?",
                                  (objective_id, rollback_generation)).fetchone()
            restored = json.loads(record[0]) if record and record[0] else None
            if restored is None or (restored["behavior_profile_id"], restored["behavior_profile_version"]) != (profile_id, version):
                raise BehaviorProfileError("behavior.rollback_target_unavailable")
            policy = json.loads(record[1])
        elif policy_mode == "current":
            policy = _policy_snapshot(platform, chat_id, thread_id)
        else:
            if previous_policy is None:
                raise BehaviorProfileError("behavior.legacy_policy_unknown: explicitly accept current policy to migrate")
            policy = previous_policy
        historical_projects = {
            (json.loads(r[0]).get("identity") or {}).get("project")
            for r in conn.execute("SELECT contract_snapshot FROM grace_delegations WHERE objective_id=? AND contract_snapshot IS NOT NULL", (objective_id,))
        }
        namespace = previous["project_namespace"] if previous else project_namespace
        if namespace is None:
            if len(historical_projects) == 1:
                namespace = next(iter(historical_projects))
            else:
                raise BehaviorProfileError("behavior.migration_project_unknown: specify the original project namespace")
        if (not isinstance(namespace, str) or not namespace.strip()
                or (project_namespace is not None and project_namespace != namespace)
                or historical_projects - {namespace}):
            raise BehaviorProfileError("behavior.migration_project_mismatch")
        candidate = _make_pin(profile_id, version, policy, generation, project_namespace=namespace)
        verify_pin(candidate)
        result = {"objective_id": objective_id, "previous_pin": previous,
                  "next_pin": candidate, "expected_revision": expected_revision,
                  "next_revision": expected_revision + 1, "applied": bool(apply)}
        if apply:
            if recovery_review_id or kernel_repair_review_id:
                repin_review_id = (
                    recovery_review_id or kernel_repair_review_id
                )
                review = conn.execute(
                    "SELECT body FROM tasks WHERE id=? AND status IN ('blocked','todo') "
                    "AND current_run_id IS NULL",
                    (repin_review_id,),
                ).fetchone()
                if review is None:
                    raise BehaviorProfileError("behavior.migration_review_marker")
                body = _replace_behavior_marker(
                    review["body"], previous_pin=previous, next_pin=candidate,
                )
                if kernel_repair_review_id:
                    body = _replace_compiled_behavior_pin(
                        body,
                        previous_pin=previous,
                        next_pin=candidate,
                    )
                cur = conn.execute(
                    "UPDATE tasks SET body=? WHERE id=? AND status IN ('blocked','todo') "
                    "AND current_run_id IS NULL AND body=?",
                    (body, repin_review_id, review["body"]),
                )
                if cur.rowcount != 1:
                    raise BehaviorProfileError("behavior.migration_review_marker")
            migration_reason = reason
            if closure_review_id:
                closure_callback = conn.execute(
                    "SELECT attempt_event_id FROM grace_loop_callbacks "
                    "WHERE review_task_id=? AND objective_id=?",
                    (closure_review_id, objective_id),
                ).fetchone()
                if (
                    closure_callback is None
                    or closure_callback["attempt_event_id"] is None
                ):
                    raise BehaviorProfileError(
                        "behavior.migration_closure_recovery_invalid"
                    )
                migration_reason = json.dumps(
                    {
                        "kind": "terminal_closure_recovery",
                        "review_task_id": closure_review_id,
                        "review_event_id": int(
                            closure_callback["attempt_event_id"]
                        ),
                        "previous_pin_hash": digest(previous),
                        "next_generation": candidate["generation"],
                        "reason": reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            elif kernel_repair_review_id:
                migration_reason = json.dumps(
                    {
                        "kind": "kernel_repair_review_repin",
                        "review_task_id": kernel_repair_review_id,
                        "review_run_id": kernel_repair_run_id,
                        "previous_pin_hash": digest(previous),
                        "next_generation": candidate["generation"],
                        "reason": reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            conn.execute("INSERT INTO grace_behavior_migrations VALUES (?,?,?,?,?,?,?)",
                         (objective_id, generation, json.dumps(previous) if previous else None,
                          json.dumps(previous_policy, ensure_ascii=False) if previous_policy else None,
                          json.dumps(candidate), migration_reason, int(time.time())))
            conn.execute("INSERT INTO grace_objective_behavior_pins VALUES (?,?,?) ON CONFLICT(objective_id) DO UPDATE SET pin=excluded.pin,policy=excluded.policy",
                         (objective_id, json.dumps(candidate), json.dumps(policy, ensure_ascii=False)))
            conn.execute("UPDATE grace_objectives SET revision=revision+1,updated_at=? WHERE objective_id=? AND revision=?",
                         (int(time.time()), objective_id, expected_revision))
        return result


def resume_kernel_repair_review(
    conn, *, objective_id, review_task_id, apply=False,
):
    """Resume the one review authorized by the latest kernel-repair receipt."""
    from hermes_cli import kanban_db as kb

    objective_id = str(objective_id or "").strip()
    review_task_id = str(review_task_id or "").strip()
    if not objective_id or not review_task_id:
        raise BehaviorProfileError("behavior.kernel_repair_resume_invalid")

    with kb.write_txn(conn) if apply else _read_transaction(conn):
        objective = kb.get_grace_objective(conn, objective_id)
        current_pin = get_pin(conn, objective_id)
        task = kb.get_task(conn, review_task_id)
        callback = kb.get_grace_loop_callback(conn, review_task_id)
        migration = conn.execute(
            "SELECT generation,reason FROM grace_behavior_migrations "
            "WHERE objective_id=? ORDER BY generation DESC LIMIT 1",
            (objective_id,),
        ).fetchone()
        try:
            receipt = json.loads(migration["reason"]) if migration else None
        except (TypeError, ValueError, json.JSONDecodeError):
            receipt = None
        marker = None
        compiled = None
        if task is not None:
            marker_lines = [
                line[len("GRACE_BEHAVIOR_PIN: "):]
                for line in str(task.body or "").splitlines()
                if line.startswith("GRACE_BEHAVIOR_PIN: ")
            ]
            if len(marker_lines) == 1:
                try:
                    marker = json.loads(marker_lines[0])
                except (TypeError, ValueError, json.JSONDecodeError):
                    marker = None
            compiled = kb._grace_compiled_contract(str(task.body or ""))
        latest_event = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (review_task_id,),
        ).fetchone()
        valid = bool(
            objective is not None
            and callback is not None
            and objective["status"] in kb._ACTIVE_GRACE_OBJECTIVE_STATUSES
            and objective["current_stage_key"] == callback.get("stage_key")
            and current_pin is not None
            and migration is not None
            and int(migration["generation"]) == int(current_pin["generation"])
            and isinstance(receipt, dict)
            and receipt.get("kind") == "kernel_repair_review_repin"
            and receipt.get("review_task_id") == review_task_id
            and isinstance(receipt.get("review_run_id"), int)
            and receipt.get("next_generation") == current_pin["generation"]
            and callback.get("objective_id") == objective_id
            and callback.get("state") == "attention"
            and int(callback.get("attempts") or 0) >= 3
            and callback.get("attempt_event_id") is not None
            and callback.get("lease_event_id") is None
            and callback.get("lease_owner") is None
            and callback.get("outcome_event_id") is None
            and callback.get("outcome_kind") is None
            and callback.get("user_report_delivered_at") is None
            and str(callback.get("last_error") or "").startswith(
                ("control_plane_repair_in_progress:",
                 "Grace callback delivery failed: RuntimeError: Grace callback lease expired, "
                 "ownership changed, or its trigger event was superseded before finalization.")
            )
            and task is not None
            and task.status == "blocked"
            and task.current_run_id is None
            and marker is not None
            and marker.get("behavior_pin") == current_pin
            and isinstance(compiled, dict)
            and compiled.get("behavior_pin") == current_pin
            and latest_event is not None
            and int(latest_event["id"]) == int(callback["attempt_event_id"])
        )
        if not valid:
            raise BehaviorProfileError("behavior.kernel_repair_resume_invalid")
        result = {
            "objective_id": objective_id,
            "review_task_id": review_task_id,
            "review_run_id": receipt["review_run_id"],
            "generation": current_pin["generation"],
            "applied": bool(apply),
        }
        if apply:
            previous_event_id = int(callback["attempt_event_id"])
            if not kb.unblock_task(conn, review_task_id):
                raise BehaviorProfileError("behavior.kernel_repair_resume_changed")
            resumed_task = kb.get_task(conn, review_task_id)
            if resumed_task is not None and resumed_task.status == "todo":
                promoted, _ = kb.promote_task(
                    conn,
                    review_task_id,
                    actor="behavior-kernel-repair",
                    reason="retry exact review after verified kernel repair",
                    force=True,
                )
                if not promoted:
                    raise BehaviorProfileError(
                        "behavior.kernel_repair_resume_changed"
                    )
            cur = conn.execute(
                "UPDATE grace_loop_callbacks SET state='pending',attempts=0,"
                "last_error=NULL,last_event_id=?,attempt_event_id=NULL,"
                "lease_event_id=NULL,lease_owner=NULL,lease_expires=NULL "
                "WHERE review_task_id=? AND objective_id=? AND state='attention' "
                "AND attempt_event_id=? AND outcome_event_id IS NULL "
                "AND lease_owner IS NULL",
                (
                    previous_event_id,
                    review_task_id,
                    objective_id,
                    previous_event_id,
                ),
            )
            if cur.rowcount != 1:
                raise BehaviorProfileError("behavior.kernel_repair_resume_changed")
            kb._append_event(
                conn,
                review_task_id,
                "callback_kernel_repair_resume_requested",
                {
                    "objective_id": objective_id,
                    "review_run_id": receipt["review_run_id"],
                    "generation": current_pin["generation"],
                },
            )
        return result
