#!/usr/bin/env python3
"""Install the tracked MissionCrew model-routing policy into Managed Policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proactive.policy_registry import create_policy_version, policy_status


POLICY_PATH = (
    REPO_ROOT
    / "config"
    / "managed-policies"
    / "missioncrew-model-routing-v1.json"
)


def _backfill_legacy_grace_reviews() -> int:
    """Bind pre-routing, nonterminal formal reviews to the active policy."""
    from hermes_cli import kanban_db as kb
    from proactive.model_routing import route_grace

    route = route_grace("acceptance_review", {"task_risk": "critical"})
    updated = 0
    with kb.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, routing_decision
              FROM tasks
             WHERE executor_profile = 'grace-policy-review'
               AND status NOT IN ('done', 'cancelled')
            """
        ).fetchall()
        for row in rows:
            try:
                decision = json.loads(row["routing_decision"] or "{}")
            except (TypeError, json.JSONDecodeError):
                decision = {}
            if not isinstance(decision, dict):
                decision = {}
            if isinstance(decision.get("model_route"), dict):
                continue
            decision["selected_backend"] = "hermes"
            decision["model_route"] = route
            conn.execute(
                """
                UPDATE tasks
                   SET routing_decision = ?, model_override = ?
                 WHERE id = ?
                """,
                (
                    json.dumps(decision, ensure_ascii=False, sort_keys=True),
                    route["requested_model"],
                    row["id"],
                ),
            )
            updated += 1
        conn.commit()
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--activate",
        action="store_true",
        help="atomically activate v1 after immutable write/readback verification",
    )
    args = parser.parse_args()

    content = POLICY_PATH.read_text(encoding="utf-8")
    policy = json.loads(content)
    policy_id = str(policy["policy_id"])
    version = str(policy["version"])
    expected_active = None
    try:
        expected_active = policy_status(policy_id).get("active_version")
    except (FileNotFoundError, ValueError):
        pass
    result = create_policy_version(
        policy_id,
        version,
        content,
        owner_scope="global",
        owner_id="missioncrew",
        activate=args.activate,
        expected_active_version=expected_active,
    )
    if args.activate:
        result["legacy_grace_reviews_backfilled"] = _backfill_legacy_grace_reviews()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
