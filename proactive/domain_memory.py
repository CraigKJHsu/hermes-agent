"""Typed domain-memory contracts and accepted MemoryDelta validation.

The operational registry is deliberately separate from semantic recall.  This
module contains only deterministic schema selection and payload validation; the
SQLite projection lives in :mod:`hermes_cli.kanban_db` so acceptance and the
registry update can share one transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping


class DomainMemoryError(ValueError):
    """Raised when a domain contract or MemoryDelta is not deterministic."""


BUILTIN_DOMAIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "solobizai.case.v1": {
        "schema_id": "solobizai.case.v1",
        "domain_key": "solobizai",
        "entity_type": "SoloBizAiCase",
        "required_entity_fields": ["entity_id", "label", "status"],
        "artifact_types": [
            "facebook_page_post",
            "podcast_episode",
            "audio_brief",
        ],
        "required_artifact_fields": ["artifact_type", "status"],
    },
    "secondhand.item.v1": {
        "schema_id": "secondhand.item.v1",
        "domain_key": "secondhand",
        "entity_type": "ResaleItem",
        "required_entity_fields": ["entity_id", "label", "status"],
        "artifact_types": [
            "shopee_listing",
            "facebook_marketplace_listing",
            "facebook_group_post",
        ],
        "required_artifact_fields": ["artifact_type", "status"],
    },
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_BUILTIN_ARTIFACT_PLATFORMS = {
    "facebook_page_post": "facebook",
    "facebook_marketplace_listing": "facebook",
    "facebook_group_post": "facebook",
    "shopee_listing": "shopee",
    "podcast_episode": "podcast",
    "audio_brief": "internal",
}
_ENTITY_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_ISOISH_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _clean_text(value: object, *, field: str, max_length: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise DomainMemoryError(f"{field} is required")
    if len(text) > max_length:
        raise DomainMemoryError(f"{field} exceeds {max_length} characters")
    return text


def _clean_slug(value: object, *, field: str) -> str:
    text = str(value or "").strip().casefold()
    if not _SLUG_RE.fullmatch(text):
        raise DomainMemoryError(f"{field} must be a stable lowercase slug")
    return text


def _clean_string_list(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise DomainMemoryError(f"{field} must be a list")
    cleaned = [_clean_text(item, field=field, max_length=128) for item in value]
    if not allow_empty and not cleaned:
        raise DomainMemoryError(f"{field} must not be empty")
    if len(set(cleaned)) != len(cleaned):
        raise DomainMemoryError(f"{field} must not contain duplicates")
    return cleaned


def normalize_domain_memory_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize a Loop Contract ``domain_memory`` section."""
    if not isinstance(value, Mapping):
        raise DomainMemoryError("domain_memory must be an object")
    mode = str(value.get("mode") or "").strip().casefold()
    if mode not in {"query", "mutate"}:
        raise DomainMemoryError("domain_memory.mode must be query or mutate")
    schema_id = _clean_slug(value.get("schema_id"), field="domain_memory.schema_id")
    builtin = BUILTIN_DOMAIN_SCHEMAS.get(schema_id)
    definition = (
        deepcopy(builtin)
        if builtin is not None
        else {
            "schema_id": schema_id,
            "domain_key": _clean_slug(
                value.get("domain_key"), field="domain_memory.domain_key"
            ),
            "entity_type": _clean_text(
                value.get("entity_type"),
                field="domain_memory.entity_type",
                max_length=128,
            ),
            "required_entity_fields": _clean_string_list(
                value.get("required_entity_fields"),
                field="domain_memory.required_entity_fields",
            ),
            "artifact_types": _clean_string_list(
                value.get("artifact_types"),
                field="domain_memory.artifact_types",
            ),
            "required_artifact_fields": _clean_string_list(
                value.get("required_artifact_fields"),
                field="domain_memory.required_artifact_fields",
            ),
        }
    )
    if not _ENTITY_TYPE_RE.fullmatch(str(definition["entity_type"])):
        raise DomainMemoryError(
            "domain_memory.entity_type must be a stable PascalCase identifier"
        )

    # Built-in schemas are system-owned. Callers may repeat their definition,
    # but cannot silently change it under the same versioned schema id.
    for key in (
        "domain_key",
        "entity_type",
        "required_entity_fields",
        "artifact_types",
        "required_artifact_fields",
    ):
        if key in value and value[key] != definition[key]:
            raise DomainMemoryError(
                f"domain_memory.{key} conflicts with built-in {schema_id}"
            )

    expected_total = value.get("expected_total")
    if expected_total is not None:
        if not isinstance(expected_total, int) or expected_total < 0:
            raise DomainMemoryError(
                "domain_memory.expected_total must be a non-negative integer or null"
            )

    require_delta = bool(value.get("require_delta_on_acceptance", mode == "mutate"))
    if mode == "mutate" and not require_delta:
        raise DomainMemoryError(
            "domain_memory mutate mode requires require_delta_on_acceptance=true"
        )
    if mode == "query" and require_delta:
        raise DomainMemoryError(
            "domain_memory query mode requires require_delta_on_acceptance=false"
        )
    return {
        **definition,
        "mode": mode,
        "require_delta_on_acceptance": require_delta,
        "expected_total": expected_total,
    }


def _contract_text(contract: Mapping[str, Any]) -> str:
    identity = contract.get("identity")
    goal = contract.get("goal")
    payload = {
        "project": identity.get("project") if isinstance(identity, Mapping) else None,
        "topic": identity.get("topic_name") if isinstance(identity, Mapping) else None,
        "task_type": (
            contract.get("routing", {}).get("task_type")
            if isinstance(contract.get("routing"), Mapping)
            else None
        ),
        "objective": goal.get("objective") if isinstance(goal, Mapping) else None,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()


def infer_builtin_domain_memory(
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a conservative built-in domain contract for known Topic families.

    Mutation mode is inferred only from registered task types. Free-form user
    wording never grants a registry write by itself.
    """
    text = _contract_text(contract)
    routing = contract.get("routing")
    task_type = str(
        routing.get("task_type") if isinstance(routing, Mapping) else ""
    ).strip()
    schema_id = ""
    if any(
        marker in text
        for marker in ("solobizai", "solobiz ai", "ai bizweek", "ai_bizweek")
    ):
        schema_id = "solobizai.case.v1"
    elif task_type.startswith("secondhand_") or any(
        marker in text for marker in ("secondhand", "二手拍賣", "二手商品")
    ):
        schema_id = "secondhand.item.v1"
    if not schema_id:
        return None
    mutation_task_types = {
        "facebook_page_api_publish",
        "facebook_marketplace_group_publish",
        "facebook_marketplace_price_update",
    }
    delivery = contract.get("user_facing_delivery")
    if (
        task_type == "product_marketing"
        and isinstance(delivery, Mapping)
        and delivery.get("body_field") == "inline_content_package"
    ):
        # Draft production is not a registry inventory. Inferring query mode
        # here makes the worker compiler discard the user-provided source.
        return None
    return normalize_domain_memory_contract({
        "schema_id": schema_id,
        "mode": "mutate" if task_type in mutation_task_types else "query",
        "require_delta_on_acceptance": task_type in mutation_task_types,
    })


def attach_domain_memory_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Attach an explicit or conservatively inferred domain contract."""
    normalized = deepcopy(dict(contract))
    supplied = normalized.get("domain_memory")
    if supplied is not None:
        normalized["domain_memory"] = normalize_domain_memory_contract(supplied)
        return normalized
    inferred = infer_builtin_domain_memory(normalized)
    if inferred is not None:
        normalized["domain_memory"] = inferred
    return normalized


def _canonical_json_mapping(
    value: object,
    *,
    field: str,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainMemoryError(f"{field} must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DomainMemoryError(f"{field} must be JSON serializable") from exc
    if not isinstance(decoded, dict):
        raise DomainMemoryError(f"{field} must be an object")
    return decoded


def _field_present(
    name: str,
    *,
    top_level: Mapping[str, Any],
    attributes: Mapping[str, Any],
) -> bool:
    value = top_level.get(name, attributes.get(name))
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def normalize_memory_deltas(
    raw_deltas: object,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate worker deltas against one normalized domain contract."""
    spec = normalize_domain_memory_contract(contract)
    if raw_deltas is None:
        raw_deltas = []
    if not isinstance(raw_deltas, list):
        raise DomainMemoryError("metadata.domain_memory_deltas must be a list")
    if spec["mode"] == "query" and raw_deltas:
        raise DomainMemoryError(
            "query-mode domain contracts cannot write domain_memory_deltas"
        )
    if spec["require_delta_on_acceptance"] and not raw_deltas:
        raise DomainMemoryError(
            "this domain mutation requires at least one domain_memory_delta"
        )
    normalized: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for index, raw in enumerate(raw_deltas):
        field = f"metadata.domain_memory_deltas[{index}]"
        if not isinstance(raw, Mapping):
            raise DomainMemoryError(f"{field} must be an object")
        top_level_artifact_fields = {
            "artifact_id",
            "artifact_type",
            "artifact_key",
            "platform",
            "external_id",
            "public_url",
            "evidence_url",
            "evidence_ref",
            "group_id",
            "group_name",
        }
        misplaced = sorted(top_level_artifact_fields.intersection(raw.keys()))
        if misplaced:
            raise DomainMemoryError(
                f"{field} must be an entity delta with artifact state inside "
                "artifacts[]; top-level artifact fields are not allowed: "
                + ", ".join(misplaced)
            )
        operation = str(raw.get("operation") or "upsert").strip().casefold()
        if operation != "upsert":
            raise DomainMemoryError(f"{field}.operation must be upsert")
        entity_id = _clean_text(
            raw.get("entity_id"), field=f"{field}.entity_id", max_length=256
        )
        if entity_id in seen_entities:
            raise DomainMemoryError(
                f"{field}.entity_id duplicates another delta in this completion"
            )
        seen_entities.add(entity_id)
        label = _clean_text(raw.get("label"), field=f"{field}.label")
        status = _clean_slug(raw.get("status"), field=f"{field}.status")
        attributes = _canonical_json_mapping(
            raw.get("attributes"), field=f"{field}.attributes"
        )
        entity_top = {
            "entity_id": entity_id,
            "label": label,
            "status": status,
        }
        missing_entity = [
            name
            for name in spec["required_entity_fields"]
            if not _field_present(name, top_level=entity_top, attributes=attributes)
        ]
        if missing_entity:
            raise DomainMemoryError(
                f"{field} is missing required entity fields: "
                + ", ".join(missing_entity)
            )

        raw_artifacts = raw.get("artifacts") or []
        if not isinstance(raw_artifacts, list):
            raise DomainMemoryError(f"{field}.artifacts must be a list")
        artifacts: list[dict[str, Any]] = []
        seen_artifacts: set[tuple[str, str, str]] = set()
        for artifact_index, artifact_raw in enumerate(raw_artifacts):
            artifact_field = f"{field}.artifacts[{artifact_index}]"
            if not isinstance(artifact_raw, Mapping):
                raise DomainMemoryError(f"{artifact_field} must be an object")
            artifact_type = _clean_slug(
                artifact_raw.get("artifact_type"),
                field=f"{artifact_field}.artifact_type",
            )
            if artifact_type not in set(spec["artifact_types"]):
                raise DomainMemoryError(
                    f"{artifact_field}.artifact_type is not allowed by {spec['schema_id']}"
                )
            platform = _clean_slug(
                artifact_raw.get("platform")
                or _BUILTIN_ARTIFACT_PLATFORMS.get(artifact_type),
                field=f"{artifact_field}.platform",
            )
            artifact_status = _clean_slug(
                artifact_raw.get("status"), field=f"{artifact_field}.status"
            )
            group_id_alias = None
            if artifact_type == "facebook_group_post" and "group_id" in artifact_raw:
                raw_group_id = artifact_raw["group_id"]
                if (
                    not isinstance(raw_group_id, str)
                    or re.fullmatch(r"[1-9][0-9]*", raw_group_id.strip()) is None
                ):
                    raise DomainMemoryError(
                        f"{artifact_field}.group_id must be ASCII digits without leading zeros"
                    )
                group_id_alias = raw_group_id.strip()
            explicit_external_id = str(
                artifact_raw.get("external_id") or ""
            ).strip()
            if (
                group_id_alias
                and explicit_external_id
                and group_id_alias != explicit_external_id
            ):
                raise DomainMemoryError(
                    f"{artifact_field}.group_id must match external_id"
                )
            external_id = str(
                explicit_external_id
                or group_id_alias
                or ""
            ).strip()
            public_url = str(artifact_raw.get("public_url") or "").strip()
            artifact_key = str(artifact_raw.get("artifact_key") or "").strip()
            if not artifact_key:
                stable_source = external_id or public_url
                if not stable_source:
                    raise DomainMemoryError(
                        f"{artifact_field} requires artifact_key, external_id, or public_url"
                    )
                artifact_key = (
                    artifact_type
                    + ":"
                    + hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
                )
            artifact_attributes = _canonical_json_mapping(
                artifact_raw.get("attributes"),
                field=f"{artifact_field}.attributes",
            )
            if "group_name" in artifact_raw and "group_name" not in artifact_attributes:
                if not isinstance(artifact_raw["group_name"], str):
                    raise DomainMemoryError(
                        f"{artifact_field}.group_name must be a string"
                    )
                artifact_attributes["group_name"] = _clean_text(
                    artifact_raw["group_name"],
                    field=f"{artifact_field}.group_name",
                )
            if (
                "membership_status" in artifact_raw
                and "membership_status" not in artifact_attributes
            ):
                artifact_attributes["membership_status"] = _clean_slug(
                    artifact_raw["membership_status"],
                    field=f"{artifact_field}.membership_status",
                )
            artifact_top = {
                "artifact_type": artifact_type,
                "platform": platform,
                "status": artifact_status,
                "external_id": external_id,
                "public_url": public_url,
                "artifact_key": artifact_key,
            }
            evidence_ref = str(artifact_raw.get("evidence_ref") or "").strip()
            missing_artifact = [
                name
                for name in spec["required_artifact_fields"]
                if not _field_present(
                    name,
                    top_level=artifact_top,
                    attributes=artifact_attributes,
                )
            ]
            if missing_artifact:
                raise DomainMemoryError(
                    f"{artifact_field} is missing required fields: "
                    + ", ".join(missing_artifact)
                )
            identity = (artifact_type, platform, artifact_key)
            if identity in seen_artifacts:
                raise DomainMemoryError(
                    f"{artifact_field} duplicates another artifact in this entity delta"
                )
            seen_artifacts.add(identity)
            verified_at = str(artifact_raw.get("verified_at") or "").strip()
            if verified_at and not _ISOISH_RE.fullmatch(verified_at):
                raise DomainMemoryError(
                    f"{artifact_field}.verified_at must be ISO-8601 with timezone"
                )
            externally_materialized = artifact_status in {
                "published",
                "listed",
                "active",
                "live",
                "created",
                "verified",
                "existing",
                "sold",
                "removed",
            }
            if externally_materialized and not (external_id or public_url):
                raise DomainMemoryError(
                    f"{artifact_field} requires public_url or external_id for "
                    f"status={artifact_status}"
                )
            if externally_materialized and not verified_at:
                raise DomainMemoryError(
                    f"{artifact_field}.verified_at is required for "
                    f"status={artifact_status}"
                )
            if externally_materialized and not evidence_ref:
                raise DomainMemoryError(
                    f"{artifact_field}.evidence_ref is required for "
                    f"status={artifact_status}"
                )
            if (
                artifact_type == "podcast_episode"
                and artifact_status == "published"
                and not _field_present(
                    "episode_number",
                    top_level=artifact_top,
                    attributes=artifact_attributes,
                )
            ):
                raise DomainMemoryError(
                    f"{artifact_field}.attributes.episode_number is required "
                    "for a published podcast episode"
                )
            artifacts.append({
                **artifact_top,
                "attributes": artifact_attributes,
                "verified_at": verified_at or None,
                "evidence_ref": evidence_ref or None,
            })

        if spec["mode"] == "mutate":
            represented_types = {item["artifact_type"] for item in artifacts}
            missing_slots = sorted(set(spec["artifact_types"]) - represented_types)
            if missing_slots:
                raise DomainMemoryError(
                    f"{field}.artifacts must explicitly represent every domain "
                    "artifact slot; missing: " + ", ".join(missing_slots)
                )

        raw_evidence_refs = raw.get("evidence_refs")
        if raw_evidence_refs is None:
            raw_evidence_refs = list(
                dict.fromkeys(
                    item["evidence_ref"]
                    for item in artifacts
                    if item.get("evidence_ref")
                )
            )
        evidence_refs = _clean_string_list(
            raw_evidence_refs, field=f"{field}.evidence_refs"
        )
        normalized.append({
            "operation": operation,
            "entity_id": entity_id,
            "label": label,
            "status": status,
            "attributes": attributes,
            "artifacts": artifacts,
            "evidence_refs": evidence_refs,
        })
    return normalized


def validate_delta_external_effect_refs(
    deltas: list[Mapping[str, Any]],
    external_effects: list[Mapping[str, Any]],
) -> None:
    """Require each externally projected artifact to cite its task effect."""
    available = {
        f"task_external_effect:{str(effect.get('platform') or '').strip()}:"
        f"{str(effect.get('effect_key') or 'create').strip()}"
        for effect in external_effects
    }
    informational_statuses = {
        "not_published",
        "not_listed",
        "not_created",
        "planned",
        "draft",
        "unknown",
    }
    for delta in deltas:
        refs = set(delta.get("evidence_refs") or [])
        for artifact in delta.get("artifacts") or []:
            if str(artifact.get("status") or "") in informational_statuses:
                continue
            platform = str(artifact.get("platform") or "").strip()
            evidence_ref = str(artifact.get("evidence_ref") or "").strip()
            if evidence_ref not in available:
                raise DomainMemoryError(
                    "domain artifact evidence_ref has no exact completion "
                    f"external effect for platform {platform}"
                )
            if not evidence_ref.startswith(f"task_external_effect:{platform}:"):
                raise DomainMemoryError(
                    "domain artifact evidence_ref platform does not match "
                    f"artifact platform {platform}"
                )
            if evidence_ref not in refs:
                raise DomainMemoryError(
                    "domain delta evidence_refs must include each artifact's "
                    "exact evidence_ref"
                )
