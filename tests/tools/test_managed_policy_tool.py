from __future__ import annotations

import hashlib
import json
import sqlite3

from hermes_state import SessionDB
from proactive.policy_registry import bind_topic_policies, create_policy_version
import tools.managed_policy_tool as managed_policy_tool
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
            "pinned_version_verified": True,
            "latest_active_verified": True,
        }
    ]
    assert result["execution_policy_receipts"] == [
        {**result["review_policy_receipts"][0], "role": "execution"}
    ]


def test_managed_policy_read_accepts_task_pinned_absent_topic_binding(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb
    from proactive.policy_registry import policy_snapshot_marker, resolve_contract_policies

    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    namespace = "telegram:-1003938559457:2120/kj_profile"
    normalized = resolve_contract_policies(
        {"memory": {"namespace": namespace}, "policy_requirements": []}
    )
    marker = policy_snapshot_marker(normalized)
    assert marker is not None

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Grace review with no Topic policy binding",
            body=f"GRACE_LOOP_CONTRACT_STAGE: grace_review\n{marker}\n",
            assignee="default",
        )
    finally:
        conn.close()
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("empty-binding-review-session", "cli")
    finally:
        db.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    result = json.loads(
        managed_policy_read(session_id="empty-binding-review-session")
    )

    assert result["success"] is True
    assert result["binding"] == normalized["policy_binding_snapshot"]
    assert result["policies"] == []
    assert result["review_policy_receipts"] == []


def test_managed_policy_read_returns_ai_bizweek_asset_guidance_for_review_task(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb
    from proactive.policy_registry import (
        policy_snapshot_marker,
        resolve_contract_policies,
    )

    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    create_policy_version(
        "ai-bizweek-audio-brief-cover",
        "v1",
        "# Audio Brief\n\nComplete cover policy.\n",
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    create_policy_version(
        "ai-bizweek-page-hero",
        "v1",
        "# Page Hero\n\nComplete hero policy.\n",
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    namespace = "telegram:chat:4641/project"
    bind_topic_policies(
        namespace,
        [
            {"policy_id": "ai-bizweek-audio-brief-cover", "resolution": "latest_active"},
            {"policy_id": "ai-bizweek-page-hero", "resolution": "latest_active"},
        ],
    )
    marker = policy_snapshot_marker(
        resolve_contract_policies(
            {
                "memory": {"namespace": namespace},
                "policy_requirements": [],
            }
        )
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Grace review",
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
                f"{marker}\n"
                "Review the AI BizWeek Audio Brief 1:1 podcast cover.\n"
            ),
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

    guidance = result["asset_policy_guidance"]
    assert guidance["requested_asset_families"] == ["audio_brief"]
    assert "page_hero" not in guidance["asset_families"]
    audio = guidance["asset_families"]["audio_brief"]
    assert audio["output_spec"]["aspect_ratio"] == "1:1"
    assert "deterministic HTML/Canvas/Pillow overlay" in audio["production_rule"]


def test_managed_policy_read_returns_carter_operational_readiness(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    content = "# Carter Page Hero\n\nComplete Carter policy content.\n"
    page_hero_sha = hashlib.sha256(content.encode()).hexdigest()
    monkeypatch.setattr(
        managed_policy_tool,
        "_CARTER_PAGE_HERO_SHA256",
        page_hero_sha,
    )
    create_policy_version(
        "ai-bizweek-page-hero",
        managed_policy_tool._CARTER_PAGE_HERO_VERSION,
        content,
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    namespace = "telegram:-1003938559457:4641/topic-project"
    bind_topic_policies(
        namespace,
        [{"policy_id": "ai-bizweek-page-hero", "resolution": "latest_active"}],
    )
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(
            "session-carter",
            "telegram",
            chat_id="-1003938559457",
            chat_type="group",
            thread_id="4641",
        )
    finally:
        db.close()

    monkeypatch.setattr(
        managed_policy_tool,
        "_ai_bizweek_known_readiness_tasks",
        lambda: {
            "tasks": {
                "page_hero_policy_replacement": {
                    "task_id": "t_8b14cafd",
                    "status": "done",
                },
                "runtime_probe_repair": {
                    "task_id": "t_70bf2afe",
                    "status": "done",
                    "latest_run": {
                        "manual_probe_ok": True,
                        "plugin_discovery_attempted": False,
                        "external_actions_performed": False,
                    },
                },
            },
            "obsolete_tasks": [
                {
                    "task_id": "t_7987e610",
                    "status": "blocked",
                    "note": "obsolete stale policy snapshot",
                }
            ],
        },
    )

    result = json.loads(managed_policy_read(session_id="session-carter"))

    readiness = result["operational_readiness_evidence"]
    assert readiness["complete"] is True
    assert readiness["status"] == "ready_for_fresh_package_task"
    assert readiness["tasks"]["runtime_probe_repair"]["task_id"] == "t_70bf2afe"
    assert readiness["worker_probe"] == {
        "verified": True,
        "manual_probe_ok": True,
        "plugin_discovery_attempted": False,
    }
    assert "Do not ask KJ" in readiness["canonical_note"]


def test_managed_policy_read_returns_carter_page_source_material(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    content = "# Carter Page Hero\n\nComplete Carter policy content.\n"
    page_hero_sha = hashlib.sha256(content.encode()).hexdigest()
    monkeypatch.setattr(
        managed_policy_tool,
        "_CARTER_PAGE_HERO_SHA256",
        page_hero_sha,
    )
    create_policy_version(
        "ai-bizweek-page-hero",
        managed_policy_tool._CARTER_PAGE_HERO_VERSION,
        content,
        owner_scope="brand",
        owner_id="AI BizWeek",
        activate=True,
    )
    namespace = "telegram:-1003938559457:4641/topic-project"
    bind_topic_policies(
        namespace,
        [{"policy_id": "ai-bizweek-page-hero", "resolution": "latest_active"}],
    )
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(
            "session-carter",
            "telegram",
            chat_id="-1003938559457",
            chat_type="group",
            thread_id="4641",
        )
    finally:
        db.close()

    raw_source = (
        "[KJ HSU] Carter Page body paragraph.\n\n"
        "Page → Group 導流。\n\n"
        "請提供完整發布包，並嚴格遵守instructions中的所有要求。"
    )
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            """
            INSERT INTO messages (id, session_id, role, content, timestamp)
            VALUES (?, ?, 'user', ?, 123.0)
            """,
            (
                managed_policy_tool._CARTER_SOURCE_MESSAGE_ID,
                managed_policy_tool._CARTER_SOURCE_SESSION_ID,
                raw_source,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = json.loads(managed_policy_read(session_id="session-carter"))

    source = result["content_source_evidence"]
    assert source["available"] is True
    assert source["task_bound"] is False
    assert source["message_id"] == managed_policy_tool._CARTER_SOURCE_MESSAGE_ID
    assert source["facebook_page_source_text"] == (
        "Carter Page body paragraph.\n\nPage → Group 導流。"
    )
    assert source["facebook_page_source_sha256"] == hashlib.sha256(
        source["facebook_page_source_text"].encode()
    ).hexdigest()
    assert "do not ask KJ to repost it" in source["canonical_note"]

    from plugins.openclaw_bridge.clawops_delegate import _augment_ai_bizweek_source_evidence

    contract = {
        "original_request": (
            "Use the stored Carter EP04 Page copy for AI BizWeek. "
            "Ignore this obsolete source block:\n"
            "BEGIN_FACEBOOK_PAGE_SOURCE_TEXT\nold unrelated source\n"
            "END_FACEBOOK_PAGE_SOURCE_TEXT"
        ),
        "scope": {"allowed": [source["selection_scope_entry"]]},
    }
    bound = _augment_ai_bizweek_source_evidence(contract, session_id="session-carter")
    assert source["facebook_page_source_text"] in bound["original_request"]
    assert f"source=session:{source['session_id']}" in bound["original_request"]
    contract["scope"]["allowed"] = [source["selection_scope_entry"] + "-wrong-hash"]
    assert _augment_ai_bizweek_source_evidence(contract, session_id="session-carter") == contract


def test_worker_profile_session_reads_only_its_task_policy(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from proactive.policy_registry import policy_snapshot_marker, resolve_contract_policies

    home = tmp_path / "hermes"
    profile = home / "profiles" / "worker"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    db.create_session("worker-session", "telegram", chat_id="wrong", thread_id="wrong")
    db.close()
    conn = kb.connect()
    task_ids = []
    try:
        for index in (1, 2):
            policy_id = f"case-{index}"
            create_policy_version(policy_id, "v1", f"Policy for case {index}", owner_scope="brand", owner_id="test", activate=True)
            namespace = f"telegram:chat:{index}/project"
            bind_topic_policies(namespace, [{"policy_id": policy_id, "resolution": "latest_active"}])
            normalized = resolve_contract_policies({"memory": {"namespace": namespace}, "policy_requirements": []})
            task_ids.append(kb.create_task(conn, title=f"Case {index}", body=policy_snapshot_marker(normalized), assignee="worker"))
    finally:
        conn.close()
    db = SessionDB(db_path=profile / "state.db")
    db.create_session("worker-session", "cli")
    db.close()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    for index, task_id in enumerate(task_ids, 1):
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        result = json.loads(managed_policy_read(session_id="worker-session"))
        assert result["success"] is True
        assert result["scope"] == {"kind": "kanban_task", "task_id": task_id}
        assert [p["policy_id"] for p in result["policies"]] == [f"case-{index}"]
        assert result["policies"][0]["content"] == f"Policy for case {index}"
    # A root-only session cannot authorize this profile's worker context.
    db = SessionDB(db_path=home / "state.db")
    db.create_session("root-only", "cli")
    db.close()
    assert json.loads(managed_policy_read(session_id="root-only"))["success"] is False


def test_task_policy_receipts_satisfy_behavior_review_validator(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from proactive.behavior_profiles import registry as br
    from proactive.grace_task_compiler import render_review_body
    from scripts.replay_behavior_observation import load_cases, make_contract
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    case = load_cases()["cases"][0]
    contract = make_contract(load_cases(), case)
    contract["objective_ref"]["objective_id"] = "go_receipt"
    identity = contract["identity"]
    create_policy_version("receipt-policy", "v1", "Full pinned policy", owner_scope="topic", owner_id="test", activate=True)
    namespace = f"telegram:{identity['chat_id']}:{identity['thread_id']}/{identity['project']}"
    contract["memory"]["namespace"] = namespace
    bind_topic_policies(namespace, [{"policy_id": "receipt-policy", "resolution": "latest_active"}])
    with kb.connect_closing() as conn:
        br.set_selection(conn, platform="telegram", chat_id=identity["chat_id"], thread_id=identity["thread_id"], project=identity["project"], profile_id="ai_bizweek", version="v8", expected_revision=0, reason="Receipt integration fixture")
        kb.create_grace_objective(conn, objective_id="go_receipt", platform="telegram", chat_id=identity["chat_id"], thread_id=identity["thread_id"], session_key="fixture", title="fixture", objective="fixture", original_request_sha256="a"*64, required_stage_keys=["prepare", "publish"], terminal_stage_key="publish", acceptance_criteria=["verified"], behavior_project=identity["project"])
        bound = br.bind_contract(conn, contract)
        body = render_review_body(bound, "t_parent")
        task_id = kb.create_task(conn, title="review", body=body)
    db = SessionDB(db_path=home / "state.db")
    db.create_session("receipt-session", "cli")
    db.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    result = json.loads(managed_policy_read(session_id="receipt-session"))
    assert result["success"]
    assert br.validate_task_policy_completion(body, {"policy_receipts": result["review_policy_receipts"]}, "review") is True
