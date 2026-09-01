from __future__ import annotations

from plugins.openclaw_bridge.tools import (
    OPENCLAW_DELEGATE_SCHEMA,
    handle_openclaw_delegate,
    handle_openclaw_dry_run,
    pre_gateway_dispatch,
)
from plugins.openclaw_bridge.clawops_delegate import (
    CLAWOPS_CANCEL_SCHEMA,
    CLAWOPS_DELEGATE_SCHEMA,
    CLAWOPS_RETRY_REVIEW_SCHEMA,
    GRACE_CALLBACK_OUTCOME_SCHEMA,
    handle_clawops_cancel,
    handle_clawops_delegate,
    handle_clawops_retry_review,
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
        name="clawops_cancel",
        toolset="openclaw",
        schema=CLAWOPS_CANCEL_SCHEMA,
        handler=handle_clawops_cancel,
        description=(
            "When KJ explicitly asks to stop an existing ClawOps task, cancel "
            "that exact lane-bound Loop directly. Never create a new delegated "
            "task whose objective is to cancel another task."
        ),
        emoji="STOP",
    )
    ctx.register_tool(
        name="clawops_retry_review",
        toolset="openclaw",
        schema=CLAWOPS_RETRY_REVIEW_SCHEMA,
        handler=handle_clawops_retry_review,
        description=(
            "After KJ explicitly requests retry and a runtime/capability repair is loaded, "
            "requeue that exact blocked Grace Review without creating another Execution."
        ),
        emoji="RETRY",
    )
    ctx.register_tool(
        name="grace_callback_outcome",
        toolset="openclaw",
        schema=GRACE_CALLBACK_OUTCOME_SCHEMA,
        handler=handle_grace_callback_outcome,
        description=(
            "Inside a Grace Loop callback, record whether the originating "
            "outcome closed, continued through a queued delegation, or is "
            "blocked on one exact approval question."
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
