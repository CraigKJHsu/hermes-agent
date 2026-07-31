from __future__ import annotations

import pytest

from proactive.loop_contract import LoopContractError, validate_loop_contract
from proactive.grace_task_compiler import render_execution_body, render_review_body


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


def test_facebook_crosspost_guidance_allows_bound_selling_fallback():
    contract = _contract()
    contract["external_targets"] = [
        "https://www.facebook.com/marketplace/item/111/",
        "https://www.facebook.com/groups/222/",
    ]
    contract["facebook_crosspost"] = {
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    body = render_execution_body(contract)

    assert "Marketplace Selling / Your listings" in body
    assert "atomically bound to that same listing ID" in body
    assert "listing-bound direct List in more places" in body
    assert "Do not use Marketplace Selling-list controls" not in body
    assert "Share, Sell Something, Edit, Boost" in body


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
    contract = _contract()
    contract["external_targets"] = [external_target]
    contract["facebook_crosspost"] = {
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    with pytest.raises(LoopContractError, match="facebook_crosspost"):
        validate_loop_contract(contract)


def test_facebook_crosspost_accepts_canonical_matching_display_ids():
    contract = _contract()
    contract["external_targets"] = [
        "Facebook Marketplace item 111 → Facebook Group 222",
    ]
    contract["facebook_crosspost"] = {
        "marketplace_listing_id": "111",
        "group_ids": ["222"],
    }

    validated = validate_loop_contract(contract)

    assert validated["facebook_crosspost"] == contract["facebook_crosspost"]


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
    contract = _contract()
    contract["external_targets"] = external_targets
    contract["facebook_crosspost"] = {
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
        contract = _contract()
        contract["external_targets"] = external_targets
        contract["facebook_crosspost"] = {
            "marketplace_listing_id": "111",
            "group_ids": ["222"],
        }

        with pytest.raises(LoopContractError, match="facebook_crosspost"):
            validate_loop_contract(contract)
