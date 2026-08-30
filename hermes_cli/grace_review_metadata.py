"""Grace review metadata verdict helpers.

The gateway callback sender and DB callback outcome guard must share this
boundary. A prose-only summary is not enough to close a delegated task; accepted
metadata needs a structured verdict or structured evidence.
"""

from __future__ import annotations

from typing import Any


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _structured_checks(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and any(item is True for item in value.values())
    return _nonempty_string_list(value)


def _parse_dimensions(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        for key in ("dimensions", "dimensions_px", "pixel_dimensions", "size"):
            parsed = _parse_dimensions(value.get(key))
            if parsed is not None:
                return parsed
        width = value.get("width") or value.get("pixel_width")
        height = value.get("height") or value.get("pixel_height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
        return None
    if not isinstance(value, str):
        return None
    clean = value.strip().lower().replace("×", "x")
    parts = clean.split("x", 1)
    if len(parts) != 2:
        return None
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _matches_ratio(dimensions: tuple[int, int], ratio: tuple[int, int]) -> bool:
    width, height = dimensions
    ratio_width, ratio_height = ratio
    return width * ratio_height == height * ratio_width


def _asset_declarations_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    page_hero = value.get("page_hero")
    if page_hero is not None:
        dimensions = _parse_dimensions(page_hero)
        if dimensions is None or not _matches_ratio(dimensions, (16, 9)):
            return False
    audio_brief = value.get("audio_brief")
    if audio_brief is not None:
        dimensions = _parse_dimensions(audio_brief)
        if dimensions is None or not _matches_ratio(dimensions, (1, 1)):
            return False
    return True


def _declares_page_hero(metadata: dict[str, Any]) -> bool:
    declarations = metadata.get("asset_declarations")
    return (
        str(metadata.get("asset_family") or "").strip().lower() == "page_hero"
        or (isinstance(declarations, dict) and declarations.get("page_hero") is not None)
    )


def _page_hero_visual_safety_valid(metadata: dict[str, Any]) -> bool:
    if not _declares_page_hero(metadata):
        return True
    visual_review = metadata.get("visual_review")
    return (
        isinstance(visual_review, dict)
        and visual_review.get("all_required_text_readable") is True
        and visual_review.get("text_occlusion_free") is True
        and visual_review.get("disclosure_non_obstructive") is True
        and visual_review.get("defects_found") == []
    )


def grace_review_accepted(metadata: Any) -> bool:
    review_metadata = metadata if isinstance(metadata, dict) else {}
    if (
        review_metadata.get("approved") is False
        or review_metadata.get("accepted") is False
    ):
        return False
    if not _asset_declarations_valid(review_metadata.get("asset_declarations")):
        return False
    if not _page_hero_visual_safety_valid(review_metadata):
        return False
    canonical_verdict = str(
        review_metadata.get("review_outcome") or ""
    ).strip().lower()
    legacy_verdicts = [
        str(review_metadata.get(field) or "").strip().lower()
        for field in ("review_result", "review_verdict")
    ]
    legacy_verdicts = [value for value in legacy_verdicts if value]
    rejected_verdicts = {"rejected", "rejected_incomplete", "blocked", "failed"}
    if canonical_verdict:
        return (
            canonical_verdict == "accepted"
            and not any(value in rejected_verdicts for value in legacy_verdicts)
        )
    if legacy_verdicts:
        return all(value == "accepted" for value in legacy_verdicts)

    criteria = review_metadata.get("acceptance_criteria_met")
    evidence = review_metadata.get("evidence")
    verification_notes = review_metadata.get("verification_notes")
    visual_review = review_metadata.get("visual_review")
    parent_verified_file = review_metadata.get("parent_verified_file")
    verified_checks = review_metadata.get("verified_checks")
    evidence_lines = review_metadata.get("evidence_lines")

    has_review_evidence = (
        (isinstance(evidence, dict) and bool(evidence))
        or (
            isinstance(review_metadata.get("verified_facts"), dict)
            and bool(review_metadata["verified_facts"])
        )
        or (
            isinstance(review_metadata.get("reviewed_artifacts"), dict)
            and bool(review_metadata["reviewed_artifacts"])
        )
        or _nonempty_string_list(verification_notes)
        or _structured_checks(verified_checks)
        or (isinstance(evidence_lines, dict) and bool(evidence_lines))
        or _nonempty_string_list(review_metadata.get("authoritative_sources_verified"))
        or _nonempty_string(review_metadata.get("reviewed_file"))
        or _nonempty_string(review_metadata.get("verification_artifact"))
    )
    visual_review_accepted = (
        isinstance(visual_review, dict)
        and visual_review.get("approved") is True
        and (
            (isinstance(parent_verified_file, dict) and bool(parent_verified_file))
            or visual_review.get("defects_found") == []
        )
    )
    return (
        visual_review_accepted
        or (
            (
                review_metadata.get("approved") is True
                or review_metadata.get("accepted") is True
            )
            and (
                criteria is True
                or _nonempty_string_list(criteria)
                or has_review_evidence
            )
        )
    )


def grace_review_acceptance_error(metadata: Any) -> str:
    """Return the precise completion error for non-accepted review metadata."""
    review_metadata = metadata if isinstance(metadata, dict) else {}
    if (
        review_metadata.get("approved") is False
        or review_metadata.get("accepted") is False
    ):
        return (
            "Grace review completion metadata contains an explicit rejected "
            "approval flag."
        )
    if not _asset_declarations_valid(review_metadata.get("asset_declarations")):
        return (
            "Grace review asset_declarations are invalid. For page_hero use "
            "exact 16:9 dimensions; for audio_brief use exact 1:1 dimensions."
        )
    if _declares_page_hero(review_metadata) and not _page_hero_visual_safety_valid(
        review_metadata
    ):
        return (
            "Grace review completion metadata conflicts with its canonical "
            "accepted verdict. For page_hero, include "
            "visual_review.all_required_text_readable=true, "
            "text_occlusion_free=true, disclosure_non_obstructive=true, "
            "and defects_found=[]."
        )
    return (
        "Grace review completion metadata conflicts with its canonical "
        "accepted verdict. For text-only reviews, do not add page_hero or "
        "visual_review fields; include review_outcome='accepted' plus "
        "evidence, verification_notes, verified_checks, evidence_lines, "
        "reviewed_file, or verification_artifact."
    )


def grace_review_rejected(metadata: Any) -> bool:
    review_metadata = metadata if isinstance(metadata, dict) else {}
    verdict = str(review_metadata.get("review_verdict") or "").strip().lower()
    outcome = str(review_metadata.get("review_outcome") or "").strip().lower()
    result = str(review_metadata.get("review_result") or "").strip().lower()
    return (
        review_metadata.get("approved") is False
        or review_metadata.get("accepted") is False
        or verdict in {"rejected", "rejected_incomplete", "blocked", "failed"}
        or outcome in {"rejected", "blocked", "failed"}
        or result in {"rejected", "blocked", "failed"}
    )
