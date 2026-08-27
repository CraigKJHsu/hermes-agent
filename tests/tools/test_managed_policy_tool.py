from __future__ import annotations

import hashlib
import json

from hermes_state import SessionDB
from proactive.policy_registry import bind_topic_policies, create_policy_version
from tools.managed_policy_tool import managed_policy_read


def test_managed_policy_read_uses_trusted_session_topic(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    content = "# Audio Brief\n\nComplete formal policy content.\n"
    create_policy_version(
        "audio-brief-policy",
        "v1",
        content,
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    bind_topic_policies(
        "telegram:-1003938559457:4641/topic-project",
        [{"policy_id": "audio-brief-policy", "resolution": "latest_active"}],
    )
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(
            "session-1",
            "telegram",
            chat_id="-1003938559457",
            chat_type="group",
            thread_id="4641",
        )
    finally:
        db.close()

    result = json.loads(managed_policy_read(session_id="session-1"))

    assert result["success"] is True
    assert result["scope"]["thread_id"] == "4641"
    assert result["policies"][0]["content"] == content
    assert result["policies"][0]["sha256"] == hashlib.sha256(
        content.encode()
    ).hexdigest()


def test_managed_policy_read_rejects_missing_trusted_session():
    result = json.loads(managed_policy_read(session_id=None))
    assert result == {"success": False, "error": "trusted session_id is required"}


def test_managed_policy_read_uses_current_policy_pinned_kanban_task(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb
    from proactive.policy_registry import (
        policy_snapshot_marker,
        resolve_contract_policies,
    )

    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    content = "# Review policy\n\nPinned task instructions.\n"
    create_policy_version(
        "review-policy",
        "v1",
        content,
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    namespace = "telegram:chat:4641/project"
    bind_topic_policies(
        namespace,
        [{"policy_id": "review-policy", "resolution": "latest_active"}],
    )
    contract = {
        "memory": {"namespace": namespace},
        "policy_requirements": [],
    }
    normalized = resolve_contract_policies(contract)
    marker = policy_snapshot_marker(normalized)
    assert marker is not None
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Grace review",
            body=f"GRACE_LOOP_CONTRACT_STAGE: grace_review\n{marker}\n",
            assignee="default",
        )
    finally:
        conn.close()
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("review-session", "cli")
    finally:
        db.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    result = json.loads(managed_policy_read(session_id="review-session"))

    assert result["success"] is True
    assert result["scope"] == {"kind": "kanban_task", "task_id": task_id}
    assert result["policies"][0]["content"] == content
    assert result["review_policy_receipts"] == [
        {
            "role": "review",
            "policy_id": "review-policy",
            "version": "v1",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "loaded": True,
            "latest_active_verified": True,
        }
    ]
