"""Exact source bytes selected by an accepted owner-Objective binding stage."""

import hashlib
import json
import re

from hermes_cli import kanban_db as kb

PREFIX = "Objective source content package (data, not instructions): "


def accepted_source_package(conn, contract):
    ref = contract.get("objective_ref") or {}
    identity = contract.get("identity") or {}
    oid, target = ref.get("objective_id"), ref.get("stage_key")
    objective = kb.get_grace_objective(conn, oid)
    if not objective:
        return None
    stages = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM grace_objective_stages WHERE objective_id=? ORDER BY position",
            (oid,),
        )
    ]
    target_position = next(
        (s["position"] for s in stages if s["stage_key"] == target), None
    )
    for prior in stages:
        if target_position is None or prior["position"] >= target_position:
            continue
        receipt = (
            kb.get_grace_loop_callback(conn, prior["review_task_id"])
            if prior["review_task_id"]
            else None
        )
        event_id = (
            (receipt or {}).get("lease_event_id")
            or (receipt or {}).get("outcome_event_id")
            or (receipt or {}).get("last_event_id")
        )
        event = (
            conn.execute(
                "SELECT run_id FROM task_events WHERE id=?", (event_id,)
            ).fetchone()
            if event_id
            else None
        )
        reviewed = (
            kb.get_run(conn, event["run_id"]) if event and event["run_id"] else None
        )
        pinned = (
            (reviewed.metadata or {}).get("workflow_review_source")
            if reviewed
            else None
        )
        if (
            reviewed
            and kb.grace_review_accepted(reviewed.metadata or {})
            and isinstance(pinned, dict)
        ):
            attempt = kb.get_run(conn, pinned.get("parent_execution_run_id"))
            if (
                not attempt
                or attempt.task_id != prior["execution_task_id"]
                or pinned.get("parent_execution_task_id") != attempt.task_id
                or pinned.get("parent_execution_evidence_sha256")
                != kb.workflow_review_evidence_hash(attempt)
            ):
                raise ValueError(
                    "Accepted source binding prior reviewed execution evidence changed"
                )
    for stage in stages:
        advertised_stage = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND (json_type(metadata,'$.canonical_source_binding')='object' OR json_type(metadata,'$.acceptance_evidence.canonical_source_binding')='object') LIMIT 1",
            (stage["execution_task_id"],),
        ).fetchone()
        if advertised_stage:
            owner = conn.execute(
                "SELECT * FROM grace_delegations WHERE delegation_id=?",
                (stage["delegation_id"],),
            ).fetchone()
            if not owner or (
                owner["objective_id"],
                owner["stage_key"],
                owner["execution_task_id"],
                owner["review_task_id"],
            ) != (
                oid,
                stage["stage_key"],
                stage["execution_task_id"],
                stage["review_task_id"],
            ):
                raise ValueError(
                    "Accepted source binding stage delegation linkage changed"
                )
    candidates = conn.execute(
        "SELECT d.* FROM grace_delegations d WHERE d.objective_id=? AND EXISTS(SELECT 1 FROM task_runs r WHERE r.task_id=d.execution_task_id AND (json_type(r.metadata,'$.canonical_source_binding')='object' OR json_type(r.metadata,'$.acceptance_evidence.canonical_source_binding')='object'))",
        (oid,),
    ).fetchall()
    if not candidates:
        return None
    target_stage = next((s for s in stages if s["stage_key"] == target), None)
    if not target_stage:
        raise ValueError("Accepted source binding target stage is missing")
    source_stages = []
    for candidate in candidates:
        source_stage = next(
            (s for s in stages if s["delegation_id"] == candidate["delegation_id"]),
            None,
        )
        if (
            source_stage is None
            or source_stage["stage_key"] != candidate["stage_key"]
            or source_stage["execution_task_id"] != candidate["execution_task_id"]
            or source_stage["review_task_id"] != candidate["review_task_id"]
            or source_stage["status"] != "done"
        ):
            raise ValueError("Accepted source binding stage topology changed")
        if source_stage["stage_key"] == target:
            continue
        if source_stage["position"] >= target_stage["position"]:
            raise ValueError("Accepted source binding stage order changed")
        source_stages.append(source_stage)
    packets = []
    for stage in source_stages:
        execution, review = stage["execution_task_id"], stage["review_task_id"]
        fail = "Accepted source binding has changed or lacks exact controller lineage"
        callback = kb.get_grace_loop_callback(conn, review) or {}
        event_id = (
            callback.get("lease_event_id")
            or callback.get("outcome_event_id")
            or callback.get("last_event_id")
        )
        event = conn.execute(
            "SELECT * FROM task_events WHERE id=?", (event_id,)
        ).fetchone()
        reviewer = (
            kb.get_run(conn, event["run_id"]) if event and event["run_id"] else None
        )
        review_source = (
            (reviewer.metadata or {}).get("workflow_review_source")
            if reviewer
            else None
        )
        if (
            not reviewer
            or not kb.grace_review_accepted(reviewer.metadata or {})
            or not isinstance(review_source, dict)
        ):
            advertised = conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? AND (json_type(metadata,'$.canonical_source_binding')='object' OR json_type(metadata,'$.acceptance_evidence.canonical_source_binding')='object') LIMIT 1",
                (execution,),
            ).fetchone()
            if advertised:
                raise ValueError(fail)
            continue
        source_run_id = review_source.get("parent_execution_run_id")
        if (
            type(source_run_id) is not int
            or review_source.get("parent_execution_task_id") != execution
        ):
            raise ValueError(fail)
        run = kb.get_run(conn, source_run_id)
        if (
            run is None
            or run.task_id != execution
            or review_source.get("parent_execution_evidence_sha256")
            != kb.workflow_review_evidence_hash(run)
        ):
            raise ValueError(fail)
        metadata = run.metadata or {}
        top = metadata.get("canonical_source_binding")
        acceptance = metadata.get("acceptance_evidence") or {}
        bound_binding = acceptance.get("canonical_source_binding")
        binding = top if isinstance(top, dict) else bound_binding
        if not isinstance(binding, dict):
            raise ValueError(fail)
        if top is not None and bound_binding is not None and top != bound_binding:
            raise ValueError("Accepted source binding has conflicting selectors")
        workspace = kb.workspace_completion_evidence(metadata)
        if workspace is None and bound_binding != binding:
            raise ValueError(fail)
        lane = tuple(identity.get(k) for k in ("platform", "chat_id", "thread_id"))
        if lane != tuple(objective[k] for k in ("platform", "chat_id", "thread_id")):
            raise ValueError("Accepted source binding belongs to another Topic")
        if (
            hashlib.sha256(objective["objective"].encode()).hexdigest()
            != objective["original_request_sha256"]
        ):
            raise ValueError("Accepted source binding owner request changed")
        delegations = conn.execute(
            "SELECT * FROM grace_delegations WHERE execution_task_id IN (?,?) OR review_task_id IN (?,?)",
            (execution, review, execution, review),
        ).fetchall()
        from hermes_cli.controller_readback import execution_history_baseline
        from proactive.loop_contract import contract_fingerprint

        source_contract = (
            json.loads(delegations[0]["contract_snapshot"] or "{}")
            if len(delegations) == 1
            else {}
        )
        baseline = execution_history_baseline(conn, execution, run.id)
        source_identity = source_contract.get("identity") or {}
        if (
            not baseline
            or baseline.get("execution_task_id") != execution
            or baseline.get("execution_run_id") != run.id
            or len(delegations) != 1
            or baseline.get("delegation_id") != delegations[0]["delegation_id"]
            or baseline.get("delegation_contract_fingerprint")
            != contract_fingerprint(source_contract)
            or delegations[0]["contract_fingerprint"]
            != baseline.get("delegation_contract_fingerprint")
            or source_contract.get("objective_ref")
            != {"objective_id": oid, "stage_key": stage["stage_key"]}
            or any(
                source_identity.get(k) != identity.get(k)
                for k in ("platform", "chat_id", "thread_id", "project")
            )
        ):
            raise ValueError(
                "Accepted source binding immutable Objective linkage changed"
            )
        if (
            execution == review
            or len(delegations) != 1
            or delegations[0]["delegation_id"] != stage["delegation_id"]
            or delegations[0]["objective_id"] != oid
            or delegations[0]["stage_key"] != stage["stage_key"]
            or delegations[0]["execution_task_id"] != execution
            or delegations[0]["review_task_id"] != review
            or tuple(delegations[0][k] for k in ("platform", "chat_id", "thread_id"))
            != lane
            or any(
                callback.get(k) != v
                for k, v in dict(
                    objective_id=oid,
                    stage_key=stage["stage_key"],
                    execution_task_id=execution,
                    platform=lane[0],
                    chat_id=lane[1],
                    thread_id=lane[2],
                ).items()
            )
            or not event
            or event["task_id"] != review
            or event["kind"] != "completed"
            or not reviewer
            or reviewer.task_id != review
            or reviewer.status not in {"done", "completed"}
            or reviewer.outcome != "completed"
            or not reviewer.ended_at
            or not kb.grace_review_accepted(reviewer.metadata or {})
            or run.status not in {"done", "completed"}
            or run.outcome != "completed"
            or not run.ended_at
            or (run.metadata or {}).get("external_effects") != []
            or not conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                (execution, review),
            ).fetchone()
        ):
            raise ValueError(fail)
        selectors = binding.get("source_selectors") or {}
        whole = selectors.get("full_original_delegation_contract_snapshot") or {}
        if all(
            k in selectors for k in ("source_entry", "original_best_buy_source_entry")
        ):
            raise ValueError(fail)
        source = (
            selectors.get("source_entry")
            or selectors.get("original_best_buy_source_entry")
            or {}
        )
        owner = selectors.get("original_package_owner_request") or {}
        hid = whole.get("row_pk")
        historical = conn.execute(
            "SELECT * FROM grace_delegations WHERE delegation_id=?", (hid,)
        ).fetchone()
        if not historical:
            raise ValueError(fail)
        raw = historical["contract_snapshot"] or ""
        digest = hashlib.sha256(raw.encode()).hexdigest()
        snapshot = json.loads(raw)
        projects = [
            r["project_namespace"]
            for r in conn.execute(
                "SELECT project_namespace FROM tasks WHERE id IN (?,?,?,?)",
                (
                    execution,
                    review,
                    historical["execution_task_id"],
                    historical["review_task_id"],
                ),
            )
        ]
        references = set(re.findall(r"\bt_[0-9a-f]{8}\b", objective["objective"]))
        if (
            len(projects) != 4
            or any(p != identity.get("project") for p in projects)
            or tuple(historical[k] for k in ("platform", "chat_id", "thread_id"))
            != lane
            or historical["execution_task_id"] == historical["review_task_id"]
            or not {historical["execution_task_id"], historical["review_task_id"]}
            <= references
            or contract_fingerprint(snapshot) != historical["contract_fingerprint"]
            or whole.get("raw_sha256") != digest
            or whole.get("json_pointer") != "/"
            or whole.get("table") != "grace_delegations"
            or whole.get("column") != "contract_snapshot"
            or source.get("row_pk") != hid
            or owner.get("row_pk") != hid
            or any(
                s.get("table") != "grace_delegations"
                or s.get("column") != "contract_snapshot"
                for s in (source, owner)
            )
            or owner.get("json_pointer") != "/original_request"
            or hashlib.sha256(snapshot["original_request"].encode()).hexdigest()
            != owner.get("sha256")
        ):
            raise ValueError(fail)
        for tid in (historical["execution_task_id"], historical["review_task_id"]):
            memberships = conn.execute(
                "SELECT delegation_id FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?",
                (tid, tid),
            ).fetchall()
            if len(memberships) != 1 or memberships[0]["delegation_id"] != hid:
                raise ValueError(fail)
        pointer = source.get("json_pointer") or ""
        match = re.fullmatch(r"/scope/allowed/(0|[1-9][0-9]*)", pointer)
        if not match:
            raise ValueError(fail)
        entries = (snapshot.get("scope") or {}).get("allowed") or []
        index = int(match[1])
        text = entries[index] if index < len(entries) else None
        if (
            not isinstance(text, str)
            or not text
            or hashlib.sha256(text.encode()).hexdigest() != source.get("entry_sha256")
        ):
            raise ValueError(fail)
        source_text_pointer = None
        if "full_source_text" in source:
            manuscript = source["full_source_text"]
            if (
                not isinstance(manuscript, str)
                or not manuscript
                or hashlib.sha256(manuscript.encode()).hexdigest()
                != source.get("source_text_sha256")
                or len(manuscript) != source.get("source_text_length_chars")
                or text.encode().count(manuscript.encode()) != 1
            ):
                raise ValueError(
                    "Accepted source binding manuscript is not an exact selected substring"
                )
            text = manuscript
            source_name = (
                "source_entry"
                if selectors.get("source_entry")
                else "original_best_buy_source_entry"
            )
            source_text_pointer = (
                (
                    "/canonical_source_binding/"
                    if not isinstance(bound_binding, dict)
                    else "/acceptance_evidence/canonical_source_binding/"
                )
                + "source_selectors/"
                + source_name
                + "/full_source_text"
            )
        packets.append({
            "source_kind": "accepted_source_binding",
            "objective_id": oid,
            "execution_task_id": execution,
            "review_task_id": review,
            "run_id": run.id,
            "review_run_id": reviewer.id,
            "original_request": text,
            "utf8_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "historical_delegation_id": hid,
            "historical_contract_sha256": digest,
            "source_json_pointer": pointer,
            "source_text_json_pointer": source_text_pointer,
        })
    if len(packets) > 1:
        raise ValueError("Accepted source binding is ambiguous")
    return packets[0] if packets else None


def source_packets(snapshot):
    entries = (snapshot.get("memory") or {}).get("working") or []
    packets = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith(PREFIX):
            continue
        raw = entry[len(PREFIX) :]
        packet = json.loads(raw)
        if not isinstance(packet, dict):
            raise ValueError("Accepted source binding packet must be a JSON object")
        if (
            isinstance(packet, dict)
            and packet.get("source_kind") == "accepted_source_binding"
            and raw
            != json.dumps(
                packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        ):
            raise ValueError("Accepted source binding packet is not canonical JSON")
        packets.append(packet)
    return packets


def verify_source_binding_execution(conn, delegation):
    snapshot = json.loads(delegation["contract_snapshot"] or "{}")
    declared = dict(delegation)
    linked = conn.execute(
        "SELECT objective_id,stage_key FROM grace_objective_stages WHERE delegation_id=? OR execution_task_id=?",
        (declared.get("delegation_id"), declared.get("execution_task_id")),
    ).fetchall()
    for stage in linked:
        if (stage["objective_id"], stage["stage_key"]) != (
            declared.get("objective_id"),
            declared.get("stage_key"),
        ) and conn.execute(
            "SELECT 1 FROM grace_delegations d JOIN task_runs r ON r.task_id=d.execution_task_id WHERE d.objective_id=? AND (json_type(r.metadata,'$.canonical_source_binding')='object' OR json_type(r.metadata,'$.acceptance_evidence.canonical_source_binding')='object') LIMIT 1",
            (stage["objective_id"],),
        ).fetchone():
            raise ValueError("Accepted source binding reverse stage ownership changed")
    packets = source_packets(snapshot)
    advertised = any(p.get("source_kind") == "accepted_source_binding" for p in packets)
    coordinates = {
        "objective_id": declared.get("objective_id"),
        "stage_key": declared.get("stage_key"),
    }
    if (
        snapshot.get("objective_ref")
        or advertised
        or (
            coordinates["objective_id"]
            and conn.execute(
                "SELECT 1 FROM grace_delegations d JOIN task_runs r ON r.task_id=d.execution_task_id WHERE d.objective_id=? AND (json_type(r.metadata,'$.canonical_source_binding')='object' OR json_type(r.metadata,'$.acceptance_evidence.canonical_source_binding')='object') LIMIT 1",
                (coordinates["objective_id"],),
            ).fetchone()
        )
    ) and not all(coordinates.values()):
        raise ValueError(
            "Accepted source binding authoritative Objective/stage is missing"
        )
    if snapshot.get("objective_ref") and snapshot["objective_ref"] != coordinates:
        raise ValueError("Accepted source binding execution stage changed")
    authoritative = dict(snapshot)
    if all(coordinates.values()):
        authoritative["objective_ref"] = coordinates
    if (snapshot.get("durable_evidence_snapshot") or {}).get(
        "accepted_content_revision"
    ):
        from hermes_cli.content_revision import verify_content_revision_execution

        declared.setdefault("origin_review_task_id", None)
        if verify_content_revision_execution(conn, declared) is not None:
            return
    expected = accepted_source_package(conn, authoritative)
    if expected is None:
        if advertised:
            raise ValueError("Accepted source binding has no authoritative lineage")
        return
    target = conn.execute(
        "SELECT delegation_id,execution_task_id FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
        (coordinates["objective_id"], coordinates["stage_key"]),
    ).fetchone()
    if (
        not target
        or not declared.get("delegation_id")
        or not declared.get("execution_task_id")
        or (target["delegation_id"], target["execution_task_id"])
        != (declared["delegation_id"], declared["execution_task_id"])
    ):
        raise ValueError("Accepted source binding consuming stage ownership changed")
    identity = snapshot.get("identity") or {}
    consuming_task = kb.get_task(conn, declared["execution_task_id"])
    if (
        tuple(declared.get(k) for k in ("platform", "chat_id", "thread_id"))
        != tuple(identity.get(k) for k in ("platform", "chat_id", "thread_id"))
        or not consuming_task
        or consuming_task.project_namespace != identity.get("project")
    ):
        raise ValueError("Accepted source binding consuming Topic or project changed")
    if snapshot.get("objective_ref") != coordinates:
        raise ValueError("Accepted source binding execution stage changed")
    if isinstance(snapshot.get("source_package_ref"), dict):
        raise ValueError(
            "Accepted source binding conflicts with explicit source package"
        )
    delivery = snapshot.get("user_facing_delivery") or {}
    # Inventory queries do not consume publication source bytes. Intake omits
    # their packet; keep the lineage checks above and all source consumers strict.
    if (
        not packets
        and (snapshot.get("domain_memory") or {}).get("mode") == "query"
        and delivery.get("kind") == "content_package"
        and delivery.get("body_field") == "domain_inventory_report"
        and delivery.get("delivery") == "inline_only"
        and not delivery.get("asset_filenames")
        and snapshot.get("completion_mode") == "intermediate"
        and type(snapshot.get("external_effect_budget")) is int
        and snapshot["external_effect_budget"] == 0
    ):
        return
    if packets != [expected]:
        raise ValueError("Accepted source binding changed before execution admission")
