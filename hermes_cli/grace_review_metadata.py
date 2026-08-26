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
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _truthy_check_dict(value: Any) -> bool:
    return isinstance(value, dict) and any(item is True for item in value.values())


def grace_review_accepted(metadata: Any) -> bool:
    review_metadata = metadata if isinstance(metadata, dict) else {}
    criteria = review_metadata.get("acceptance_criteria_met")
    evidence = review_metadata.get("evidence")
    verification_notes = review_metadata.get("verification_notes")
    visual_review = review_metadata.get("visual_review")
    parent_verified_file = review_metadata.get("parent_verified_file")
    verified_checks = review_metadata.get("verified_checks")
    evidence_lines = review_metadata.get("evidence_lines")

    has_review_evidence = (
        (isinstance(evidence, dict) and bool(evidence))
        or _nonempty_string_list(verification_notes)
        or _truthy_check_dict(verified_checks)
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
        review_metadata.get("review_outcome") == "accepted"
        or review_metadata.get("review_result") == "accepted"
        or visual_review_accepted
        or (
            review_metadata.get("approved") is True
            and (
                criteria is True
                or (
                    isinstance(criteria, list)
                    and all(str(item).strip() for item in criteria)
                )
                or has_review_evidence
            )
        )
    )


def grace_review_rejected(metadata: Any) -> bool:
    review_metadata = metadata if isinstance(metadata, dict) else {}
    verdict = str(review_metadata.get("review_verdict") or "").strip().lower()
    outcome = str(review_metadata.get("review_outcome") or "").strip().lower()
    result = str(review_metadata.get("review_result") or "").strip().lower()
    return (
        review_metadata.get("approved") is False
        or verdict in {"rejected", "rejected_incomplete", "blocked", "failed"}
        or outcome in {"rejected", "blocked", "failed"}
        or result in {"rejected", "blocked", "failed"}
    )
