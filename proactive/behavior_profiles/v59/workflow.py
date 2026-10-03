"""Transactional objective planning and evidence-based resale continuation.

Operator interface: python -m hermes_cli.objective_workflow show ID
                       python -m hermes_cli.objective_workflow plan plan.json
Planning never dispatches a task, consumes an approval, or changes external state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from proactive.behavior_observation import observe_objective


def migrate(conn):
    kb._add_column_if_missing(conn, "grace_objectives", "revision", "revision INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE TABLE IF NOT EXISTS grace_objective_workflows (objective_id TEXT PRIMARY KEY, specification TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS grace_objective_revisions (
        objective_id TEXT NOT NULL, revision INTEGER NOT NULL, restart_stage_key TEXT NOT NULL,
        reason TEXT NOT NULL, source_message_id TEXT, snapshot TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY(objective_id,revision))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS grace_objective_plan_requests (
        request_id TEXT PRIMARY KEY, objective_id TEXT NOT NULL,
        delegation_id TEXT NOT NULL, execution_task_id TEXT NOT NULL,
        execution_run_id INTEGER NOT NULL, expected_revision INTEGER NOT NULL,
        specification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        result TEXT, created_at INTEGER NOT NULL, applied_at INTEGER,
        UNIQUE(objective_id,delegation_id,execution_run_id))""")


def configuration(conn, objective_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_workflows'").fetchone():
        return None
    row = conn.execute(
        "SELECT specification FROM grace_objective_workflows WHERE objective_id=?",
        (objective_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _history_reference(conn, objective, project, listing_id, reference):
    """Pin legacy evidence without rewriting old contracts or granting permission."""
    from .review import grace_review_accepted

    execution_id = reference["execution_task_id"]
    review_id = reference["review_task_id"]
    link = conn.execute(
        "SELECT d.* FROM grace_delegations d JOIN task_links l "
        "ON l.parent_id=d.execution_task_id AND l.child_id=d.review_task_id "
        "WHERE d.execution_task_id=? AND d.review_task_id=?",
        (execution_id, review_id),
    ).fetchone()
    if not link or any(link[k] != objective[k] for k in ("platform", "chat_id", "thread_id")):
        raise ValueError("Historical evidence must belong to the exact Topic and review pair")
    if link["objective_id"] and link["objective_id"] != objective["objective_id"]:
        raise ValueError("Historical evidence already belongs to another objective")
    execution = kb.latest_run(conn, execution_id)
    review = kb.latest_run(conn, review_id)
    task = kb.get_task(conn, execution_id)
    review_task = kb.get_task(conn, review_id)
    blocker = reference.get("use") == "blocker_context_only"
    accepted = (execution and review and task and review_task
                and task.status == review_task.status == "done"
                and execution.outcome == review.outcome == "completed"
                and grace_review_accepted(review.metadata))
    if not (accepted or (blocker and execution and task and task.status == "blocked"
                         and execution.outcome == "blocked" and execution.ended_at)):
        raise ValueError("Historical evidence requires completed execution and accepted review")
    contract = (execution.metadata or {}).get("loop_contract") or {}
    if (contract.get("identity") or {}).get("project") != project:
        raise ValueError("Historical evidence belongs to another project")
    if not re.search(r"(?<![0-9])" + re.escape(listing_id) + r"(?![0-9])", task.body or ""):
        raise ValueError("Historical evidence does not identify the source listing")
    receipt = (review.metadata or {}).get("evidence") or {} if review else {}
    exact_review = (isinstance(receipt, dict)
                    and receipt.get("parent_execution_task_id") == execution_id
                    and receipt.get("parent_execution_run_id") == execution.id)
    if not blocker and isinstance(receipt, dict) and receipt.get("parent_execution_run_id") is not None and not exact_review:
        raise ValueError("Historical review names a different execution run")
    # Legacy reviews are usable as historical context, never as publication grants.
    return {
        "execution_task_id": execution_id, "execution_run_id": execution.id,
        "review_task_id": review_id, "review_run_id": review.id if review else None,
        "use": "blocker_context_only" if blocker else "historical_context_only", "summary": execution.summary,
        "review_binding": "exact_run" if exact_review else "unverified_legacy_context_only",
    }


@observe_objective("objective.workflow_plan")
def plan(conn, *, objective_id, expected_revision, platform, chat_id, thread_id,
         required_stage_keys, current_stage_key, reason, acceptance_criteria=None,
         title=None, workflow=None, next_action=None,
         _excluding_delegation_id=""):
    """Add/reorder planned stages atomically, retaining all bound and finished work."""
    stages = list(required_stage_keys)
    # current_stage_key is the operator's intended resume point, not a claim
    # that predecessors have completed. Repair stages may still await review;
    # planning does not dispatch or change their status. Closure checks every
    # required stage separately, so this cursor cannot bypass acceptance.
    if (not stages or any(not isinstance(s, str) or not s.strip() for s in stages)
            or len(set(stages)) != len(stages) or current_stage_key not in stages
            or not str(reason).strip()):
        raise ValueError("Plan requires unique stages, a declared current stage, and a reason")
    with kb.write_txn(conn):
        objective = kb.get_grace_objective(conn, objective_id)
        if not objective or objective["status"] not in kb._ACTIVE_GRACE_OBJECTIVE_STATUSES:
            raise ValueError("Objective is unknown or inactive")
        if (objective["platform"], objective["chat_id"], objective["thread_id"]) != (platform, chat_id, thread_id):
            raise ValueError("Objective belongs to another Topic")
        if objective["revision"] != expected_revision:
            raise ValueError("Objective revision changed; inspect before retrying")
        before = [dict(r) for r in conn.execute(
            "SELECT * FROM grace_objective_stages WHERE objective_id=? ORDER BY position",
            (objective_id,),
        )]
        if stages[-1] != objective["terminal_stage_key"]:
            from hermes_cli.objective_workflow import _prepare_terminal_retry_plan
            _prepare_terminal_retry_plan(conn, objective, stages[-1])
        retrying_cancelled_stage = {
            r["stage_key"] for r in before
            if r["stage_key"] == current_stage_key
            and r["status"] == "done"
            and r["outcome_kind"] == "cancelled"
        }
        resume = next((r for r in before if r["stage_key"] == current_stage_key), None)
        if resume and current_stage_key not in retrying_cancelled_stage and (
                resume["status"] != "planned" or resume["delegation_id"]
                or resume["execution_task_id"] or resume["review_task_id"]
                or resume["evidence"]):
            raise ValueError("Plan current stage must be unbound")
        fixed = [
            r["stage_key"] for r in before
            if r["stage_key"] not in retrying_cancelled_stage
            and (
                r["delegation_id"] or r["status"] != "planned"
                or r["execution_task_id"] or r["review_task_id"] or r["evidence"]
            )
        ]
        if not set(fixed).issubset(stages):
            raise ValueError("Plan cannot remove bound stages or their evidence")
        if [s for s in stages if s in fixed] != fixed:
            raise ValueError("Plan cannot reverse bound or completed stage history")
        if conn.execute(
            "SELECT 1 FROM grace_objective_stages s JOIN task_runs r "
            "ON r.task_id IN (s.execution_task_id,s.review_task_id) "
            "WHERE s.objective_id=? AND r.ended_at IS NULL LIMIT 1", (objective_id,),
        ).fetchone():
            raise ValueError("Objective has an active run; wait for it before planning")
        if has_in_flight_delegation(
            conn, objective_id,
            excluding_delegation_id=_excluding_delegation_id,
        ):
            raise ValueError("Objective has an in-flight delegation; finish or block it before planning")
        criteria = acceptance_criteria if acceptance_criteria is not None else json.loads(objective["acceptance_criteria"])
        if not isinstance(criteria, list) or not criteria or any(not isinstance(c, str) or not c.strip() for c in criteria):
            raise ValueError("Acceptance criteria must be nonempty strings")
        previous = configuration(conn, objective_id)
        spec = previous
        if workflow is not None:
            spec = dict(workflow)
            if isinstance(spec.get("project"), str):
                spec["project"] = spec["project"].strip()
            if (not isinstance(spec.get("source_listing_id"), str)
                    or not re.fullmatch(r"[1-9][0-9]*", spec["source_listing_id"])
                    or type(spec.get("expected_destinations")) is not int
                    or spec["expected_destinations"] < 1
                    or not isinstance(spec.get("project"), str) or not spec["project"].strip()):
                raise ValueError("Workflow requires project, exact listing, and positive destination target")
            if previous and any(previous[k] != spec[k] for k in ("project", "source_listing_id", "expected_destinations")):
                raise ValueError("Plan cannot change the originating listing or destination target")
            # This operator-only recovery API establishes the initial structured
            # binding from the authorized plan, alongside acceptance_criteria.
            # Legacy free-text objectives cannot authoritatively supply those
            # fields. Publication callers cannot initialize or edit this binding.
            excluded = spec.get("excluded_destination_ids", [])
            if not isinstance(excluded, list) or any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]*", v) for v in excluded):
                raise ValueError("Excluded destinations must be numeric group IDs")
            names = spec.get("excluded_destination_names", [])
            if not isinstance(names, list) or any(not isinstance(v, str) or not v.strip() for v in names):
                raise ValueError("Historical destination names must be nonempty strings")
            spec["excluded_destination_names"] = names = [name.strip() for name in names]
            pinned = {r["execution_task_id"]: r for r in (previous or {}).get("historical_evidence", [])}
            history = []
            for ref in spec.get("historical_evidence", []):
                existing = pinned.get(ref.get("execution_task_id"))
                if existing:
                    if (ref.get("review_task_id") != existing["review_task_id"]
                            or any(key in ref and ref[key] != existing[key]
                                   for key in ("execution_run_id", "review_run_id"))):
                        raise ValueError("Plan cannot replace pinned historical task/run references")
                    history.append(existing)
                else:
                    history.append(_history_reference(conn, objective, spec["project"], spec["source_listing_id"], ref))
            spec["historical_evidence"] = history
            if previous:
                old = {r["execution_task_id"] for r in previous.get("historical_evidence", [])}
                if not old.issubset(r["execution_task_id"] for r in spec["historical_evidence"]):
                    raise ValueError("Plan cannot discard historical evidence")
                if not set(previous.get("excluded_destination_ids", [])).issubset(excluded):
                    raise ValueError("Plan cannot discard historical destination exclusions")
                if not set(previous.get("excluded_destination_names", [])).issubset(names):
                    raise ValueError("Plan cannot discard historical destination names")
        now = int(time.time())
        revision = expected_revision + 1
        conn.execute(
            "INSERT INTO grace_objective_revisions "
            "(objective_id,revision,restart_stage_key,reason,snapshot,created_at) VALUES (?,?,?,?,?,?)",
            (objective_id, revision, current_stage_key, reason,
             json.dumps({"objective": objective, "stages": before, "workflow": previous}, ensure_ascii=False), now),
        )
        for row in before:
            if row["stage_key"] not in stages:
                conn.execute("DELETE FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
                             (objective_id, row["stage_key"]))
        for position, stage in enumerate(stages):
            conn.execute(
                "INSERT INTO grace_objective_stages (objective_id,stage_key,position,status,created_at,updated_at) "
                "VALUES (?,?,?,'planned',?,?) ON CONFLICT(objective_id,stage_key) DO UPDATE "
                "SET position=excluded.position,updated_at=excluded.updated_at",
                (objective_id, stage, position, now, now),
            )
        for stage in retrying_cancelled_stage:
            conn.execute(
                "UPDATE grace_objective_stages SET status='planned',delegation_id=NULL,"
                "execution_task_id=NULL,review_task_id=NULL,outcome_kind=NULL,evidence=NULL,"
                "completed_at=NULL,updated_at=? WHERE objective_id=? AND stage_key=?",
                (now, objective_id, stage),
            )
        conn.execute(
            "UPDATE grace_objectives SET required_stage_keys=?,current_stage_key=?,acceptance_criteria=?,"
            "title=?,revision=?,next_action=?,updated_at=?,terminal_stage_key=? WHERE objective_id=?",
            (json.dumps(stages), current_stage_key, json.dumps(criteria, ensure_ascii=False),
             title or objective["title"], revision,
             objective["next_action"] if next_action is None else next_action, now, stages[-1], objective_id),
        )
        if spec is not None:
            conn.execute(
                "INSERT INTO grace_objective_workflows (objective_id,specification) VALUES (?,?) "
                "ON CONFLICT(objective_id) DO UPDATE SET specification=excluded.specification",
                (objective_id, json.dumps(spec, ensure_ascii=False)),
            )
        actual = [r[0] for r in conn.execute(
            "SELECT stage_key FROM grace_objective_stages WHERE objective_id=? ORDER BY position", (objective_id,),
        )]
        if actual != stages:
            raise ValueError("Stage plan readback failed")
        return inspect(conn, objective_id)


def request_plan(conn, *, origin_execution_task_id, **specification):
    """Record a self-planner's proposal for application after exact review."""
    allowed_fields = {
        "objective_id", "expected_revision", "platform", "chat_id", "thread_id",
        "required_stage_keys", "current_stage_key", "reason", "acceptance_criteria",
        "title", "workflow", "next_action",
    }
    unsupported = set(specification) - allowed_fields
    if unsupported:
        raise ValueError(
            "Deferred plan contains unsupported fields: "
            + ", ".join(sorted(str(key) for key in unsupported))
        )
    objective_id = str(specification.get("objective_id") or "").strip()
    execution_task_id = str(origin_execution_task_id or "").strip()
    if not objective_id or not execution_task_id:
        raise ValueError("Deferred plan requires objective and execution task IDs")
    expected_revision = specification.get("expected_revision")
    if type(expected_revision) is not int:
        raise ValueError("Deferred plan expected_revision must be an integer")
    encoded = json.dumps(specification, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    with kb.write_txn(conn):
        objective = kb.get_grace_objective(conn, objective_id)
        delegation = conn.execute(
            "SELECT * FROM grace_delegations WHERE objective_id=? "
            "AND execution_task_id=?",
            (objective_id, execution_task_id),
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (execution_task_id,),
        ).fetchone()
        if not objective or not delegation or not run:
            raise ValueError("Deferred plan must originate from its active objective execution")
        if (objective["current_stage_key"] != delegation["stage_key"]
                or expected_revision != objective["revision"]):
            raise ValueError("Deferred plan origin stage or objective revision changed")
        stages = specification.get("required_stage_keys")
        current = specification.get("current_stage_key")
        existing = json.loads(objective["required_stage_keys"])
        if (not isinstance(stages, list) or current not in stages
                or current == delegation["stage_key"]
                or stages[-1:] != [objective["terminal_stage_key"]]
                or not set(existing).issubset(stages)):
            raise ValueError("Deferred plan must select another stage and retain the spine")
        resume = conn.execute(
            "SELECT * FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
            (objective_id, current),
        ).fetchone()
        if resume and not (
                resume["status"] == "done" and resume["outcome_kind"] == "cancelled"
        ) and (
                resume["status"] != "planned" or resume["delegation_id"]
                or resume["execution_task_id"] or resume["review_task_id"]
                or resume["evidence"]):
            raise ValueError("Deferred plan resume stage must be unbound")
        specification_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
        request_id = "gpr_" + hashlib.sha256(
            f"{delegation['delegation_id']}:{run['id']}:{encoded}".encode()
        ).hexdigest()[:32]
        prior = conn.execute(
            "SELECT * FROM grace_objective_plan_requests WHERE objective_id=? "
            "AND delegation_id=? AND execution_run_id=?",
            (objective_id, delegation["delegation_id"], run["id"]),
        ).fetchone()
        if prior:
            if prior["specification"] != encoded:
                raise ValueError("This execution run already requested a different plan")
            return dict(prior)
        conn.execute(
            "INSERT INTO grace_objective_plan_requests "
            "(request_id,objective_id,delegation_id,execution_task_id,execution_run_id,"
            "expected_revision,specification,state,created_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
            (request_id, objective_id, delegation["delegation_id"], execution_task_id,
             run["id"], objective["revision"], encoded, int(time.time())),
        )
        try:
            run_metadata = json.loads(run["metadata"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            run_metadata = {}
        if not isinstance(run_metadata, dict):
            run_metadata = {}
        run_metadata["objective_plan_request"] = {
            "request_id": request_id,
            "specification_sha256": specification_sha256,
            "specification": specification,
        }
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=? AND ended_at IS NULL",
            (json.dumps(run_metadata, ensure_ascii=False, sort_keys=True), run["id"]),
        )
        return dict(conn.execute(
            "SELECT * FROM grace_objective_plan_requests WHERE request_id=?", (request_id,),
        ).fetchone())


def apply_reviewed_plan_request(conn, *, review_task_id, review_run_id):
    """Apply only the proposal accepted for its exact, now-ended execution run."""
    if type(review_run_id) is not int:
        return None
    with kb.write_txn(conn):
        delegation = conn.execute(
            "SELECT * FROM grace_delegations WHERE review_task_id=?",
            (str(review_task_id or "").strip(),),
        ).fetchone()
        if not delegation:
            return None
        review = kb.get_run(conn, review_run_id)
        if (not review or review.task_id != delegation["review_task_id"]
                or review.outcome != "completed"):
            return None
        from .review import grace_review_accepted
        evidence = (review.metadata or {}).get("evidence") or {}
        execution_run_id = evidence.get("parent_execution_run_id")
        if (not grace_review_accepted(review.metadata)
                or evidence.get("parent_execution_task_id") != delegation["execution_task_id"]
                or type(execution_run_id) is not int):
            return None
        review_source = (review.metadata or {}).get("workflow_review_source")
        if not isinstance(review_source, dict) or any(
            review_source.get(key) != evidence.get(key)
            for key in (
                "parent_execution_task_id",
                "parent_execution_run_id",
                "parent_execution_evidence_sha256",
                "objective_plan_request_id",
                "objective_plan_specification_sha256",
            )
        ):
            raise ValueError(
                "Reviewed plan receipt does not match the controller-pinned "
                "review source"
            )
        execution = kb.get_run(conn, execution_run_id)
        if (not execution or execution.task_id != delegation["execution_task_id"]
                or execution.outcome != "completed" or not execution.ended_at):
            return None
        if evidence.get("parent_execution_evidence_sha256") != kb.workflow_review_evidence_hash(execution):
            raise ValueError("Reviewed plan execution evidence changed after review")
        if execution_run_id not in _accepted_execution_runs(
            conn,
            delegation["objective_id"],
            delegation["execution_task_id"],
        ):
            raise ValueError(
                "Reviewed plan acceptance was superseded by a later exact verdict"
            )
        request = conn.execute(
            "SELECT * FROM grace_objective_plan_requests WHERE objective_id=? "
            "AND delegation_id=? AND execution_run_id=? AND state='pending'",
            (delegation["objective_id"], delegation["delegation_id"], execution_run_id),
        ).fetchone()
        if not request:
            return None
        specification_sha256 = hashlib.sha256(
            request["specification"].encode()
        ).hexdigest()
        if (evidence.get("objective_plan_request_id") != request["request_id"]
                or evidence.get("objective_plan_specification_sha256")
                != specification_sha256):
            raise ValueError("Reviewed plan receipt is not bound to the exact request and specification")
        result = plan(
            conn, **json.loads(request["specification"]),
            _excluding_delegation_id=delegation["delegation_id"],
        )
        now = int(time.time())
        conn.execute(
            "UPDATE grace_objective_plan_requests SET state='applied',result=?,applied_at=? "
            "WHERE request_id=? AND state='pending'",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), now, request["request_id"]),
        )
        return result


def has_in_flight_delegation(conn, objective_id, *, excluding_delegation_id=""):
    return conn.execute(
        "SELECT 1 FROM grace_delegations d LEFT JOIN tasks t ON t.id=d.execution_task_id "
        "LEFT JOIN tasks v ON v.id=d.review_task_id "
        "LEFT JOIN grace_objective_stages s ON s.objective_id=d.objective_id "
        "AND s.delegation_id=d.delegation_id "
        "WHERE d.objective_id=? AND d.delegation_id<>? "
        "AND (s.status IS NULL OR s.status<>'done') AND "
        "(t.status IN ('triage','todo','scheduled','ready','running','review') OR "
        "v.status IN ('scheduled','triage','ready','running','review') OR (v.status='todo' AND t.status='done') OR "
        "(d.execution_task_id IS NULL AND d.state IN ('authorized','building'))) LIMIT 1",
        (objective_id, excluding_delegation_id),
    ).fetchone() is not None


def _accepted_execution_runs(conn, objective_id, task_id):
    """Keep exact run approvals across retries; never approve task-wide effects."""
    # Task status describes its latest attempt, not every historical run. A new
    # attempt cannot revoke an older accepted run; a newer review explicitly
    # bound to that same run supersedes its prior verdict below.
    from .review import (
        grace_review_accepted,
        grace_review_rejected,
    )

    pair = conn.execute(
        "SELECT review_task_id FROM grace_delegations WHERE objective_id=? AND execution_task_id=?",
        (objective_id, task_id),
    ).fetchone()
    if pair is None:
        return set()
    accepted, seen = set(), set()
    for row in conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC", (pair[0],)):
        review = kb.get_run(conn, row[0])
        if review.outcome != "completed":
            continue
        review_metadata = review.metadata or {}
        evidence = review_metadata.get("evidence") or {}
        review_source = review_metadata.get("workflow_review_source")
        if (
            not isinstance(evidence, dict)
            or not isinstance(review_source, dict)
            or evidence.get("parent_execution_task_id") != task_id
            or any(
                review_source.get(key) != evidence.get(key)
                for key in (
                    "parent_execution_task_id",
                    "parent_execution_run_id",
                    "parent_execution_evidence_sha256",
                )
            )
        ):
            continue
        run_id = evidence.get("parent_execution_run_id")
        if type(run_id) is not int or run_id in seen:
            continue
        execution = kb.get_run(conn, run_id)
        exact_evidence = bool(
            execution
            and execution.task_id == task_id
            and evidence.get("parent_execution_evidence_sha256")
            == kb.workflow_review_evidence_hash(execution)
        )
        # A later exact verdict may supersede the same run. Corrective or
        # prose-only rejection attempts without the evidence hash cannot revoke
        # an acceptance that was already durably bound to those bytes.
        if not exact_evidence or not (
            grace_review_accepted(review.metadata)
            or grace_review_rejected(review.metadata)
        ):
            continue
        seen.add(run_id)
        if (execution and execution.task_id == task_id and execution.outcome == "completed"
                and grace_review_accepted(review.metadata)
                and exact_evidence):
            accepted.add(run_id)
    return accepted


def _visible_publication(readback, group_id, listing_id, post_url):
    return (
        isinstance(readback, dict) and readback.get("status") == "published"
        and readback.get("visible") is True and readback.get("pending_review") is False
        and readback.get("group_id") == group_id and readback.get("source_listing_id") == listing_id
        and readback.get("post_url") == post_url
        and type(readback.get("observed_at")) in (int, float) and 0 < readback["observed_at"] <= time.time()
        and re.fullmatch(r"https://(?:www\.)?facebook\.com/groups/" + re.escape(group_id)
                         + r"/(?:posts|permalink)/[1-9][0-9]*/?(?:\?[^\s]*)?", post_url)
    )


def _readonly_contract_scope(contract, listing_id, group_id=None, group_alias=None):
    routing = contract.get("routing") or {}
    assignment = (routing.get("resolved") or {}).get("assignment") or {}
    allowed_entries = [
        str(item) for item in (contract.get("scope") or {}).get("allowed") or []
    ]
    allowed = " ".join(allowed_entries)
    listing_present = re.search(
        r"(?<![0-9])" + re.escape(listing_id) + r"(?![0-9])", allowed
    ) is not None
    normalized_alias = (
        re.sub(r"\s+", " ", group_alias.strip()).casefold()
        if isinstance(group_alias, str) and group_alias.strip()
        else ""
    )
    alias_present = any(
        normalized == normalized_alias
        or re.search(
            r"(?:^|\s)(?:(?:facebook|fb)\s+)?(?:groups?|社團|群組)"
            r"\s*[:：#-]?\s*" + re.escape(normalized_alias) + r"$",
            normalized,
            re.IGNORECASE,
        )
        is not None
        for normalized in (
            re.sub(r"\s+", " ", entry.strip()).casefold().rstrip(" .,:;!?。；！？，")
            for entry in allowed_entries
        )
        if normalized_alias
    )
    group_present = group_id is None or (
        re.search(r"(?<![0-9])" + re.escape(group_id) + r"(?![0-9])", allowed)
        is not None
        or alias_present
    )
    return (
        routing.get("task_type") in {"facebook_marketplace_readonly", "secondhand_commerce_group_status"}
        and assignment.get("interaction_mode") == "interactive_readonly"
        and not contract.get("facebook_group_publish")
        and listing_present
        and group_present
    )


def progress(conn, objective_id):
    """Project durable effects; candidate/Join counts can never close publication."""
    spec = configuration(conn, objective_id)
    if spec is None:
        return None
    ids = {r[0] for r in conn.execute(
        "SELECT execution_task_id FROM grace_delegations WHERE objective_id=? AND execution_task_id IS NOT NULL",
        (objective_id,),
    )}
    native_ids = set(ids)
    ids.update(r["execution_task_id"] for r in spec.get("historical_evidence", []))
    rows = {}
    observations = []
    for task_id in sorted(ids):
        accepted_runs = _accepted_execution_runs(conn, objective_id, task_id) if task_id in native_ids else set()
        for run_id in sorted(accepted_runs):
            run = kb.get_run(conn, run_id)
            evidence = (run.metadata or {}).get("acceptance_evidence") or {}
            if isinstance(evidence, dict) and isinstance(evidence.get("publication_reconciliation"), list):
                observer_contract = (run.metadata or {}).get("loop_contract") or {}
                for item in evidence["publication_reconciliation"]:
                    if (isinstance(item, dict) and isinstance(item.get("group_id"), str)
                            and type(item.get("observed_at")) in (int, float)
                            and run.ended_at and run.started_at <= item["observed_at"] <= run.ended_at
                            and (observer_contract.get("objective_ref") or {}).get("objective_id") == objective_id
                            and (observer_contract.get("identity") or {}).get("project") == spec["project"]
                            and _readonly_contract_scope(observer_contract, spec["source_listing_id"], item["group_id"])):
                        observations.append((item, task_id, run.id))
        for effect in kb.list_external_effects(conn, task_id):
            origin = kb.get_run(conn, effect["run_id"]) if effect.get("run_id") in accepted_runs else None
            # Native effect APIs require the current active run and stamp its ID;
            # completed runs cannot receive late writes under the same identity.
            reviewed_effect = bool(origin and origin.ended_at and effect["updated_at"] <= origin.ended_at)
            match = re.fullmatch(r"group:([1-9][0-9]*)", effect["effect_key"])
            if effect["platform"] != "facebook" or not match:
                continue
            details = effect.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            if isinstance(details.get("readback"), dict):
                details = {**details["readback"], **details}
            group_id = match[1]
            origin_contract = (origin.metadata or {}).get("loop_contract") or {} if origin else {}
            origin_scope = origin_contract.get("facebook_group_publish") or {}
            destination = next(
                (
                    item
                    for item in origin_scope.get("destinations", [])
                    if isinstance(item, dict) and item.get("group_id") == group_id
                ),
                None,
            ) if isinstance(origin_scope, dict) else None
            authorized_destination = (
                isinstance(origin_scope, dict) and origin_scope.get("mode") == "listing_bound_chooser"
                and origin_scope.get("source_listing_id") == spec["source_listing_id"]
                and isinstance(destination, dict)
                and str(effect.get("external_id") or "") == group_id
                and str(details.get("canonical_name") or "").strip()
                == str(destination.get("canonical_name") or "").strip()
                and str(details.get("canonical_url") or "").rstrip("/")
                == str(destination.get("canonical_url") or "").rstrip("/")
            )
            reviewed_effect = reviewed_effect and authorized_destination
            row = rows.setdefault(group_id, {"group_id": group_id, "effects": [], "publication_state": "not_submitted"})
            row["effects"].append({"task_id": task_id, "state": effect["state"], "details": details})
            if effect["state"] in {"absent_verified", "not_joined_verified"}:
                continue
            action = str(details.get("action") or "").lower()
            membership = (details.get("readback") or {}).get("membership_button") if isinstance(details.get("readback"), dict) else None
            if "join" in action or effect["state"] in {"joined", "join_started", "needs_questions"} or (not action and membership in {"Joined", "Pending"}):
                row["membership_evidence"] = "historical_join_effect; refresh before use"
                continue
            row["native_effect"] = row.get("native_effect", False) or task_id in native_ids
            if action == "publish_existing_listing" and str(details.get("source_listing_id")) == spec["source_listing_id"]:
                post_url = str(details.get("post_url") or "")
                name = str(details.get("canonical_name") or "").strip()
                row["canonical_name"] = name or row.get("canonical_name", "")
                row["native_submission"] = row.get("native_submission", False) or task_id in native_ids
                row["accepted_native_submission"] = row.get("accepted_native_submission", False) or reviewed_effect
                if reviewed_effect and post_url:
                    previous_post_url = str(row.get("submission_post_url") or "")
                    if (
                        previous_post_url
                        and previous_post_url.rstrip("/") != post_url.rstrip("/")
                    ):
                        row["submission_identity_ambiguous"] = True
                    else:
                        row["submission_post_url"] = post_url
                row["submitted_at"] = max(row.get("submitted_at", 0), effect["created_at"])
                row["effect_updated_at"] = max(row.get("effect_updated_at", 0), effect["updated_at"])
                old_names = {n.casefold() for n in spec.get("excluded_destination_names", [])}
                readback = details.get("post_submit_or_pending_readback")
                visible_readback = _visible_publication(readback, group_id, spec["source_listing_id"], post_url)
                if visible_readback:
                    # Effects may be recorded at completion, after the browser
                    # readback. The native run window is the durable chronology
                    # boundary; an older observation cannot prove this attempt.
                    visible_readback = bool(origin and origin.started_at <= readback["observed_at"] <= origin.ended_at)
                # Imported legacy pairs preserve context and replay protection only.
                # Their effects never satisfy this objective's publication count.
                confirmed = (
                    reviewed_effect and effect["state"] == "verified"
                    and details.get("publication_state") == "published"
                    and visible_readback and name
                    and name.casefold() not in old_names
                )
                state = "published" if confirmed else "submitted_unresolved"
            else:
                state = "reconciliation_required"
            if row["publication_state"] != "published":
                row["publication_state"] = state
    # Read-only verification is an observation, not a new external write. Fold
    # exact-run accepted receipts over existing native submissions without
    # rewriting the historical effect ledger or manufacturing another effect.
    for observation, task_id, run_id in observations:
        group_id = observation.get("group_id")
        row = rows.get(group_id) if isinstance(group_id, str) else None
        observed = observation.get("observed_at")
        if (not row or not row.get("native_submission")
                or observation.get("source_listing_id") != spec["source_listing_id"]
                or type(observed) not in (int, float)
                or not 0 < observed <= time.time()
                or observed < row.get("effect_updated_at", 0)
                or (observed, run_id) <= row.get("last_observation", (0, 0))):
            continue
        submission_post_url = str(row.get("submission_post_url") or "")
        observed_post_url = str(observation.get("post_url") or "")
        if (
            row.get("submission_identity_ambiguous")
            or not submission_post_url
            or observed_post_url.rstrip("/") != submission_post_url.rstrip("/")
        ):
            continue
        visible = _visible_publication(
            observation,
            group_id,
            spec["source_listing_id"],
            submission_post_url,
        )
        rejected = (observation.get("status") == "rejected" and observation.get("visible") is False
                    and observation.get("pending_review") is False)
        if not visible and not rejected:
            continue
        row["last_observation"] = (observed, run_id)
        row["reconciliation_source"] = {"task_id": task_id, "run_id": run_id, "readback": observation}
        old_names = {n.casefold() for n in spec.get("excluded_destination_names", [])}
        row["publication_state"] = ("published" if visible and row.get("accepted_native_submission") and row.get("canonical_name")
                                    and row["canonical_name"].casefold() not in old_names else
                                    "rejected" if rejected else "submitted_unresolved")
    excluded = set(spec.get("excluded_destination_ids", []))
    published = sum(r["publication_state"] == "published" and gid not in excluded for gid, r in rows.items())
    unresolved = sum((r["publication_state"] in {"submitted_unresolved", "reconciliation_required"}
                      and r.get("native_effect", False))
                     or (r["publication_state"] == "published" and gid in excluded and r.get("native_effect"))
                     for gid, r in rows.items())
    # The target is visible publications, not lifetime attempts. A confirmed
    # rejection releases exactly its occupied slot; any unused slots remain
    # available. The rejected group itself stays excluded from replay.
    return {
        "source_listing_id": spec["source_listing_id"], "expected_destinations": spec["expected_destinations"],
        "published_count": published, "publication_gap": max(0, spec["expected_destinations"] - published),
        "unresolved_submission_count": unresolved,
        "submission_capacity": max(0, spec["expected_destinations"] - published - unresolved),
        "complete": published >= spec["expected_destinations"], "destinations": list(rows.values()),
        "historical_evidence": spec.get("historical_evidence", []),
        "excluded_destination_ids": sorted(excluded),
        "excluded_destination_names": spec.get("excluded_destination_names", []),
        "continuation_rule": "Retain Join and submission evidence. Reconcile submitted/unknown destinations before retry; candidates and membership are not publications. Refresh exact listing-bound preflight before approval. For read-only reconciliation return acceptanceEvidence.publication_reconciliation rows with group_id, source_listing_id, status (published or rejected), visible, pending_review, post_url, and observed_at epoch seconds. Only a fresh exact-run accepted review can update progress; never manufacture another external effect for a read-only observation.",
    }


def resolve_preflight(conn, contract):
    """Resolve an accepted preflight into the only writable UI route it proved."""
    from .review import grace_review_accepted

    publish = dict(contract.get("facebook_group_publish") or {})
    if publish.get("mode") != "accepted_preflight":
        return publish
    source = publish.get("preflight_source")
    if not isinstance(source, dict):
        raise ValueError(
            "accepted_preflight group publishing requires preflight_source"
        )
    execution_id = str(source.get("execution_task_id") or "").strip()
    review_id = str(source.get("review_task_id") or "").strip()
    if not all(re.fullmatch(r"t_[0-9a-f]+", item) for item in (execution_id, review_id)):
        raise ValueError("Group preflight source task IDs are invalid")

    identity = contract.get("identity") or {}
    link = conn.execute(
        "SELECT d.platform, d.chat_id, d.thread_id FROM grace_delegations d "
        "JOIN task_links l ON l.parent_id=d.execution_task_id "
        "AND l.child_id=d.review_task_id "
        "WHERE d.execution_task_id=? AND d.review_task_id=?",
        (execution_id, review_id),
    ).fetchone()
    if link is None or any(
        (key != "thread_id" and not identity.get(key)) or str(link[key] or "") != str(identity.get(key) or "")
        for key in ("platform", "chat_id", "thread_id")
    ):
        raise ValueError(
            "Accepted group preflight must belong to this exact Topic and review link"
        )
    execution = kb.get_task(conn, execution_id)
    review = kb.get_task(conn, review_id)
    execution_run = kb.latest_run(conn, execution_id)
    review_run = kb.latest_run(conn, review_id)
    if any(effect["state"] not in {"absent_verified", "not_joined_verified"}
           for effect in kb.list_external_effects(conn, execution_id)):
        raise ValueError("Selected preflight has external effects; a zero-effect preflight is required")
    if (
        execution is None or review is None
        or execution.status != "done" or review.status != "done"
        or execution_run is None or review_run is None
        or execution_run.outcome != "completed" or review_run.outcome != "completed"
        or not grace_review_accepted(review_run.metadata)
    ):
        raise ValueError(
            "Selected group preflight has no completed Grace review of its latest execution"
        )
    review_metadata = review_run.metadata or {}
    review_evidence = review_metadata.get("evidence") or {}
    review_source = review_metadata.get("workflow_review_source")
    if not (
        isinstance(review_evidence, dict)
        and review_evidence.get("parent_execution_task_id") == execution_id
        and type(review_evidence.get("parent_execution_run_id")) is int
        and review_evidence["parent_execution_run_id"] > 0
        and review_evidence.get("parent_execution_run_id") == execution_run.id
    ):
        raise ValueError(
            "Accepted group preflight review is not bound to the exact execution run"
        )
    reviewed_hash = review_evidence.get("parent_execution_evidence_sha256")
    if reviewed_hash != kb.workflow_review_evidence_hash(execution_run):
        raise ValueError("Accepted preflight evidence is unpinned or changed after review")
    if not isinstance(review_source, dict) or any(
        review_source.get(key) != review_evidence.get(key)
        for key in (
            "parent_execution_task_id", "parent_execution_run_id",
            "parent_execution_evidence_sha256",
        )
    ):
        raise ValueError(
            "Accepted group preflight requires controller-pinned workflow review source"
        )
    source_contract = (execution_run.metadata or {}).get("loop_contract") or {}
    from .preflight import requested as preflight_requested

    try:
        source_requested_preflight = preflight_requested(source_contract)
    except ValueError as exc:
        raise ValueError(
            "Accepted preflight must originate from the requested Marketplace "
            "read-only route"
        ) from exc
    if not source_requested_preflight:
        raise ValueError(
            "Accepted preflight source did not request the Facebook group preflight schema"
        )
    if not _readonly_contract_scope(source_contract, str(publish.get("source_listing_id") or "")):
        raise ValueError("Accepted preflight must originate from the resolved Marketplace read-only route")
    source_identity = source_contract.get(
        "identity"
    ) or {}
    if not identity.get("project") or source_identity.get("project") != identity["project"]:
        raise ValueError("Accepted group preflight belongs to another project")
    current_objective = contract.get("objective_ref") or {}
    source_objective = ((execution_run.metadata or {}).get("loop_contract") or {}).get(
        "objective_ref"
    ) or {}
    if (
        not current_objective.get("objective_id")
        or source_objective.get("objective_id") != current_objective["objective_id"]
    ):
        raise ValueError(
            "Accepted group preflight must belong to the same durable objective"
        )

    evidence = (execution_run.metadata or {}).get("acceptance_evidence") or {}
    listing = evidence.get("sourceListing")
    coverage = evidence.get("coverageReconciliation")
    groups = evidence.get("groups")
    if not (
        isinstance(listing, dict)
        and isinstance(coverage, dict)
        and isinstance(groups, list)
        and evidence.get("sideEffectsPerformed") is False
        and listing.get("list_in_more_places_available") is True
    ):
        raise ValueError(
            "Accepted group preflight did not prove a zero-effect List in more places route"
        )
    listing_id = str(publish.get("source_listing_id") or "").strip()
    from .preflight import validate as validate_preflight

    rows = validate_preflight(evidence, started_at=execution_run.started_at,
                              ended_at=execution_run.ended_at, listing_id=listing_id)
    eligible = set(coverage["verified_eligible_for_later_approval"])
    destinations = publish.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        raise ValueError("Group publication requires at least one destination")
    destination_ids: list[str] = []
    destination_identity: list[dict[str, str]] = []
    for destination in destinations:
        group_id = str(destination.get("group_id") or "").strip()
        row = rows.get(group_id)
        if not (
            group_id in eligible
            and _readonly_contract_scope(
                source_contract, listing_id, group_id,
                str(destination.get("canonical_name") or ""),
            )
            and isinstance(row, dict)
            and row.get("chooser_presence") == "present"
            and row.get("chooser_selectability") == "selectable_unchecked"
            and str(row.get("chooser_group_id") or "").strip() == group_id
            and str(row.get("chooser_canonical_url") or "").rstrip("/")
            == str(destination.get("canonical_url") or "").rstrip("/")
            and str(row.get("canonical_url") or "").rstrip("/")
            == str(destination.get("canonical_url") or "").rstrip("/")
            and str(row.get("readable_name") or "").strip()
            == str(destination.get("canonical_name") or "").strip()
        ):
            raise ValueError(
                f"Destination {group_id or '(missing)'} was not an exact selectable row "
                "in the accepted preflight"
            )
        destination_ids.append(group_id)
        destination_identity.append({
            "group_id": group_id,
            "requested_alias": str(destination.get("canonical_name") or "").strip(),
            "canonical_name": str(destination.get("canonical_name") or "").strip(),
            "canonical_url": str(destination.get("canonical_url") or "").rstrip("/"),
        })
    if len(set(destination_ids)) != len(destination_ids):
        raise ValueError("Group publication destinations must be unique")

    pinned = {
        "execution_task_id": execution_id,
        "execution_run_id": execution_run.id,
        "review_task_id": review_id,
        "review_run_id": review_run.id,
        "list_in_more_places_available": True,
        "side_effects_performed": False,
        "observed_at": listing.get("observed_at"),
        "eligible_destination_ids": destination_ids,
        "destination_identity": destination_identity,
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    publish["mode"] = "listing_bound_chooser"
    publish["preflight_evidence"] = pinned
    return publish



def validate_publication(conn, contract):
    """Reject stale/repeated publication before approval or task admission."""
    publish = contract.get("facebook_group_publish")
    if not publish:
        return
    objective_id = (contract.get("objective_ref") or {}).get("objective_id")
    spec = configuration(conn, objective_id)
    if spec is None:
        raise ValueError("Facebook group publication requires a planned objective workflow; use objective_workflow plan")
    if (publish.get("source_listing_id") != spec["source_listing_id"]
            or (contract.get("identity") or {}).get("project") != spec["project"]):
        raise ValueError("Publication does not match objective listing/project")
    unresolved_effects = False
    reconciliation_by_task = {}
    for row in conn.execute(
        "SELECT r.task_id,r.metadata FROM grace_delegations d "
        "JOIN task_runs r ON r.task_id=d.execution_task_id "
        "WHERE d.objective_id=? AND d.execution_task_id IS NOT NULL "
        "ORDER BY r.id DESC",
        (objective_id,),
    ):
        task_id = row["task_id"]
        if task_id in reconciliation_by_task:
            continue
        metadata = json.loads(row["metadata"] or "{}")
        flag = metadata.get("external_effect_reconciliation_required")
        if isinstance(flag, bool):
            reconciliation_by_task[task_id] = flag
            unresolved_effects = unresolved_effects or flag
    if unresolved_effects:
        raise ValueError(
            "Objective has an execution run requiring external-effect reconciliation; "
            "reconcile it before publishing again"
        )
    if publish.get("mode") != "listing_bound_chooser":
        raise ValueError("Existing-listing publication requires accepted_preflight; canonical group entry may create a duplicate listing")
    forbidden = " ".join((contract.get("scope") or {}).get("forbidden", [])).lower()
    if any(term in forbidden for term in ("list in more places", "listing_bound_chooser", "chooser")):
        raise ValueError("Publish contract forbids its accepted preflight route; correct the contract before approval")
    selected = {d["group_id"] for d in publish["destinations"]}
    if selected & set(spec.get("excluded_destination_ids", [])):
        raise ValueError("Publication includes historical/excluded destinations instead of new groups")
    names = {n.strip().casefold() for n in spec.get("excluded_destination_names", [])}
    if any(d["canonical_name"].strip().casefold() in names for d in publish["destinations"]):
        raise ValueError("Publication includes a historical destination name instead of a new group")
    current = progress(conn, objective_id)
    occupied = {r["group_id"] for r in current["destinations"] if r["publication_state"] != "not_submitted"}
    if selected & occupied:
        raise ValueError("Destinations already submitted or uncertain; reconcile their effects instead of republishing")
    if len(selected) > current["submission_capacity"]:
        raise ValueError("Publication exceeds remaining submission capacity; reconcile pending/unknown submissions first")
    resolved = resolve_preflight(conn, {
        **contract, "facebook_group_publish": {**publish, "mode": "accepted_preflight"},
    })
    if resolved != publish:
        raise ValueError("Accepted preflight evidence changed since approval; inspect before retrying")


def inspect(conn, objective_id):
    objective = kb.get_grace_objective(conn, objective_id)
    if objective is None:
        raise ValueError("Unknown Grace objective")
    return {"objective": objective, "stages": [dict(r) for r in conn.execute(
        "SELECT * FROM grace_objective_stages WHERE objective_id=? ORDER BY position", (objective_id,),
    )], "publication_progress": progress(conn, objective_id)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "plan", "request-plan"))
    parser.add_argument("value", help="Objective ID or JSON plan path")
    parser.add_argument("--board", default="default")
    args = parser.parse_args()
    with kb.connect_closing(board=args.board) as conn:
        if args.command == "show":
            result = inspect(conn, args.value)
        else:
            payload = json.loads(Path(args.value).read_text(encoding="utf-8"))
            if any(str(key).startswith("_") for key in payload):
                raise ValueError("Plan contains an unsupported private field")
            worker_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
            objective_id = str(payload.get("objective_id") or "").strip()
            active_objective_tasks = [
                str(row["execution_task_id"])
                for row in conn.execute(
                    "SELECT DISTINCT s.execution_task_id FROM grace_objective_stages s "
                    "JOIN task_runs r ON r.task_id=s.execution_task_id "
                    "WHERE s.objective_id=? AND s.execution_task_id IS NOT NULL "
                    "AND r.ended_at IS NULL",
                    (objective_id,),
                )
            ]
            if len(active_objective_tasks) > 1:
                raise ValueError("Objective has multiple active execution tasks; reconcile before planning")
            active_objective_task = active_objective_tasks[0] if active_objective_tasks else ""
            if worker_task_id and active_objective_task and worker_task_id != active_objective_task:
                raise ValueError("Active worker marker does not match the objective execution task")
            if active_objective_task and not worker_task_id:
                raise ValueError(
                    "Objective has an active execution task; direct planning is unavailable "
                    "and a worker-authenticated request requires HERMES_KANBAN_TASK"
                )
            if args.command == "request-plan" and not worker_task_id:
                raise ValueError(
                    "CLI request-plan requires an authenticated active worker marker"
                )
            if args.command == "request-plan" or worker_task_id:
                if worker_task_id:
                    supplied = str(payload.get("origin_execution_task_id") or "").strip()
                    if supplied and supplied != worker_task_id:
                        raise ValueError("Deferred plan origin does not match the active worker task")
                    payload["origin_execution_task_id"] = worker_task_id
                result = request_plan(conn, **payload)
            else:
                result = plan(conn, **payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
