"""Authenticated read-only API for the unified interaction timeline."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from hermes_cli.interaction_index import InteractionIndex


router = APIRouter()


@router.get("/interactions")
def list_interactions(
    limit: int = Query(200, ge=1, le=500),
    before: Optional[float] = Query(None),
    before_id: Optional[str] = Query(None, max_length=500),
    session_id: Optional[str] = Query(None, max_length=200),
    delegation_id: Optional[str] = Query(None, max_length=200),
    classes: Optional[str] = Query(
        None,
        description="Comma-separated interaction classes.",
    ),
    include_internal: bool = Query(False),
    include_unlinked_openclaw: bool = Query(False),
):
    selected = None
    if classes:
        selected = [value.strip() for value in classes.split(",") if value.strip()]
    try:
        return InteractionIndex().query(
            limit=limit,
            before=before,
            before_id=(before_id or "").strip() or None,
            session_id=(session_id or "").strip() or None,
            delegation_id=(delegation_id or "").strip() or None,
            interaction_classes=selected,
            include_internal=include_internal,
            include_unlinked_openclaw=include_unlinked_openclaw,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
