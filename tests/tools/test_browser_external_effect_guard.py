from __future__ import annotations

import hashlib
import json
import secrets

import pytest

from hermes_cli import kanban_db as kb
from proactive.loop_contract import (
    canonical_marketplace_readonly_sections,
    contract_fingerprint,
)
from tools import browser_camofox
from tools import browser_supervisor
from tools import browser_tool
from tools import facebook_page_graph_tool as graph_tool


def test_durable_evidence_url_preserves_canonical_group_publication_routes():
    assert browser_tool._durable_evidence_url(
        "https://www.facebook.com/groups/1333742673375089/posts/987654321"
        "?__cft__[0]=secret#comments"
    ) == (
        "https://www.facebook.com/groups/1333742673375089/posts/987654321"
    )
    assert browser_tool._durable_evidence_url(
        "https://www.facebook.com/groups/1333742673375089/user/625346908/"
    ) == (
        "https://www.facebook.com/groups/1333742673375089/user/625346908"
    )


def _execution_task(
    conn,
    body="GRACE_LOOP_CONTRACT_STAGE: execution",
    *,
    bind_approval=True,
):
    approval_record = None
    contract = kb._grace_compiled_contract(body)
    if (
        bind_approval
        and isinstance(contract, dict)
        and contract.get("external_targets")
    ):
        contract = json.loads(json.dumps(contract))
        contract.pop("authorization", None)
        token = secrets.token_hex(8)
        request_id = "request-" + secrets.token_hex(8)
        user_hash = "a" * 64
        original_request = "test approved Facebook group action"
        fingerprint = contract_fingerprint({
            **contract,
            "original_request": original_request,
        })
        contract["approval_provenance"] = {
            "source": "one_time_authenticated_owner_challenge",
            "scope_binding": "exact_loop_contract_fingerprint",
            "internal": False,
            "platform": "telegram",
            "requested_message_id": "request-message",
            "approved_message_id": "approval-message",
            "user_id_sha256": user_hash,
            "challenge_token_sha256": hashlib.sha256(
                token.encode("utf-8")
            ).hexdigest(),
            "contract_fingerprint": fingerprint,
        }
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
        approval_record = {
            "token": token,
            "request_id": request_id,
            "user_hash": user_hash,
            "fingerprint": fingerprint,
            "original_request": original_request,
        }
    task_id = kb.create_task(
        conn,
        title="protected external draft",
        body=body,
        assignee="clawops-browser",
    )
    run = kb.claim_task(conn, task_id)
    assert run is not None
    if approval_record is not None:
        conn.execute(
            """
            INSERT INTO grace_approval_challenges (
                token, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                user_id_sha256, requested_message_id, action_summary,
                approval_platform, approval_scope, delegation_args,
                state, created_at, expires_at, consumed_at,
                approved_message_id
            ) VALUES (?, ?, ?, 'telegram', 'chat-1', '2',
                      'session-key', 'session-id', ?, 'request-message',
                      'test action', 'Facebook', '[]', ?, 'consumed',
                      1, 2, 2, 'approval-message')
            """,
            (
                approval_record["token"],
                approval_record["fingerprint"],
                approval_record["request_id"],
                approval_record["user_hash"],
                json.dumps({
                    "original_request": approval_record["original_request"],
                }),
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
            ) VALUES (?, ?, ?, ?, 'telegram', 'chat-1', '2',
                      'session-key', 'session-id', ?, 'approval-message',
                      '{"task_type":"browser_publish"}', 1, 'queued', ?,
                      'review-1', 1, 1)
            """,
            (
                "gd-" + approval_record["token"],
                approval_record["fingerprint"],
                approval_record["request_id"],
                approval_record["token"],
                approval_record["user_hash"],
                task_id,
            ),
        )
        conn.commit()
    return task_id, run


def test_successful_browser_readback_is_durable_before_completion(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "browser-evidence.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    conn = kb.connect(db_path)
    try:
        task_id, run = _execution_task(conn)
        assert run.current_run_id is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))

        browser_tool._record_guarded_browser_evidence(
            operation="snapshot",
            url=(
                "https://www.facebook.com/marketplace/you/selling/?view="
                + ("x" * 5_000)
                + "#access_token=fragment-secret"
            ),
            title="Marketplace Selling" + (" title" * 500),
            visible_text=(
                "Kolin KD-291M06 — In stock — "
                "Listed on Marketplace and at least 1 group "
                "Authorization: Bearer sk-projABC123xyz"
            ),
        )

        events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "browser_evidence_recorded"
        ]
        assert len(events) == 1
        assert events[0].run_id == run.current_run_id
        assert events[0].payload["operation"] == "snapshot"
        assert "visible_text" not in events[0].payload
        assert "title" not in events[0].payload
        assert len(events[0].payload["url"]) <= 2_000
        assert events[0].payload["url"] == (
            "https://www.facebook.com/marketplace/you/selling"
        )
        assert "fragment-secret" not in events[0].payload["url"]
        assert events[0].payload["observed_at"] > 0
        assert events[0].payload["visible_text_sha256"] == hashlib.sha256(
            browser_tool.redact_sensitive_text(
                "Kolin KD-291M06 — In stock — "
                "Listed on Marketplace and at least 1 group "
                "Authorization: Bearer sk-projABC123xyz",
                force=True,
            ).encode("utf-8"),
        ).hexdigest()
        assert events[0].payload["visible_text_length"] > 0
        context = kb.build_worker_context(conn, task_id)
        assert "## Durable browser evidence" in context
        assert "Listed on Marketplace and at least 1 group" not in context
    finally:
        conn.close()


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


def test_crosspost_reconciliation_only_satisfies_fresh_exact_public_effects(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    listing_id = "915975414881937"
    public_group = "902016640291333"
    pending_group = "1333742673375089"
    stale_group = "1703088130054399"
    wrong_listing_group = "123456789012345"
    group_ids = {
        public_group,
        pending_group,
        stale_group,
        wrong_listing_group,
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "external_targets": [
                f"Facebook Marketplace item {listing_id}",
                *[f"Facebook Group {group_id}" for group_id in group_ids],
            ],
            "facebook_crosspost": {
                "marketplace_listing_id": listing_id,
                "group_ids": sorted(group_ids),
            },
            "routing": {"task_type": "browser_publish"},
        })
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        common = {
            "group_name": "Exact approved group",
            "evidence": "Exact listing title is visible in the live group",
            "external_state_changed": False,
        }
        for group_id, details in {
            public_group: {
                **common,
                "listing_id": listing_id,
                "group_id": public_group,
                "posting_status": "public",
                "observed_at": 4_900,
            },
            pending_group: {
                **common,
                "listing_id": listing_id,
                "group_id": pending_group,
                "posting_status": "pending",
                "observed_at": 4_900,
            },
            stale_group: {
                **common,
                "listing_id": listing_id,
                "group_id": stale_group,
                "posting_status": "public",
                "observed_at": 4_000,
            },
            wrong_listing_group: {
                **common,
                "listing_id": "37276725125275496",
                "group_id": wrong_listing_group,
                "posting_status": "public",
                "observed_at": 4_900,
            },
        }.items():
            kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                effect_key=f"group:{group_id}",
                state="verified",
                external_id=group_id,
                details=details,
                expected_run_id=run.current_run_id,
            )

    assert browser_tool._fresh_verified_existing_crosspost_groups(
        task_id,
        run.current_run_id,
        listing_id,
        group_ids,
        now=5_000,
    ) == {public_group}


def test_crosspost_reservation_accepts_only_exact_reconciled_remainder(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db(db_path)
    listing_id = "915975414881937"
    public_group = "902016640291333"
    missing_group = "1333742673375089"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "external_targets": [
                f"Facebook Marketplace item {listing_id}",
                f"Facebook Group {public_group}",
                f"Facebook Group {missing_group}",
            ],
            "facebook_crosspost": {
                "marketplace_listing_id": listing_id,
                "group_ids": [public_group, missing_group],
            },
            "routing": {"task_type": "browser_publish"},
        })
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        now = 5_000
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            effect_key=f"group:{public_group}",
            state="verified",
            external_id=public_group,
            details={
                "listing_id": listing_id,
                "group_id": public_group,
                "group_name": "【大台北地區】二手家具、二手家電買賣",
                "posting_status": "public",
                "observed_at": now,
                "evidence": "Exact Kolin listing is visible in the group",
                "external_state_changed": False,
            },
            expected_run_id=run.current_run_id,
        )
        monkeypatch.setattr(kb.time, "time", lambda: now)

        assert "fresh same-run public reconciliation" in (
            kb.reserve_external_group_posts(
                conn,
                task_id,
                [public_group, missing_group],
                expected_run_id=run.current_run_id,
                allow_crosspost=True,
            ) or ""
        )
        assert kb.reserve_external_group_posts(
            conn,
            task_id,
            [missing_group],
            expected_run_id=run.current_run_id,
            allow_crosspost=True,
        ) is None
        effects = {
            effect["effect_key"]: effect
            for effect in kb.list_external_effects(conn, task_id)
        }

    assert effects[f"group:{public_group}"]["state"] == "verified"
    assert effects[f"group:{missing_group}"]["state"] == "create_started"


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
        '"routing":{"task_type":"facebook_marketplace_group_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
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
        '"routing":{"task_type":"facebook_marketplace_group_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
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


def test_marketplace_existing_listing_crosspost_uses_bound_state_machine(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    group_id = "1333742673375089"
    preselected_group_id = "1466446866915040"
    live_url = f"https://www.facebook.com/marketplace/item/{listing_id}"
    page_identity = f"{live_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        f'"Facebook Marketplace item {listing_id} → Facebook Group {group_id}",'
        f'"Facebook Marketplace item {listing_id} → '
        f'Facebook Group {preselected_group_id}"],'
        '"facebook_crosspost":{'
        '"transport":"browser",'
        f'"marketplace_listing_id":"{listing_id}",'
        f'"group_ids":["{group_id}","{preselected_group_id}"]}},'
        '"routing":{"task_type":"facebook_marketplace_group_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        assert kb.grace_task_facebook_group_permissions(
            conn,
            task_id,
        ) == (frozenset(), False)
        assert kb.grace_task_facebook_crosspost_permissions(
            conn,
            task_id,
        ) == (
            listing_id,
            frozenset({group_id, preselected_group_id}),
            True,
        )
        assert "must equal the exact approved destination set" in (
            kb.reserve_external_group_posts(
                conn,
                task_id,
                [group_id],
                expected_run_id=run.current_run_id,
                allow_crosspost=True,
            )
            or ""
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
    browser_tool._facebook_crosspost_contexts.pop("browser-crosspost", None)
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            if (
                kwargs.get("crosspost_stage") == "submit"
                and not any(
                    call.get("crosspost_stage") == "select_group"
                    for call in calls[:-1]
                )
            ):
                return {
                    "ok": False,
                    "error": "selected set did not match",
                }
            result = {
                "ok": True,
                "action": "click",
                "crosspost_for_sale_item_id": listing_id,
            }
            if kwargs.get("crosspost_stage") == "select_group":
                result["crosspost_group_id"] = group_id
                result["crosspost_preselected_group_ids"] = [
                    preselected_group_id,
                ]
            return {"ok": True, "result": result}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def click_ref(ref, role, name, *, checked=None):
        metadata = {
            "role": role,
            "name": name,
            "backend_node_id": len(calls) + 100,
            "captured_session_id": "captured-session",
        }
        if checked is not None:
            metadata["checked"] = checked
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-crosspost",
            {
                "page_identity": page_identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {ref.lstrip("@"): metadata},
            },
        )
        return json.loads(
            browser_tool.browser_click(ref, task_id="browser-crosspost")
        )

    blocked = click_ref("@e0", "button", "Share")
    assert blocked["success"] is False
    assert "only a listing-bound More options" in blocked["error"]
    assert calls == []

    first_result = click_ref(
        "@e1", "button", "More options for Kolin KD-291M06"
    )
    assert first_result["success"] is True, first_result
    assert click_ref(
        "@e2", "menuitem", "List in more places"
    )["success"] is True
    direct_submit = click_ref("@e2a", "button", "Post")
    assert direct_submit["success"] is False
    assert calls[2]["selected_crosspost_group_ids"] == sorted([
        group_id,
        preselected_group_id,
    ])
    with kb.connect_closing(db_path) as conn:
        assert kb.list_external_effects(conn, task_id) == []
    assert click_ref(
        "@e3", "checkbox", "(北市新北) 冷氣 家電", checked=False
    )["success"] is True
    assert click_ref("@e4", "button", "Post")["success"] is True

    assert [call["crosspost_stage"] for call in calls] == [
        "open_menu",
        "open_dialog_from_menu",
        "submit",
        "select_group",
        "submit",
    ]


def test_marketplace_price_update_requires_exact_edit_price_save_sequence(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "1666446304587399"
    target_price = 89000
    item_url = f"https://www.facebook.com/marketplace/item/{listing_id}"
    edit_url = (
        "https://www.facebook.com/marketplace/edit/"
        f"?listing_id={listing_id}"
    )
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps({
            "identity": {
                "project": "secondhand_commerce",
                "topic_name": "test",
                "request_instance_id": "price-update-test",
            },
            "original_request": "update one listing price",
            "grace_interpretation": "update only the approved price",
            "trigger": "owner approved",
            "completion_mode": "terminal",
            "goal": {
                "objective": "update price",
                "deliverables": ["readback"],
                "non_goals": ["other fields"],
            },
            "scope": {"allowed": ["price"], "forbidden": ["other fields"]},
            "verification": {
                "checks": ["readback"],
                "evidence_required": ["visible price"],
                "acceptance_criteria": ["exact price"],
            },
            "stop_rules": {
                "success": ["readback"], "blocked": ["guard"],
                "no_progress": ["same failure"], "max_iterations": 1,
                "max_runtime_seconds": 60,
            },
            "memory": {
                "namespace": "test", "working": ["price"],
                "promote_on_acceptance": ["price"],
            },
            "routing": {"task_type": "facebook_marketplace_price_update"},
            "external_targets": [f"Facebook Marketplace item {listing_id}"],
            "facebook_marketplace_price_update": {
                "action": "update_price", "transport": "browser",
                "marketplace_listing_id": listing_id, "currency": "TWD",
                "price_twd": target_price,
            },
        }, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    calls = []
    forced_failure = {"value": None}

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            if forced_failure["value"] is not None:
                return forced_failure["value"]
            return {"ok": True, "result": {"action": kwargs["action"]}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def act(url, identity, ref, role, name, *, action, text=None):
        monkeypatch.setattr(
            browser_tool,
            "_browser_page_identity",
            lambda *_args: (url, identity, None),
        )
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-price-update",
            {
                "page_identity": identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    ref.lstrip("@"): {
                        "role": role, "name": name,
                        "backend_node_id": len(calls) + 100,
                        "captured_session_id": "captured-session",
                    },
                },
            },
        )
        if action == "click":
            return json.loads(browser_tool.browser_click(ref, task_id="browser-price-update"))
        return json.loads(browser_tool.browser_type(ref, text, task_id="browser-price-update"))

    first = act(item_url, f"{item_url}|1", "@e1", "link", "Edit", action="click")
    assert first["success"], first
    assert "browser-price-update" in browser_tool._facebook_marketplace_price_contexts
    edit_identity = f"{edit_url}|2"
    filled = act(
        edit_url, edit_identity, "@e2", "textbox", "Price",
        action="fill", text="89,000",
    )
    assert filled["success"], filled
    assert act(
        edit_url, edit_identity, "@e3", "button", "Save", action="click"
    )["success"]
    assert [call["action"] for call in calls] == ["click", "fill", "click"]
    assert calls[1]["text"] == "89000"
    assert [call["marketplace_price_stage"] for call in calls] == [
        "edit", "fill", "submit",
    ]
    assert calls[1]["marketplace_price_token"]
    assert (
        calls[2]["marketplace_price_token"]
        == calls[1]["marketplace_price_token"]
    )
    assert all(
        call["required_marketplace_listing_id"] == listing_id
        for call in calls
    )
    assert all(
        call["required_marketplace_for_sale_item_id"] == listing_id
        for call in calls
    )
    assert all(
        call["required_marketplace_price_twd"] == target_price
        for call in calls
    )
    assert "browser-price-update" not in (
        browser_tool._facebook_marketplace_price_contexts
    )

    blocked = act(item_url, f"{item_url}|4", "@e4", "link", "Edit", action="click")
    assert blocked["success"] is True
    wrong_price = act(edit_url, f"{edit_url}|5", "@e5", "textbox", "Price", action="fill", text="89001")
    assert wrong_price["success"] is False
    assert wrong_price["error_code"] == "facebook_marketplace_price_scope_denied"

    exact_again = act(
        edit_url, f"{edit_url}|5", "@e6", "textbox", "Price",
        action="fill", text="89000",
    )
    assert exact_again["success"] is True
    replaced_page = act(
        edit_url, f"{edit_url}|6", "@e7", "button", "Save",
        action="click",
    )
    assert replaced_page["success"] is False
    assert len(calls) == 5

    restarted = act(
        item_url, f"{item_url}|7", "@e8", "link", "Edit", action="click",
    )
    assert restarted["success"] is True
    forced_failure["value"] = {
        "ok": False,
        "error_code": "facebook_marketplace_price_fill_guard_rejected",
        "error": "price fill rejected",
        "guard_diagnostics": {
            "failed_predicate": "normalized_live_value",
            "live_value": "89.000",
            "normalized_live_value": None,
        },
    }
    failed_fill = act(
        edit_url, f"{edit_url}|8", "@e9", "textbox", "Price",
        action="fill", text="89000",
    )
    assert failed_fill["success"] is False
    assert failed_fill["error_code"] == (
        "facebook_marketplace_price_fill_guard_rejected"
    )
    assert "blocker" in failed_fill, failed_fill
    assert failed_fill["blocker"]["operation"] == "price_fill"
    assert failed_fill["blocker"]["guard_diagnostics"][
        "failed_predicate"
    ] == "normalized_live_value"
    failed_context = browser_tool._facebook_marketplace_price_contexts[
        "browser-price-update"
    ]
    assert failed_context["stage"] == "guard_failed"
    assert failed_context["page_identity"] == f"{edit_url}|8"

    forced_failure["value"] = None
    denied_submit = act(
        edit_url, f"{edit_url}|8", "@e10", "button", "Update",
        action="click",
    )
    assert denied_submit["success"] is False
    assert denied_submit["error_code"] == (
        "facebook_marketplace_price_scope_denied"
    )


def test_marketplace_crosspost_allows_only_selling_listing_filter(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    group_id = "1333742673375089"
    live_url = "https://www.facebook.com/marketplace/you/selling"
    page_identity = f"{live_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "external_targets": [
                f"Facebook Marketplace item {listing_id}",
                f"Facebook Group {group_id}",
            ],
            "facebook_crosspost": {
                "marketplace_listing_id": listing_id,
                "group_ids": [group_id],
            },
            "routing": {"task_type": "browser_publish"},
        })
        + "\n```"
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
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def install_ref(name):
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-crosspost-filter",
            {
                "page_identity": page_identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    "e1": {
                        "role": "textbox",
                        "name": name,
                        "backend_node_id": 100,
                        "captured_session_id": "captured-session",
                    },
                },
            },
        )

    install_ref("Search your listings")
    filtered = json.loads(browser_tool.browser_type(
        "@e1",
        "Carimali Armonia Soft Plus",
        task_id="browser-crosspost-filter",
    ))
    assert filtered["success"] is True
    assert calls[-1]["action"] == "fill"
    assert calls[-1]["text"] == "Carimali Armonia Soft Plus"

    install_ref("Write to buyer")
    chat_fill = json.loads(browser_tool.browser_type(
        "@e1",
        "not allowed",
        task_id="browser-crosspost-filter",
    ))
    assert chat_fill["success"] is False
    assert len(calls) == 1


def test_selling_more_options_requires_fresh_canonical_item_title_proof(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    group_id = "1333742673375089"
    title = "Kolin KD-291M06"
    live_url = "https://www.facebook.com/marketplace/you/selling"
    page_identity = f"{live_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "external_targets": [
                f"Facebook Marketplace item {listing_id}",
                f"Facebook Group {group_id}",
            ],
            "facebook_crosspost": {
                "marketplace_listing_id": listing_id,
                "group_ids": [group_id],
            },
            "routing": {"task_type": "browser_publish"},
        })
        + "\n```"
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
    browser_key = "browser-selling-proof"
    browser_tool._facebook_crosspost_contexts.pop(browser_key, None)
    browser_tool._facebook_crosspost_source_proofs.pop(browser_key, None)
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        browser_key,
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": f"More options for {title}",
                    "backend_node_id": 100,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    missing = json.loads(browser_tool.browser_click("@e1", task_id=browser_key))
    assert missing["success"] is False
    assert missing["error_code"] == (
        "facebook_crosspost_canonical_item_proof_missing"
    )
    assert calls == []

    monkeypatch.setitem(
        browser_tool._facebook_crosspost_source_proofs,
        browser_key,
        {
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "listing_id": listing_id,
            "listing_title": title,
            "boost_label": (
                f"Boost listing for {title}. "
                "Boost to reach more potential buyers"
            ),
            "boost_target_id": "37276725125275496",
            "for_sale_item_id": listing_id,
            "group_ids": [group_id],
            "group_names": [],
            "observed_at": 1786527630,
        },
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        browser_key,
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": f"More options for {title}",
                    "backend_node_id": 100,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    monkeypatch.setattr(browser_tool.time, "time", lambda: 1786527627)
    future = json.loads(browser_tool.browser_click("@e1", task_id=browser_key))
    assert future["success"] is False
    assert future["error_code"] == (
        "facebook_crosspost_canonical_item_proof_missing"
    )
    assert calls == []

    monkeypatch.setitem(
        browser_tool._facebook_crosspost_source_proofs,
        browser_key,
        {
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "listing_id": listing_id,
            "listing_title": title,
            "boost_label": (
                f"Boost listing for {title}. "
                "Boost to reach more potential buyers"
            ),
            "boost_target_id": "37276725125275496",
            "for_sale_item_id": listing_id,
            "group_ids": [group_id],
            "group_names": [],
            "observed_at": 1786527600,
        },
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        browser_key,
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": f"More options for {title}",
                    "backend_node_id": 100,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    monkeypatch.setattr(browser_tool.time, "time", lambda: 1786527627)
    clicked = json.loads(browser_tool.browser_click("@e1", task_id=browser_key))
    assert clicked["success"] is True, clicked
    assert calls[0]["required_marketplace_listing_id"] == listing_id
    assert calls[0]["required_marketplace_listing_title"] == title
    assert calls[0]["required_marketplace_source_entity_id"] == (
        "37276725125275496"
    )
    assert calls[0]["required_marketplace_boost_label"] == (
        f"Boost listing for {title}. Boost to reach more potential buyers"
    )
    assert calls[0]["required_marketplace_for_sale_item_id"] == listing_id


def test_marketplace_crosspost_exact_names_resolve_before_reservation(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    names_by_id = {
        "1333742673375089": "咖啡器材買賣維修社團",
        "1466446866915040": "二手新舊咖啡設備大賣場",
    }
    live_url = f"https://www.facebook.com/marketplace/item/{listing_id}"
    page_identity = f"{live_url}|790.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        f'"Facebook Marketplace item {listing_id}",'
        '"Facebook group name: 咖啡器材買賣維修社團",'
        '"Facebook group name: 二手新舊咖啡設備大賣場"],'
        '"facebook_crosspost":{'
        '"transport":"browser",'
        f'"marketplace_listing_id":"{listing_id}",'
        '"group_names":["咖啡器材買賣維修社團",'
        '"二手新舊咖啡設備大賣場"]},'
        '"routing":{"task_type":"facebook_marketplace_group_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        assert kb.grace_task_facebook_crosspost_permissions(
            conn, task_id
        ) == (None, frozenset(), False)
        assert kb.grace_task_facebook_crosspost_name_permissions(
            conn, task_id
        ) == (listing_id, frozenset(names_by_id.values()), True)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    browser_tool._facebook_crosspost_contexts.pop("browser-crosspost-names", None)
    calls = []
    selected: dict[str, str] = {}

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            result = {
                "ok": True,
                "action": "click",
                "crosspost_for_sale_item_id": listing_id,
            }
            if kwargs.get("crosspost_stage") == "select_group":
                group_name = kwargs["expected_name"]
                group_id = next(
                    key for key, value in names_by_id.items() if value == group_name
                )
                result.update({
                    "crosspost_group_id": group_id,
                    "crosspost_group_name": group_name,
                    "crosspost_preselected_group_ids": list(selected),
                    "crosspost_preselected_group_names_by_id": dict(selected),
                })
                selected[group_id] = group_name
            return {"ok": True, "result": result}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def click_ref(ref, role, name, *, checked=None):
        metadata = {
            "role": role,
            "name": name,
            "backend_node_id": len(calls) + 200,
            "captured_session_id": "captured-session",
        }
        if checked is not None:
            metadata["checked"] = checked
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-crosspost-names",
            {
                "page_identity": page_identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {ref.lstrip("@"): metadata},
            },
        )
        return json.loads(
            browser_tool.browser_click(ref, task_id="browser-crosspost-names")
        )

    assert click_ref("@e1", "button", "More options for Carimali")["success"]
    assert click_ref("@e2", "menuitem", "List in more places")["success"]
    assert click_ref(
        "@e3", "checkbox", "咖啡器材買賣維修社團", checked=False
    )["success"]
    assert click_ref(
        "@e4", "checkbox", "二手新舊咖啡設備大賣場", checked=False
    )["success"]
    submit_result = click_ref("@e5", "button", "Post")
    assert submit_result["success"]
    assert {
        (row["group_id"], row["group_name"])
        for row in submit_result["result"]["crosspost_destinations"]
    } == set(names_by_id.items())

    assert calls[-1]["allowed_crosspost_group_ids"] == []
    assert set(calls[-1]["allowed_crosspost_group_names"]) == set(
        names_by_id.values()
    )
    assert set(calls[-1]["selected_crosspost_group_ids"]) == set(names_by_id)
    assert set(calls[-1]["selected_crosspost_group_names"]) == set(
        names_by_id.values()
    )
    assert all(
        call["required_marketplace_for_sale_item_id"] == listing_id
        for call in calls
    )
    with kb.connect_closing(db_path) as conn:
        effects = kb.list_external_effects(conn, task_id)
    assert {effect["effect_key"] for effect in effects} == {
        f"group:{group_id}" for group_id in names_by_id
    }
    assert {
        effect["details"]["approved_group_name"]
        for effect in effects
    } == set(names_by_id.values())
    assert all(effect["state"] == "create_started" for effect in effects)
    with kb.connect_closing(db_path) as conn:
        for group_id in names_by_id:
            recorded = kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                effect_key=f"group:{group_id}",
                state="created",
                external_id=group_id,
                details={"posting_status": "submitted"},
                expected_run_id=run.current_run_id,
            )
            assert recorded["state"] == "created"
        with pytest.raises(
            ValueError,
            match="same-run exact-name reservation",
        ):
            kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                effect_key="group:999999999999999",
                state="created",
                external_id="999999999999999",
                details={"posting_status": "submitted"},
                expected_run_id=run.current_run_id,
            )
    assert (
        "browser-crosspost-names"
        not in browser_tool._facebook_crosspost_contexts
    )


def test_name_bound_crosspost_completion_accepts_same_run_reservations(
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    names_by_id = {
        "1333742673375089": "咖啡器材買賣維修社團",
        "1466446866915040": "二手新舊咖啡設備大賣場",
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        f'"Facebook Marketplace item {listing_id}",'
        '"Facebook group name: 咖啡器材買賣維修社團",'
        '"Facebook group name: 二手新舊咖啡設備大賣場"],'
        '"facebook_crosspost":{'
        f'"marketplace_listing_id":"{listing_id}",'
        '"group_names":["咖啡器材買賣維修社團",'
        '"二手新舊咖啡設備大賣場"]},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        assert kb.reserve_external_group_posts(
            conn,
            task_id,
            list(names_by_id),
            expected_run_id=run.current_run_id,
            allow_crosspost=True,
            resolved_group_names_by_id=names_by_id,
        ) is None
        assert kb.complete_task(
            conn,
            task_id,
            summary="guarded Post succeeded",
            metadata={
                "external_effects": [
                    {
                        "platform": "facebook",
                        "effect_key": f"group:{group_id}",
                        "state": "created",
                        "external_id": group_id,
                        "details": {
                            "approved_group_name": group_name,
                            "posting_status": "created_submitted",
                        },
                    }
                    for group_id, group_name in names_by_id.items()
                ],
            },
            expected_run_id=run.current_run_id,
        )
        effects = kb.list_external_effects(conn, task_id)
    assert {effect["state"] for effect in effects} == {"created"}
    assert {
        effect["details"]["approved_group_name"] for effect in effects
    } == set(names_by_id.values())


def test_crosspost_group_reservations_are_all_or_nothing(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    first_group_id = "1333742673375089"
    second_group_id = "1466446866915040"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        f'"Facebook Group {first_group_id}",'
        f'"Facebook Group {second_group_id}"],'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            effect_key=f"group:{second_group_id}",
            state="verified",
            external_id=second_group_id,
            expected_run_id=run.current_run_id,
        )

        error = kb.reserve_external_group_posts(
            conn,
            task_id,
            [first_group_id, second_group_id],
            expected_run_id=run.current_run_id,
        )
        effects = kb.list_external_effects(conn, task_id)

    assert error is not None
    assert "durable state is already verified" in error
    assert [effect["effect_key"] for effect in effects] == [
        f"group:{second_group_id}",
    ]


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


def test_facebook_page_target_requires_one_canonical_public_page():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        '"https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_facebook_page_target(body) == (
        "https://www.facebook.com/solobizai"
    )
    assert kb.canonical_facebook_page_url(
        "https://facebook.com/SoloBizAi/"
    ) == "https://www.facebook.com/solobizai"
    assert kb.canonical_facebook_page_url(
        "https://www.facebook.com/groups/123"
    ) is None
    assert kb.canonical_facebook_page_url(
        "https://www.facebook.com:8443/SoloBizAi"
    ) is None
    assert kb.canonical_facebook_page_url(
        "https://www.facebook.com:bad/SoloBizAi"
    ) is None

    ambiguous = body.replace(
        '"https://www.facebook.com/solobizai"',
        '"https://www.facebook.com/solobizai",'
        '"https://www.facebook.com/anotherpage"',
    )
    assert kb.grace_facebook_page_target(ambiguous) is None
    generic_publish = body.replace(
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},',
        "",
    )
    assert kb.grace_facebook_page_target(generic_publish) is None
    mixed_group_target = body.replace(
        '"https://www.facebook.com/solobizai"],',
        '"Facebook Group 123 https://www.facebook.com/solobizai"],',
    )
    assert kb.grace_facebook_page_target(mixed_group_target) is None
    mixed_marketplace_target = body.replace(
        '"https://www.facebook.com/solobizai"],',
        '"Facebook Marketplace item 123 '
        'https://www.facebook.com/solobizai"],',
    )
    assert kb.grace_facebook_page_target(mixed_marketplace_target) is None


def test_graph_page_permission_is_separate_and_reserves_exact_hashes(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    page_url = "https://www.facebook.com/solobizai"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai",'
        '"transport":"graph_api","message_sha256":"' + "a" * 64 + '",'
        '"image_sha256":"' + "b" * 64 + '"},'
        '"routing":{"task_type":"facebook_page_api_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        permission = kb.grace_task_facebook_page_api_permission(conn, task_id)
        assert permission == {
            "page_url": page_url,
            "message_sha256": "a" * 64,
            "image_sha256": "b" * 64,
        }
        assert kb.grace_task_facebook_page_post_permission(conn, task_id) is None

        assert kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
            transport="graph_api",
            reservation_details={
                "message_sha256": "a" * 64,
                "image_sha256": "b" * 64,
            },
        ) is None
        row = conn.execute(
            "SELECT state, details FROM task_external_effects "
            "WHERE task_id = ? AND platform = 'facebook' "
            "AND effect_key = 'create'",
            (task_id,),
        ).fetchone()
        assert row["state"] == "create_started"
        details = json.loads(row["details"])
        assert details["transport"] == "graph_api"
        assert details["message_sha256"] == "a" * 64
        assert details["image_sha256"] == "b" * 64

        retry_error = kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
            transport="graph_api",
        )
        assert "durable state is already create_started" in retry_error


def test_graph_page_publish_records_verified_post_and_blocks_retry(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    image = tmp_path / "questgen-v2.png"
    approved_image_bytes = b"\x89PNG\r\n\x1a\nverified-image"
    image.write_bytes(approved_image_bytes)
    message = "Questgen 最終正文\n#SoloBizAI"
    message_sha256 = hashlib.sha256(message.encode()).hexdigest()
    image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    page_url = "https://www.facebook.com/solobizai"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai",'
        '"transport":"graph_api","message_sha256":"' + message_sha256 + '",'
        '"image_sha256":"' + image_sha256 + '"},'
        '"routing":{"task_type":"facebook_page_api_publish"}}\n```'
    )
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "531289396730654")
    monkeypatch.setenv(
        "FACEBOOK_PAGE_NAME",
        "AI BizWeek｜SoloBiz AI 一人公司商業誌",
    )
    monkeypatch.setenv("FACEBOOK_PAGE_URL", page_url)
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "secret-page-token")

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def page_identity(self):
            # Simulate a file replacement after hash verification. The tool
            # must still upload the immutable bytes that were approved.
            image.write_bytes(b"\x89PNG\r\n\x1a\nunapproved-replacement")
            return {
                "id": "531289396730654",
                "name": "AI BizWeek｜SoloBiz AI 一人公司商業誌",
                "link": "https://www.facebook.com/SoloBizAi/",
            }

        def publish_photo(
            self,
            supplied_message,
            supplied_image,
            supplied_image_bytes,
        ):
            assert supplied_message == message
            assert supplied_image == image.resolve()
            assert supplied_image_bytes == approved_image_bytes
            assert supplied_image_bytes != image.read_bytes()
            return {
                "id": "photo-1",
                "post_id": "531289396730654_987",
            }

        def read_post(self, post_id):
            assert post_id == "531289396730654_987"
            return {
                "id": post_id,
                "message": message,
                "created_time": "2026-08-15T12:00:00+0000",
                "permalink_url": (
                    "https://www.facebook.com/531289396730654/posts/987"
                ),
                "attachments": {"data": [{"media_type": "photo"}]},
            }

    monkeypatch.setattr(graph_tool, "FacebookPageGraphClient", FakeClient)

    result = json.loads(graph_tool.facebook_page_graph_publish(
        page_url=page_url,
        message=message,
        image_path=str(image),
    ))
    assert result["success"] is True
    assert result["published"] is True
    assert result["verified"] is True
    assert result["durable_verified"] is True
    assert result["post_id"] == "531289396730654_987"

    image.write_bytes(approved_image_bytes)
    retry = json.loads(graph_tool.facebook_page_graph_publish(
        page_url=page_url,
        message=message,
        image_path=str(image),
    ))
    assert retry["success"] is False
    assert "durable state is already verified" in retry["error"]

    with kb.connect_closing(db_path) as conn:
        effect = conn.execute(
            "SELECT state, external_id, details FROM task_external_effects "
            "WHERE task_id = ? AND platform = 'facebook' "
            "AND effect_key = 'create'",
            (task_id,),
        ).fetchone()
        assert effect["state"] == "verified"
        assert effect["external_id"] == "531289396730654_987"
        assert json.loads(effect["details"])["permalink_url"].endswith(
            "/531289396730654/posts/987"
        )


@pytest.mark.parametrize("submit_ambiguous", [False, True])
def test_page_publish_contract_allows_only_its_trusted_composer(
    monkeypatch,
    tmp_path,
    submit_ambiguous,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    page_url = "https://www.facebook.com/solobizai"
    page_identity = f"{page_url}|789.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        assert kb.grace_task_facebook_page_post_permission(
            conn,
            task_id,
        ) == "https://www.facebook.com/solobizai"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    live = {"url": page_url, "identity": page_identity}
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live["url"], live["identity"], None),
    )
    browser_key = "browser-page-post"
    browser_tool._facebook_page_post_contexts.pop(browser_key, None)

    actions = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            actions.append(kwargs)
            if (
                submit_ambiguous
                and kwargs.get("page_composer_stage") == "submit"
            ):
                return {
                    "ok": False,
                    "dispatch_ambiguous": True,
                    "error": "renderer dispatch result is unknown",
                }
            result = {"ok": True}
            if kwargs.get("page_composer_stage") == "open":
                result["boundPageComposerToken"] = kwargs.get(
                    "page_composer_token"
                )
                result["boundPageActor"] = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
                result["pageGuardDiagnostics"] = {
                    "page_url_match": True,
                    "manage_page_context_visible": True,
                    "switch_into_page_visible": False,
                    "composer_actor_match": True,
                }
            return {"ok": True, "result": result}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def snapshot_ref(ref, role, name, backend_node_id):
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            browser_key,
            {
                "page_identity": live["identity"],
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    ref: {
                        "role": role,
                        "name": name,
                        "backend_node_id": backend_node_id,
                        "captured_session_id": "captured-session",
                    }
                },
            },
        )

    snapshot_ref("e1", "button", "Photo/video", 1001)
    opened = json.loads(browser_tool.browser_click("@e1", task_id=browser_key))
    assert opened["success"] is True
    assert browser_key in browser_tool._facebook_page_post_contexts
    assert (
        browser_tool._facebook_page_post_contexts[browser_key]
        ["open_guard_diagnostics"]
        ["switch_into_page_visible"]
        is False
    )

    snapshot_ref("e2", "textbox", "What's on your mind?", 1002)
    filled = json.loads(
        browser_tool.browser_type("@e2", "approved body", task_id=browser_key)
    )
    assert filled["success"] is True
    assert (
        browser_tool._facebook_page_post_contexts[browser_key]
        ["composer_page_identity"]
        == page_identity
    )

    snapshot_ref("e3", "button", "Publish", 1003)
    published = json.loads(
        browser_tool.browser_click("@e3", task_id=browser_key)
    )
    assert published["success"] is (not submit_ambiguous)
    if submit_ambiguous:
        assert "unknown" in published["error"]
    assert browser_key not in browser_tool._facebook_page_post_contexts
    assert [action["action"] for action in actions] == [
        "click",
        "fill",
        "click",
    ]
    assert all(action["required_group_id"] is None for action in actions)
    composer_tokens = [action["page_composer_token"] for action in actions]
    assert composer_tokens[0]
    assert len(set(composer_tokens)) == 1
    assert [action["page_composer_stage"] for action in actions] == [
        "open", "compose", "submit",
    ]
    assert actions[0]["timeout"] == (
        browser_tool.FACEBOOK_PAGE_COMPOSER_OPEN_TIMEOUT
    )
    assert "timeout" not in actions[1]
    assert "timeout" not in actions[2]
    assert actions[0]["required_facebook_page_actor"] is None
    assert {
        actions[1]["required_facebook_page_actor"],
        actions[2]["required_facebook_page_actor"],
    } == {"AI BizWeek｜SoloBiz AI 一人公司商業誌"}
    with kb.connect_closing(db_path) as conn:
        effects = kb.list_external_effects(conn, task_id)
        retry_error = kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
        )
    assert effects[0]["effect_key"] == "create"
    assert effects[0]["state"] == "create_started"
    assert effects[0]["details"]["page_url"] == (
        "https://www.facebook.com/solobizai"
    )
    assert "already create_started" in retry_error


def test_page_publish_rejects_reload_or_navigation_after_opening_composer(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    page_url = "https://www.facebook.com/SoloBizAi"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    live = {"url": page_url, "identity": f"{page_url}|789.0"}
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live["url"], live["identity"], None),
    )
    browser_key = "browser-business-page-post"
    browser_tool._facebook_page_post_contexts.pop(browser_key, None)

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            return {
                "ok": True,
                "result": {
                    "ok": True,
                    "boundPageComposerToken": kwargs.get(
                        "page_composer_token"
                    ),
                    "boundPageActor": "AI BizWeek｜SoloBiz AI 一人公司商業誌",
                },
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    def snapshot_ref(ref, role, name, backend_node_id):
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            browser_key,
            {
                "page_identity": live["identity"],
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    ref: {
                        "role": role,
                        "name": name,
                        "backend_node_id": backend_node_id,
                        "captured_session_id": "captured-session",
                    }
                },
            },
        )

    snapshot_ref("e1", "button", "What's on your mind?", 2001)
    assert json.loads(
        browser_tool.browser_click("@e1", task_id=browser_key)
    )["success"] is True

    live["identity"] = f"{page_url}|790.0"
    snapshot_ref(
        "e2",
        "textbox",
        "What's on your mind?",
        2002,
    )
    reloaded = json.loads(
        browser_tool.browser_type("@e2", "must fail", task_id=browser_key)
    )
    assert reloaded["success"] is False
    assert "Page load changed" in reloaded["error"]

    live["url"] = (
        "https://business.facebook.com/latest/composer?"
        "asset_id=531289396730654&business_id=4007321396150350"
    )
    live["identity"] = f"{live['url']}|791.0"
    snapshot_ref(
        "e3",
        "combobox",
        "Write into the dialogue box to include text with your post.",
        2003,
    )
    typed = json.loads(
        browser_tool.browser_type("@e3", "approved body", task_id=browser_key)
    )
    assert typed["success"] is False
    assert "neither an authorized numeric group" in typed["error"]


def test_page_publish_absence_evidence_never_reopens_create(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    page_url = "https://www.facebook.com/solobizai"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
        noncanonical_error = kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            "https://facebook.com/SoloBizAi/",
            expected_run_id=run.current_run_id,
        )
        assert "not a canonical Page URL" in noncanonical_error
        assert kb.list_external_effects(conn, task_id) == []

        assert kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
        ) is None
        kb.release_external_facebook_page_post_reservation(
            conn,
            task_id,
            "https://facebook.com/SoloBizAi/",
            expected_run_id=run.current_run_id,
            reason="noncanonical release must not match",
        )
        assert kb.list_external_effects(conn, task_id)[0]["state"] == (
            "create_started"
        )
        kb.release_external_facebook_page_post_reservation(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
            reason="canonical pre-dispatch release",
        )
        assert kb.list_external_effects(conn, task_id) == []
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            state="absent_verified",
            expected_run_id=run.current_run_id,
        )

        error = kb.reserve_external_facebook_page_post(
            conn,
            task_id,
            page_url,
            expected_run_id=run.current_run_id,
        )

    assert "already absent_verified" in error


def test_page_publish_rejects_other_page_and_comment_box(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":["https://www.facebook.com/solobizai"],'
        '"facebook_page_post":{"action":"create_post",'
        '"page_url":"https://www.facebook.com/solobizai"},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    live_url = "https://www.facebook.com/AnotherPage"
    page_identity = f"{live_url}|789.0"
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-wrong-page",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "What's on your mind?",
                    "backend_node_id": 3001,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    wrong_page = json.loads(
        browser_tool.browser_click("@e1", task_id="browser-wrong-page")
    )
    assert wrong_page["success"] is False
    assert "approved Page composer" in wrong_page["error"]

    live_url = "https://www.facebook.com/SoloBizAi"
    page_identity = f"{live_url}|790.0"
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-wrong-page",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e2": {
                    "role": "textbox",
                    "name": "Comment as SoloBizAi",
                    "backend_node_id": 3002,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    comment = json.loads(
        browser_tool.browser_type(
            "@e2",
            "must fail",
            task_id="browser-wrong-page",
        )
    )
    assert comment["success"] is False
    assert "only its post text" in comment["error"]


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
        task_id, _ = _execution_task(
            conn,
            body=body,
            bind_approval=False,
        )
        conn.execute(
            """
            INSERT INTO grace_approval_challenges (
                token, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                user_id_sha256, requested_message_id, action_summary,
                approval_platform, approval_scope, delegation_args,
                state, created_at,
                expires_at, consumed_at, approved_message_id
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
        '```json\n{"external_targets":['
        '"Facebook Group 897927458651235「大台灣二手家具家電交流」'
        'https://www.facebook.com/groups/1466446866915040/"]}\n```'
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


def test_structured_facebook_crosspost_binds_listing_and_group_ids():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        '"Facebook Marketplace item 915975414881937 → '
        'Facebook Group 1333742673375089"],'
        '"facebook_crosspost":{'
        '"marketplace_listing_id":"915975414881937",'
        '"group_ids":["1333742673375089"]},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset({
        "1333742673375089",
    })
    assert kb.grace_facebook_crosspost_scope(body) == (
        "915975414881937",
        frozenset({"1333742673375089"}),
    )


def test_structured_facebook_crosspost_rejects_display_id_mismatch():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        '"Facebook Marketplace item 915975414881937 → '
        'Facebook Group 999999999999999"],'
        '"facebook_crosspost":{'
        '"marketplace_listing_id":"915975414881937",'
        '"group_ids":["1333742673375089"]},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset()
    assert kb.grace_facebook_crosspost_scope(body) == (None, frozenset())


def test_structured_facebook_crosspost_binds_exact_group_names():
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        '```json\n{"external_targets":['
        '"Facebook Marketplace item 915975414881937",'
        '"Facebook group name: 咖啡器材買賣維修社團",'
        '"Facebook group name: 二手新舊咖啡設備大賣場"],'
        '"facebook_crosspost":{'
        '"marketplace_listing_id":"915975414881937",'
        '"group_names":["咖啡器材買賣維修社團",'
        '"二手新舊咖啡設備大賣場"]},'
        '"routing":{"task_type":"browser_publish"}}\n```'
    )

    assert kb.grace_external_group_ids(body) == frozenset()
    assert kb.grace_external_group_names(body) == frozenset({
        "咖啡器材買賣維修社團",
        "二手新舊咖啡設備大賣場",
    })
    assert kb.grace_facebook_crosspost_name_scope(body) == (
        "915975414881937",
        frozenset({
            "咖啡器材買賣維修社團",
            "二手新舊咖啡設備大賣場",
        }),
    )


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


def test_name_bound_canonical_item_snapshot_records_for_sale_source_proof(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    group_name = "(北市新北) 冷氣 家電 家具 五金 雜貨全新中古買賣"
    title = "Kolin KD-291M06"
    live_url = f"https://www.facebook.com/marketplace/item/{listing_id}"
    identity = f"{live_url}|100.0"
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "external_targets": [
                f"Facebook Marketplace item {listing_id}",
                f"Facebook group name: {group_name}",
            ],
            "facebook_crosspost": {
                "marketplace_listing_id": listing_id,
                "group_names": [group_name],
            },
            "routing": {"task_type": "browser_publish"},
        }, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body)
    snapshot = {"data": {"url": live_url, "refs": {}, "snapshot": ""}}
    boost_label = (
        f"Boost listing for {title}. "
        "Boost to reach more potential buyers"
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, identity, None),
    )
    monkeypatch.setattr(
        browser_tool,
        "_snapshot_ax_nodes",
        lambda *_args: ([
            {
                "backend_node_id": 10,
                "role": "button",
                "name": f"Share {title}",
                "captured_session_id": "captured-session",
            },
            {
                "backend_node_id": 11,
                "role": "link",
                "name": boost_label,
                "captured_session_id": "captured-session",
            },
        ], None),
    )

    class FakeSupervisor:
        def call_session_cdp(self, _session_id, method, _params):
            if method == "DOM.resolveNode":
                return {
                    "ok": True,
                    "result": {"object": {"objectId": "boost-object"}},
                }
            return {
                "ok": True,
                "result": {"result": {"value": "37276725125275496"}},
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )
    browser_tool._facebook_crosspost_source_proofs.pop(
        "browser-name-proof",
        None,
    )

    error = browser_tool._remember_snapshot_refs(
        "browser-name-proof",
        snapshot,
        identity,
    )

    assert error is None
    proof = browser_tool._facebook_crosspost_source_proofs[
        "browser-name-proof"
    ]
    assert proof["listing_id"] == listing_id
    assert proof["for_sale_item_id"] == listing_id
    assert proof["group_ids"] == []
    assert proof["group_names"] == [group_name]


def test_snapshot_ax_capture_passes_exact_page_load_identity(monkeypatch):
    live_url = "https://www.facebook.com/marketplace/you/selling/"
    page_identity = f"{live_url}|200.0"
    calls = []

    class FakeSupervisor:
        def capture_ax_tree_for_url(self, expected_url, **kwargs):
            calls.append((expected_url, kwargs))
            return {
                "ok": True,
                "session_id": "current-session",
                "result": {
                    "nodes": [
                        {
                            "backendDOMNodeId": 42,
                            "role": {"value": "button"},
                            "name": {"value": "Options"},
                            "properties": [{
                                "name": "hasPopup",
                                "value": {"value": "menu"},
                            }],
                        }
                    ]
                },
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    nodes, error = browser_tool._snapshot_ax_nodes(
        "browser-1",
        live_url,
        page_identity,
    )

    assert error is None
    assert nodes[0]["backend_node_id"] == 42
    assert nodes[0]["captured_session_id"] == "current-session"
    assert nodes[0]["haspopup"] == "menu"
    assert nodes[0]["haspopup_source"] == "ax_full"
    assert calls == [(
        live_url,
        {"expected_page_identity": page_identity},
    )]


def test_readonly_more_options_accepts_unique_item_page_options_alias(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    live_url = f"https://www.facebook.com/marketplace/item/{listing_id}"
    page_identity = f"{live_url}|789.0"
    contract = {
        **canonical_marketplace_readonly_sections(listing_id),
        "external_targets": [f"Facebook Marketplace item {listing_id}"],
        "routing": {"task_type": "secondhand_commerce_group_status"},
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": [f"facebook_marketplace:{listing_id}"],
        },
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
        )
        assert kb.grace_task_facebook_crosspost_inspection_permission(
            conn,
            task_id,
        ) == listing_id
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
        "browser-inspection",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "More options",
                    "backend_node_id": 42,
                    "captured_session_id": "captured-session",
                    "haspopup": "menu",
                }
            },
        },
    )
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    result = json.loads(
        browser_tool.browser_click("@e1", task_id="browser-inspection")
    )

    assert result["success"] is True
    assert calls[0]["required_popup_role"] == "menu"
    assert calls[0]["crosspost_stage"] == "open_menu"
    assert calls[0]["required_marketplace_listing_id"] == listing_id

    browser_tool._facebook_crosspost_contexts.pop(
        "browser-inspection-generic",
        None,
    )
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-generic",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e2": {
                    "role": "button",
                    "name": "Options",
                    "backend_node_id": 43,
                    "captured_session_id": "captured-session",
                    "haspopup": "menu",
                    "haspopup_source": "ax_full",
                },
                "e3": {
                    "role": "menuitem",
                    "name": "List in more places",
                    "backend_node_id": 44,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    generic_result = json.loads(
        browser_tool.browser_click(
            "@e2",
            task_id="browser-inspection-generic",
        )
    )
    assert generic_result["success"] is True
    assert calls[-1]["required_popup_role"] == "menu"
    assert calls[-1]["expected_popup_semantics_source"] == "ax_full"
    assert calls[-1]["crosspost_stage"] == "open_menu"
    assert calls[-1]["required_marketplace_listing_id"] == listing_id
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-no-popup",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e4": {
                    "role": "button",
                    "name": "Options",
                    "backend_node_id": 45,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    no_popup_result = json.loads(
        browser_tool.browser_click(
            "@e4",
            task_id="browser-inspection-no-popup",
        )
    )
    assert no_popup_result["success"] is True
    assert calls[-1]["required_popup_role"] is None
    assert calls[-1]["crosspost_stage"] == "open_menu"
    assert calls[-1]["required_marketplace_listing_id"] == listing_id

    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-duplicate-options",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e7": {
                    "role": "button",
                    "name": "Options",
                    "backend_node_id": 47,
                    "captured_session_id": "captured-session",
                },
                "e8": {
                    "role": "button",
                    "name": "Options",
                    "backend_node_id": 48,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    duplicate_result = json.loads(
        browser_tool.browser_click(
            "@e7",
            task_id="browser-inspection-duplicate-options",
        )
    )
    assert duplicate_result["success"] is False
    assert "only listing-bound More options" in duplicate_result["error"]
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-generic",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e3": {
                    "role": "menuitem",
                    "name": "List in more places",
                    "backend_node_id": 44,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )
    direct_result = json.loads(
        browser_tool.browser_click(
            "@e3",
            task_id="browser-inspection-generic",
        )
    )
    assert direct_result["success"] is True
    assert calls[-1]["crosspost_stage"] == "open_dialog_from_menu"
    assert len(calls) == 4

    for ref, role, name, expected_error in (
        ("e5", "checkbox", "台北二手家電", "group selection is forbidden"),
        ("e6", "button", "Post", "Post/Publish is forbidden"),
    ):
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-inspection-generic",
            {
                "page_identity": page_identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    ref: {
                        "role": role,
                        "name": name,
                        "backend_node_id": 50,
                        "captured_session_id": "captured-session",
                    },
                },
            },
        )
        forbidden_result = json.loads(
            browser_tool.browser_click(
                f"@{ref}",
                task_id="browser-inspection-generic",
            )
        )
        assert forbidden_result["success"] is False
        assert expected_error in forbidden_result["error"]
    assert len(calls) == 4


def test_group_status_readonly_allows_exact_product_evidence_link(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    group_id = "1703088130054399"
    observed_at = 1786683254
    report = {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": False,
        "as_of": "2026-08-14T05:00:54Z",
        "observed_at": observed_at,
        "rows": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "destination_id": group_id,
            "destination_name": "二手冷氣買賣",
            "status": "unknown",
            "status_label": "尚未驗證",
            "observed_at": observed_at,
            "verified_at": "2026-08-14T05:00:54Z",
            "evidence": "Durable destination awaiting exact post evidence.",
            "source_listing_id": listing_id,
        }],
        "coverage": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "complete": False,
            "named_count": 1,
            "gap_count": 0,
            "expected_total": 1,
            "note": "Exact group remains unverified.",
        }],
    }
    contract = {
        **canonical_marketplace_readonly_sections(listing_id),
        "external_targets": [f"facebook:marketplace:{listing_id}"],
        "routing": {"task_type": "secondhand_commerce_group_status"},
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": ["kolin-kd291m06"],
        },
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        legacy_task = kb.create_task(conn, title="Saved group", body=body)
        assert kb.complete_task(
            conn,
            legacy_task,
            summary="Saved destination",
            metadata={"user_facing_report": report},
        )
        task_id, run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
        )
        allowed_ids, product_tokens = (
            kb.grace_task_facebook_group_inspection_permissions(
                conn,
                task_id,
            )
        )
    assert allowed_ids == frozenset({group_id})
    assert "kd-291m06" in product_tokens

    live_url = f"https://www.facebook.com/groups/{group_id}/search/"
    page_identity = f"{live_url}|789.0"
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
        "browser-group-inspection",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "link",
                    "name": "歌林 Kolin 冷暖型移動式空調 KD-291M06",
                    "backend_node_id": 84,
                    "captured_session_id": "captured-session",
                }
            },
        },
    )
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "result": {
                    "readonlyCanonicalGroupUrl": (
                        f"https://www.facebook.com/groups/{group_id}/posts/123"
                    ),
                },
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    result = json.loads(browser_tool.browser_click(
        "@e1",
        task_id="browser-group-inspection",
    ))

    assert result["success"] is True
    assert calls[0]["required_group_id"] == group_id
    assert calls[0]["require_readonly_group_navigation"] is True

    for ref, role, name in (
        ("e2", "button", "Join group"),
        ("e3", "link", "歌林冷暖型移動式空調 KD-291M06A"),
        ("e4", "link", "歌林冷暖型移動式空調 KD-291M06-A"),
    ):
        monkeypatch.setitem(
            browser_tool._snapshot_ref_contexts,
            "browser-group-inspection-rejected",
            {
                "page_identity": page_identity,
                "kanban_task_id": task_id,
                "kanban_run_id": run.current_run_id,
                "refs": {
                    ref: {
                        "role": role,
                        "name": name,
                        "backend_node_id": 85,
                        "captured_session_id": "captured-session",
                    }
                },
            },
        )
        rejected = json.loads(browser_tool.browser_click(
            f"@{ref}",
            task_id="browser-group-inspection-rejected",
        ))
        assert rejected["success"] is False
        assert "exact control is not" in rejected["error"]
    assert len(calls) == 1


def test_group_status_readonly_more_options_is_bound_on_selling_page(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "36803832485927906"
    live_url = "https://www.facebook.com/marketplace/you/selling/"
    page_identity = f"{live_url}|789.0"
    contract = {
        **canonical_marketplace_readonly_sections(listing_id),
        "external_targets": [f"facebook:marketplace:{listing_id}"],
        "routing": {"task_type": "secondhand_commerce_group_status"},
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": [f"facebook_marketplace:{listing_id}"],
        },
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
        )
        assert kb.grace_task_facebook_crosspost_inspection_permission(
            conn,
            task_id,
        ) == listing_id
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda *_args: (live_url, page_identity, None),
    )
    snapshot_context = {
        "page_identity": page_identity,
        "kanban_task_id": task_id,
        "kanban_run_id": run.current_run_id,
        "refs": {
            "e1": {
                "role": "button",
                "name": "More options for Carimali Armonia Soft Plus",
                "backend_node_id": 42,
                "captured_session_id": "captured-session",
            },
        },
    }
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-selling",
        snapshot_context,
    )
    calls = []

    class FakeSupervisor:
        def guarded_dom_action(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: FakeSupervisor(),
    )

    result = json.loads(
        browser_tool.browser_click(
            "@e1",
            task_id="browser-inspection-selling",
        )
    )

    assert result["success"] is True
    assert calls[0]["crosspost_stage"] == "open_menu"
    assert calls[0]["required_marketplace_listing_id"] == listing_id
    assert calls[0]["required_popup_role"] is None
    assert calls[0]["expected_popup_semantics_source"] is None

    snapshot_context["refs"] = {
        "e4": {
            "role": "button",
            "name": "Options",
            "backend_node_id": 45,
            "captured_session_id": "captured-session",
            "haspopup": "menu",
        },
    }
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-selling",
        snapshot_context,
    )
    generic_selling = json.loads(
        browser_tool.browser_click(
            "@e4",
            task_id="browser-inspection-selling",
        )
    )
    assert generic_selling["success"] is False
    assert "only listing-bound More options" in generic_selling["error"]
    assert len(calls) == 1


    snapshot_context["refs"] = {
        "e2": {
            "role": "button",
            "name": "Share Carimali Armonia Soft Plus",
            "backend_node_id": 43,
            "captured_session_id": "captured-session",
        },
    }
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-selling",
        snapshot_context,
    )
    blocked = json.loads(
        browser_tool.browser_click(
            "@e2",
            task_id="browser-inspection-selling",
        )
    )

    assert blocked["success"] is False
    assert "only listing-bound More options" in blocked["error"]
    assert len(calls) == 1

    failure_context = {
        **snapshot_context,
        "refs": {
            "e3": {
                "role": "button",
                "name": "More options for Carimali Armonia Soft Plus",
                "backend_node_id": 44,
                "captured_session_id": "captured-session",
                "haspopup": "menu",
            },
        },
    }
    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-popup-mismatch",
        failure_context,
    )

    class PopupMismatchSupervisor:
        def guarded_dom_action(self, **_kwargs):
            return {
                "ok": False,
                "error_code": (
                    "popup_semantics_changed_before_atomic_action"
                ),
                "error": (
                    "Captured snapshot popup semantics changed before "
                    "atomic action"
                ),
                "expected_popup_role": "menu",
                "actual_popup_role": "missing",
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: PopupMismatchSupervisor(),
    )
    mismatch = json.loads(
        browser_tool.browser_click(
            "@e3",
            task_id="browser-inspection-popup-mismatch",
        )
    )

    assert mismatch["success"] is False
    assert "error_code" in mismatch, mismatch
    assert mismatch["error_code"] == (
        "popup_semantics_changed_before_atomic_action"
    )
    assert mismatch["expected_popup_role"] == "menu"
    assert mismatch["actual_popup_role"] == "missing"
    assert mismatch["blocker"]["listing_id"] == listing_id
    assert mismatch["blocker"]["external_state_changed"] is False
    with kb.connect_closing(db_path) as conn:
        blocker_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "browser_blocker_recorded"
        ]
    assert len(blocker_events) == 1
    assert blocker_events[0].payload == mismatch["blocker"]

    monkeypatch.setitem(
        browser_tool._snapshot_ref_contexts,
        "browser-inspection-different-listing",
        failure_context,
    )

    class DifferentListingSupervisor:
        def guarded_dom_action(self, **_kwargs):
            return {
                "ok": False,
                "error_code": (
                    "facebook_crosspost_control_different_listing"
                ),
                "error": "Cross-post control belongs to a different listing",
            }

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda _task_id: DifferentListingSupervisor(),
    )
    different_listing = json.loads(
        browser_tool.browser_click(
            "@e3",
            task_id="browser-inspection-different-listing",
        )
    )
    assert different_listing["success"] is False
    assert different_listing["error_code"] == (
        "facebook_crosspost_control_different_listing"
    )
    assert different_listing["blocker"]["observed_at"] > 0
    assert different_listing["blocker"]["exact_error"] == (
        "Cross-post control belongs to a different listing"
    )
    with kb.connect_closing(db_path) as conn:
        blocker_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "browser_blocker_recorded"
        ]
        assert len(blocker_events) == 2
        assert blocker_events[-1].payload == different_listing["blocker"]
        assert kb.block_task(
            conn,
            task_id,
            reason="controlled browser popup semantics mismatch",
            kind="capability",
            expected_run_id=run.current_run_id,
        )
        blocked_event = next(
            event
            for event in reversed(kb.list_events(conn, task_id))
            if event.kind == "blocked"
        )
    assert blocked_event.payload["blocker"] == different_listing["blocker"]


def test_worker_readonly_route_grants_exact_listing_inspection_permission(
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "27700220586305145"
    contract = {
        **canonical_marketplace_readonly_sections(listing_id),
        "external_targets": [
            f"facebook:marketplace-group-status:{listing_id}",
        ],
        "routing": {"task_type": "facebook_marketplace_readonly"},
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, _run = _execution_task(
            conn,
            body=body,
            bind_approval=False,
        )

        assert kb.grace_task_facebook_crosspost_inspection_permission(
            conn,
            task_id,
        ) == listing_id


def test_legacy_readonly_scope_denial_records_canonical_blocker(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    listing_id = "915975414881937"
    live_url = "https://www.facebook.com/marketplace/you/selling"
    page_identity = f"{live_url}|789.0"
    contract = {
        "goal": {
            "objective": (
                "僅以唯讀模式檢視 Facebook Marketplace listing "
                f"{listing_id} 的 More options → List in more places"
            ),
            "deliverables": ["候選社團名稱與狀態"],
        },
        "scope": {
            "allowed": [
                "僅對 Facebook Marketplace listing "
                f"{listing_id} 進行 More options → List in more places 的唯讀檢視",
            ],
            "forbidden": [
                "任何 Facebook 外部狀態變更",
                "勾選或取消任何社團 checkbox",
                "按 Post、Publish、Submit",
            ],
        },
        "verification": {
            "checks": ["讀取 List in more places 可見候選社團"],
        },
        "external_targets": [listing_id],
        "routing": {"task_type": "browser_readonly"},
    }
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn, body=body, bind_approval=False)
        assert kb.grace_task_facebook_crosspost_inspection_permission(
            conn,
            task_id,
        ) is None
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
        "browser-legacy-readonly",
        {
            "page_identity": page_identity,
            "kanban_task_id": task_id,
            "kanban_run_id": run.current_run_id,
            "refs": {
                "e1": {
                    "role": "button",
                    "name": "More options for Kolin KD-291M06",
                    "backend_node_id": 42,
                    "captured_session_id": "captured-session",
                },
            },
        },
    )

    result = json.loads(
        browser_tool.browser_click("@e1", task_id="browser-legacy-readonly")
    )

    assert result["success"] is False
    assert result["error_code"] == "facebook_readonly_scope_denied"
    assert result["blocker"]["operation"] == "open_more_options"
    assert result["blocker"]["listing_id"] == listing_id
    with kb.connect_closing(db_path) as conn:
        blocker_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "browser_blocker_recorded"
        ]
    assert len(blocker_events) == 1
    assert blocker_events[0].payload == result["blocker"]


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
