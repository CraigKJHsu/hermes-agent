"""Stdin/stdout broker for the OpenClaw Facebook Page capability plugin."""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from tools.facebook_page_graph_tool import (
    OpenClawCapabilityScope,
    execute_openclaw_facebook_page_capability,
)


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def main() -> int:
    try:
        payload = _record(json.load(sys.stdin))
        scope_data = _record(payload.get("scope"))
        scope = OpenClawCapabilityScope(
            task_id=str(scope_data.get("task_id") or "").strip(),
            run_id=int(scope_data.get("run_id") or 0),
            delegation_id=str(scope_data.get("delegation_id") or "").strip(),
            contract_fingerprint=str(
                scope_data.get("contract_fingerprint") or ""
            ).strip(),
            approval_grant_id=str(
                scope_data.get("approval_grant_id") or ""
            ).strip(),
            backend_agent_id=str(scope_data.get("backend_agent_id") or "").strip(),
            board=str(scope_data.get("board") or "").strip(),
            task_type=str(scope_data.get("task_type") or "").strip(),
        )
        result = execute_openclaw_facebook_page_capability(
            str(payload.get("operation") or "").strip(),
            _record(payload.get("args")),
            scope,
        )
    except Exception as exc:
        result = json.dumps(
            {
                "success": False,
                "published": False,
                "error": f"Capability broker rejected request: {type(exc).__name__}",
            },
            ensure_ascii=False,
        )
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
