from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from proactive.clawops_intake import (
    auto_publish_preapproved,
    create_clawops_task,
    infer_clawops_metadata,
    resolve_clawops_assignee,
    subscribe_clawops_task,
)
from proactive.hubops_routing import (
    registered_worker_task_types,
    route_clawops_objective,
)


def test_resolve_clawops_assignee_prefers_env(monkeypatch):
    monkeypatch.setenv("HERMES_CLAWOPS_ASSIGNEE", "ops-runtime")

    assert resolve_clawops_assignee({"clawops": {"default_assignee": "config-agent"}}) == "ops-runtime"


def test_resolve_clawops_assignee_falls_back_to_default_profile(monkeypatch):
    monkeypatch.delenv("HERMES_CLAWOPS_ASSIGNEE", raising=False)

    assert resolve_clawops_assignee({}) == "default"


def test_raw_clawops_intake_cannot_create_dispatchable_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_CLAWOPS_ASSIGNEE", "clawops-test")

    with pytest.raises(ValueError, match="Every ClawOps execution"):
        create_clawops_task(
            "verify proactive runtime queue",
            source={"platform": "telegram", "chat_id": "chat-1", "user_id": "kj"},
        )
    assert not db_path.exists()


@pytest.mark.parametrize("backend", ["openclaw", "codex"])
def test_generic_clawops_intake_rejects_unowned_external_backend(backend):
    with pytest.raises(ValueError, match="dedicated start adapter"):
        create_clawops_task(
            "run through an external backend",
            executor_backend=backend,
        )


def test_hubops_routing_selects_dev_worker_from_yaml():
    envelope = route_clawops_objective(
        "修正 Hermes bridge health check",
        project="hub_ops",
        task_type="devops",
        risk_level="low",
    )

    assert envelope["status"] == "routed"
    assert envelope["assignment"]["assigned_worker"] == "clawops.dev"
    assert envelope["assignment"]["approval_required"] is False
    assert envelope["assignment"]["timeout_seconds"] == 1800
    assert envelope["approval_checklist"] == "DevOps and Integration"


def test_hubops_routing_selects_secondhand_agent_and_browser_worker():
    envelope = route_clawops_objective(
        "繼續追加 Facebook 社團群組發佈，再10個",
        project="secondhand_commerce",
        task_type="browser_publish",
        risk_level="medium",
        approved=True,
    )

    assert envelope["status"] == "routed"
    assert envelope["agent_assignment"]["assigned_agent"] == "secondhand_commerce"
    assert envelope["assignment"]["assigned_worker"] == "clawops.browser"
    assert envelope["assignment"]["runtime_profile"] == "clawops-browser"
    assert envelope["assignment"]["risk_level_limit"] == "medium"
    assert envelope["assignment"]["effective_risk_level_limit"] == "medium"
    assert "browser_upload_files" in envelope["assignment"]["allowed_tools"]
    assert envelope["approval_checklist"] == "External Browser Publish"


def test_hubops_routing_blocks_worker_with_missing_required_callable_tools():
    envelope = route_clawops_objective(
        "發布已核准的 Facebook Page 貼文",
        project="hub_ops",
        task_type="facebook_page_api_publish",
        risk_level="medium",
        approved=True,
        runtime_callable_tools={"clawops-ops": {"kanban_show"}},
    )

    assert envelope["status"] == "blocked"
    assert "Runtime capability admission failed" in envelope["blocked_reason"]
    assert "facebook_page_graph_status" in envelope["blocked_reason"]
    assert "facebook_page_graph_publish" in envelope["blocked_reason"]


def test_hubops_routing_admits_worker_with_all_required_callable_tools():
    envelope = route_clawops_objective(
        "發布已核准的 Facebook Page 貼文",
        project="hub_ops",
        task_type="facebook_page_api_publish",
        risk_level="medium",
        approved=True,
        runtime_callable_tools={
            "clawops-ops": {
                "facebook_page_graph_status",
                "facebook_page_graph_publish",
            }
        },
    )

    assert envelope["status"] == "routed"
    assert envelope["assignment"]["required_callable_tools"] == [
        "facebook_page_graph_status",
        "facebook_page_graph_publish",
    ]


def test_hubops_routing_normalizes_listing_aliases_to_browser_publish():
    aliases = (
        "listing",
        "relisting",
        "cross_platform_listing",
        "secondhand_commerce_cross_platform_listing",
        "facebook_existing_listing_group_distribution",
    )

    for alias in aliases:
        envelope = route_clawops_objective(
            "重新刊登二手商品",
            project="secondhand_commerce",
            task_type=alias,
            risk_level="medium",
            approved=True,
        )

        assert envelope["status"] == "routed"
        assert envelope["requested_task_type"] == alias
        assert envelope["task_type"] == "browser_publish"
        assert envelope["assignment"]["assigned_worker"] == "clawops.browser"


def test_hubops_routing_selects_legal_compliance_agent_and_review_worker():
    envelope = route_clawops_objective(
        "請做合約與個資合規風險 briefing，不要提供正式法律意見",
        project="hub_ops",
        task_type="legal_review",
        risk_level="medium",
        approved=True,
    )

    assert envelope["status"] == "routed"
    assert envelope["requested_task_type"] == "legal_review"
    assert envelope["task_type"] == "legal_compliance"
    assert envelope["agent_assignment"]["assigned_agent"] == "legal_compliance"
    assert envelope["agent_assignment"]["role"] == "legal_compliance_research"
    assert envelope["assignment"]["assigned_worker"] == "clawops.review"
    assert envelope["assignment"]["runtime_profile"] == "clawops-review"
    assert envelope["approval_checklist"] == "Legal & Compliance"


def test_hubops_routing_rejects_unknown_task_type_with_registered_choices():
    envelope = route_clawops_objective(
        "重新刊登二手商品",
        project="secondhand_commerce",
        task_type="invented_listing_mode",
        risk_level="medium",
        approved=True,
    )

    assert envelope["status"] == "blocked"
    assert "Unsupported task_type=invented_listing_mode" in envelope["blocked_reason"]
    for task_type in registered_worker_task_types():
        assert task_type in envelope["blocked_reason"]


def test_hubops_routing_requires_contract_fingerprint_above_worker_ceiling():
    envelope = route_clawops_objective(
        "將既有 Facebook listing 分發到已核准社團",
        project="secondhand_commerce",
        task_type="browser_publish",
        risk_level="high",
        approved=True,
    )

    assert envelope["status"] == "blocked"
    assert "single-Loop-Contract authorization is required" in envelope["blocked_reason"]
    assert envelope["assignment"]["risk_level_limit"] == "medium"


def test_hubops_routing_grants_one_validated_browser_contract_without_global_elevation():
    envelope = route_clawops_objective(
        "將既有 Facebook listing 分發到已核准社團",
        project="secondhand_commerce",
        task_type="browser_publish",
        risk_level="high",
        approved=True,
        contract_fingerprint="sha256:test-contract",
    )

    assert envelope["status"] == "routed"
    assert envelope["assignment"]["risk_level_limit"] == "medium"
    assert envelope["assignment"]["effective_risk_level_limit"] == "high"
    assert envelope["risk_authorization"] == {
        "mode": "single_loop_contract",
        "issued_by": "Hermes",
        "contract_fingerprint": "sha256:test-contract",
        "risk_level": "high",
        "human_approved": True,
        "worker_risk_level_limit": "medium",
        "contract_risk_level_limit": "high",
        "effective_risk_level_limit": "high",
        "reusable": False,
    }


def test_hubops_routing_scoped_grant_does_not_bypass_worker_contract_ceiling():
    envelope = route_clawops_objective(
        "變更 production bridge",
        project="hub_ops",
        task_type="devops",
        risk_level="high",
        approved=True,
        contract_fingerprint="sha256:test-contract",
    )

    assert envelope["status"] == "blocked"
    assert "contract_risk_level_limit=medium" in envelope["blocked_reason"]


def test_hubops_routing_browser_contract_grant_never_extends_to_critical():
    envelope = route_clawops_objective(
        "執行 critical browser operation",
        project="secondhand_commerce",
        task_type="browser_publish",
        risk_level="critical",
        approved=True,
        contract_fingerprint="sha256:test-contract",
    )

    assert envelope["status"] == "blocked"
    assert "contract_risk_level_limit=high" in envelope["blocked_reason"]


def test_hubops_routing_blocks_unapproved_high_risk_work():
    envelope = route_clawops_objective(
        "部署 OpenClaw bridge 到 production",
        project="hub_ops",
        task_type="devops",
        risk_level="high",
        approved=False,
    )

    assert envelope["status"] == "blocked"
    assert envelope["assignment"]["assigned_worker"] == "clawops.dev"
    assert "approval" in envelope["blocked_reason"].lower()


def test_raw_low_risk_hubops_intake_still_requires_delegation(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="Every ClawOps execution"):
        create_clawops_task(
            "修正 Hermes bridge health check",
            source={
                "project": "hub_ops",
                "task_type": "devops",
                "risk_level": "low",
                "approved": "false",
            },
        )
    assert not db_path.exists()


def test_facebook_clawops_task_declares_browser_upload_capabilities(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "請繼續 #7 咖啡器材新舊交流團的刊登流程，只允許點 Next，不要送出刊登",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_incomplete_browser_contract_is_rejected_at_final_intake_boundary(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    contract = {
        "goal": {"objective": "上架已核准二手商品"},
        "stop_rules": {"max_iterations": 6},
        "scope": {"allowed": ["使用指定實拍圖"], "forbidden": ["不得變更圖片"]},
    }

    with pytest.raises(ValueError, match="identity.project is required"):
        create_clawops_task(
            "重新上架均質機到蝦皮",
            source={
                "project": "secondhand_commerce",
                "task_type": "browser_publish",
                "risk_level": "medium",
                "approved": "true",
            },
            contract=contract,
        )
    assert not db_path.exists()


def test_facebook_preapproved_copy_task_allows_auto_publish(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "Facebook 社團刊登：之前發佈文案 Hermes 已經傳給我確認過了，後續自動發佈",
            source={
                "platform": "telegram",
                "chat_id": "chat-1",
                "auto_publish_preapproved": "true",
            },
        )
    assert not db_path.exists()


def test_facebook_preapproved_copy_task_uses_browser_capable_runtime_not_openclaw_dry_run(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "繼續追加 Facebook 社團群組發佈，再10個。之前發佈文案 Hermes 已經傳給我確認過；後續自動發佈",
            source={
                "platform": "telegram",
                "chat_id": "chat-1",
                "auto_publish_preapproved": "true",
                "previous_copy_confirmed": "true",
            },
        )
    assert not db_path.exists()


def test_natural_language_secondhand_facebook_publish_routes_to_browser_worker(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "繼續追加 Facebook 社團群組發佈，再10個。之前發佈文案 Hermes 已經傳給 KJ 確認過；後續自動發佈",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_natural_language_secondhand_posting_status_routes_to_browser_ops_worker(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "請列出目前已經有刊登的所有社團群組清單，並顯示相關的狀態",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_secondhand_image_generation_routes_to_content_worker(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "二手望遠鏡拍賣：請生成圖片素材與商品圖草稿，讓我確認後再使用",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_facebook_listing_image_generation_routes_to_content_before_browser(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "我要的是像官方商品圖那種不同角度、同一台 130EQ，適合 Facebook 二手拍賣群組刊登的生成圖",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_course_marketing_image_generation_routes_to_content_worker_and_marketing_operator(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="reserved Grace delegation"):
        create_clawops_task(
            "課程行銷：請生成招生推廣圖片素材與社群圖，交給我確認後再使用",
            source={"platform": "telegram", "chat_id": "chat-1"},
        )
    assert not db_path.exists()


def test_course_marketing_image_generation_routes_to_marketing_operator():
    inferred = infer_clawops_metadata(
        "課程行銷：請生成招生推廣圖片素材與社群圖",
        source={},
    )

    assert inferred["project"] == "course_marketing"
    assert inferred["task_type"] == "campaign"


def test_infer_clawops_metadata_covers_known_project_agents():
    cases = [
        ("請規劃 Hahow 課程大綱", "hahow_course", "course_design"),
        ("請規劃課程招生行銷活動", "course_marketing", "campaign"),
        ("二手咖啡機 Facebook 社團發佈", "secondhand_commerce", "browser_publish"),
        ("請列出目前已經有刊登的所有社團群組清單，並顯示狀態", "secondhand_commerce", "browser_ops"),
        ("二手望遠鏡 生成圖片素材", "secondhand_commerce", "content_draft"),
        ("ingrids SEO 內容規劃", "ingrids_marketing", "product_marketing"),
        ("請建立合約與個資合規風險 briefing", "hub_ops", "legal_compliance"),
        ("修正 OpenClaw bridge health check", "hub_ops", "devops"),
    ]

    for objective, project, task_type in cases:
        inferred = infer_clawops_metadata(objective, source={})
        assert inferred["project"] == project
        assert inferred["task_type"] == task_type


def test_auto_publish_preapproved_requires_explicit_signal():
    assert auto_publish_preapproved("Facebook 社團刊登，文案已確認，請自動發佈") is True
    assert auto_publish_preapproved(
        "Facebook 社團刊登",
        source={"copy_approved": "approved"},
    ) is True
    assert auto_publish_preapproved("Facebook 社團刊登，請先檢查") is False


def test_raw_non_browser_clawops_task_is_also_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with pytest.raises(ValueError, match="Every ClawOps execution"):
        create_clawops_task("summarize local runtime logs")
    assert not db_path.exists()


def test_subscribe_clawops_task_writes_notify_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn, title="watch terminal update path", assignee="clawops-test",
        )

    subscribed = subscribe_clawops_task(
        task_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="thread-1",
        user_id="kj",
        notifier_profile="main",
    )

    with kb.connect_closing(db_path) as conn:
        subs = kb.list_notify_subs(conn, task_id)

    assert subscribed is True
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-1"
    assert subs[0]["thread_id"] == "thread-1"
    assert subs[0]["user_id"] == "kj"
    assert subs[0]["notifier_profile"] == "main"


def test_decomposed_clawops_children_inherit_root_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn, title="coordinate multi-step publish flow", assignee="default",
        )

    subscribed = subscribe_clawops_task(
        task_id,
        platform="telegram",
        chat_id="chat-1",
        user_id="kj",
        notifier_profile="main",
    )
    assert subscribed is True

    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'triage' WHERE id = ?",
            (task_id,),
        )
        child_ids = kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="default",
            children=[
                {"title": "Draft Marketplace listing", "assignee": "default"},
                {
                    "title": "Compile final report",
                    "assignee": "default",
                    "parents": [0],
                },
            ],
            author="auto-decomposer",
        )
        assert child_ids is not None

        root_subs = kb.list_notify_subs(conn, task_id)
        first_child_subs = kb.list_notify_subs(conn, child_ids[0])
        second_child_subs = kb.list_notify_subs(conn, child_ids[1])

    assert len(root_subs) == 1
    assert len(first_child_subs) == 1
    assert len(second_child_subs) == 1
    assert first_child_subs[0]["platform"] == "telegram"
    assert first_child_subs[0]["chat_id"] == "chat-1"
    assert first_child_subs[0]["user_id"] == "kj"
    assert first_child_subs[0]["notifier_profile"] == "main"
    assert second_child_subs[0]["chat_id"] == "chat-1"
