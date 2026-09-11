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


def set_selection(conn, *, platform, chat_id, thread_id, project, profile_id,
                  version, expected_revision, reason):
    """CAS a new-Objective pointer. None disables it; existing pins never change."""
    from hermes_cli import kanban_db as kb
    if not all(isinstance(v, str) and v.strip() for v in (platform, chat_id, thread_id, project, reason)):
        raise BehaviorProfileError("behavior.selection_scope: exact Topic, project and reason required")
    if profile_id is not None:
        profile(profile_id, version)
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


def verify_pin(pin):
    manifest = profile(pin["behavior_profile_id"], pin["behavior_profile_version"])
    expected = {
        "contract_schema_version": manifest["contract_schema_version"],
        "validator_set_hash": manifest["validator_set_hash"],
        "behavior_bundle_hash": digest(manifest),
        "safety_kernel_version": manifest["safety_kernel_version"],
        "safety_kernel_hash": manifest["safety_kernel_hash"],
    }
    if any(pin.get(k) != v for k, v in expected.items()):
        raise BehaviorProfileError("behavior.manifest_mismatch")
    kernel_version = str(pin["safety_kernel_version"])
    if not re.fullmatch(r"[1-9][0-9]*", kernel_version):
        raise BehaviorProfileError("behavior.kernel_version")
    kernel = json.loads((ROOT / f"kernel-v{kernel_version}.json").read_text())
    if digest(kernel) != pin["safety_kernel_hash"]:
        raise BehaviorProfileError("behavior.kernel_migration_required")
    root = CODE_ROOT
    for path, expected in kernel.items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != expected:
            raise BehaviorProfileError(f"behavior.kernel_migration_required: {path}")
    from hermes_cli import kanban_db as kb
    if kb._review_runtime_digest() != kb._REVIEW_RUNTIME_SHA256:
        raise BehaviorProfileError("behavior.runtime_reload_required")
    return manifest


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
    if not contract.get("objective_ref") and contract.get("behavior_pin") is None:
        return deepcopy(dict(contract))
    with _read_board(contract.get("identity") or {}) as conn:
        if conn is None:
            if contract.get("behavior_pin") is not None:
                raise BehaviorProfileError("behavior.board_missing")
            return deepcopy(dict(contract))
        return bind_contract(conn, contract)


def checked_pin(contract):
    """Read the authoritative board, never trust version metadata from a worker."""
    identity = contract.get("identity") or {}
    objective_id = (contract.get("objective_ref") or {}).get("objective_id")
    supplied = contract.get("behavior_pin")
    if not objective_id:
        if supplied is None:
            return None
        raise BehaviorProfileError("behavior.pin_unbound")
    with _read_board(identity) as conn:
        if conn is None:
            if supplied is None:
                return None
            raise BehaviorProfileError("behavior.board_missing")
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
        with _read_board(contract.get("identity") or {}) as conn:
            if conn is None:
                raise BehaviorProfileError("behavior.board_missing")
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
    identity = contract["identity"]
    with _read_board(identity) as conn:
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


def migrate_objective(conn, *, objective_id, platform, chat_id, thread_id,
                      profile_id, version, expected_revision, expected_pin_hash,
                      reason, policy_mode="retain", rollback_generation=None,
                      project_namespace=None, apply=False):
    """Explicit idle-boundary migration; never rewrites tasks, evidence or effects."""
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
        if has_in_flight_delegation(conn, objective_id) or conn.execute(
            "SELECT 1 FROM grace_delegations d JOIN task_runs r ON r.task_id IN (d.execution_task_id,d.review_task_id) WHERE d.objective_id=? AND r.ended_at IS NULL LIMIT 1",
            (objective_id,),
        ).fetchone():
            raise BehaviorProfileError("behavior.migration_inflight: finish or cancel the active work first")
        # Superseded callbacks remain cancelled, never fabricated as delivered.
        if conn.execute("""SELECT 1 FROM grace_loop_callbacks c WHERE c.objective_id=?
            AND (c.lease_owner IS NOT NULL OR NOT (
                c.state IN ('delivered','attention') OR (c.state='cancelled' AND EXISTS (
                    SELECT 1 FROM grace_objective_stages s
                    WHERE s.objective_id=c.objective_id AND s.stage_key=c.stage_key
                    AND s.status='done' AND s.outcome_kind IN ('superseded_by_retry','cancelled','intermediate_blocked')
                ))
            )) LIMIT 1""", (objective_id,)).fetchone():
            raise BehaviorProfileError("behavior.migration_callback_pending")
        # Cover a partially built card before its saga has attached task IDs.
        for task in conn.execute("SELECT body FROM tasks WHERE status NOT IN ('done','blocked','archived') AND instr(body,'GRACE_BEHAVIOR_PIN: ')>0"):
            for line in task[0].splitlines():
                if line.startswith("GRACE_BEHAVIOR_PIN: "):
                    marker = json.loads(line[len("GRACE_BEHAVIOR_PIN: "):])
                    if (marker.get("objective_ref") or {}).get("objective_id") == objective_id:
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
            conn.execute("INSERT INTO grace_behavior_migrations VALUES (?,?,?,?,?,?,?)",
                         (objective_id, generation, json.dumps(previous) if previous else None,
                          json.dumps(previous_policy, ensure_ascii=False) if previous_policy else None,
                          json.dumps(candidate), reason, int(time.time())))
            conn.execute("INSERT INTO grace_objective_behavior_pins VALUES (?,?,?) ON CONFLICT(objective_id) DO UPDATE SET pin=excluded.pin,policy=excluded.policy",
                         (objective_id, json.dumps(candidate), json.dumps(policy, ensure_ascii=False)))
            conn.execute("UPDATE grace_objectives SET revision=revision+1,updated_at=? WHERE objective_id=? AND revision=?",
                         (int(time.time()), objective_id, expected_revision))
        return result
