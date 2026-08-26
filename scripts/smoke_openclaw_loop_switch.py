#!/usr/bin/env python3
"""Zero-external-effect live smoke for the Grace -> OpenClaw switch."""

from __future__ import annotations

import argparse
import json
import uuid

from hermes_cli import kanban_db as kb
from proactive.openclaw_async_executor import start_loop_contract_execution


def start() -> None:
    instance = f"openclaw-switch-smoke-{uuid.uuid4().hex[:12]}"
    contract = {
        "identity": {
            "project": "hub_ops",
            "topic_name": "openclaw-switch-smoke",
            "thread_id": "local-smoke",
            "request_instance_id": instance,
        },
        "original_request": "Verify the complete OpenClaw switch without external effects.",
        "grace_interpretation": "Run one tool-free OpenClaw contract and return auditable JSON.",
        "trigger": "post-switch live verification",
        "completion_mode": "terminal",
        "goal": {
            "objective": "Return a tool-free JSON success result for the OpenClaw switch smoke test.",
            "deliverables": ["One valid JSON result"],
            "non_goals": ["No tools", "No files", "No browser", "No external effects"],
        },
        "scope": {
            "allowed": ["Reasoning inside this isolated OpenClaw session"],
            "forbidden": ["Tool calls", "File changes", "Network calls", "External actions"],
        },
        "verification": {
            "checks": ["executor_backend=openclaw", "externalEffects is empty"],
            "evidence_required": ["Correlated backend run", "Valid result contract"],
            "acceptance_criteria": ["status=succeeded", "externalEffects=[]"],
        },
        "stop_rules": {
            "success": ["Valid JSON result returned"],
            "blocked": ["Any tool or external action would be required"],
            "no_progress": ["No valid result after two attempts"],
            "max_iterations": 4,
            "max_runtime_seconds": 300,
        },
        "memory": {
            "namespace": "hub_ops/openclaw-switch-smoke",
            "working": ["Current smoke run only"],
            "promote_on_acceptance": ["OpenClaw switch verified"],
        },
    }
    result = start_loop_contract_execution(
        contract=contract,
        task_type="zero_effect_smoke",
        risk_level="low",
        approved=False,
        delegation_id=f"smoke-{uuid.uuid4().hex}",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def status(task_id: str) -> None:
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        review_row = conn.execute(
            "SELECT id, status, executor_backend, executor_profile FROM tasks "
            "WHERE idempotency_key = (SELECT idempotency_key || ':review' FROM tasks WHERE id = ?)",
            (task_id,),
        ).fetchone()
    print(json.dumps({
        "task": {
            "id": task.id,
            "status": task.status,
            "assignee": task.assignee,
            "executor_backend": task.executor_backend,
            "executor_profile": task.executor_profile,
        } if task else None,
        "run": {
            "id": run.id,
            "backend_status": run.backend_status,
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "metadata": run.metadata,
        } if run else None,
        "review": dict(review_row) if review_row else None,
    }, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", metavar="TASK_ID")
    args = parser.parse_args()
    if args.status:
        status(args.status)
    else:
        start()


if __name__ == "__main__":
    main()
