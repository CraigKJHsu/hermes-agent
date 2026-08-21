from __future__ import annotations

import time

from hermes_cli.user_facing_report import (
    canonicalize_commerce_subject_keys,
    commerce_subject_listing_ids,
    normalize_user_facing_report,
    render_user_facing_report_chunks,
    report_matches_user_facing_delivery,
    user_facing_report_digest,
)


def _partial_carimali_report() -> dict:
    now = int(time.time())
    return {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": False,
        "as_of": "2026-08-05T14:48:56Z",
        "observed_at": now,
        "rows": [],
        "coverage": [{
            "subject_key": "carimali-armonia-soft-plus",
            "subject_label": "Carimali Armonia Soft Plus",
            "complete": False,
            "named_count": 0,
            "gap_count": None,
            "expected_total": None,
            "note": "Controlled Facebook UI was unavailable.",
        }],
    }


def test_known_product_label_and_listing_id_collapse_to_one_subject():
    assert canonicalize_commerce_subject_keys([
        "Carimali",
        "facebook_marketplace:36803832485927906",
    ]) == ["carimali-armonia-soft-plus"]


def test_replacement_listing_id_keeps_the_stable_kolin_subject():
    assert canonicalize_commerce_subject_keys([
        "facebook_marketplace:37217119148451132",
        "facebook_marketplace:915975414881937",
    ]) == ["kolin-kd291m06"]
    assert commerce_subject_listing_ids("kolin-kd291m06") == frozenset({
        "37217119148451132",
        "915975414881937",
    })


def test_partial_report_matches_delivery_contract_written_with_aliases():
    contract = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["Carimali", "36803832485927906"],
    }

    assert report_matches_user_facing_delivery(
        _partial_carimali_report(),
        contract,
    )


def test_unknown_subject_alias_does_not_broaden_delivery_contract():
    contract = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["another-product"],
    }

    assert not report_matches_user_facing_delivery(
        _partial_carimali_report(),
        contract,
    )


def test_report_structure_canonicalizes_row_and_coverage_aliases():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "36803832485927906",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "260697590957215",
        "destination_name": "咖啡器材買賣維修社團",
        "status": "unknown",
        "status_label": "尚未驗證",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Controlled Facebook UI was unavailable.",
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0]["subject_key"] = "Carimali"
    report["coverage"][0]["named_count"] = 1

    normalized = normalize_user_facing_report(report)

    assert normalized["rows"][0]["subject_key"] == (
        "carimali-armonia-soft-plus"
    )
    assert normalized["rows"][0]["subject_label"] == (
        "Carimali Armonia Soft Plus"
    )
    assert normalized["rows"][0]["status_label"] == "尚未驗證"
    assert normalized["coverage"][0]["subject_key"] == (
        "carimali-armonia-soft-plus"
    )


def test_visible_named_row_without_group_id_gets_non_external_stable_key():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_name": "咖啡器材買賣維修社團",
        "status": "not_posted",
        "status_label": "未刊登",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Visible unchecked row in List in more places.",
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0]["named_count"] = 1

    first = normalize_user_facing_report(report)
    second = normalize_user_facing_report(first)

    row = first["rows"][0]
    assert row["destination_id"].startswith("visible-name-sha256:")
    assert row["destination_identity_kind"] == "visible_name"
    assert second["rows"][0]["destination_id"] == row["destination_id"]


def test_visible_named_row_rejects_fabricated_non_external_id():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "visible-name-sha256:" + "0" * 64,
        "destination_name": "咖啡器材買賣維修社團",
        "status": "not_posted",
        "status_label": "未刊登",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Visible unchecked row in List in more places.",
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0]["named_count"] = 1

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "canonical visible-name key" in str(exc)
    else:
        raise AssertionError("fabricated local row identity must be rejected")


def test_twenty_seven_visible_rows_fit_structured_report_and_chat_chunks():
    report = _partial_carimali_report()
    report["rows"] = [
        {
            "subject_key": "Carimali",
            "subject_label": "Carimali Armonia Soft Plus",
            "destination_name": (
                f"第 {index} 個可讀社團名稱／二手咖啡器材買賣交流區"
            ),
            "status": "not_posted",
            "status_label": "未刊登",
            "observed_at": report["observed_at"],
            "verified_at": report["as_of"],
            "evidence": (
                "List in more places showed a visible unchecked checkbox; "
                "the controlled UI did not expose numeric group ID."
            ),
            "source_listing_id": "36803832485927906",
        }
        for index in range(1, 28)
    ]
    report["coverage"][0]["named_count"] = 27
    report["coverage"][0]["note"] = (
        "27 named rows; 10 Join group controls lacked readable names."
    )

    normalized = normalize_user_facing_report(report)
    chunks = render_user_facing_report_chunks(normalized)

    assert len(normalized["rows"]) == 27
    assert len(chunks) >= 2
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks)
    assert "第 27 個可讀社團名稱" in "".join(chunks)


def test_render_chunks_use_telegram_utf16_units_and_canonical_labels():
    report = _partial_carimali_report()
    report["coverage"][0]["subject_label"] = "Wrong product"
    report["coverage"][0]["note"] = "😀" * 40
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Wrong product",
        "destination_id": "260697590957215",
        "destination_name": "咖啡器材買賣維修社團",
        "status": "public",
        "status_label": "未刊登",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Visible in the listing destination list.",
        "evidence_url": (
            "https://www.facebook.com/groups/260697590957215/posts/123456789"
        ),
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0]["named_count"] = 1

    chunks = render_user_facing_report_chunks(report, max_chars=25)

    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 25 for chunk in chunks)
    assert "Carimali Armonia Soft Plus" in "".join(chunks)
    assert "Wrong product" not in "".join(chunks)
    assert "：已刊登" in "".join(chunks)
    assert "：未刊登" not in "".join(chunks)


def test_render_chunks_only_claim_all_listings_for_durable_scope():
    selected = _partial_carimali_report()
    all_listings = _partial_carimali_report()
    all_listings["scope"] = "all_listings"

    selected_text = "".join(render_user_facing_report_chunks(selected))
    all_text = "".join(render_user_facing_report_chunks(all_listings))

    assert selected_text.startswith("Facebook 刊登與互動狀態清單（截至 ")
    assert all_text.startswith("Facebook 全部刊登與互動狀態清單（截至 ")


def test_all_listings_scope_cannot_close_with_omitted_known_subjects():
    report = _partial_carimali_report()
    report["scope"] = "all_listings"
    report["complete"] = True
    report["coverage"][0].update({
        "complete": True,
        "expected_total": 0,
        "gap_count": 0,
    })

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "complete must match coverage completeness" in str(exc)
    else:
        raise AssertionError("all-listings scope cannot omit known products")


def test_render_chunks_show_engagement_and_keep_unavailable_distinct_from_zero():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "260697590957215",
        "destination_name": "咖啡器材買賣維修社團",
        "group_listing_id": "1395845029128823",
        "status": "public",
        "status_label": "已刊登",
        "reaction_count": 0,
        "comment_count": 2,
        "view_count": None,
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Exact group commerce listing was visible.",
        "evidence_url": (
            "https://www.facebook.com/marketplace/item/1395845029128823"
        ),
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0].update({
        "named_count": 1,
        "listing_click_count": 1524,
        "listing_click_window_days": 14,
    })

    text = "".join(render_user_facing_report_chunks(report))

    assert "Marketplace 商品詳情點擊：1,524（最近 14 天）" in text
    assert "讚 0｜留言 2｜觀看 —" in text
    assert "Marketplace 36803832485927906" in text
    assert "群組刊登 1395845029128823" in text
    assert "— 代表 Facebook 未提供或目前不可見，不等於 0" in text


def test_public_status_rejects_candidate_list_without_canonical_proof_url():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "260697590957215",
        "destination_name": "咖啡器材買賣維修社團",
        "status": "public",
        "status_label": "已刊登",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "List in more places candidate row was visible.",
        "source_listing_id": "36803832485927906",
    }]
    report["coverage"][0]["named_count"] = 1

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "evidence_url is required for public status" in str(exc)
    else:
        raise AssertionError("candidate-list prose must not prove publication")


def test_report_digest_and_render_order_are_stable_across_row_order():
    report = _partial_carimali_report()
    first = {
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "260697590957215",
        "destination_name": "B group",
        "status": "unknown",
        "status_label": "尚未驗證",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Historical destination identity only.",
        "source_listing_id": "36803832485927906",
    }
    second = dict(first, destination_id="878122105538734", destination_name="A group")
    report["rows"] = [first, second]
    report["coverage"][0]["named_count"] = 2
    reversed_report = dict(report, rows=list(reversed(report["rows"])))

    assert user_facing_report_digest(report) == user_facing_report_digest(
        reversed_report
    )
    assert render_user_facing_report_chunks(report) == (
        render_user_facing_report_chunks(reversed_report)
    )


def test_report_digest_canonicalizes_listing_id_set_order():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "kolin-kd291m06",
        "subject_label": "Kolin KD-291M06",
        "destination_id": "1333742673375089",
        "destination_name": "家電買賣社團",
        "status": "unknown",
        "status_label": "尚未驗證",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Historical destination identity only.",
        "source_listing_id": "915975414881937",
        "source_listing_ids": ["915975414881937", "37217119148451132"],
    }]
    report["coverage"] = [{
        **report["coverage"][0],
        "subject_key": "kolin-kd291m06",
        "subject_label": "Kolin KD-291M06",
        "named_count": 1,
    }]
    reversed_ids = {
        **report,
        "rows": [{
            **report["rows"][0],
            "source_listing_ids": list(
                reversed(report["rows"][0]["source_listing_ids"])
            ),
        }],
    }

    assert user_facing_report_digest(report) == user_facing_report_digest(
        reversed_ids
    )


def test_report_rejects_half_specified_listing_click_window():
    report = _partial_carimali_report()
    report["coverage"][0]["listing_click_count"] = 1524

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "listing_click_count and listing_click_window_days" in str(exc)
    else:
        raise AssertionError("listing click count without its window must fail")


def test_report_structure_rejects_duplicate_alias_coverage():
    report = _partial_carimali_report()
    duplicate = dict(report["coverage"][0])
    duplicate["subject_key"] = "36803832485927906"
    report["coverage"].append(duplicate)

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "duplicate coverage" in str(exc)
    else:
        raise AssertionError("logical duplicate aliases must be rejected")


def test_report_structure_rejects_row_for_different_listing():
    report = _partial_carimali_report()
    report["rows"] = [{
        "subject_key": "Carimali",
        "subject_label": "Carimali Armonia Soft Plus",
        "destination_id": "260697590957215",
        "destination_name": "咖啡器材買賣維修社團",
        "status": "public",
        "status_label": "已刊登",
        "observed_at": report["observed_at"],
        "verified_at": report["as_of"],
        "evidence": "Visible in the listing destination list.",
        "source_listing_id": "37217119148451132",
    }]
    report["coverage"][0]["named_count"] = 1

    try:
        normalize_user_facing_report(report)
    except ValueError as exc:
        assert "must match" in str(exc)
        assert "36803832485927906" in str(exc)
    else:
        raise AssertionError("cross-listing report row must be rejected")
