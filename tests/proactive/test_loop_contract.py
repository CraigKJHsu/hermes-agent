from __future__ import annotations

import pytest

from proactive.loop_contract import (
    LoopContractError,
    canonical_facebook_page_url,
    canonicalize_name_bound_facebook_crosspost_targets,
    canonical_marketplace_readonly_sections,
    facebook_crosspost_inspection_listing_id,
    facebook_crosspost_target_ids,
    marketplace_readonly_user_request_listing_id,
    validate_loop_contract,
)
from proactive.grace_task_compiler import (
    contract_execution_skills,
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


def test_facebook_page_post_forbids_browser_for_graph_only_page():
    contract = _contract()
    page_url = "https://www.facebook.com/solobizai"
    contract["routing"] = {"task_type": "browser_publish"}
    contract["external_targets"] = [page_url]
    contract["facebook_page_post"] = {
        "action": "create_post",
        "page_url": page_url,
    }

    with pytest.raises(LoopContractError, match="requires transport=graph_api"):
        validate_loop_contract(contract)
    assert canonical_facebook_page_url(
        "https://www.facebook.com:443/SoloBizAi/"
    ) == page_url
    assert canonical_facebook_page_url(
        "https://www.facebook.com/login"
    ) is None
    assert canonical_facebook_page_url(
        "https://www.facebook.com/anotherpage"
    ) is None

    contract["facebook_page_post"]["page_url"] = (
        "https://www.facebook.com:8443/solobizai"
    )
    with pytest.raises(LoopContractError, match="canonical public"):
        validate_loop_contract(contract)


def test_facebook_page_post_rejects_generic_or_mismatched_action():
    contract = _contract()
    contract["routing"] = {"task_type": "facebook_page_api_publish"}
    contract["external_targets"] = [
        "https://www.facebook.com/solobizai",
    ]
    contract["facebook_page_post"] = {
        "action": "publish",
        "page_url": "https://www.facebook.com/anotherpage",
    }

    with pytest.raises(LoopContractError, match="facebook_page_post"):
        validate_loop_contract(contract)


def test_facebook_marketplace_price_update_binds_exact_listing_and_twd_amount():
    contract = _contract()
    listing_id = "1666446304587399"
    contract["routing"] = {"task_type": "facebook_marketplace_price_update"}
    contract["external_targets"] = [f"Facebook Marketplace item {listing_id}"]
    contract["facebook_marketplace_price_update"] = {
        "action": "update_price",
        "transport": "browser",
        "marketplace_listing_id": listing_id,
        "currency": "TWD",
        "price_twd": 89000,
    }

    validated = validate_loop_contract(contract)
    assert validated["facebook_marketplace_price_update"] == (
        contract["facebook_marketplace_price_update"]
    )

    contract["facebook_marketplace_price_update"]["price_twd"] = "89000"
    with pytest.raises(LoopContractError, match="price_twd"):
        validate_loop_contract(contract)


def test_facebook_page_graph_post_binds_exact_message_and_image_hashes():
    contract = _contract()
    page_url = "https://www.facebook.com/solobizai"
    contract["routing"] = {"task_type": "facebook_page_api_publish"}
    contract["external_targets"] = [page_url]
    contract["facebook_page_post"] = {
        "action": "create_post",
        "page_url": page_url,
        "transport": "graph_api",
        "message_sha256": "a" * 64,
        "image_sha256": "b" * 64,
    }

    validated = validate_loop_contract(contract)
    assert validated["facebook_page_post"]["transport"] == "graph_api"
    execution = render_execution_body(validated)
    assert "facebook_page_graph_publish" in execution
    assert "Do not navigate to Facebook" in execution

    contract["routing"] = {"task_type": "browser_publish"}
    with pytest.raises(
        LoopContractError,
        match="requires routing.task_type=facebook_page_api_publish",
    ):
        validate_loop_contract(contract)

    contract["routing"] = {"task_type": "facebook_page_api_publish"}
    contract["facebook_page_post"]["message_sha256"] = "A" * 64
    with pytest.raises(LoopContractError, match="lowercase SHA-256"):
        validate_loop_contract(contract)


def test_facebook_page_graph_post_rejects_unbound_extra_fields():
    contract = _contract()
    page_url = "https://www.facebook.com/solobizai"
    contract["routing"] = {"task_type": "facebook_page_api_publish"}
    contract["external_targets"] = [page_url]
    contract["facebook_page_post"] = {
        "action": "create_post",
        "page_url": page_url,
        "transport": "graph_api",
        "message_sha256": "a" * 64,
        "image_sha256": "b" * 64,
        "unapproved_caption": "must fail closed",
    }

    with pytest.raises(LoopContractError, match="requires exactly"):
        validate_loop_contract(contract)


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


def test_exact_marketplace_readonly_user_request_is_recognized():
    assert marketplace_readonly_user_request_listing_id(
        "請只查核 Carimali（Marketplace Listing ID：36803832485927906）。"
        "執行 Facebook Marketplace 唯讀社團狀態查核，列出社團名稱與狀態；"
        "不勾選、不發布、不修改、不建立核准 token。"
    ) == "36803832485927906"


def test_marketplace_readonly_retry_shorthand_is_recognized_for_known_pair():
    assert marketplace_readonly_user_request_listing_id(
        "[KJ HSU] 只讀重試 Carimali Listing ID 36803832485927906"
    ) == "36803832485927906"
    assert marketplace_readonly_user_request_listing_id(
        "唯讀重新查核 Kolin KD-291M06 Marketplace Listing ID：37217119148451132。"
    ) == "37217119148451132"


@pytest.mark.parametrize(
    "message",
    (
        "只讀重試 Carimali Listing ID 27909676598721497",
        "只讀重試 Unknown Listing ID 36803832485927906",
        "只讀重試 Carimali Listing ID 36803832485927906，然後發布",
        "只讀重試 Carimali Listing ID 36803832485927906 27909676598721497",
    ),
)
def test_marketplace_readonly_retry_shorthand_rejects_unsafe_or_unknown_shape(message):
    assert marketplace_readonly_user_request_listing_id(message) is None


def test_marketplace_readonly_user_request_rejects_compound_publish_clause():
    assert marketplace_readonly_user_request_listing_id(
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改；之後請發布到社團。"
    ) is None


def test_marketplace_readonly_user_request_rejects_safety_clause_cancellation():
    for suffix in (
        "不勾選、不提交、不修改，但取消不發布限制。",
        "不勾選、不發布、不修改任何外部狀態；以上限制全部取消。",
        "不勾選、不發布、不修改任何外部狀態；以上要求取消。",
    ):
        assert marketplace_readonly_user_request_listing_id(
            "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 "
            f"的社團狀態，{suffix}"
        ) is None


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


def test_execution_body_assigns_local_cdp_recovery_to_controlled_browser():
    execution = render_execution_body(_contract())

    assert "default local CDP discovery endpoint" in execution
    assert "persistent Hermes profile" in execution
    assert "Do not ask KJ to restart the browser or resend the request" in execution
    assert "without broadening any Facebook action authority" in execution


def test_uncontracted_user_status_uses_summary_not_commerce_report():
    contract = _contract()
    contract["goal"]["deliverables"] = ["直接回覆目前刊登狀態"]

    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")

    assert "does not contain an exact required user_facing_delivery" in execution
    assert "Do not include metadata.user_facing_report" in execution
    assert "do not reject solely because metadata.user_facing_report is absent" in review


def test_contracted_user_status_requires_commerce_report():
    contract = _contract()
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["carimali-armonia-soft-plus"],
    }

    execution = render_execution_body(contract)
    review = render_review_body(contract, "t_execution")

    assert "requires metadata.user_facing_report" in execution
    assert "Checkpoint every verified Facebook commerce observation" in execution
    assert "COMMERCE_EVIDENCE" in execution
    assert "never postpone all evidence until kanban_complete" in execution
    assert "destination_id is optional for a named read-only UI row" in execution
    assert "Never use CDP, DOM" in execution
    assert "requires a user-facing delivery" in review


def test_secondhand_delivery_subject_aliases_are_canonicalized():
    contract = _contract()
    contract["routing"] = {
        "task_type": "secondhand_commerce_group_status",
    }
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["Carimali", "36803832485927906"],
    }
    contract["external_targets"] = [
        "Facebook Marketplace listing ID 36803832485927906",
    ]

    normalized = validate_loop_contract(contract)

    assert normalized["user_facing_delivery"]["subject_keys"] == [
        "carimali-armonia-soft-plus",
    ]


def test_secondhand_delivery_rejects_malformed_or_conflicting_scope():
    contract = _contract()
    contract["routing"] = {
        "task_type": "secondhand_commerce_group_status",
    }
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["Carimali"],
    }
    contract["external_targets"] = [
        "Facebook Marketplace item 36803832485927906 -> Facebook Group nope",
    ]

    with pytest.raises(LoopContractError, match="canonical Marketplace"):
        validate_loop_contract(contract)

    contract["external_targets"] = []
    contract["user_facing_delivery"]["subject_keys"] = [
        "Carimali",
        "Celestron",
    ]
    with pytest.raises(LoopContractError, match="exactly one known canonical"):
        validate_loop_contract(contract)


@pytest.mark.parametrize(
    "external_target",
    [
        "Facebook Marketplace item 999 → Facebook Group 222",
        "Facebook Marketplace item 111 → Facebook Group 999",
    ],
)
def test_facebook_crosspost_requires_canonical_matching_display_ids(
    external_target,
):
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = [external_target]
    contract["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    with pytest.raises(LoopContractError, match="facebook_crosspost"):
        validate_loop_contract(contract)


def test_facebook_crosspost_accepts_canonical_matching_display_ids():
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = [
        "Facebook Marketplace item 111 → Facebook Group 222",
    ]
    contract["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    validated = validate_loop_contract(contract)

    assert validated["facebook_crosspost"] == contract["facebook_crosspost"]


def test_facebook_crosspost_uses_dedicated_browser_transport_not_graph_api():
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = [
        "Facebook Marketplace item 111",
        "Facebook Group 222",
    ]
    contract["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    validated = validate_loop_contract(contract)
    assert validated["facebook_crosspost"]["transport"] == "browser"

    contract["facebook_crosspost"]["transport"] = "graph_api"
    with pytest.raises(
        LoopContractError,
        match="Meta removed publish_to_groups",
    ):
        validate_loop_contract(contract)

    contract["facebook_crosspost"]["transport"] = "browser"
    contract["routing"]["task_type"] = "browser_publish"
    with pytest.raises(
        LoopContractError,
        match="facebook_marketplace_group_publish",
    ):
        validate_loop_contract(contract)

    contract["routing"]["task_type"] = "facebook_marketplace_group_publish"
    contract["facebook_crosspost"]["marketplace_listing_id"] = "１１１"
    with pytest.raises(LoopContractError, match="numeric string"):
        validate_loop_contract(contract)


def test_facebook_crosspost_accepts_exact_named_destinations():
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = [
        "Facebook Marketplace item 111",
        "Facebook group name: 咖啡器材買賣維修社團",
        "Facebook group name: 二手新舊咖啡設備大賣場",
    ]
    contract["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_names": [
            "咖啡器材買賣維修社團",
            "二手新舊咖啡設備大賣場",
        ],
    }

    validated = validate_loop_contract(contract)

    assert validated["facebook_crosspost"] == contract["facebook_crosspost"]


def test_name_bound_crosspost_canonicalizes_plain_grace_targets():
    crosspost = {
        "transport": "browser",
        "marketplace_listing_id": "36803832485927906",
        "group_names": [
            "咖啡器材買賣維修社團",
            "二手新舊咖啡設備大賣場",
        ],
    }

    canonical = canonicalize_name_bound_facebook_crosspost_targets(
        [
            "36803832485927906",
            "咖啡器材買賣維修社團",
            "二手新舊咖啡設備大賣場",
        ],
        crosspost,
    )

    assert canonical == [
        "Facebook Marketplace item 36803832485927906",
        "Facebook group name: 咖啡器材買賣維修社團",
        "Facebook group name: 二手新舊咖啡設備大賣場",
    ]
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = canonical
    contract["facebook_crosspost"] = crosspost
    assert validate_loop_contract(contract)["external_targets"] == canonical


def test_name_bound_crosspost_canonicalizes_grace_group_label_aliases():
    crosspost = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_names": ["社團甲", "社團乙"],
    }

    assert canonicalize_name_bound_facebook_crosspost_targets(
        [
            "Facebook Marketplace listing ID 111",
            "Facebook group: 社團甲",
            "Facebook 社團：社團乙",
        ],
        crosspost,
    ) == [
        "Facebook Marketplace item 111",
        "Facebook group name: 社團甲",
        "Facebook group name: 社團乙",
    ]


@pytest.mark.parametrize(
    "targets",
    [
        ["111", "社團甲"],
        ["111", "社團甲", "社團乙", "未核准社團"],
        ["111", "社團甲", "社團甲", "社團乙"],
        ["999", "社團甲", "社團乙"],
    ],
)
def test_name_bound_crosspost_rejects_incomplete_or_conflicting_targets(targets):
    with pytest.raises(LoopContractError, match="name-bound facebook_crosspost"):
        canonicalize_name_bound_facebook_crosspost_targets(
            targets,
            {
                "transport": "browser",
                "marketplace_listing_id": "111",
                "group_names": ["社團甲", "社團乙"],
            },
        )


@pytest.mark.parametrize(
    "crosspost",
    [
        {
            "marketplace_listing_id": "111",
            "group_ids": ["222"],
            "group_names": ["二手新舊咖啡設備大賣場"],
        },
        {"marketplace_listing_id": "111", "group_names": [" 名稱有多餘空白 "]},
        {"marketplace_listing_id": "111", "group_names": ["同名", "同名"]},
    ],
)
def test_facebook_crosspost_rejects_ambiguous_or_nonexact_named_scope(crosspost):
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = [
        "Facebook Marketplace item 111",
        "Facebook group name: 二手新舊咖啡設備大賣場",
        "Facebook group name: 同名",
    ]
    contract["facebook_crosspost"] = crosspost

    with pytest.raises(LoopContractError, match="facebook_crosspost"):
        validate_loop_contract(contract)


@pytest.mark.parametrize(
    "external_targets",
    [
        [
            "https://www.facebook.com/marketplace/item/111/",
            "https://www.facebook.com/groups/222/",
        ],
        [
            "Facebook Marketplace listing ID: 111",
            "Facebook group ID: 222",
        ],
        [
            "Facebook 市集項目 111 → 社團 "
            "https://www.facebook.com/groups/222/",
        ],
        [
            "facebook.com/marketplace/item/111",
            "facebook.com/groups/222",
        ],
        ["facebook:marketplace:111", "facebook:group:222"],
        ["Facebook Marketplace item 111.", "Facebook 群組 222）"],
        ["facebook.com/marketplace/item/111.", "facebook:group:222!"],
        ['"Facebook Marketplace item 111."', "(Facebook Group 222.)"],
    ],
)
def test_facebook_crosspost_accepts_explicit_url_and_id_labels(
    external_targets,
):
    contract = _facebook_crosspost_contract()
    contract["external_targets"] = external_targets
    contract["facebook_crosspost"] = {
        "transport": "browser",
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    validated = validate_loop_contract(contract)

    assert validated["facebook_crosspost"] == contract["facebook_crosspost"]


def test_facebook_crosspost_rejects_embedded_lookalike_hosts_and_urns():
    bad_targets = [
        [
            "https://evil.example/facebook.com/marketplace/item/111",
            "https://evil.example/facebook.com/groups/222",
        ],
        [
            "https://evil.example/path;facebook.com/marketplace/item/111",
            "https://evil.example/path;facebook.com/groups/222",
        ],
        ["facebook.com/marketplace/item/111_suffix", "facebook:group:222-x"],
        ["evilfacebook.com/marketplace/item/111", "notfacebook:group:222"],
    ]
    for external_targets in bad_targets:
        contract = _facebook_crosspost_contract()
        contract["external_targets"] = external_targets
        contract["facebook_crosspost"] = {
            "transport": "browser",
            "marketplace_listing_id": "111",
            "group_ids": ["222"],
        }

        with pytest.raises(LoopContractError, match="facebook_crosspost"):
            validate_loop_contract(contract)


def _facebook_crosspost_contract():
    contract = _contract()
    contract["routing"] = {
        "task_type": "facebook_marketplace_group_publish",
    }
    return contract


def _readonly_marketplace_inspection_contract():
    contract = _contract()
    contract["routing"] = {
        "task_type": "secondhand_commerce_group_status",
    }
    contract["external_targets"] = [
        "Facebook Marketplace item 111",
    ]
    contract.update(canonical_marketplace_readonly_sections("111"))
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["facebook_marketplace:111"],
    }
    return contract


def test_readonly_marketplace_inspection_accepts_one_exact_listing():
    contract = _readonly_marketplace_inspection_contract()

    validated = validate_loop_contract(contract)

    assert validated["external_targets"] == [
        "Facebook Marketplace item 111",
    ]
    assert facebook_crosspost_target_ids(
        ["facebook:marketplace_listing:111"],
    )[0] == frozenset({"111"})


def test_canonical_group_status_audit_requires_actual_group_evidence():
    contract = _readonly_marketplace_inspection_contract()

    validated = validate_loop_contract(contract)
    allowed = " ".join(validated["scope"]["allowed"])
    checks = " ".join(validated["verification"]["checks"])
    evidence = " ".join(validated["verification"]["evidence_required"])

    assert "seller group contribution pages" in allowed
    assert "is not publication proof" in allowed
    assert "every durable-ledger destination" in checks
    assert "Exact group page URL" in evidence


def test_group_status_contract_can_request_readonly_marketplace_inspection():
    contract = _readonly_marketplace_inspection_contract()
    listing_id = "36803832485927906"
    contract["routing"] = {
        "task_type": "secondhand_commerce_group_status",
    }
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["carimali-armonia-soft-plus"],
    }
    contract["external_targets"] = [
        f"facebook:marketplace:{listing_id}",
    ]
    contract.update(canonical_marketplace_readonly_sections(listing_id))
    contract["user_facing_delivery"]["subject_keys"] = [
        f"facebook_marketplace:{listing_id}",
    ]
    contract["scope"]["allowed"] = [
        "唯讀開啟 Facebook Marketplace listing "
        f"{listing_id} 的 More options → List in more places，"
        "只讀取可見社團名稱與狀態",
    ]

    validated = validate_loop_contract(contract)

    assert facebook_crosspost_inspection_listing_id(validated) == listing_id


def test_resolved_marketplace_readonly_route_keeps_listing_inspection_authority():
    contract = _readonly_marketplace_inspection_contract()
    listing_id = "27700220586305145"
    contract["routing"] = {"task_type": "facebook_marketplace_readonly"}
    contract["external_targets"] = [
        f"facebook:marketplace-group-status:{listing_id}",
    ]
    contract.update(canonical_marketplace_readonly_sections(listing_id))
    contract["user_facing_delivery"]["subject_keys"] = [
        f"facebook_marketplace:{listing_id}",
    ]
    contract["scope"]["allowed"] = [
        "唯讀開啟 Facebook Marketplace listing "
        f"{listing_id} 的 More options → List in more places，"
        "只讀取可見社團名稱與狀態",
    ]

    validated = validate_loop_contract(contract)

    assert facebook_crosspost_inspection_listing_id(validated) == listing_id


def test_readonly_marketplace_inspection_rejects_split_transition_fragments():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"] = [
        "唯讀開啟 Facebook Marketplace listing 111 的 More options",
        "唯讀開啟 List in more places 並讀取社團名稱",
    ]

    with pytest.raises(LoopContractError, match="same listing id"):
        validate_loop_contract(contract)


@pytest.mark.parametrize(
    "allowed_transition",
    [
        "Read-only Marketplace listing 111: List in more places → More options",
        "Read-only Marketplace listing 111: do not use More options; "
        "open List in more places directly",
        "Read-only Facebook Marketplace listing 111: no More options → "
        "List in more places",
        "Read-only Facebook Marketplace listing 111: cannot open More options → "
        "List in more places",
        "唯讀 Marketplace listing 111：不要開啟更多選項，"
        "直接刊登到更多地方",
    ],
)
def test_readonly_marketplace_inspection_rejects_reversed_or_negated_transition(
    allowed_transition,
):
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"] = [allowed_transition]

    with pytest.raises(LoopContractError, match="same listing id"):
        validate_loop_contract(contract)


@pytest.mark.parametrize(
    ("allowed_transition", "forbidden"),
    [
        (
            "Read-only Facebook Marketplace listing 111: open More options → "
            "List in more places and read visible destination names/status only",
            [
                "Do not select any option",
                "Do not click Post or Publish",
                "Do not change external state",
            ],
        ),
        (
            "唯讀開啟 Facebook Marketplace listing 111 的更多選項 → "
            "刊登到更多地方，只讀取可見社團名稱與狀態",
            ["不得勾選任何社團", "不得發布", "不得變更任何外部狀態"],
        ),
    ],
)
def test_readonly_marketplace_inspection_accepts_supported_ui_labels_and_split_bans(
    allowed_transition,
    forbidden,
):
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"] = [allowed_transition]
    contract["scope"]["forbidden"] = forbidden

    assert validate_loop_contract(contract)["external_targets"] == [
        "Facebook Marketplace item 111",
    ]


@pytest.mark.parametrize(
    "external_targets",
    [
        [],
        [
            "Facebook Marketplace item 111",
            "Facebook Marketplace item 222",
        ],
        [
            "Facebook Marketplace item 111",
            "Facebook Group 333",
        ],
    ],
)
def test_readonly_marketplace_inspection_rejects_unbound_or_multi_target_scope(
    external_targets,
):
    contract = _readonly_marketplace_inspection_contract()
    if external_targets:
        contract["external_targets"] = external_targets
    else:
        contract.pop("external_targets")

    with pytest.raises(LoopContractError, match="split multiple listings"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_extra_unrecognized_target():
    contract = _readonly_marketplace_inspection_contract()
    contract["external_targets"].append("Shopee Seller Center")

    with pytest.raises(LoopContractError, match="multiple listings"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_mutating_allowed_scope():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"].append("Share 或 Edit 這筆刊登")

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_publish_allowed_scope():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"].append("Publish the listing after inspection")

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_mutating_goal_intent():
    contract = _readonly_marketplace_inspection_contract()
    contract["goal"]["objective"] = (
        "Publish this listing after reading the destination status"
    )

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_embedded_localized_republish():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"].append("重新刊登到更多地方")

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_any_second_allowed_action():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"].append(
        "Open List in more places directly",
    )

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_other_listing_in_goal():
    contract = _readonly_marketplace_inspection_contract()
    contract["goal"]["objective"] = (
        "Read Facebook Marketplace listing 222 destination status"
    )

    with pytest.raises(LoopContractError, match="same listing id"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_detached_listing_transition():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"] = [
        "唯讀 Facebook Marketplace listing 111",
        "唯讀 Facebook Marketplace listing 222 的 More options → "
        "List in more places",
    ]

    with pytest.raises(LoopContractError, match="no mutation action"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_requires_explicit_no_submit_scope():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["forbidden"] = ["不得修改商品"]

    with pytest.raises(LoopContractError, match="checkbox selection"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_does_not_treat_menu_options_as_selection_ban():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["forbidden"] = [
        "Do not open More options",
        "Do not click Post or Publish",
    ]

    with pytest.raises(LoopContractError, match="checkbox selection"):
        validate_loop_contract(contract)


def test_readonly_marketplace_inspection_rejects_generic_options_wording():
    contract = _readonly_marketplace_inspection_contract()
    contract["scope"]["allowed"] = [
        "Read-only Facebook Marketplace listing 111: inspect delivery "
        "options and the List in more places label",
    ]

    with pytest.raises(LoopContractError, match="More options"):
        validate_loop_contract(contract)
