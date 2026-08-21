"""Hermes-owned intake for ClawOps runtime work.

This module deliberately stops at kanban task creation. Hermes remains the
planner and user-facing owner; ClawOps workers only receive queued execution
work and report results back through the existing kanban notifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
from typing import Any, Iterable, Mapping, Optional

from hermes_cli import kanban_db as kb
from proactive.hubops_routing import (
    resolved_route_binding,
    route_clawops_objective,
    route_requires_owner_approval,
)
from proactive.loop_contract import (
    facebook_crosspost_inspection_listing_id,
    validate_loop_contract,
)


DEFAULT_ASSIGNEE = "default"
DEFAULT_CREATED_BY = "hermes-clawops-intake"
DEFAULT_MAX_RUNTIME_SECONDS = 1800
KNOWN_PROJECTS = {"hub_ops", "hahow_course", "course_marketing", "secondhand_commerce", "ingrids_marketing"}


EXTERNAL_BROWSER_TARGET_TERMS = (
    "facebook",
    "fb marketplace",
    "marketplace",
    "社團",
    "交流團",
    "group",
    "商品表單",
)
EXTERNAL_BROWSER_ACTION_TERMS = (
    "刊登",
    "發布",
    "發佈",
    "貼文",
    "post",
    "publish",
    "listing",
    "join",
    "加入",
    "next",
    "上傳",
    "照片",
    "檢查",
    "核對",
    "只讀",
    "列出",
    "清單",
    "狀態",
)
FACEBOOK_PAGE_GRAPH_TARGET_TERMS = (
    "facebook page",
    "facebook 粉專",
    "粉絲專頁",
    "粉絲專業",
    "粉專",
    "solobizai",
    "solo biz ai",
    "ai bizweek",
    "一人公司商業誌",
)
FACEBOOK_PROTECTED_PAGE_IDENTITY_TERMS = (
    "solobizai",
    "solo biz ai",
    "ai bizweek",
    "一人公司商業誌",
)
FACEBOOK_PAGE_GRAPH_ACTION_TERMS = (
    "發布",
    "發佈",
    "貼文",
    "po文",
    "po 文",
    "post",
    "publish",
)
FACEBOOK_NON_PAGE_TARGET_TERMS = (
    "marketplace",
    "fb marketplace",
    "社團",
    "群組",
    "交流團",
    "/groups/",
    "group post",
    "crosspost",
    "跨社團",
)
FACEBOOK_MARKETPLACE_GROUP_TARGET_TERMS = (
    "marketplace",
    "fb marketplace",
    "市集",
)
FACEBOOK_GROUP_DESTINATION_TERMS = (
    "社團",
    "群組",
    "交流團",
    "facebook group",
    "facebook groups",
    "/groups/",
    "group post",
    "crosspost",
    "跨社團",
)
FACEBOOK_GROUP_PUBLISH_ACTION_TERMS = (
    "發布",
    "發佈",
    "刊登",
    "跨貼",
    "分享",
    "轉貼",
    "轉發",
    "加入",
    "新增",
    "加到",
    "放到",
    "追加",
    "list in more places",
    "post",
    "publish",
    "share",
    "add",
    "cross-post",
    "cross post",
    "distribute",
    "distribution",
)
FACEBOOK_GROUP_READONLY_ACTION_TERMS = (
    "查看",
    "查詢",
    "查核",
    "清單",
    "狀態",
    "互動",
    "按讚",
    "留言",
    "觀看",
    "read-only",
    "readonly",
    "inspect",
    "audit",
    "status",
)
FACEBOOK_GROUP_READONLY_LISTING_PHRASES = (
    "既有刊登",
    "已刊登",
    "現有刊登",
    "existing listing",
    "existing post",
)


def _contains_intent_term(text: str, terms: Iterable[str]) -> bool:
    """Match English intent as tokens and CJK/control labels as substrings."""
    for term in terms:
        if term.isascii() and any(char.isalpha() for char in term):
            if re.search(
                rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                text,
            ):
                return True
        elif term in text:
            return True
    return False
AUTO_PUBLISH_APPROVAL_TERMS = (
    "已確認",
    "已核准",
    "已同意",
    "已經傳給我確認",
    "文案已確認",
    "文案已核准",
    "之前發佈文案",
    "之前發布文案",
    "自動發佈",
    "自動發布",
    "自動 publish",
    "auto publish",
    "approved copy",
    "preapproved",
)
IMAGE_CONTENT_TERMS = (
    "生成圖片",
    "生圖",
    "圖片生成",
    "產出圖",
    "產生圖",
    "視覺素材",
    "圖片素材",
    "商品圖",
    "宣傳圖",
    "image generation",
    "image_generate",
    "generate image",
    "image_gen",
    "visual asset",
)
LEGAL_COMPLIANCE_TERMS = (
    "legal",
    "compliance",
    "合規",
    "法務",
    "法律",
    "法規",
    "律師",
    "合約",
    "契約",
    "條款",
    "個資",
    "隱私",
    "資安",
    "廣告法",
    "平台規範",
    "平台合規",
    "privacy",
    "contract",
    "terms of service",
    "platform policy",
)


@dataclass(frozen=True)
class ClawOpsTask:
    task_id: str
    status: str
    assignee: str
    title: str
    body: str
    board: Optional[str] = None
    risk_authorization: Optional[dict[str, Any]] = None


def resolve_clawops_assignee(config: Optional[Mapping[str, Any]] = None) -> str:
    """Resolve the worker profile that should claim ClawOps tasks."""
    env_value = os.getenv("HERMES_CLAWOPS_ASSIGNEE", "").strip()
    if env_value:
        return env_value

    cfg = config or {}
    for section_name, key in (
        ("clawops", "default_assignee"),
        ("kanban", "clawops_assignee"),
        ("proactive", "clawops_assignee"),
    ):
        section = cfg.get(section_name)
        if isinstance(section, Mapping):
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return DEFAULT_ASSIGNEE


def create_clawops_task(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
    assignee: Optional[str] = None,
    board: Optional[str] = None,
    created_by: str = DEFAULT_CREATED_BY,
    priority: int = 0,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    config: Optional[Mapping[str, Any]] = None,
    contract: Optional[Mapping[str, Any]] = None,
    authorize_contract_risk: bool = False,
    delegation_id: str = "",
    delegation_build_owner: str = "",
    resolved_route: Optional[Mapping[str, Any]] = None,
    idempotency_key: str = "",
    initial_status: str = "running",
    session_id: Optional[str] = None,
    executor_backend: str = "hermes",
    executor_profile: Optional[str] = None,
    skills: Optional[Iterable[str]] = None,
) -> ClawOpsTask:
    """Create a Hermes-owned ClawOps task in the existing kanban queue."""
    clean_objective = (objective or "").strip()
    if not clean_objective:
        raise ValueError("objective is required")
    clean_executor_backend = str(executor_backend or "").strip().lower()
    if clean_executor_backend != "hermes":
        raise ValueError(
            "create_clawops_task only admits Hermes-owned work; external "
            "backends must use their dedicated start adapter so the task and "
            "executable run are created atomically."
        )

    enriched_source = infer_clawops_metadata(clean_objective, source=source)
    normalized_contract = (
        validate_loop_contract(contract) if contract is not None else None
    )
    contract = normalized_contract
    contract_fingerprint = (
        _contract_fingerprint(normalized_contract)
        if normalized_contract is not None
        else ""
    )
    hubops_envelope = _route_hubops_if_requested(
        clean_objective,
        enriched_source,
        contract_fingerprint=contract_fingerprint,
    )
    if hubops_envelope and hubops_envelope.get("status") == "blocked":
        raise ValueError(str(hubops_envelope.get("blocked_reason") or "HubOps routing blocked this task."))

    bound_route = dict(
        resolved_route
        or (
            contract.get("routing", {}).get("resolved")
            if isinstance(contract, Mapping)
            else {}
        )
        or {}
    )
    if hubops_envelope:
        fresh_route = resolved_route_binding(hubops_envelope)
        if bound_route and json.dumps(
            bound_route, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) != json.dumps(
            fresh_route, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ):
            raise ValueError(
                "Resolved ClawOps route changed after contract authorization."
            )
        if not bound_route:
            bound_route = fresh_route

    approval_required = bool(
        (
            isinstance(contract, Mapping)
            and list(contract.get("external_targets") or [])
            and facebook_crosspost_inspection_listing_id(contract) is None
        )
        or (
            hubops_envelope
            and route_requires_owner_approval(hubops_envelope)
        )
    )
    delegation: Optional[dict[str, Any]] = None
    if contract is None or not contract_fingerprint or not delegation_id.strip():
        raise ValueError(
            "Every ClawOps execution requires a validated Loop Contract and "
            "reserved Grace delegation."
        )
    with kb.connect_closing(board=board) as conn:
        delegation = kb.get_grace_delegation(
            conn, delegation_id=delegation_id,
        )
    if delegation is None:
        raise ValueError("Reserved Grace delegation was not found.")
    expected_route_json = json.dumps(
        bound_route,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    state_authorized = (
        delegation.get("state") == "building"
        and delegation.get("build_owner") == delegation_build_owner.strip()
        and int(delegation.get("build_lease_expires") or 0) > int(time.time())
    )
    if (
        delegation.get("contract_fingerprint") != contract_fingerprint
        or delegation.get("resolved_route") != expected_route_json
        or not state_authorized
        or bool(delegation.get("approval_required")) != approval_required
    ):
        raise ValueError(
            "Reserved Grace delegation does not authorize this exact "
            "contract, route, or approval class."
        )

    resolved_assignee = _resolve_task_assignee(
        hubops_envelope=hubops_envelope,
        assignee=assignee,
        config=config,
    )
    title = _title_from_objective(clean_objective)
    risk_authorization = (
        dict(hubops_envelope.get("risk_authorization") or {})
        if hubops_envelope else {}
    )
    if risk_authorization and not (
        delegation and bool(delegation.get("approval_required"))
    ):
        raise ValueError(
            "Scoped risk authorization requires an approved Grace delegation."
        )
    if contract is not None:
        from proactive.grace_task_compiler import render_execution_body
        execution_contract = json.loads(json.dumps(dict(contract), ensure_ascii=False))
        if risk_authorization:
            execution_contract["authorization"] = risk_authorization
        body = render_execution_body(execution_contract)
        if hubops_envelope:
            body += "\n" + "\n".join(_hubops_routing_contract(hubops_envelope))
            body += "\n" + "\n".join(
                _scoped_worker_capability_contract(hubops_envelope)
            )
    else:
        body = _body_from_objective(clean_objective, source=enriched_source, hubops_envelope=hubops_envelope)

    with kb.connect_closing(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=resolved_assignee,
            created_by=created_by,
            priority=priority,
            workspace_kind="scratch",
            max_runtime_seconds=max_runtime_seconds,
            goal_mode=contract is not None,
            goal_max_turns=(
                int(contract.get("stop_rules", {}).get("max_iterations", 6))
                if contract is not None else None
            ),
            initial_status=initial_status,
            session_id=(session_id or "").strip() or None,
            idempotency_key=idempotency_key.strip() or None,
            executor_backend=clean_executor_backend,
            executor_profile=executor_profile,
            project_namespace=str(enriched_source.get("project") or "").strip() or None,
            skills=skills,
        )
        row = kb.get_task(conn, task_id)
        status = str(row.status if row else "ready")

    return ClawOpsTask(
        task_id=task_id,
        status=status,
        assignee=resolved_assignee,
        title=title,
        body=body,
        board=board,
        risk_authorization=risk_authorization or None,
    )


def _resolve_task_assignee(
    *,
    hubops_envelope: Optional[Mapping[str, Any]],
    assignee: Optional[str],
    config: Optional[Mapping[str, Any]],
) -> str:
    """Keep routed tasks bound to the worker profile authorized by HubOps."""
    if hubops_envelope:
        assignment = hubops_envelope.get("assignment")
        if not isinstance(assignment, Mapping):
            raise ValueError("HubOps routing did not provide a worker assignment.")
        assigned_worker = str(assignment.get("assigned_worker") or "").strip()
        runtime_profile = str(assignment.get("runtime_profile") or "").strip()
        route_assignee = runtime_profile or assigned_worker
        if not route_assignee:
            raise ValueError(
                "HubOps routing did not provide an executable worker profile."
            )
        return route_assignee
    return (assignee or "").strip() or resolve_clawops_assignee(config)


def subscribe_clawops_task(
    task_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    board: Optional[str] = None,
) -> bool:
    """Subscribe the originating Hermes channel to terminal task updates."""
    clean_platform = (platform or "").strip().lower()
    clean_chat_id = (chat_id or "").strip()
    if not task_id or not clean_platform or not clean_chat_id:
        return False

    with kb.connect_closing(board=board) as conn:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=clean_platform,
            chat_id=clean_chat_id,
            thread_id=(thread_id or "").strip() or None,
            user_id=(user_id or "").strip() or None,
            notifier_profile=(notifier_profile or "").strip() or None,
        )
    return True


def _title_from_objective(objective: str) -> str:
    single_line = " ".join(objective.split())
    if len(single_line) <= 96:
        return f"ClawOps: {single_line}"
    return f"ClawOps: {single_line[:93].rstrip()}..."


def _body_from_objective(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
    hubops_envelope: Optional[Mapping[str, Any]] = None,
) -> str:
    needs_image_generation = requires_image_generation_capabilities(objective, source=source)
    needs_page_graph_api = (
        requires_facebook_page_graph_api(objective, source=source)
        and not needs_image_generation
    )
    needs_marketplace_group_publish = (
        requires_facebook_marketplace_group_publish(objective, source=source)
        and not needs_image_generation
        and not needs_page_graph_api
    )
    needs_external_browser = (
        requires_external_browser_capabilities(objective, source=source)
        and not needs_image_generation
        and not needs_page_graph_api
    )
    publish_preapproved = auto_publish_preapproved(objective, source=source)
    execution_owner = (
        "Browser-capable Hermes/ClawOps runtime may execute only the delegated browser work in this queued task."
        if needs_external_browser
        else "ClawOps Page API runtime may execute only the contract-bound Meta Graph API publication."
        if needs_page_graph_api
        else "ClawOps content runtime may execute only the delegated content and asset work in this queued task."
        if needs_image_generation
        else "ClawOps runtime may execute only delegated work in this queued task."
    )
    lines = [
        "ClawOps runtime task created by Hermes.",
        "",
        "Control boundary:",
        "- Hermes remains the primary agent and user-facing decision owner.",
        f"- {execution_owner}",
        "- Results must return to Hermes for audit, review, and user-facing summary.",
        "",
        "Objective:",
        objective,
    ]
    if hubops_envelope:
        lines.extend(_hubops_routing_contract(hubops_envelope))
    if needs_external_browser:
        lines.extend(
            _external_browser_capability_contract_for(
                auto_publish_preapproved=publish_preapproved,
            )
        )
    if needs_page_graph_api:
        lines.extend(_facebook_page_graph_capability_contract())
    if needs_marketplace_group_publish:
        lines.extend(_facebook_marketplace_group_capability_contract())
    if needs_image_generation:
        lines.extend(_image_generation_capability_contract())
    if source:
        lines.extend(["", "Source:"])
        for key in sorted(source):
            value = source.get(key)
            if value is None or value == "":
                continue
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _route_hubops_if_requested(
    objective: str,
    source: Optional[Mapping[str, Any]],
    *,
    contract_fingerprint: str = "",
) -> Optional[dict[str, Any]]:
    if not source or not any(key in source for key in ("project", "task_type", "risk_level", "approved")):
        return None
    return route_clawops_objective(
        objective,
        project=str(source.get("project") or "hub_ops"),
        task_type=str(source.get("task_type") or "ops"),
        risk_level=str(source.get("risk_level") or "low"),
        approved=_read_bool(source.get("approved")),
        contract_fingerprint=contract_fingerprint,
    )


def _contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Bind a risk elevation to one immutable compiled Loop Contract."""
    from proactive.loop_contract import contract_fingerprint

    return contract_fingerprint(contract)


def infer_clawops_metadata(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Infer project/task metadata so /clawops natural language uses YAML routing."""
    inferred: dict[str, Any] = dict(source or {})
    haystack = " ".join(
        [objective or "", *[str(v) for v in inferred.values() if v is not None]]
    ).lower()

    if not str(inferred.get("project") or "").strip():
        inferred["project"] = _infer_project(haystack)
    if not str(inferred.get("task_type") or "").strip():
        inferred["task_type"] = _infer_task_type(haystack, str(inferred.get("project") or ""))
    if not str(inferred.get("risk_level") or "").strip():
        inferred["risk_level"] = (
            "medium"
            if inferred.get("task_type")
            in {
                "browser_publish",
                "facebook_marketplace_group_publish",
                "facebook_page_api_publish",
            }
            else "low"
        )
    if auto_publish_preapproved(objective, source=inferred):
        inferred.setdefault("auto_publish_preapproved", "true")
        inferred.setdefault("previous_copy_confirmed", "true")
        inferred.setdefault("approved", "true")
    return inferred


def _infer_project(haystack: str) -> str:
    if any(term in haystack for term in ("二手", "secondhand", "咖啡機", "咖啡器材", "facebook", "marketplace", "社團", "群組", "交流團", "商品")):
        return "secondhand_commerce"
    if any(term in haystack for term in ("hahow", "課綱", "課程大綱", "課程設計", "proposal")):
        return "hahow_course"
    if any(term in haystack for term in ("招生", "crm", "lead", "名單", "課程行銷", "course marketing")):
        return "course_marketing"
    if any(term in haystack for term in ("ingrids", "seo", "trading", "投資", "交易")):
        return "ingrids_marketing"
    if any(term in haystack for term in ("openclaw", "hermes", "bridge", "health", "runtime", "kanban", "hubops", "clawops")):
        return "hub_ops"
    if any(term in haystack for term in LEGAL_COMPLIANCE_TERMS):
        return "hub_ops"
    return "hub_ops"


def _infer_task_type(haystack: str, project: str) -> str:
    if any(term in haystack for term in IMAGE_CONTENT_TERMS):
        return "campaign" if project == "course_marketing" else "content_draft"
    if requires_facebook_marketplace_group_publish(haystack):
        return "facebook_marketplace_group_publish"
    if requires_facebook_page_graph_api(haystack):
        return "facebook_page_api_publish"
    if requires_external_browser_capabilities(haystack):
        if any(
            term in haystack
            for term in (
                "列出",
                "清單",
                "狀態",
                "已經有刊登",
                "已刊登",
                "目前刊登",
                "status",
                "inspect",
                "read-only",
                "readonly",
            )
        ):
            return "browser_ops"
        return "browser_publish" if any(term in haystack for term in ("發佈", "發布", "刊登", "post", "publish", "listing")) else "browser_ops"
    if any(term in haystack for term in LEGAL_COMPLIANCE_TERMS):
        return "legal_compliance"
    if any(term in haystack for term in ("health", "bridge", "runtime", "修正", "fix", "deploy", "部署")):
        return "devops"
    if project == "ingrids_marketing":
        return "product_marketing"
    if any(term in haystack for term in ("研究", "research", "分析", "競品")):
        return "research"
    if any(term in haystack for term in ("內容", "文案", "draft", "草稿")):
        return "content_draft"
    if project == "hahow_course":
        return "course_design"
    if project == "course_marketing" and any(term in haystack for term in ("crm", "名單", "私訊", "followup")):
        return "crm"
    if project == "course_marketing":
        return "campaign"
    return "ops"


def _read_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "approved"}
    return False


def _hubops_routing_contract(envelope: Mapping[str, Any]) -> list[str]:
    assignment = envelope.get("assignment")
    assignment = assignment if isinstance(assignment, Mapping) else {}
    agent_assignment = envelope.get("agent_assignment")
    agent_assignment = agent_assignment if isinstance(agent_assignment, Mapping) else {}
    return [
        "",
        "HubOps routing:",
        f"- status: {envelope.get('status', '')}",
        f"- assigned_agent: {agent_assignment.get('assigned_agent', '')}",
        f"- assigned_worker: {assignment.get('assigned_worker', '')}",
        f"- runtime_profile: {assignment.get('runtime_profile', '')}",
        f"- risk_level_limit: {assignment.get('risk_level_limit', '')}",
        f"- effective_risk_level_limit: {assignment.get('effective_risk_level_limit', '')}",
        f"- approval_required: {assignment.get('approval_required', '')}",
        f"- approval_checklist: {envelope.get('approval_checklist', '')}",
        f"- output_schema: {envelope.get('output_schema', {})}",
    ]


def _scoped_worker_capability_contract(envelope: Mapping[str, Any]) -> list[str]:
    """Render deterministic tool-use rules for compiled browser contracts."""
    assignment = envelope.get("assignment")
    assignment = assignment if isinstance(assignment, Mapping) else {}
    allowed_tools = [str(item) for item in assignment.get("allowed_tools") or []]
    if "browser_upload_files" not in allowed_tools:
        return []
    return [
        "",
        "Scoped browser capability contract:",
        f"- HubOps-authorized tools for this worker include: {', '.join(allowed_tools)}.",
        "- For a local file required by a browser form, call browser_upload_files before "
        "trying OS keystrokes, clipboard injection, a local HTTP server, or page-created "
        "File/DataTransfer objects.",
        "- Select the exact browser tab and exact input[type=file]. If multiple matching "
        "tabs or inputs exist, supply page_index and input_index or a narrower selector; "
        "never guess silently.",
        "- Supply verify_text_contains when the page exposes a visible post-upload count "
        "or success marker. Treat the upload as successful only when that postcondition "
        "is visible (for example, a product-image count changing from 0/9 to 1/9).",
        "- browser_upload_files only populates the file input. It never authorizes a final "
        "Publish/Post/Submit; the Loop Contract approval and verification gates still apply.",
    ]


def requires_external_browser_capabilities(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return True for delegated work that must run in ClawOps browser mode."""
    haystack_parts = [objective or ""]
    if source:
        haystack_parts.extend(str(v) for v in source.values() if v is not None)
    haystack = " ".join(haystack_parts).lower()
    if requires_facebook_page_graph_api(haystack):
        return False
    return any(term in haystack for term in EXTERNAL_BROWSER_TARGET_TERMS) and any(
        term in haystack for term in EXTERNAL_BROWSER_ACTION_TERMS
    )


def requires_facebook_page_graph_api(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Route Facebook Page writes to Graph API before browser classification."""
    haystack_parts = [objective or ""]
    if source:
        haystack_parts.extend(str(v) for v in source.values() if v is not None)
    haystack = " ".join(haystack_parts).casefold()
    has_publish_action = any(
        term in haystack for term in FACEBOOK_PAGE_GRAPH_ACTION_TERMS
    )
    has_explicit_non_page_target = any(
        term in haystack for term in FACEBOOK_NON_PAGE_TARGET_TERMS
    )
    if (
        has_publish_action
        and not has_explicit_non_page_target
        and any(
        term in haystack for term in FACEBOOK_PROTECTED_PAGE_IDENTITY_TERMS
        )
    ):
        return True
    return (
        any(term in haystack for term in FACEBOOK_PAGE_GRAPH_TARGET_TERMS)
        and has_publish_action
        and not has_explicit_non_page_target
    )


def requires_facebook_marketplace_group_publish(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Route Marketplace-to-group writes to one dedicated browser worker.

    Meta removed the Groups API and ``publish_to_groups`` from every Graph API
    version in 2024.  This classifier therefore never selects the Page Graph
    transport; it isolates the only supported controlled-browser workflow.
    """
    haystack_parts = [objective or ""]
    if source:
        haystack_parts.extend(str(v) for v in source.values() if v is not None)
    haystack = " ".join(haystack_parts).casefold()
    write_intent_haystack = haystack
    for readonly_phrase in FACEBOOK_GROUP_READONLY_LISTING_PHRASES:
        write_intent_haystack = write_intent_haystack.replace(
            readonly_phrase,
            "",
        )
    if (
        _contains_intent_term(haystack, FACEBOOK_GROUP_READONLY_ACTION_TERMS)
        and not _contains_intent_term(
            write_intent_haystack,
            FACEBOOK_GROUP_PUBLISH_ACTION_TERMS,
        )
    ):
        return False
    return (
        any(term in haystack for term in FACEBOOK_MARKETPLACE_GROUP_TARGET_TERMS)
        and any(term in haystack for term in FACEBOOK_GROUP_DESTINATION_TERMS)
        and _contains_intent_term(
            haystack,
            FACEBOOK_GROUP_PUBLISH_ACTION_TERMS,
        )
    )


def requires_image_generation_capabilities(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return True for delegated work that needs ClawOps content image generation."""
    haystack_parts = [objective or ""]
    if source:
        haystack_parts.extend(str(v) for v in source.values() if v is not None)
    haystack = " ".join(haystack_parts).lower()
    return any(term in haystack for term in IMAGE_CONTENT_TERMS)


def _image_generation_capability_contract() -> list[str]:
    return [
        "",
        "Image generation capability contract:",
        "- This task must be executed by the ClawOps content runtime using the assigned content sub agent.",
        "- Required capabilities: image_generate, local artifact write access, and report_generate for generated asset paths.",
        "- Do not route this task through any dry-run bridge; it must either produce image files or call kanban_block with block_kind=capability and the concrete missing provider.",
        "- If FAL_KEY, Nous Portal login, or another configured image provider is unavailable, stop and report the missing provider instead of fabricating image completion.",
        "- Generated images for resale listings must be labeled as AI-assisted/reference visuals unless the task explicitly uses actual user-provided photos.",
    ]


def _facebook_page_graph_capability_contract() -> list[str]:
    return [
        "",
        "Facebook Page Graph API capability contract:",
        "- Use facebook_page_graph_status for preflight and "
        "facebook_page_graph_publish for the single approved write.",
        "- Never navigate to Facebook, open a composer, or call browser tools "
        "for this Page publication.",
        "- Bind the exact canonical Page URL, UTF-8 message SHA-256, and image "
        "byte SHA-256 in the one-time Loop Contract before approval.",
        "- If Graph configuration is unavailable, block once as a capability "
        "failure. Do not fall back to clawops-browser.",
        "- Never retry an ambiguous POST or any task with an existing durable "
        "Facebook create effect; reconcile by post_id or Page feed.",
    ]


def _facebook_marketplace_group_capability_contract() -> list[str]:
    return [
        "",
        "Facebook Marketplace group publication capability contract:",
        "- Meta removed publish_to_groups and the Groups API from all Graph "
        "API versions on 2024-04-22; never call Graph API or claim an API "
        "fallback exists for this workflow.",
        "- Use only the contract-bound Marketplace listing and the exact "
        "approved group ID/name set through List in more places.",
        "- The structured facebook_crosspost contract must explicitly set "
        "transport=browser; missing or implicit transport is invalid.",
        "- This worker may navigate, inspect, select exact approved group "
        "rows, and submit once; it has no browser_type or upload capability.",
        "- Never Join a group, substitute a similar group, use Share to Group, "
        "create a duplicate listing, or change listing content/assets.",
        "- On one guard/capability failure, stop with the exact error. Do not "
        "retry through a Page Graph worker, generic browser worker, or OpenClaw.",
        "- Before submit, reserve every exact group effect. After submit, "
        "reconcile visible status; never repeat an ambiguous or partial POST.",
    ]


def _external_browser_capability_contract() -> list[str]:
    return _external_browser_capability_contract_for(auto_publish_preapproved=False)


def _external_browser_capability_contract_for(
    *,
    auto_publish_preapproved: bool,
) -> list[str]:
    final_action_policy = (
        "- Approved-copy auto-publish: if the task uses the exact Hermes-confirmed "
        "copy/assets and the page state matches that approved payload, the worker may "
        "click final Post/Publish/Submit without asking KJ again."
        if auto_publish_preapproved
        else "- For Facebook listing flows without explicit final-publish approval, stop before Post/Publish/Submit and report page state, remaining required fields, and visible final action buttons."
    )
    if auto_publish_preapproved:
        execution_boundary = [
            "- This task must be executed by the browser-capable Hermes/ClawOps runtime with audit evidence.",
            "- Do not route this task through the OpenClaw dry-run bridge; that bridge cannot execute Facebook/external browser side effects.",
        ]
    else:
        execution_boundary = [
            "- This task must be executed by the browser-capable Hermes/ClawOps runtime with audit evidence.",
            "- Do not route this task through the OpenClaw dry-run bridge; that bridge cannot execute Facebook/external browser side effects.",
        ]

    return [
        "",
        "External browser capability contract:",
        *execution_boundary,
        "- Required capabilities: logged-in browser CDP session via BROWSER_CDP_URL, browser_cdp, browser_snapshot/browser_navigate as needed, and browser_upload_files for local file inputs.",
        "- If BROWSER_CDP_URL is absent, Facebook login/checkpoint appears, browser_upload_files is unavailable, or required local files cannot be accessed, call kanban_block with block_kind=capability and a concrete missing-capability reason.",
        "- Safety boundary: do not Join groups, send messages, comment, pay, promote, or change account settings unless the objective contains explicit user approval for that exact action.",
        final_action_policy,
        "- If visible Facebook content differs from the confirmed copy/assets, required fields are ambiguous, or the flow adds new destinations not covered by the task, stop and call kanban_block with block_kind=approval.",
        "- After an automatic publish/post, capture URL/screenshot/page state and report the published destination, timestamp, visible status, and any Facebook warning or review state.",
    ]


def auto_publish_preapproved(
    objective: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return True only when the task explicitly carries prior copy approval."""
    if source:
        for key in (
            "auto_publish_preapproved",
            "previous_copy_confirmed",
            "approved_copy",
            "copy_approved",
            "final_publish_approved",
        ):
            if _read_bool(source.get(key)):
                return True

    haystack_parts = [objective or ""]
    if source:
        haystack_parts.extend(str(v) for v in source.values() if v is not None)
    haystack = " ".join(haystack_parts).lower()
    return any(term.lower() in haystack for term in AUTO_PUBLISH_APPROVAL_TERMS)
