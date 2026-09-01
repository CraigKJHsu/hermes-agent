from __future__ import annotations

import time

import pytest

from hermes_cli.user_facing_report import (
    delivery_contract_from_report,
    normalize_user_facing_report,
    render_user_facing_report_chunks,
    report_satisfies_user_facing_delivery,
)


def _inline_text_report() -> dict:
    return {
        "kind": "content_package",
        "delivery": "inline_only",
        "complete": True,
        "title": "Tasker 提案",
        "body": "完整繁體中文提案正文",
        "body_field": "finalPasteReadyDraft",
        "observed_at": int(time.time()),
        "assets": [],
    }


def test_inline_text_content_package_round_trips_delivery_contract():
    report = normalize_user_facing_report(_inline_text_report())
    contract = delivery_contract_from_report(report)

    assert contract == {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_only",
        "body_field": "finalPasteReadyDraft",
    }
    assert report_satisfies_user_facing_delivery(report, contract)
    assert "完整繁體中文提案正文" in "".join(
        render_user_facing_report_chunks(report)
    )


def test_inline_text_content_package_rejects_assets():
    report = _inline_text_report()
    report["assets"] = [{
        "filename": "unexpected.png",
        "label": "unexpected",
        "path": "/tmp/unexpected.png",
        "sha256": "0" * 64,
    }]

    with pytest.raises(ValueError, match="assets must be empty"):
        normalize_user_facing_report(report)
