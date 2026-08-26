from __future__ import annotations

import hashlib
import json

from hermes_cli import kanban_db as kb
from proactive.loop_contract import contract_fingerprint
from tools import browser_camofox
from tools import browser_supervisor
from tools import browser_tool


def _execution_task(conn, body="GRACE_LOOP_CONTRACT_STAGE: execution"):
    task_id = kb.create_task(
        conn,
        title="protected external draft",
        body=body,
        assignee="clawops-browser",
    )
    run = kb.claim_task(conn, task_id)
    assert run is not None
    return task_id, run


def test_external_effect_ledger_keeps_multiple_objects_per_platform(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 1703088130054399",'
                '"Facebook Group 207110076321670"]}\n```'
            ),
        )
        for group_id in ("1703088130054399", "207110076321670"):
            kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                effect_key=f"group:{group_id}",
                state="not_joined_verified",
                external_id=group_id,
                expected_run_id=run.current_run_id,
            )
        effects = kb.list_external_effects(conn, task_id)

    assert [effect["effect_key"] for effect in effects] == [
        "group:1703088130054399",
        "group:207110076321670",
    ]


def test_external_effect_ledger_migrates_legacy_platform_key(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        conn.execute("DROP TABLE task_external_effects")
        conn.execute(
            """
            CREATE TABLE task_external_effects (
                task_id TEXT NOT NULL, platform TEXT NOT NULL,
                state TEXT NOT NULL, external_id TEXT, details TEXT,
                run_id INTEGER, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (task_id, platform)
            )
            """
        )
        conn.execute(
            "INSERT INTO task_external_effects VALUES "
            "('t_old', 'facebook', 'verified', '123', NULL, 1, 1, 1)"
        )
        conn.commit()

    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT effect_key, external_id FROM task_external_effects "
            "WHERE task_id = 't_old'"
        ).fetchone()
        pk_columns = [
            item["name"]
            for item in conn.execute(
                "PRAGMA table_info(task_external_effects)"
            ).fetchall()
            if item["pk"]
        ]

    assert dict(row) == {"effect_key": "create", "external_id": "123"}
    assert pk_columns == ["task_id", "platform", "effect_key"]

    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO task_external_effects VALUES "
            "('t_old', 'facebook', 'group:456', 'joined', '456', "
            "NULL, 2, 2, 2)"
        )
        conn.commit()
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        keys = [
            row["effect_key"]
            for row in conn.execute(
                "SELECT effect_key FROM task_external_effects "
                "WHERE task_id = 't_old' ORDER BY effect_key"
            )
        ]
    assert keys == ["create", "group:456"]


def test_browser_navigate_rejects_duplicate_external_create(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            state="verified",
            external_id="draft-123",
            expected_run_id=run.current_run_id,
        )

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))

    result = json.loads(
        browser_tool.browser_navigate(
            "https://www.facebook.com/marketplace/create/item",
        )
    )

    assert result["success"] is False
    assert "already verified" in result["error"]
    assert "draft-123" in result["error"]


def test_browser_navigate_reserves_first_create_for_current_run(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (
            "https://seller.shopee.tw/portal/product/new",
            "https://seller.shopee.tw/portal/product/new|123.0",
            None,
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "url": "https://seller.shopee.tw/portal/product/new",
            "title": "Shopee",
            "snapshot": "",
        },
    )

    first = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )
    second = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )

    assert first["success"] is True
    assert second["success"] is False
    assert "already create_started" in second["error"]
    with kb.connect_closing(db_path) as conn:
        effects = kb.list_external_effects(conn, task_id)
    assert effects[0]["state"] == "create_started"
    assert effects[0]["run_id"] == run.current_run_id


def test_camofox_mutation_is_guarded_before_backend_call(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (
            "https://m.facebook.com/marketplace/create/item",
            "https://m.facebook.com/marketplace/create/item|456.0",
            None,
        ),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "no active task-scoped reservation" in result["error"]
    assert "exact page load" in result["error"]
    assert called is False


def test_browser_mutation_fails_closed_without_page_identity(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": False,
            "error": "eval unavailable",
        }),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "failed closed" in result["error"]
    assert called is False


def test_protected_ref_action_is_blocked_even_with_bound_reservation(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    protected_url = "https://m.facebook.com/marketplace/create/item"
    page_identity = f"{protected_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        assert kb.reserve_external_create(
            conn,
            task_id,
            protected_url,
            expected_run_id=run.current_run_id,
        ) is None
        assert kb.bind_external_create_page(
            conn,
            task_id,
            protected_url,
            page_identity=page_identity,
            expected_run_id=run.current_run_id,
        ) is None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (protected_url, page_identity, None),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "cannot validate the exact page load atomically" in result["error"]
    assert called is False


def test_grace_ref_click_uses_exact_snapshot_bound_dom_node(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399"
    page_identity = f"{live_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 1703088130054399"]}\n```'
            ),
        )

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    actions = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            actions.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda task_id: FakeSupervisor(),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is True
    assert actions == [{
        "backend_node_id": 2468,
        "expected_page_identity": page_identity,
        "action": "click",
        "text": None,
        "expected_role": "button",
        "expected_name": "Join group",
        "required_group_id": "1703088130054399",
        "captured_session_id": "captured-session",
        "require_group_composer": False,
    }]
    with kb.connect_closing(db_path) as conn:
        effect = kb.list_external_effects(conn, task_id)[0]
    assert effect["effect_key"] == "group:1703088130054399"
    assert effect["state"] == "join_started"


def test_browser_publish_contract_allows_only_group_post_composer_open(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399/"
    page_identity = f"{live_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Write something...",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    actions = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            actions.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is True
    assert actions[0]["required_group_id"] == "1703088130054399"
    with kb.connect_closing(db_path) as conn:
        assert kb.list_external_effects(conn, task_id) == []
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e2": {
                    "role": "button",
                    "name": "Post",
                    "backend_node_id": 9753,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )

    submitted = json.loads(
        browser_tool.browser_click("@e2", task_id="browser-1")
    )

    assert submitted["success"] is True
    assert actions[1]["require_group_composer"] is True
    with kb.connect_closing(db_path) as conn:
        effects = kb.list_external_effects(conn, task_id)
    assert effects[0]["effect_key"] == "group:1703088130054399"
    assert effects[0]["state"] == "create_started"


def test_browser_publish_contract_rejects_group_comment_fill(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399/"
    page_identity = f"{live_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "textbox",
                    "name": "Comment as Craig",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("comment must fail before CDP action")
        ),
    )

    result = json.loads(
        browser_tool.browser_type("@e1", "not allowed", task_id="browser-1")
    )

    assert result["success"] is False
    assert "whitelisted browser_publish" in result["error"]


def test_grace_ref_click_rejects_group_outside_contract(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399"
    page_identity = f"{live_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 207110076321670"]}\n```'
            ),
        )

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("out-of-contract group must fail before CDP action")
        ),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "not listed in this exact Loop Contract" in result["error"]


def test_grace_ref_click_rejects_nested_group_outside_contract(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = (
        "https://www.facebook.com/groups/1703088130054399/"
        "permalink/999"
    )
    page_identity = f"{live_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                'Facebook Group 207110076321670'
            ),
        )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "not listed in this exact Loop Contract" in result["error"]


def test_grace_ref_click_rejects_custom_slug_group_route(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/example.slug"
    page_identity = f"{live_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 1703088130054399"]}\n```'
            ),
        )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "cannot be mapped" in result["error"]


def test_grace_ref_click_rejects_join_button_on_discovery_page(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/search/groups?q=appliances"
    page_identity = f"{live_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 1703088130054399"]}\n```'
            ),
        )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "neither an authorized numeric group" in result["error"]


def test_body_note_does_not_expand_structured_group_targets():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "Note: Facebook Group 999999 must not be authorized.\n"
        '```json\n{"external_targets": ['
        '"Facebook Group 1703088130054399"]}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset({
        "1703088130054399",
    })


def test_lowercase_group_targets_and_browser_publish_authority_are_accepted():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset({
        "1703088130054399",
    })
    assert kb.grace_allows_facebook_group_posting(body) is True


def test_compiler_display_targets_require_consumed_challenge(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    token = "challenge-token"
    user_hash = "a" * 64
    original_request = "publish to the two approved groups"
    contract = {
        "approval_provenance": {
            "source": "one_time_authenticated_owner_challenge",
            "scope_binding": "exact_loop_contract_fingerprint",
            "internal": False,
            "platform": "telegram",
            "requested_message_id": "2391",
            "approved_message_id": "2395",
            "user_id_sha256": user_hash,
            "challenge_token_sha256": hashlib.sha256(
                token.encode("utf-8")
            ).hexdigest(),
        },
        "external_targets": [
            "Facebook Group 897927458651235「大台灣二手家具家電交流*"
            "免費贈送＆民眾/店家買賣」"
            "https://www.facebook.com/groups/897927458651235/",
            "Facebook Group 1466446866915040「二手｜液晶電視 中古 家電"
            " 買賣交流 社團」"
            "https://www.facebook.com/groups/1466446866915040/",
        ],
        "routing": {"task_type": "browser_publish"},
    }
    fingerprint = contract_fingerprint({
        **contract,
        "original_request": original_request,
    })
    contract["approval_provenance"]["contract_fingerprint"] = fingerprint
    contract["audit"] = {
        "original_request_location": (
            "Grace session history only; not disclosed to ClawOps"
        ),
        "original_request_sha256": hashlib.sha256(
            original_request.encode("utf-8")
        ).hexdigest(),
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        f"```json\n{json.dumps(contract, ensure_ascii=False)}\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, _ = _execution_task(conn, body=body)
        conn.execute(
            """
            INSERT INTO grace_approval_challenges (
                token, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                user_id_sha256, requested_message_id, action_summary,
                approval_platform, approval_scope, delegation_args,
                state, created_at, expires_at, consumed_at,
                approved_message_id
            ) VALUES (?, ?, 'request-1', 'telegram', 'chat-1', '2',
                      'session-key', 'session-id', ?, '2391', 'publish',
                      'Facebook', '[]', ?, 'consumed', 1, 2, 2, '2395')
            """,
            (
                token,
                fingerprint,
                user_hash,
                json.dumps({"original_request": original_request}),
            ),
        )
        conn.execute(
            """
            INSERT INTO grace_delegations (
                delegation_id, contract_fingerprint, request_instance_id,
                challenge_token, platform, chat_id, thread_id, session_key,
                session_id, user_id_sha256, approved_message_id,
                resolved_route, approval_required, state, execution_task_id,
                review_task_id, created_at, updated_at
            ) VALUES ('gd-1', ?, 'request-1', ?, 'telegram', 'chat-1', '2',
                      'session-key', 'session-id', ?, '2395',
                      '{"task_type":"browser_publish"}', 1, 'queued', ?,
                      'review-1', 1, 1)
            """,
            (fingerprint, token, user_hash, task_id),
        )
        conn.commit()
        assert kb.grace_external_group_ids(body) == frozenset({
            "897927458651235",
            "1466446866915040",
        })
        assert kb.grace_allows_facebook_group_posting(body) is False
        assert kb.grace_task_allows_facebook_group_posting(
            conn,
            task_id,
        ) is True


def test_compiler_display_group_target_rejects_mismatched_url_id():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":['
        '"Facebook Group 897927458651235「大台灣二手家具家電交流」'
        'https://www.facebook.com/groups/1466446866915040/"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset()
    assert kb.grace_allows_facebook_group_posting(body) is False


def test_compiler_display_group_target_rejects_unstructured_trailing_text():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        '"Facebook Group 897927458651235 untrusted note"]}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset()


def test_group_posting_rejects_incomplete_challenge_proof():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"approval_provenance":{'
        '"source":"one_time_authenticated_owner_challenge",'
        '"scope_binding":"exact_loop_contract_fingerprint",'
        '"internal":false,"platform":"telegram",'
        '"requested_message_id":"2391","approved_message_id":"2395",'
        f'"user_id_sha256":"{"a" * 64}",'
        f'"challenge_token_sha256":"{"b" * 64}"'
        '},"external_targets":["Facebook Group 897927458651235"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset({
        "897927458651235",
    })
    assert kb.grace_allows_facebook_group_posting(body) is False


def test_group_post_reservation_is_one_shot(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        first = kb.reserve_external_group_post(
            conn,
            task_id,
            "1703088130054399",
            expected_run_id=run.current_run_id,
        )
        second = kb.reserve_external_group_post(
            conn,
            task_id,
            "1703088130054399",
            expected_run_id=run.current_run_id,
        )
        effects = kb.list_external_effects(conn, task_id)

    assert first is None
    assert "already create_started" in second
    assert effects[0]["state"] == "create_started"


def test_group_post_retry_only_accepts_proven_guarded_ref_failure(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            effect_key="group:1703088130054399",
            state="failed",
            details={
                "posting_status": "not_created_guarded_ref_unavailable",
            },
            expected_run_id=run.current_run_id,
        )
        retry = kb.reserve_external_group_post(
            conn,
            task_id,
            "1703088130054399",
            expected_run_id=run.current_run_id,
        )
        effect = kb.list_external_effects(conn, task_id)[0]

    assert retry is None
    assert effect["state"] == "create_started"
    assert effect["details"]["prior_state"] == "failed"
    assert (
        effect["details"]["prior_posting_status"]
        == "not_created_guarded_ref_unavailable"
    )


def test_group_post_retry_rejects_failed_listing_route(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"authorization":{"human_approved":true},'
        '"external_targets":["facebook group 1703088130054399"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            effect_key="group:1703088130054399",
            state="failed",
            details={"posting_status": "not_created_listing_route_forbidden"},
            expected_run_id=run.current_run_id,
        )
        retry = kb.reserve_external_group_post(
            conn,
            task_id,
            "1703088130054399",
            expected_run_id=run.current_run_id,
        )
        effect = kb.list_external_effects(conn, task_id)[0]

    assert "durable state is already failed" in retry
    assert effect["state"] == "failed"


def test_extra_json_fence_makes_group_authority_ambiguous():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets": ["Facebook Group 999"]}\n```\n'
        '```json\n{"external_targets": ['
        '"Facebook Group 1703088130054399"]}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset()


def test_non_facebook_target_does_not_discard_group_scope():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets": ['
        '"Facebook Group 1703088130054399",'
        '"Shopee Product 43833004526"]}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset({
        "1703088130054399",
    })


def test_snapshot_ref_is_consumed_when_claimed(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    identity = "https://example.com|100"
    browser_tool._snapshot_ref_contexts["browser-once"] = {
        "page_identity": identity,
        "kanban_task_id": task_id,
        "kanban_run_id": run.current_run_id,
        "refs": {
            "e1": {
                "role": "button",
                "name": "Submit",
                "backend_node_id": 5,
            }
        },
    }

    first, first_error = browser_tool._snapshot_ref_metadata(
        "browser-once",
        "@e1",
        identity,
    )
    second, second_error = browser_tool._snapshot_ref_metadata(
        "browser-once",
        "@e1",
        identity,
    )

    assert first["backend_node_id"] == 5
    assert first_error is None
    assert second is None
    assert "fresh browser_snapshot" in second_error


def test_external_group_effect_rejects_noncanonical_or_unscoped_key(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n"
                '```json\n{"external_targets": ['
                '"Facebook Group 1703088130054399"]}\n```'
            ),
        )
        for effect_key in ("GROUP:1703088130054399", "group:999"):
            try:
                kb.record_external_effect(
                    conn,
                    task_id,
                    platform="facebook",
                    effect_key=effect_key,
                    state="joined",
                    external_id=effect_key.rsplit(":", 1)[-1],
                    expected_run_id=run.current_run_id,
                )
            except ValueError:
                pass
            else:
                raise AssertionError(f"{effect_key} should be rejected")


def test_grace_press_fails_closed_without_trusted_atomic_backend(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, f"{live_url}|789.0", None),
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("Grace key action must fail before backend call")
        ),
    )

    result = json.loads(browser_tool.browser_press("Enter", task_id="browser-1"))

    assert result["success"] is False
    assert "no trusted atomic key backend" in result["error"]


def test_snapshot_refs_require_stable_page_and_bind_duplicate_nodes(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    live_url = "https://example.com/form"
    identity = f"{live_url}|100.0"
    snapshot = {
        "data": {
            "url": live_url,
            "refs": {},
        }
    }
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: (
            [
                {"backend_node_id": 10, "role": "textbox", "name": "Answer"},
                {"backend_node_id": 11, "role": "textbox", "name": "Answer"},
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, identity, None),
    )

    browser_tool._remember_snapshot_refs("browser-1", snapshot, identity)

    context = browser_tool._snapshot_ref_contexts["browser-1"]
    assert context["refs"]["e1"]["backend_node_id"] == 10
    assert context["refs"]["e2"]["backend_node_id"] == 11
    assert snapshot["data"]["guarded_refs_available"] is True
    assert snapshot["data"]["snapshot"].startswith(
        'Guarded interactive controls:\n- textbox "Answer" [ref=e1]'
    )

    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, f"{live_url}|200.0", None),
    )
    browser_tool._remember_snapshot_refs("browser-1", snapshot, identity)
    assert "browser-1" not in browser_tool._snapshot_ref_contexts
    assert snapshot["data"]["refs"] == {}
    assert snapshot["data"]["guarded_refs_available"] is False
    assert "page identity changed" in snapshot["data"]["guarded_ref_error"]


def test_guarded_snapshot_scrubs_native_refs_when_ax_capture_fails(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    live_url = "https://www.facebook.com/marketplace/item/915975414881937"
    identity = f"{live_url}|100.0"
    snapshot = {
        "data": {
            "url": live_url,
            "refs": {"e29": {"role": "button", "name": "Share"}},
            "snapshot": '- button "Share" [ref=e29]',
        }
    }
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: ([], "CDP supervisor is unavailable for this browser session"),
    )
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, identity, None),
    )

    error = browser_tool._remember_snapshot_refs(
        "browser-marketplace",
        snapshot,
        identity,
    )

    assert error == "CDP supervisor is unavailable for this browser session"
    assert snapshot["data"]["refs"] == {}
    assert snapshot["data"]["guarded_refs_available"] is False
    assert snapshot["data"]["guarded_ref_error"] == error
    assert "[ref=e29]" not in snapshot["data"]["snapshot"]
    assert "Do not attempt browser_click" in snapshot["data"]["snapshot"]
    assert "browser-marketplace" not in browser_tool._snapshot_ref_contexts


def test_guarded_snapshot_reports_url_mismatch_without_exposing_refs(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    snapshot_url = "https://www.facebook.com/marketplace/item/915975414881937"
    live_url = f"{snapshot_url}?ref=share_attachment"
    identity = f"{live_url}|100.0"
    snapshot = {
        "data": {
            "url": snapshot_url,
            "refs": {"e29": {"role": "button", "name": "Share"}},
            "snapshot": '- button "Share" [ref=e29]',
        }
    }
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: (
            _ for _ in ()
        ).throw(
            AssertionError(
                "URL mismatch must fail before rebinding the supervisor"
            )
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, identity, None),
    )

    error = browser_tool._remember_snapshot_refs(
        "browser-marketplace",
        snapshot,
        identity,
    )

    assert error == "snapshot URL did not match the live page URL"
    assert snapshot["data"]["refs"] == {}
    assert "[ref=e29]" not in snapshot["data"]["snapshot"]


def test_guarded_snapshot_with_only_static_ax_nodes_returns_error(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
    live_url = "https://www.facebook.com/marketplace/item/915975414881937"
    identity = f"{live_url}|100.0"
    snapshot = {
        "data": {
            "url": live_url,
            "refs": {"e29": {"role": "button", "name": "Share"}},
            "snapshot": '- button "Share" [ref=e29]',
        }
    }
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: (
            [{"backend_node_id": 29, "role": "StaticText", "name": "Share"}],
            None,
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, identity, None),
    )

    error = browser_tool._remember_snapshot_refs(
        "browser-marketplace",
        snapshot,
        identity,
    )

    assert error == (
        "Accessibility tree contained no supported interactive controls"
    )
    assert snapshot["data"]["refs"] == {}
    assert snapshot["data"]["guarded_refs_available"] is False


def test_guarded_page_identity_uses_snapshot_session_not_supervisor(
    monkeypatch,
):
    live_url = "https://www.facebook.com/marketplace/you/selling"
    calls: list[tuple[str, str, list[str]]] = []

    def fake_run(task_id, command, args):
        calls.append((task_id, command, args))
        return {
            "success": True,
            "data": {
                "result": json.dumps({
                    "href": live_url,
                    "timeOrigin": 123.5,
                }),
            },
        }

    monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("identity must not use a possibly misbound supervisor")
        ),
    )

    url, identity, error = browser_tool._browser_page_identity("browser-1")

    assert error is None
    assert url == live_url
    assert identity == f"{live_url}|123.5"
    assert calls[0][0:2] == ("browser-1", "eval")


def test_ordinary_kanban_snapshot_keeps_native_refs(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary browser task",
            body="not a Grace Loop execution contract",
            assignee="default",
        )
    snapshot = {
        "data": {
            "url": "https://example.com",
            "refs": {"e1": {"role": "button", "name": "Continue"}},
            "snapshot": '- button "Continue" [ref=e1]',
        }
    }
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("ordinary tasks must not require guarded AX capture")
        ),
    )

    error = browser_tool._remember_snapshot_refs(
        "browser-ordinary",
        snapshot,
        None,
    )

    assert error is None
    assert snapshot["data"]["refs"]["e1"]["name"] == "Continue"
    assert "[ref=e1]" in snapshot["data"]["snapshot"]


def test_grace_ref_click_rejects_stale_snapshot_before_eval(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    live_url = "https://www.facebook.com/groups/1703088130054399"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, f"{live_url}|200.0", None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-1",
        {
            "page_identity": f"{live_url}|100.0",
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "Join group",
                    "backend_node_id": 2468,
                }
            },
        },
    )
    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale snapshot must fail before CDP action")
        ),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "page load changed" in result["error"]


def test_stale_worker_is_blocked_on_non_create_page(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?",
            (task_id,),
        )
        conn.commit()

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (
            "https://www.facebook.com/marketplace/you/selling",
            "https://www.facebook.com/marketplace/you/selling|999.0",
            None,
        ),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "not the active worker run" in result["error"]
    assert called is False


def test_required_create_binding_rejects_live_redirect_away(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_redirected")
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda _task_id: (
            "https://seller.shopee.tw/portal/product/list/all",
            "https://seller.shopee.tw/portal/product/list/all|1000",
            None,
        ),
    )

    error = browser_tool._bind_external_create_page(
        "browser-1",
        require_protected=True,
    )

    assert error is not None
    assert "no longer a protected create route" in error


def test_non_grace_kanban_task_keeps_ordinary_ref_actions(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary browser task",
            body="Inspect a local dashboard",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("ordinary tasks must not require page identity"),
        ),
    )
    monkeypatch.setattr(
        browser_camofox,
        "camofox_click",
        lambda ref, task_id=None: json.dumps({
            "success": True,
            "clicked": ref,
        }),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is True


def test_non_grace_navigation_skips_create_binding_probe(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary browser navigation",
            body="Inspect a route",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("ordinary navigation must not bind a page"),
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "data": {
                "url": "https://seller.shopee.tw/portal/product/new",
                "title": "Route",
                "snapshot": "",
            },
        },
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )

    assert result["success"] is True
