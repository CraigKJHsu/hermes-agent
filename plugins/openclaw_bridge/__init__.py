from __future__ import annotations

from plugins.openclaw_bridge.tools import (
    OPENCLAW_DELEGATE_SCHEMA,
    handle_openclaw_delegate,
    handle_openclaw_dry_run,
    pre_gateway_dispatch,
)
from plugins.openclaw_bridge.clawops_delegate import (
    CLAWOPS_DELEGATE_SCHEMA,
    CLAWOPS_FINALIZE_SAVED_EVIDENCE_SCHEMA,
    GRACE_CALLBACK_OUTCOME_SCHEMA,
    handle_clawops_delegate,
    handle_clawops_finalize_saved_evidence,
    handle_grace_callback_outcome,
)
from proactive.grace_execution_policy import enforce_grace_execution_boundary


def register(ctx) -> None:
    ctx.register_middleware("tool_execution", enforce_grace_execution_boundary)
    ctx.register_tool(
        name="clawops_delegate",
        toolset="openclaw",
        schema=CLAWOPS_DELEGATE_SCHEMA,
        handler=handle_clawops_delegate,
        description=(
            "After Grace has fully understood an execution request, compile and delegate a "
            "complete Loop Contract to ClawOps. Never pass raw user text as the instruction. "
            "Questions and explanations must be answered by Grace without this tool."
        ),
        emoji="GC",
    )
    ctx.register_tool(
        name="clawops_finalize_saved_evidence",
        toolset="openclaw",
        schema=CLAWOPS_FINALIZE_SAVED_EVIDENCE_SCHEMA,
        handler=handle_clawops_finalize_saved_evidence,
        description=(
            "Resume one exact schema-blocked commerce execution from its "
            "durable evidence only. This preserves the original execution, "
            "review, board, and callback lineage and never opens Facebook or "
            "creates a new task."
        ),
        emoji="GF",
    )
    ctx.register_tool(
        name="grace_callback_outcome",
        toolset="openclaw",
        schema=GRACE_CALLBACK_OUTCOME_SCHEMA,
        handler=handle_grace_callback_outcome,
        description=(
            "Inside a Grace Loop callback, record whether the originating "
            "outcome closed, continued through a queued delegation, or is "
            "blocked on one exact approval question, user decision, or "
            "verified runtime capability fault."
        ),
        emoji="GO",
    )
    ctx.register_tool(
        name="openclaw_delegate",
        toolset="openclaw",
        schema=OPENCLAW_DELEGATE_SCHEMA,
        handler=handle_openclaw_delegate,
        description=(
            "Diagnostic dry-run bridge only. Never use this as an execution fallback when "
            "clawops_delegate validation fails."
        ),
        emoji="OC",
    )
    ctx.register_command(
        "openclaw-dry-run",
        handler=handle_openclaw_dry_run,
        description="Validate Hermes-to-OpenClaw bridge routing without giving OpenClaw conversation control.",
        args_hint="<objective>",
    )
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
