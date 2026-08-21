"""Fail-closed Loop Contract validation for Grace -> ClawOps delegation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import urlsplit


CONTRACT_VERSION = "1.0"

# Page identity is intentionally an explicit allowlist.  A
# one-segment facebook.com path cannot by itself prove that the destination is
# a Page (for example, /login and /photo are application routes).  Add a slug
# only after its Page identity has been independently verified.
_FACEBOOK_AUTHORIZED_PAGE_SLUGS = frozenset({"solobizai"})
_FACEBOOK_GRAPH_API_ONLY_PAGE_URLS = frozenset(
    {"https://www.facebook.com/solobizai"}
)


class LoopContractError(ValueError):
    """Raised when Grace has not supplied an executable contract."""


def canonical_facebook_page_url(value: Any) -> str | None:
    """Return one canonical public Facebook Page URL, or fail closed."""
    candidate = str(value or "").strip()
    if not candidate or any(char.isspace() for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in {"facebook.com", "www.facebook.com"}
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 1:
        return None
    slug = path_parts[0]
    if (
        re.fullmatch(r"[A-Za-z0-9.]+", slug) is None
        or slug in {".", ".."}
        or slug.casefold() not in _FACEBOOK_AUTHORIZED_PAGE_SLUGS
    ):
        return None
    return f"https://www.facebook.com/{slug.casefold()}"


_DISPLAY_ID_END = r"(?![0-9A-Za-z_-]|\.(?=[0-9A-Za-z_]))"
_FACEBOOK_MARKETPLACE_TARGET_PATTERNS = (
    re.compile(
        r"Facebook\s+Marketplace\s+item\s+(?P<id>[0-9]+)"
        + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+Marketplace\s+(?:listing\s+)?ID\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+(?:市集項目|市集刊登)(?:\s*ID)?\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_GROUP_TARGET_PATTERNS = (
    re.compile(
        r"Facebook\s+Group(?:\s+ID)?\s*[:：]?\s*(?P<id>[0-9]+)"
        + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+(?:社團|群組)(?:\s*ID)?\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS = (
    re.compile(
        r"(?:https://)?(?:(?:www|m)\.)?facebook\.com/marketplace/item/"
        r"(?P<id>[0-9]+)(?:[/?#][^\s]*)?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:marketplace:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:marketplace_listing:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:marketplace-group-status:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_GROUP_EXACT_TARGET_PATTERNS = (
    re.compile(
        r"(?:https://)?(?:(?:www|m)\.)?facebook\.com/groups/"
        r"(?P<id>[0-9]+)(?:[/?#][^\s]*)?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:group:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
)
_EXPLICIT_TARGET_TOKEN_SPLIT_RE = re.compile(r"[\s→]+")
_EXPLICIT_TARGET_TOKEN_WRAPPERS = (
    "()[]{}<>\"'，,。；;「」.!?！？（）【】《》"
)
_FACEBOOK_GROUP_NAME_TARGET_PREFIXES = (
    "Facebook group name:",
    "Facebook group name：",
    "Facebook group:",
    "Facebook group：",
    "Facebook 社團名稱:",
    "Facebook 社團名稱：",
    "Facebook 社團:",
    "Facebook 社團：",
)
_MARKETPLACE_MORE_OPTIONS_LABELS = (
    "more options",
    "更多選項",
)
_MARKETPLACE_LIST_MORE_LABELS = (
    "list in more places",
    "list your item in more places",
    "刊登到更多地方",
    "在更多地方刊登",
)
_MARKETPLACE_SELECTION_TERMS = (
    "checkbox",
    "switch",
    "select any option",
    "select option",
    "option selection",
    "勾選",
    "選取",
)
_MARKETPLACE_SUBMISSION_TERMS = (
    "post",
    "publish",
    "submit",
    "cross-post",
    "crosspost",
    "發布",
    "刊登",
    "提交",
    "跨貼",
)
_MARKETPLACE_MUTATING_ALLOWED_TERMS = (
    "share",
    "edit",
    "boost",
    "mark out of stock",
    "mark as in stock",
    "create listing",
    "relist",
    "delete",
    "join group",
    "upload",
    "input",
    "type into",
    "勾選",
    "選取",
    "分享",
    "編輯",
    "推廣",
    "售完",
    "有庫存",
    "建立刊登",
    "重新刊登",
    "重刊",
    "刪除",
    "加入社團",
    "上傳",
    "輸入",
)
_MARKETPLACE_BROAD_MUTATION_BANS = (
    "no external state change",
    "no facebook state change",
    "do not change external state",
    "不得變更外部狀態",
    "不得變更任何外部狀態",
    "不變更外部狀態",
    "不得改變外部狀態",
    "不改變外部狀態",
    "任何外部狀態變更",
    "任何 facebook 狀態變更",
)
_MARKETPLACE_USER_SELECTION_BANS = (
    "不勾選",
    "不得勾選",
    "不要勾選",
    "請勿勾選",
    "禁止勾選",
    "不選取",
    "不得選取",
    "不要選取",
    "請勿選取",
    "禁止選取",
    "do not select",
    "please do not select",
    "must not select",
    "don't select",
    "without selecting",
    "no checkbox selection",
    "no checkbox",
)
_MARKETPLACE_USER_SUBMISSION_BANS = (
    "不發布",
    "不發佈",
    "不得發布",
    "不得發佈",
    "不要發布",
    "不要發佈",
    "請勿發布",
    "請勿發佈",
    "禁止發布",
    "禁止發佈",
    "不提交",
    "不得提交",
    "不要提交",
    "請勿提交",
    "禁止提交",
    "do not publish",
    "please do not publish",
    "must not publish",
    "don't publish",
    "without publishing",
    "do not submit",
    "please do not submit",
    "must not submit",
    "don't submit",
    "without submitting",
)
_MARKETPLACE_USER_CHANGE_BANS = (
    "不修改",
    "不得修改",
    "不要修改",
    "請勿修改",
    "禁止修改",
    "不變更",
    "不改變",
    "不得變更",
    "不得改變",
    "不要變更",
    "不要改變",
    "請勿變更",
    "請勿改變",
    "禁止變更",
    "禁止改變",
    "do not change",
    "please do not change",
    "must not change",
    "don't change",
    "without changing",
    "no external state change",
)
_MARKETPLACE_USER_SAFETY_CANCELLATION_TERMS = (
    "取消",
    "解除",
    "撤銷",
    "作廢",
    "廢除",
    "移除",
    "忽略",
    "不再遵守",
    "不必遵守",
    "不用遵守",
    "cancel",
    "revoke",
    "rescind",
    "waive",
    "disregard",
    "ignore",
    "lift the restriction",
    "remove the restriction",
    "no longer apply",
)

_MARKETPLACE_READONLY_RETRY_SHORTHAND_RE = re.compile(
    r"^(?:\[[^\]\r\n]{1,64}\]\s*)?(?:請\s*)?"
    r"(?:唯讀|只讀|read-only|readonly)\s*"
    r"(?:重試|重新查核|重新檢查|再查一次|retry|recheck)\s+"
    r"(?P<subject>"
    r"carimali(?:\s+armonia\s+soft\s+plus)?|"
    r"kolin(?:\s+kd-?291m06)?|"
    r"celestron(?:\s+130eq)?"
    r")\s*"
    r"[（(]?(?:(?:facebook\s+)?marketplace\s+)?"
    r"listing(?:\s+id)?\s*[:：]?\s*(?P<id>[1-9][0-9]*)"
    r"[）)]?[。.!！]?$",
    flags=re.IGNORECASE,
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _marketplace_listing_ids_in_scope_text(text: str) -> set[str]:
    ids = {
        match.group("id")
        for pattern in (
            *_FACEBOOK_MARKETPLACE_TARGET_PATTERNS,
            *_FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS,
        )
        for match in pattern.finditer(text)
    }
    ids.update(
        match.group("id")
        for match in re.finditer(
            r"\blisting(?:\s+id)?\s*[:：]?\s*(?P<id>[1-9][0-9]*)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    return ids


def _marketplace_readonly_retry_shorthand_listing_id(text: str) -> str | None:
    """Resolve the closed retry shorthand only for a configured product/id pair."""
    match = _MARKETPLACE_READONLY_RETRY_SHORTHAND_RE.fullmatch(text)
    if match is None:
        return None
    from hermes_cli.user_facing_report import commerce_subject_listing_ids

    subject_brand = match.group("subject").split(maxsplit=1)[0]
    listing_id = match.group("id")
    if listing_id not in commerce_subject_listing_ids(subject_brand):
        return None
    return listing_id


def marketplace_readonly_user_request_listing_id(text: str) -> str | None:
    """Recognize one explicit, non-mutating Marketplace group-status request.

    This is intentionally narrower than general intent classification.  It is
    used only to keep a fully specified read-only request from being mistaken
    for an approval-token turn because an older conversation mentioned the
    publishing workflow.
    """
    normalized = " ".join(str(text or "").split()).casefold()
    listing_ids = _marketplace_listing_ids_in_scope_text(normalized)
    if len(listing_ids) != 1:
        return None
    long_numeric_ids = set(
        re.findall(r"(?<![0-9])[1-9][0-9]{9,}(?![0-9])", normalized)
    )
    if long_numeric_ids != listing_ids:
        return None
    retry_listing_id = _marketplace_readonly_retry_shorthand_listing_id(normalized)
    if retry_listing_id is not None:
        return retry_listing_id
    if "facebook" not in normalized or "marketplace" not in normalized:
        return None
    if not _contains_any(normalized, ("唯讀", "只讀", "read-only", "readonly")):
        return None
    if not _contains_any(normalized, ("社團", "群組", "group")):
        return None
    if not _contains_any(normalized, ("狀態", "status", "state")):
        return None
    if not _contains_any(
        normalized,
        ("查核", "檢查", "讀取", "列出", "audit", "inspect", "read", "list"),
    ):
        return None
    if not _contains_any(normalized, _MARKETPLACE_USER_SELECTION_BANS):
        return None
    if not _contains_any(normalized, _MARKETPLACE_USER_SUBMISSION_BANS):
        return None
    if not _contains_any(normalized, _MARKETPLACE_USER_CHANGE_BANS):
        return None
    if _contains_any(normalized, _MARKETPLACE_USER_SAFETY_CANCELLATION_TERMS):
        return None
    residual = normalized
    readonly_phrases = (
        *_MARKETPLACE_USER_SELECTION_BANS,
        *_MARKETPLACE_USER_SUBMISSION_BANS,
        *_MARKETPLACE_USER_CHANGE_BANS,
        "do not post",
        "do not share",
        "do not edit",
        "do not join",
        "do not upload",
        "list in more places",
        "list your item in more places",
        "刊登到更多地方",
        "在更多地方刊登",
        "刊登狀態",
    )
    for phrase in sorted(readonly_phrases, key=len, reverse=True):
        residual = residual.replace(phrase, " ")
    if _contains_any(
        residual,
        (
            *_MARKETPLACE_SELECTION_TERMS,
            *_MARKETPLACE_SUBMISSION_TERMS,
            *_MARKETPLACE_MUTATING_ALLOWED_TERMS,
            "create",
            "delete",
            "relist",
            "mark in stock",
            "mark out of stock",
            "stock change",
            "庫存變更",
        ),
    ):
        return None
    return next(iter(listing_ids))


def _is_canonical_marketplace_inspection_clause(
    text: str,
    listing_id: str,
) -> bool:
    normalized = " ".join(str(text or "").split()).casefold()
    canonical = {
        (
            "read-only facebook marketplace listing "
            f"{listing_id}: open more options → list in more places "
            "and read visible destination names/status only"
        ),
        (
            "唯讀開啟 facebook marketplace listing "
            f"{listing_id} 的 more options → list in more places，"
            "只讀取可見社團名稱與狀態"
        ),
        (
            "唯讀開啟 facebook marketplace listing "
            f"{listing_id} 的更多選項 → 刊登到更多地方，"
            "只讀取可見社團名稱與狀態"
        ),
        (
            "read-only facebook marketplace listing "
            f"{listing_id}: inspect exact known group posts, group-specific "
            "commerce listings, and seller group contribution pages; more "
            "options → list in more places may enumerate candidates only and "
            "is not publication proof"
        ),
    }
    return normalized in canonical


def canonical_marketplace_readonly_sections(
    listing_id: str,
) -> dict[str, dict[str, Any]]:
    """Build executable sections for an actual group-publication audit.

    ``List in more places`` remains a listing-bound discovery surface, but a
    candidate checkbox row is never accepted as proof that a post exists.
    """
    return {
        "goal": {
            "objective": (
                "Read Facebook Marketplace listing "
                f"{listing_id} named group destinations and visible statuses "
                "without changing external state"
            ),
            "deliverables": [
                "Named group destinations, actual publication statuses, and "
                "currently visible reactions, comments, and views",
            ],
            "non_goals": [
                "No checkbox selection, submission, publishing, or external "
                "state change",
            ],
        },
        "scope": {
            "allowed": [
                "Read-only Facebook Marketplace listing "
                f"{listing_id}: inspect exact known group posts, group-specific "
                "commerce listings, and seller group contribution pages; More "
                "options → List in more places may enumerate candidates only and "
                "is not publication proof",
            ],
            "forbidden": [
                "Do not select any checkbox or switch",
                "Do not click Post, Publish, Submit, or Share",
                "Do not change external state",
            ],
        },
        "verification": {
            "checks": [
                "For every durable-ledger destination bound to this listing, "
                "inspect the exact group publication page, group-specific commerce listing, "
                "or seller group contribution page; use group search only as a "
                "fallback",
                "Use the listing-bound More options → List in more places path "
                "only to enumerate candidate names, never to prove publication",
            ],
            "evidence_required": [
                "Exact group page URL plus visible post/listing identity and "
                "publication status for every verified destination",
                "Visible reactions, comments, and views, preserving null when "
                "Facebook does not expose a metric",
            ],
            "acceptance_criteria": [
                "Report every durable-ledger destination inline by readable "
                "name, group ID, Marketplace listing ID, group listing ID when "
                "visible, status, metrics, observation time, and exact evidence",
                "Keep the report incomplete while any known destination lacks "
                "actual publication evidence or requested interaction metrics",
            ],
        },
    }


def canonical_marketplace_readonly_delegate_args(
    listing_id: str,
) -> dict[str, Any]:
    """Build every fixed argument for the exact no-token delegation route."""
    return {
        "grace_interpretation": (
            "唯讀查核 Facebook Marketplace listing "
            f"{listing_id} 的具名社團目的地與目前可見狀態，直接在對話中交付"
        ),
        "trigger": "Authenticated user requested one exact read-only listing audit",
        **canonical_marketplace_readonly_sections(listing_id),
        "stop_rules": {
            "success": ["Every visible named destination and status is reported inline"],
            "blocked": ["An exact structured browser capability blocker is recorded"],
            "no_progress": ["The same exact capability blocker recurs within cooldown"],
            "max_iterations": 6,
            "max_runtime_seconds": 900,
        },
        "memory": {
            "working": ["Current listing-bound visible destination evidence"],
            "promote_on_acceptance": ["Accepted named destination status observations"],
        },
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": [f"facebook_marketplace:{listing_id}"],
        },
        "task_type": "secondhand_commerce_group_status",
        "completion_mode": "terminal",
        "risk_level": "low",
        "approved": False,
        "external_targets": [f"Facebook Marketplace listing ID {listing_id}"],
    }


def _has_canonical_marketplace_inspection_intent(
    contract: Mapping[str, Any],
    listing_id: str,
) -> bool:
    canonical = canonical_marketplace_readonly_sections(listing_id)
    return bool(
        contract.get("goal") == canonical["goal"]
        and contract.get("verification") == canonical["verification"]
    )


def facebook_crosspost_target_ids(
    targets: Any,
) -> tuple[frozenset[str], frozenset[str]]:
    """Read only explicit Marketplace listing and Facebook group identifiers.

    This intentionally accepts the canonical labels emitted by Grace plus the
    corresponding Facebook URLs and localized ID labels. Arbitrary numbers are
    never treated as authority.
    """
    if not isinstance(targets, list):
        return frozenset(), frozenset()
    target_text = " ".join(str(target or "") for target in targets)
    listing_ids = {
        match.group("id")
        for pattern in _FACEBOOK_MARKETPLACE_TARGET_PATTERNS
        for match in pattern.finditer(target_text)
    }
    group_ids = {
        match.group("id")
        for pattern in _FACEBOOK_GROUP_TARGET_PATTERNS
        for match in pattern.finditer(target_text)
    }
    for target in targets:
        for raw_token in _EXPLICIT_TARGET_TOKEN_SPLIT_RE.split(str(target)):
            token = raw_token.strip(_EXPLICIT_TARGET_TOKEN_WRAPPERS)
            if not token:
                continue
            for pattern in _FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS:
                match = pattern.fullmatch(token)
                if match is not None:
                    listing_ids.add(match.group("id"))
            for pattern in _FACEBOOK_GROUP_EXACT_TARGET_PATTERNS:
                match = pattern.fullmatch(token)
                if match is not None:
                    group_ids.add(match.group("id"))
    return frozenset(listing_ids), frozenset(group_ids)


def normalize_facebook_group_name(value: Any) -> str:
    """Normalize harmless display whitespace without weakening exact names."""
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFC", str(value or "")),
    ).strip()


def facebook_crosspost_target_names(targets: Any) -> frozenset[str]:
    """Read only explicitly labelled, full Facebook group display names."""
    if not isinstance(targets, list):
        return frozenset()
    names: list[str] = []
    for target in targets:
        text = str(target or "").strip()
        folded_text = text.casefold()
        prefix = next(
            (
                item
                for item in _FACEBOOK_GROUP_NAME_TARGET_PREFIXES
                if folded_text.startswith(item.casefold())
            ),
            None,
        )
        if prefix is None:
            continue
        name = normalize_facebook_group_name(text[len(prefix):])
        if name:
            names.append(name)
    if len(names) != len(set(names)):
        return frozenset()
    return frozenset(names)


def exact_facebook_marketplace_listing_target_id(target: Any) -> str | None:
    """Return one listing id only when the entire target is canonical."""
    text = str(target or "").strip()
    matches = {
        match.group("id")
        for pattern in (
            *_FACEBOOK_MARKETPLACE_TARGET_PATTERNS,
            *_FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS,
        )
        for match in [pattern.fullmatch(text)]
        if match is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def browser_readonly_marketplace_fallback_listing_id(
    value: Mapping[str, Any],
) -> str | None:
    """Recover one legacy read-only Marketplace inspection target safely.

    Older Grace callback turns sometimes emitted the generic
    ``browser_readonly`` task type and a bare numeric listing target after a
    stricter canonical delegation was rejected.  The browser guard correctly
    refuses that shape, but leaving it unnormalized turns a safe read-only
    continuation into a terminal capability blocker.  Accept only the exact
    legacy shape that still proves one listing, both local UI transitions,
    and explicit bans on selection, submission, and external state changes.
    Callers must replace the supplied prose with the canonical sections before
    persisting or executing the contract.
    """
    routing = value.get("routing")
    task_type = str(
        value.get("task_type")
        or (routing.get("task_type") if isinstance(routing, Mapping) else "")
        or ""
    ).strip().casefold()
    if task_type not in {
        "browser_readonly",
        "facebook_marketplace_readonly",
        "secondhand_commerce_group_status",
    } or isinstance(value.get("facebook_crosspost"), Mapping):
        return None

    targets = value.get("external_targets")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    target_text = " ".join(str(targets[0] or "").split())
    listing_id = exact_facebook_marketplace_listing_target_id(target_text)
    if listing_id is None:
        match = re.fullmatch(
            r"(?:Facebook Marketplace listing\s+)?(?P<id>[0-9]+)"
            r"(?: \(read-only List in more places inspection only\))?",
            target_text,
            flags=re.IGNORECASE,
        )
        listing_id = match.group("id") if match is not None else None
    if listing_id is None:
        return None

    goal = value.get("goal")
    scope = value.get("scope")
    verification = value.get("verification")
    allowed = scope.get("allowed") if isinstance(scope, Mapping) else None
    forbidden = scope.get("forbidden") if isinstance(scope, Mapping) else None
    if not isinstance(allowed, list) or not isinstance(forbidden, list):
        return None

    intent_parts: list[str] = []
    for candidate in (
        goal.get("objective") if isinstance(goal, Mapping) else None,
        goal.get("deliverables") if isinstance(goal, Mapping) else None,
        allowed,
        verification.get("checks") if isinstance(verification, Mapping) else None,
    ):
        if isinstance(candidate, list):
            intent_parts.extend(str(item or "") for item in candidate)
        elif candidate is not None:
            intent_parts.append(str(candidate))
    normalized_intent = " ".join(" ".join(intent_parts).split()).casefold()
    normalized_allowed = [
        " ".join(str(item or "").split()).casefold() for item in allowed
    ]
    normalized_forbidden = " ".join(
        " ".join(str(item or "").split()).casefold() for item in forbidden
    )
    listing_bound_transition = any(
        listing_id in clause
        and "facebook" in clause
        and "marketplace" in clause
        and _contains_any(clause, _MARKETPLACE_MORE_OPTIONS_LABELS)
        and _contains_any(clause, _MARKETPLACE_LIST_MORE_LABELS)
        and _contains_any(clause, ("唯讀", "read-only", "readonly"))
        for clause in normalized_allowed
    )
    return listing_id if (
        listing_bound_transition
        and _contains_any(normalized_intent, _MARKETPLACE_MORE_OPTIONS_LABELS)
        and _contains_any(normalized_intent, _MARKETPLACE_LIST_MORE_LABELS)
        and _contains_any(normalized_intent, ("唯讀", "read-only", "readonly"))
        and _contains_any(normalized_forbidden, _MARKETPLACE_SELECTION_TERMS)
        and _contains_any(normalized_forbidden, _MARKETPLACE_SUBMISSION_TERMS)
        and (
            _contains_any(
                normalized_forbidden,
                _MARKETPLACE_BROAD_MUTATION_BANS,
            )
            or "外部狀態變更" in normalized_forbidden
        )
    ) else None


def canonicalize_name_bound_facebook_crosspost_targets(
    targets: Any,
    facebook_crosspost: Mapping[str, Any] | None,
) -> list[str]:
    """Canonicalize exact raw name-bound targets without widening authority.

    Grace naturally emits the structured listing ID and group names as plain
    ``external_targets`` values.  The durable contract, however, uses labelled
    targets so downstream routing can distinguish a listing from a group.  This
    adapter accepts only a one-to-one representation of the already structured
    authority and persists one deterministic canonical form.  Missing, extra,
    conflicting, or duplicate targets fail before approval is created.
    """
    copied_targets = [
        str(target).strip()
        for target in list(targets or [])
        if str(target).strip()
    ]
    if not isinstance(facebook_crosspost, Mapping):
        return copied_targets
    raw_names = facebook_crosspost.get("group_names")
    if not isinstance(raw_names, list) or not raw_names:
        return copied_targets

    listing_id = str(
        facebook_crosspost.get("marketplace_listing_id") or ""
    ).strip()
    names = [normalize_facebook_group_name(name) for name in raw_names]
    if (
        not re.fullmatch(r"[0-9]+", listing_id)
        or any(
            not isinstance(name, str)
            or normalize_facebook_group_name(name) != name
            or not name
            for name in raw_names
        )
        or len(names) != len(set(names))
    ):
        raise LoopContractError(
            "name-bound facebook_crosspost requires one numeric Marketplace "
            "listing id and unique normalized exact group_names"
        )

    expected_names = set(names)
    seen_listing = False
    seen_names: set[str] = set()
    for target in copied_targets:
        parsed_listing_id = exact_facebook_marketplace_listing_target_id(target)
        if target == listing_id or parsed_listing_id == listing_id:
            if seen_listing:
                raise LoopContractError(
                    "name-bound facebook_crosspost repeats its Marketplace target"
                )
            seen_listing = True
            continue
        if parsed_listing_id is not None:
            raise LoopContractError(
                "name-bound facebook_crosspost contains a conflicting "
                "Marketplace target"
            )

        labelled_names = facebook_crosspost_target_names([target])
        if labelled_names:
            target_name = next(iter(labelled_names))
        else:
            target_name = normalize_facebook_group_name(target)
        if target_name not in expected_names:
            raise LoopContractError(
                "name-bound facebook_crosspost contains an unrecognized "
                "external target"
            )
        if target_name in seen_names:
            raise LoopContractError(
                "name-bound facebook_crosspost repeats a group target"
            )
        seen_names.add(target_name)

    if not seen_listing or seen_names != expected_names:
        raise LoopContractError(
            "name-bound facebook_crosspost external_targets must contain the "
            "structured Marketplace listing and every exact group name once"
        )
    return [
        f"Facebook Marketplace item {listing_id}",
        *(f"Facebook group name: {name}" for name in names),
    ]


def browser_readonly_marketplace_inspection_requested(
    contract: Mapping[str, Any],
) -> bool:
    """Return whether a contract asks to open listing-bound inspection UI."""
    routing = contract.get("routing")
    task_type = str(
        routing.get("task_type") if isinstance(routing, Mapping) else ""
    ).strip().casefold()
    # ``secondhand_commerce_group_status`` is the semantic contract type.
    # HubOps resolves that intent to the dedicated
    # ``facebook_marketplace_readonly`` worker route.  Grace may persist either
    # representation at the contract boundary, so both must resolve to the
    # same narrow listing-bound capability.  The remaining exact-target and
    # no-mutation checks below are unchanged.
    if task_type not in {
        "secondhand_commerce_group_status",
        "facebook_marketplace_readonly",
    }:
        return False
    goal = contract.get("goal")
    scope = contract.get("scope")
    verification = contract.get("verification")
    text_parts: list[str] = []
    for value in (
        goal.get("objective") if isinstance(goal, Mapping) else None,
        goal.get("deliverables") if isinstance(goal, Mapping) else None,
        scope.get("allowed") if isinstance(scope, Mapping) else None,
        verification.get("checks")
        if isinstance(verification, Mapping)
        else None,
    ):
        if isinstance(value, list):
            text_parts.extend(str(item or "") for item in value)
        elif value is not None:
            text_parts.append(str(value))
    normalized = " ".join(" ".join(text_parts).split()).casefold()
    return (
        _contains_any(normalized, _MARKETPLACE_MORE_OPTIONS_LABELS)
        and _contains_any(normalized, _MARKETPLACE_LIST_MORE_LABELS)
        and ("marketplace" in normalized or "市集" in normalized)
        and (
            "唯讀" in normalized
            or "read-only" in normalized
            or "readonly" in normalized
        )
    )


def facebook_crosspost_inspection_listing_id(
    contract: Mapping[str, Any],
) -> str | None:
    """Return the one exact listing authorized for read-only UI inspection."""
    if (
        not browser_readonly_marketplace_inspection_requested(contract)
        or isinstance(contract.get("facebook_crosspost"), Mapping)
    ):
        return None
    targets = contract.get("external_targets")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    listing_id = exact_facebook_marketplace_listing_target_id(targets[0])
    if listing_id is None:
        return None
    scope = contract.get("scope")
    allowed = scope.get("allowed") if isinstance(scope, Mapping) else None
    forbidden = scope.get("forbidden") if isinstance(scope, Mapping) else None
    if not isinstance(allowed, list) or not isinstance(forbidden, list):
        return None
    normalized_allowed = [
        " ".join(str(item or "").split()).casefold() for item in allowed
    ]
    normalized_forbidden = [
        " ".join(str(item or "").split()).casefold() for item in forbidden
    ]
    combined_allowed = " ".join(normalized_allowed)
    combined_forbidden = " ".join(normalized_forbidden)
    source_transition_authorized = any(
        _is_canonical_marketplace_inspection_clause(item, listing_id)
        for item in normalized_allowed
    )
    selection_forbidden = any(
        _contains_any(item, _MARKETPLACE_SELECTION_TERMS)
        for item in normalized_forbidden
    )
    submission_forbidden = any(
        _contains_any(item, _MARKETPLACE_SUBMISSION_TERMS)
        for item in normalized_forbidden
    )
    goal = contract.get("goal")
    verification = contract.get("verification")
    other_intent_parts: list[str] = []
    for value in (
        goal.get("objective") if isinstance(goal, Mapping) else None,
        goal.get("deliverables") if isinstance(goal, Mapping) else None,
        verification.get("checks")
        if isinstance(verification, Mapping)
        else None,
    ):
        if isinstance(value, list):
            other_intent_parts.extend(
                " ".join(str(item or "").split()).casefold()
                for item in value
            )
        elif value is not None:
            other_intent_parts.append(
                " ".join(str(value).split()).casefold()
            )
    non_inspection_intent = " ".join(other_intent_parts)
    mutating_action_allowed = _contains_any(
        non_inspection_intent,
        _MARKETPLACE_MUTATING_ALLOWED_TERMS,
    )
    canonical_inspection_intent = _has_canonical_marketplace_inspection_intent(
        contract,
        listing_id,
    )
    submission_action_allowed = (
        False
        if canonical_inspection_intent
        else _contains_any(non_inspection_intent, _MARKETPLACE_SUBMISSION_TERMS)
    )
    intent_listing_ids = _marketplace_listing_ids_in_scope_text(
        " ".join([combined_allowed, non_inspection_intent]),
    )
    broad_mutation_forbidden = _contains_any(
        combined_forbidden,
        _MARKETPLACE_BROAD_MUTATION_BANS,
    )
    if (
        not source_transition_authorized
        or len(normalized_allowed) != 1
        or not canonical_inspection_intent
        or not selection_forbidden
        or not submission_forbidden
        or mutating_action_allowed
        or submission_action_allowed
        or intent_listing_ids != {listing_id}
        or not broad_mutation_forbidden
    ):
        return None
    return listing_id


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Return the immutable scope fingerprint used by approval and callbacks.

    The persisted fingerprint field is excluded from its own digest so the
    approval provenance can carry the exact value without creating a recursive
    hash definition.
    """
    canonical_value = deepcopy(dict(contract or {}))
    canonical_value.pop("approval_provenance", None)
    canonical = json.dumps(
        canonical_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_loop_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized contract or reject it before a task is created."""
    value = deepcopy(dict(contract or {}))
    errors: list[str] = []

    def required_text(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if not isinstance(cur, str) or not cur.strip():
            errors.append(f"{path} is required")

    def required_list(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if (
            not isinstance(cur, list)
            or not cur
            or any(not isinstance(item, str) or not item.strip() for item in cur)
        ):
            errors.append(f"{path} must contain only non-empty strings")

    for path in (
        "identity.project",
        "identity.topic_name",
        "identity.request_instance_id",
        "original_request",
        "grace_interpretation",
        "trigger",
        "goal.objective",
        "memory.namespace",
        "completion_mode",
    ):
        required_text(path)
    if value.get("completion_mode") not in {"terminal", "intermediate"}:
        errors.append("completion_mode must be terminal or intermediate")
    thread_id = (value.get("identity") or {}).get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        errors.append("identity.thread_id must be a string")
    for path in (
        "goal.deliverables",
        "goal.non_goals",
        "scope.allowed",
        "scope.forbidden",
        "verification.checks",
        "verification.evidence_required",
        "verification.acceptance_criteria",
        "stop_rules.success",
        "stop_rules.blocked",
        "stop_rules.no_progress",
        "memory.working",
        "memory.promote_on_acceptance",
    ):
        required_list(path)
    if "external_targets" in value:
        required_list("external_targets")
    routing_task_type = str(
        (value.get("routing") or {}).get("task_type")
        if isinstance(value.get("routing"), Mapping)
        else ""
    ).strip()
    if "facebook_page_post" in value:
        page_post = value.get("facebook_page_post")
        if not isinstance(page_post, Mapping):
            errors.append("facebook_page_post must be an object")
        else:
            page_url = page_post.get("page_url")
            canonical_page_url = canonical_facebook_page_url(page_url)
            if page_post.get("action") != "create_post":
                errors.append("facebook_page_post.action must be create_post")
            if canonical_page_url is None or page_url != canonical_page_url:
                errors.append(
                    "facebook_page_post.page_url must be one canonical public "
                    "Facebook Page URL"
                )
            transport = str(page_post.get("transport") or "browser").strip()
            if (
                canonical_page_url in _FACEBOOK_GRAPH_API_ONLY_PAGE_URLS
                and transport != "graph_api"
            ):
                errors.append(
                    "this Facebook Page requires transport=graph_api; browser "
                    "composer publishing and fallback are forbidden"
                )
            if transport == "browser":
                if set(page_post) != {"action", "page_url"}:
                    errors.append(
                        "browser facebook_page_post permits only action and "
                        "page_url"
                    )
            elif transport == "graph_api":
                graph_keys = {
                    "action",
                    "page_url",
                    "transport",
                    "message_sha256",
                    "image_sha256",
                }
                if set(page_post) != graph_keys:
                    errors.append(
                        "graph_api facebook_page_post requires exactly action, "
                        "page_url, transport, message_sha256, and image_sha256"
                    )
                for digest_name in ("message_sha256", "image_sha256"):
                    digest = page_post.get(digest_name)
                    if not isinstance(digest, str) or re.fullmatch(
                        r"[0-9a-f]{64}", digest
                    ) is None:
                        errors.append(
                            f"facebook_page_post.{digest_name} must be a "
                            "lowercase SHA-256 digest"
                        )
            else:
                errors.append(
                    "facebook_page_post.transport must be browser or graph_api"
                )
            targets = value.get("external_targets")
            if targets != [canonical_page_url]:
                errors.append(
                    "facebook_page_post.page_url must exactly match the sole "
                    "external_targets entry"
                )
            if isinstance(value.get("facebook_crosspost"), Mapping):
                errors.append(
                    "facebook_page_post cannot be combined with facebook_crosspost"
                )
            page_routing = value.get("routing")
            page_task_type = str(
                page_routing.get("task_type")
                if isinstance(page_routing, Mapping)
                else ""
            ).strip()
            expected_page_task_type = (
                "facebook_page_api_publish"
                if transport == "graph_api"
                else "browser_publish"
            )
            if page_task_type != expected_page_task_type:
                errors.append(
                    "facebook_page_post transport="
                    f"{transport} requires routing.task_type="
                    f"{expected_page_task_type}"
                )
    if "facebook_marketplace_price_update" in value:
        price_update = value.get("facebook_marketplace_price_update")
        if not isinstance(price_update, Mapping):
            errors.append("facebook_marketplace_price_update must be an object")
        else:
            listing_id = price_update.get("marketplace_listing_id")
            price_twd = price_update.get("price_twd")
            exact_keys = {
                "action", "transport", "marketplace_listing_id",
                "currency", "price_twd",
            }
            if set(price_update) != exact_keys:
                errors.append(
                    "facebook_marketplace_price_update requires exactly action, "
                    "transport, marketplace_listing_id, currency, and price_twd"
                )
            if price_update.get("action") != "update_price":
                errors.append(
                    "facebook_marketplace_price_update.action must be update_price"
                )
            if price_update.get("transport") != "browser":
                errors.append(
                    "facebook_marketplace_price_update.transport must be browser"
                )
            if (
                not isinstance(listing_id, str)
                or not listing_id.isascii()
                or not listing_id.isdigit()
            ):
                errors.append(
                    "facebook_marketplace_price_update.marketplace_listing_id "
                    "must be a numeric string"
                )
            if price_update.get("currency") != "TWD":
                errors.append(
                    "facebook_marketplace_price_update.currency must be TWD"
                )
            if isinstance(price_twd, bool) or not isinstance(price_twd, int) or price_twd <= 0:
                errors.append(
                    "facebook_marketplace_price_update.price_twd must be a positive integer"
                )
            if routing_task_type != "facebook_marketplace_price_update":
                errors.append(
                    "facebook_marketplace_price_update requires routing.task_type="
                    "facebook_marketplace_price_update"
                )
            if value.get("external_targets") != [
                f"Facebook Marketplace item {listing_id}"
            ]:
                errors.append(
                    "facebook_marketplace_price_update external_targets must "
                    "contain exactly its Marketplace listing"
                )
            if isinstance(value.get("facebook_crosspost"), Mapping) or isinstance(
                value.get("facebook_page_post"), Mapping
            ):
                errors.append(
                    "facebook_marketplace_price_update cannot be combined with "
                    "other Facebook mutation capabilities"
                )
    user_facing_delivery = value.get("user_facing_delivery")
    if (
        routing_task_type in {
            "secondhand_commerce_group_status",
            "facebook_marketplace_readonly",
        }
        and user_facing_delivery is None
    ):
        errors.append(
            "secondhand_commerce_group_status requires user_facing_delivery"
        )
    if user_facing_delivery is not None:
        if not isinstance(user_facing_delivery, Mapping):
            errors.append("user_facing_delivery must be an object")
        else:
            if user_facing_delivery.get("required") is not True:
                errors.append("user_facing_delivery.required must be true")
            if user_facing_delivery.get("kind") != "commerce_group_status":
                errors.append(
                    "user_facing_delivery.kind must be commerce_group_status"
                )
            if user_facing_delivery.get("delivery") != "inline_only":
                errors.append(
                    "user_facing_delivery.delivery must be inline_only"
                )
            required_list("user_facing_delivery.subject_keys")
            subject_keys = user_facing_delivery.get("subject_keys")
            if (
                routing_task_type in {
                    "secondhand_commerce_group_status",
                    "facebook_marketplace_readonly",
                }
                and isinstance(subject_keys, list)
                and subject_keys
                and all(
                    isinstance(subject, str) and subject.strip()
                    for subject in subject_keys
                )
            ):
                from hermes_cli.user_facing_report import (
                    canonicalize_commerce_subject_keys,
                )

                normalized_delivery = dict(user_facing_delivery)
                normalized_delivery["subject_keys"] = (
                    canonicalize_commerce_subject_keys(subject_keys)
                )
                value["user_facing_delivery"] = normalized_delivery
                from hermes_cli.user_facing_report import (
                    commerce_subject_listing_ids,
                )

                canonical_subjects = normalized_delivery["subject_keys"]
                allowed_subject_listing_ids = {
                    listing_id
                    for subject in canonical_subjects
                    for listing_id in commerce_subject_listing_ids(subject)
                }
                if (
                    len(canonical_subjects) != 1
                    or not allowed_subject_listing_ids
                ):
                    errors.append(
                        "secondhand_commerce_group_status requires exactly "
                        "one known canonical delivery subject"
                    )
                targets = value.get("external_targets")
                if not isinstance(targets, list) or len(targets) != 1:
                    errors.append(
                        "secondhand_commerce_group_status requires exactly one "
                        "canonical Marketplace listing target"
                    )
                else:
                    target_listing_ids = [
                        exact_facebook_marketplace_listing_target_id(target)
                        for target in targets
                    ]
                    if (
                        any(listing_id is None for listing_id in target_listing_ids)
                        or len(set(target_listing_ids)) != 1
                        or not set(target_listing_ids) <= allowed_subject_listing_ids
                    ):
                        errors.append(
                            "secondhand_commerce_group_status external_targets "
                            "must each be one canonical Marketplace listing "
                            "target matching the delivery subject"
                        )
    if "facebook_crosspost" in value:
        crosspost = value.get("facebook_crosspost")
        if not isinstance(crosspost, Mapping):
            errors.append("facebook_crosspost must be an object")
        else:
            listing_id = crosspost.get("marketplace_listing_id")
            group_ids = crosspost.get("group_ids")
            group_names = crosspost.get("group_names")
            transport = crosspost.get("transport")
            if transport != "browser":
                errors.append(
                    "facebook_crosspost.transport must be browser; Meta removed "
                    "publish_to_groups and the Groups API from all Graph API "
                    "versions on 2024-04-22"
                )
            if routing_task_type != "facebook_marketplace_group_publish":
                errors.append(
                    "facebook_crosspost requires routing.task_type="
                    "facebook_marketplace_group_publish"
                )
            if (
                not isinstance(listing_id, str)
                or not listing_id.isascii()
                or not listing_id.isdigit()
            ):
                errors.append(
                    "facebook_crosspost.marketplace_listing_id must be a "
                    "numeric string"
                )
            valid_group_ids = bool(
                isinstance(group_ids, list)
                and group_ids
                and all(
                    isinstance(group_id, str)
                    and group_id.isascii()
                    and group_id.isdigit()
                    for group_id in group_ids
                )
                and len(set(group_ids)) == len(group_ids)
            )
            normalized_group_names = (
                [normalize_facebook_group_name(name) for name in group_names]
                if isinstance(group_names, list)
                else []
            )
            valid_group_names = bool(
                isinstance(group_names, list)
                and group_names
                and len(group_names) <= 20
                and all(
                    isinstance(name, str)
                    and normalize_facebook_group_name(name) == name
                    and name
                    for name in group_names
                )
                and len(set(normalized_group_names)) == len(group_names)
            )
            if valid_group_ids == valid_group_names:
                errors.append(
                    "facebook_crosspost requires exactly one of unique numeric "
                    "group_ids or unique exact group_names"
                )
            targets = value.get("external_targets")
            mentioned_listing_ids, mentioned_group_ids = (
                facebook_crosspost_target_ids(targets)
            )
            mentioned_group_names = facebook_crosspost_target_names(targets)
            if (
                not mentioned_listing_ids
                or (valid_group_ids and not mentioned_group_ids)
                or (valid_group_names and not mentioned_group_names)
            ):
                errors.append(
                    "facebook_crosspost requires Facebook Marketplace and "
                    "group destinations in external_targets"
                )
            if valid_group_ids:
                if mentioned_group_ids != frozenset(group_ids):
                    errors.append(
                        "facebook_crosspost.group_ids must match group ids "
                        "shown in external_targets"
                    )
                if mentioned_group_names:
                    errors.append(
                        "id-bound facebook_crosspost cannot mix named group targets"
                    )
            if valid_group_names:
                if mentioned_group_names != frozenset(normalized_group_names):
                    errors.append(
                        "facebook_crosspost.group_names must match exact group "
                        "names shown in external_targets"
                    )
                if mentioned_group_ids:
                    errors.append(
                        "name-bound facebook_crosspost cannot mix numeric group targets"
                    )
            if (
                not mentioned_listing_ids
                or (
                    not isinstance(listing_id, str)
                    or mentioned_listing_ids != frozenset({listing_id})
                )
            ):
                errors.append(
                    "facebook_crosspost.marketplace_listing_id must match "
                    "the listing id shown in external_targets"
                )
    if (
        browser_readonly_marketplace_inspection_requested(value)
        and facebook_crosspost_inspection_listing_id(value) is None
    ):
        errors.append(
            "browser_readonly Marketplace More options/List in more places "
            "inspection requires exactly one Marketplace listing id in "
            "external_targets, no group targets, an allowed read-only "
            "transition naming that same listing id, and an explicit ban on "
            "checkbox selection, Post/Publish submission, and every external "
            "state change; allowed scope must contain no mutation action and "
            "split multiple listings into separate contracts"
        )

    max_iterations = value.get("stop_rules", {}).get("max_iterations")
    if not isinstance(max_iterations, int) or not 1 <= max_iterations <= 20:
        errors.append("stop_rules.max_iterations must be an integer from 1 to 20")
    max_runtime = value.get("stop_rules", {}).get("max_runtime_seconds")
    if not isinstance(max_runtime, int) or not 60 <= max_runtime <= 14400:
        errors.append("stop_rules.max_runtime_seconds must be 60..14400")

    if errors:
        raise LoopContractError("; ".join(errors))
    value["contract_version"] = CONTRACT_VERSION
    return value
