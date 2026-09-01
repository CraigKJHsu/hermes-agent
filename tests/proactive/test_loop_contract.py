from __future__ import annotations

import pytest

from proactive.loop_contract import (
    LoopContractError,
    browser_readonly_marketplace_fallback_listing_id,
    facebook_group_publish_destination_ids,
    is_internal_only_target,
    validate_loop_contract,
)
from proactive.grace_task_compiler import (
    contract_execution_skills,
    contract_declares_page_hero,
    render_execution_body,
    render_review_body,
)


def _contract():
    return {
        "identity": {
            "project": "ingrids_marketing",
            "topic_name": "ingrids.app",
            "thread_id": "270",
            "request_instance_id": "gri_test",
        },
        "original_request": "請執行下一步",
        "grace_interpretation": "完成已定義的 Lighthouse 下一步，不跨到其他專案",
        "trigger": "KJ 明確要求執行",
        "completion_mode": "terminal",
        "goal": {"objective": "完成 Lighthouse 文件核對", "deliverables": ["核對報告"], "non_goals": ["不發布"]},
        "scope": {"allowed": ["Project Lighthouse 文件"], "forbidden": ["二手拍賣"]},
        "verification": {"checks": ["逐檔核對"], "evidence_required": ["檔案路徑"], "acceptance_criteria": ["無跨專案內容"]},
        "stop_rules": {"success": ["證據齊全"], "blocked": ["需要批准"], "no_progress": ["同錯誤兩次"], "max_iterations": 6, "max_runtime_seconds": 1800},
        "memory": {"namespace": "topic:270/ingrids", "working": ["本次核對狀態"], "promote_on_acceptance": ["已驗證結論"]},
    }


def test_complete_loop_contract_is_accepted():
    assert validate_loop_contract(_contract())["contract_version"] == "1.0"


def test_contract_can_explicitly_disable_durable_memory_promotion():
    contract = _contract()
    contract["memory"]["promote_on_acceptance"] = []

    accepted = validate_loop_contract(contract)

    assert accepted["memory"]["promote_on_acceptance"] == []


def test_content_package_user_facing_delivery_is_accepted():
    contract = _contract()
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "asset_filenames": ["page_hero.png", "audio_brief.png"],
    }

    accepted = validate_loop_contract(contract)

    assert accepted["user_facing_delivery"]["kind"] == "content_package"


def test_content_package_user_facing_delivery_requires_assets():
    contract = _contract()
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_with_attachment",
    }

    with pytest.raises(LoopContractError, match="asset_filenames"):
        validate_loop_contract(contract)


def test_facebook_group_publish_requires_canonical_url_per_group():
    contract = _contract()
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496",
        "https://www.facebook.com/groups/897927458651235",
    ]
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "management_listing_id": "915975414881937",
        "destinations": [
            {
                "group_id": "897927458651235",
                "canonical_name": "二手家具 家電 買賣",
                "canonical_url": "https://www.facebook.com/groups/897927458651235",
            }
        ],
    }

    accepted = validate_loop_contract(contract)

    assert facebook_group_publish_destination_ids(accepted) == {"897927458651235"}


def test_facebook_group_publish_rejects_chooser_only_or_mismatched_identity():
    contract = _contract()
    contract["external_targets"] = ["group:897927458651235"]
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "destinations": [
            {
                "group_id": "897927458651235",
                "canonical_name": "二手家具 家電 買賣",
                "canonical_url": "https://www.facebook.com/groups/123",
            }
        ],
    }

    with pytest.raises(LoopContractError, match="canonical_url must match group_id"):
        validate_loop_contract(contract)

    contract["facebook_group_publish"]["destinations"][0]["canonical_url"] = (
        "https://www.facebook.com/groups/897927458651235"
    )
    contract["external_targets"] = ["Facebook chooser row: 二手家具 家電 買賣"]

    with pytest.raises(LoopContractError, match="must also appear in external_targets"):
        validate_loop_contract(contract)


def test_internal_only_target_accepts_explicit_zh_no_action_scope():
    assert is_internal_only_target(
        "Gemini Notebook（僅產出貼入用 Prompt，不登入或操作）"
    )
    assert is_internal_only_target(
        "Podcast Hosting／Apple Podcasts（僅產出 Title 與 Description，不上架或操作）"
    )
    assert is_internal_only_target(
        "Spotify／Podcast Hosting（僅產出可貼入 Description 與精簡 Instructions 規則，不登入或上架）"
    )
    assert is_internal_only_target(
        "Facebook Page（僅校正內部文案與主圖資料，不登入、編輯或發布）"
    )


def test_internal_only_target_accepts_internal_zero_effect_sentinel():
    assert is_internal_only_target(
        "Internal Topic asset revision only - zero Facebook, Meta, Spotify, "
        "Gemini Notebook, or other external platform action"
    )
    assert not is_internal_only_target(
        "Topic asset revision with zero external platform action"
    )
    assert not is_internal_only_target(
        "Internal campaign with zero delay before external platform action"
    )
    assert not is_internal_only_target(
        "Internal campaign with non-zero external platform action"
    )


def test_internal_only_target_rejects_platform_target_without_both_guards():
    assert not is_internal_only_target("Facebook Page（修訂貼文並發布）")
    assert not is_internal_only_target("Gemini Notebook（不登入或操作）")
    assert not is_internal_only_target("Facebook Page（僅修訂貼文文案）")
    assert not is_internal_only_target("Facebook Page（僅校正內部文案並發布）")


def test_browser_readonly_marketplace_fallback_requires_complete_zero_effect_scope():
    listing_id = "915975414881937"
    contract = {
        "task_type": "browser_readonly",
        "external_targets": [listing_id],
        "goal": {
            "objective": (
                "只讀檢視 Facebook Marketplace listing "
                f"{listing_id} 的 More options → List in more places"
            ),
            "deliverables": ["可見候選社團名稱與狀態"],
        },
        "scope": {
            "allowed": [
                "只讀檢視 Facebook Marketplace listing "
                f"{listing_id} 的 More options → List in more places"
            ],
            "forbidden": [
                "不勾選 checkbox",
                "不按 Post、Publish 或 Submit",
                "不變更任何外部狀態",
            ],
        },
        "verification": {"checks": ["讀取 List in more places 候選清單"]},
    }

    assert browser_readonly_marketplace_fallback_listing_id(contract) == listing_id

    contract["scope"]["forbidden"] = ["不變更任何外部狀態"]
    assert browser_readonly_marketplace_fallback_listing_id(contract) is None

    contract["scope"]["forbidden"] = [
        "不勾選 checkbox",
        "不按 Post、Publish 或 Submit",
        "不變更任何外部狀態",
    ]
    contract["facebook_crosspost"] = {
        "marketplace_listing_id": listing_id,
        "group_ids": ["123"],
    }
    assert browser_readonly_marketplace_fallback_listing_id(contract) is None


def test_kj_profile_traditional_chinese_writing_loads_human_polish_skill():
    contract = _contract()
    contract["identity"]["project"] = "kj_profile"
    contract["goal"]["objective"] = "以最新英文 Resume 生成完整正體中文 Resume"

    assert contract_execution_skills(contract) == ["speak-human-tw"]
    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")
    assert "automatic_post_run_summary" in execution
    assert "language_polish_summary" in review
    assert "must not become achieved" in review


def test_review_body_explains_canonical_verdict_for_fail_closed_parent():
    review = render_review_body(_contract(), "t_execution")

    assert "metadata review_outcome=accepted" in review
    assert "Do not set approved=false" in review
    assert "review_outcome=blocked" in review
    assert "parent_verdict" in review


def test_facebook_group_publish_body_forbids_chooser_identity():
    contract = _contract()
    contract["external_targets"] = ["group:897927458651235"]
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "destinations": [
            {
                "group_id": "897927458651235",
                "canonical_name": "二手家具 家電 買賣",
                "canonical_url": "https://www.facebook.com/groups/897927458651235",
            }
        ],
    }

    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")

    assert "canonical_url_per_group" in execution
    assert "Do not use Marketplace 'List in more places' chooser rows" in execution
    assert "canonical_url_per_group" in review


def test_text_only_review_body_does_not_require_page_hero_visual_review():
    review = render_review_body(_contract(), "t_execution")

    assert contract_declares_page_hero(_contract()) is False
    assert "This contract does not declare asset_family=page_hero" in review
    assert "Do not invent page_hero" in review
    assert "For an accepted asset_family=page_hero review" not in review


def test_page_hero_review_body_keeps_visual_review_gate():
    contract = _contract()
    contract["asset_family"] = "page_hero"

    review = render_review_body(contract, "t_execution")

    assert contract_declares_page_hero(contract) is True
    assert "For an accepted asset_family=page_hero review" in review
    assert "visual_review.all_required_text_readable=true" in review


def test_loop_contract_bodies_include_evidence_first_answering_gate():
    execution = render_execution_body(_contract())
    review = render_review_body(_contract(), "t_execution")

    for body in (execution, review):
        assert "Trusted evidence-first answering gate" in body
        assert "structured Kanban ledgers/registries" in body
        assert "task_runs, task_events, task_external_effects" in body
        assert "Mem0/QMD/session_search as recall or discovery aids" in body
        assert "historical verified, current live verified" in body
        assert "verified/not verified" in body


def test_ai_bizweek_guidance_uses_operational_readiness_evidence():
    contract = _contract()
    contract["identity"]["project"] = "ai_bizweek"
    contract["goal"]["objective"] = "產製 Carter's Junk Away EP04 完整發布包"
    contract["policy_snapshots"] = [
        {
            "policy_id": "ai-bizweek-page-hero",
            "version": "2026-08-28.2",
        }
    ]

    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")

    assert "managed_policy_read.operational_readiness_evidence when available" in execution
    assert "managed_policy_read.operational_readiness_evidence when available" in review
    assert "equivalent embedded evidence is present" in execution
    assert "do not ask KJ to provide t_70bf2afe evidence" in execution
    assert "do not treat obsolete stale-review tasks as current blockers" in review
    assert "managed_policy_read.content_source_evidence.available=true or equivalent embedded" in execution
    assert "facebook_page_source_text" in review
    assert "Do not ask KJ to repost the same source text" in execution


@pytest.mark.parametrize(
    "project, objective",
    [
        ("kj_profile", "生成最新英文 Resume，並與現有中文 Resume 比對"),
        ("kj_profile", "修訂 KJ Profile 的 Resume 資料來源規則"),
        ("course_marketing", "生成正體中文課程文案"),
    ],
)
def test_human_polish_skill_does_not_leak_to_other_contracts(project, objective):
    contract = _contract()
    contract["identity"]["project"] = project
    contract["goal"]["objective"] = objective

    assert contract_execution_skills(contract) == []


def test_non_topic_lane_accepts_empty_thread_id():
    contract = _contract()
    contract["identity"]["thread_id"] = ""

    assert validate_loop_contract(contract)["identity"]["thread_id"] == ""


def test_missing_verification_and_stop_rules_fail_closed():
    contract = _contract()
    contract["verification"]["checks"] = []
    contract["stop_rules"]["max_iterations"] = 0
    with pytest.raises(LoopContractError) as exc:
        validate_loop_contract(contract)
    assert "verification.checks" in str(exc.value)
    assert "max_iterations" in str(exc.value)


def test_completion_mode_is_required_and_explicit():
    contract = _contract()
    contract.pop("completion_mode")
    with pytest.raises(LoopContractError, match="completion_mode"):
        validate_loop_contract(contract)

    contract["completion_mode"] = "sometimes"
    with pytest.raises(LoopContractError, match="terminal or intermediate"):
        validate_loop_contract(contract)


def test_user_facing_delivery_is_explicit_and_fail_closed():
    contract = _contract()
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["carimali", "kolin", "celestron"],
    }
    normalized = validate_loop_contract(contract)
    assert normalized["user_facing_delivery"]["required"] is True

    contract["user_facing_delivery"]["subject_keys"] = []
    with pytest.raises(LoopContractError, match="subject_keys"):
        validate_loop_contract(contract)


def test_commerce_group_status_route_requires_delivery_contract():
    contract = _contract()
    contract["routing"] = {
        "task_type": "secondhand_commerce_group_status",
    }
    with pytest.raises(LoopContractError, match="requires user_facing_delivery"):
        validate_loop_contract(contract)

    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["carimali", "kolin", "celestron"],
    }
    assert validate_loop_contract(contract)["user_facing_delivery"]["required"]


@pytest.mark.parametrize("invalid_item", [{}, 0, ["nested"], ""])
def test_required_contract_lists_accept_only_nonempty_strings(invalid_item):
    contract = _contract()
    contract["goal"]["deliverables"] = [invalid_item]

    with pytest.raises(LoopContractError, match="only non-empty strings"):
        validate_loop_contract(contract)


def test_execution_body_fails_closed_for_malformed_scoped_authorization():
    contract = _contract()
    contract["authorization"] = {
        "mode": "single_loop_contract",
        "risk_level": "high",
        "worker_risk_level_limit": "medium",
        "contract_risk_level_limit": "high",
        "reusable": False,
    }

    body = render_execution_body(contract)

    assert "Scoped authorization is malformed" in body
    assert "effective_risk_level_limit=high" not in body


def test_execution_body_makes_scoped_effective_limit_authoritative():
    contract = _contract()
    contract["authorization"] = {
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

    body = render_execution_body(contract)

    assert "Scoped authorization decision (authoritative)" in body
    assert "effective_risk_level_limit=high" in body
    assert "does not override the scoped effective limit" in body


def test_grace_bodies_require_durable_external_effect_handoff():
    contract = _contract()

    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")

    assert "kanban_external_effect" in execution
    assert "metadata.external_effects" in execution
    assert "all cumulative evidence" in review
    assert "external-effect ledger" in review
