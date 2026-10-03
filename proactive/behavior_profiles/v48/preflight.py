"""Shared v1 chooser evidence contract; validation never invents observations."""
from __future__ import annotations

import re


VERSION = "facebook_group_preflight/v1"
LISTING_FIELDS = ("listing_id", "list_in_more_places_available", "observed_at")
GROUP_FIELDS = ("group_id", "readable_name", "canonical_url", "chooser_group_id",
                "chooser_canonical_url", "chooser_presence", "chooser_selectability")


def requested(contract):
    """Recognize existing explicit preflights without changing signed contracts."""
    routing = contract.get("routing")
    task_type = routing.get("task_type") if isinstance(routing, dict) else None
    declared = contract.get("evidence_contract")
    if declared is not None:
        if (declared != VERSION
                or task_type != "facebook_marketplace_readonly"):
            raise ValueError("Unsupported evidence_contract or non-readonly preflight route")
        return True
    # Existing signed contracts explicitly named these canonical sections in
    # structured deliverables. Keep them readable without treating examples or
    # negated prose elsewhere in the goal as an evidence-schema declaration.
    goal = contract.get("goal")
    deliverables = goal.get("deliverables") if isinstance(goal, dict) else None
    return (
        task_type == "facebook_marketplace_readonly"
        and isinstance(deliverables, list)
        and "sourceListing and coverageReconciliation evidence" in deliverables
    )


def validate(evidence, *, started_at, ended_at, listing_id=None):
    """Return indexed rows after the same checks at ingestion, review and use.

    An unavailable chooser is a valid observation with no eligible destinations.
    A claimed eligible row must have exact identity and unchecked selectability.
    This validates evidence structure, not telescope suitability or write authority.
    """
    if not isinstance(evidence, dict) or evidence.get("sideEffectsPerformed") is not False:
        raise ValueError("Preflight requires zero-effect acceptance evidence")
    listing = evidence.get("sourceListing")
    coverage = evidence.get("coverageReconciliation")
    groups = evidence.get("groups")
    if not isinstance(listing, dict) or not isinstance(coverage, dict) or not isinstance(groups, list):
        raise ValueError("Preflight requires sourceListing, groups and coverageReconciliation")
    source = listing.get("listing_id")
    if not isinstance(source, str) or not re.fullmatch(r"[1-9][0-9]*", source):
        raise ValueError("Preflight source listing ID must be a numeric string")
    if listing_id is not None and source != listing_id:
        raise ValueError("Preflight source listing does not match publish scope")
    available = listing.get("list_in_more_places_available")
    if type(available) is not bool:
        raise ValueError("Preflight chooser availability must be a boolean")
    observed = listing.get("observed_at")
    if (type(observed) is not int or observed <= 0 or not ended_at
            or not started_at <= observed <= ended_at):
        raise ValueError("Preflight sourceListing.observed_at must be a browser-observed integer "
                         "within the exact execution run; a new observation needs a new run, "
                         "never rewrite an ended run's timestamp")
    eligible = coverage.get("verified_eligible_for_later_approval")
    if (not isinstance(eligible, list)
            or any(not isinstance(g, str) or not re.fullmatch(r"[1-9][0-9]*", g) for g in eligible)
            or len(set(eligible)) != len(eligible)):
        raise ValueError("Preflight eligibility must contain unique numeric group IDs")
    rows = {}
    for row in groups:
        if not isinstance(row, dict) or any(field not in row for field in GROUP_FIELDS):
            raise ValueError("Preflight requires complete canonical group rows")
        group_id = row["group_id"]
        if (not isinstance(group_id, str) or not re.fullmatch(r"[1-9][0-9]*", group_id)
                or group_id in rows):
            raise ValueError("Preflight contains duplicate or invalid group rows")
        if (not isinstance(row["readable_name"], str) or not row["readable_name"].strip()
                or row["canonical_url"] not in (f"https://www.facebook.com/groups/{group_id}",
                                                f"https://www.facebook.com/groups/{group_id}/")):
            raise ValueError("Preflight group identity is invalid")
        rows[group_id] = row
    for group_id in eligible:
        row = rows.get(group_id)
        if (not available or not row or row["chooser_group_id"] != group_id
                or row["chooser_canonical_url"] not in (f"https://www.facebook.com/groups/{group_id}",
                                                       f"https://www.facebook.com/groups/{group_id}/")
                or row["chooser_presence"] != "present"
                or row["chooser_selectability"] != "selectable_unchecked"):
            raise ValueError(f"Preflight eligible group {group_id} is not an exact selectable row")
    return rows
