"""Keep public Page read-only handoffs focused on current controller evidence."""
from __future__ import annotations

from typing import Any



def compact_public_page_readonly_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a source-focused readback, not the Objective's archived media history.

    The sealed scope retains the exact source/Hero paths and published post ID;
    this supplementary readback only carries controller-owned stage/effect state.
    Omitting comments avoids accidentally hydrating unrelated historical images
    into a new browser run. The original Kanban history remains untouched.
    """
    stage_key = str(snapshot.get("stage_key") or "")
    if "_public_page_readonly_verification_" not in stage_key:
        return snapshot
    stages = snapshot.get("stages") or []
    if not isinstance(stages, list):
        return snapshot
    # The source, Hero and existing-post selectors live in the sealed scope;
    # only carry historical status, never a screenshot or an old review body.
    keep = (
        "schema_version", "source", "observed_at", "objective_id", "stage_key",
        "referenced_task_ids", "controller_history_baseline", "objective",
        "external_effects", "accepted_source_deliveries", "referenced_task_evidence",
        "requested_run_evidence", "publication_progress",
    )
    compact = {key: snapshot[key] for key in keep if key in snapshot}
    compact["stages"] = stages
    compact["stages_total"] = snapshot.get("stages_total", len(stages))
    compact["historical_evidence_omitted"] = (
        "Archived comments, media, callbacks and unrelated task runs are omitted "
        "from this new read-only browser prompt. Consult the sealed source and "
        "Hero selectors; historical snapshots are not current native receipts."
    )
    return compact

