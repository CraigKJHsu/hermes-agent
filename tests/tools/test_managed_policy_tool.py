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
