"""Task-scoped Facebook Page photo publishing through Meta Graph API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from tools.registry import registry


TOOLSET = "facebook-pages-api"
DEFAULT_API_VERSION = "v26.0"


@dataclass(frozen=True)
class FacebookPageConfig:
    api_version: str
    page_id: str
    page_name: str
    page_url: str
    access_token: str
    app_secret: str = ""

    @property
    def missing(self) -> list[str]:
        values = {
            "FACEBOOK_GRAPH_API_VERSION": self.api_version,
            "FACEBOOK_PAGE_ID": self.page_id,
            "FACEBOOK_PAGE_NAME": self.page_name,
            "FACEBOOK_PAGE_URL": self.page_url,
            "FACEBOOK_PAGE_ACCESS_TOKEN": self.access_token,
        }
        return [name for name, value in values.items() if not value]


class FacebookGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenClawCapabilityScope:
    task_id: str
    run_id: int
    delegation_id: str
    contract_fingerprint: str
    approval_grant_id: str
    backend_agent_id: str
    board: str = ""
    task_type: str = "facebook_page_api_publish"


def _load_page_config() -> FacebookPageConfig:
    values: dict[str, str] = {}
    try:
        from hermes_constants import get_default_hermes_root

        root_env = get_default_hermes_root() / ".env"
        if root_env.is_file():
            values.update({
                str(key): str(value or "")
                for key, value in dotenv_values(root_env).items()
            })
    except (ImportError, OSError, ValueError):
        pass
    for name in (
        "FACEBOOK_GRAPH_API_VERSION",
        "FACEBOOK_PAGE_ID",
        "FACEBOOK_PAGE_NAME",
        "FACEBOOK_PAGE_URL",
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "FACEBOOK_APP_SECRET",
    ):
        if os.environ.get(name):
            values[name] = os.environ[name]
    return FacebookPageConfig(
        api_version=str(
            values.get("FACEBOOK_GRAPH_API_VERSION") or DEFAULT_API_VERSION
        ).strip(),
        page_id=str(values.get("FACEBOOK_PAGE_ID") or "").strip(),
        page_name=str(values.get("FACEBOOK_PAGE_NAME") or "").strip(),
        page_url=str(values.get("FACEBOOK_PAGE_URL") or "").strip().rstrip("/"),
        access_token=str(values.get("FACEBOOK_PAGE_ACCESS_TOKEN") or "").strip(),
        app_secret=str(values.get("FACEBOOK_APP_SECRET") or "").strip(),
    )


def _tool_available() -> bool:
    return not _load_page_config().missing


def _appsecret_proof(config: FacebookPageConfig) -> str:
    if not config.app_secret:
        return ""
    return hmac.new(
        config.app_secret.encode("utf-8"),
        config.access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _graph_request(
    config: FacebookPageConfig,
    method: str,
    path: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Mapping[str, Any]] = None,
    files: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    query = dict(params or {})
    proof = _appsecret_proof(config)
    if proof:
        query["appsecret_proof"] = proof
    url = f"https://graph.facebook.com/{config.api_version}/{path.lstrip('/')}"
    try:
        response = httpx.request(
            method,
            url,
            params=query,
            data=dict(data or {}),
            files=files,
            headers={"Authorization": f"Bearer {config.access_token}"},
            timeout=httpx.Timeout(60.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise FacebookGraphError(
            f"Graph API transport error: {type(exc).__name__}"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise FacebookGraphError(
            f"Graph API returned non-JSON HTTP {response.status_code}"
        ) from exc
    if not isinstance(payload, dict):
        raise FacebookGraphError("Graph API response was not an object")
    if response.status_code >= 400 or "error" in payload:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = error.get("code")
        error_type = str(error.get("type") or "GraphAPIError")
        message = str(error.get("message") or f"HTTP {response.status_code}")
        raise FacebookGraphError(
            f"Graph API {error_type} code={code}: {message}"
        )
    return payload


def _fetch_page_status(config: FacebookPageConfig) -> dict[str, Any]:
    payload = _graph_request(
        config,
        "GET",
        config.page_id,
        params={"fields": "id,name,link,username"},
    )
    observed_id = str(payload.get("id") or "").strip()
    observed_name = str(payload.get("name") or "").strip()
    observed_link = _canonical_page_url(str(payload.get("link") or ""))
    observed_username = str(payload.get("username") or "").strip()
    username_link = _canonical_page_url(
        f"https://www.facebook.com/{observed_username}"
        if observed_username
        else ""
    )
    configured_link = _canonical_page_url(config.page_url)
    canonical_url_verified = bool(
        configured_link
        and (
            observed_link.casefold() == configured_link.casefold()
            or username_link.casefold() == configured_link.casefold()
        )
    )
    identity_verified = (
        observed_id == config.page_id
        and observed_name == config.page_name
        and canonical_url_verified
    )
    return {
        "success": identity_verified,
        "identity_verified": identity_verified,
        "page_id": observed_id,
        "page_name": observed_name,
        "page_url": configured_link if canonical_url_verified else observed_link,
        "graph_page_url": observed_link,
        "page_username": observed_username,
        "canonical_url_verified": canonical_url_verified,
        "api_version": config.api_version,
        "warning": None if identity_verified else "configured Page identity mismatch",
    }


def _canonical_page_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        parsed_port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or str(parsed.hostname or "").casefold() != "www.facebook.com"
        or parsed_port is not None
        or not parsed.path.startswith("/")
        or parsed.path.count("/") != 1
        or not parsed.path[1:]
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return normalized


def _worker_scope() -> tuple[str, Optional[int], str]:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    try:
        run_id = int(run_id_raw)
    except (TypeError, ValueError):
        run_id = None
    return task_id, run_id, board


def _authorized_execution_contract() -> tuple[Optional[dict[str, str]], str]:
    from hermes_cli import kanban_db as kb

    task_id, run_id, board = _worker_scope()
    if not task_id or run_id is None:
        return None, "Facebook Page publish requires an active Kanban worker run."
    with kb.connect_closing(board=board or None) as conn:
        role = kb.validate_grace_loop_worker_auth(
            conn,
            task_id=task_id,
            run_id=str(run_id),
            claim_lock=os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "").strip(),
            worker_auth_token=os.environ.get(
                "HERMES_KANBAN_WORKER_AUTH_TOKEN", ""
            ).strip(),
        )
        if role != "execution":
            return None, (
                "Facebook Page publish requires an authenticated Grace "
                "execution worker."
            )
        contract = kb.grace_task_facebook_page_post_contract(conn, task_id)
    if contract is None:
        return None, (
            "Facebook Page publish requires an exact consumed owner approval "
            "bound to the active Loop Contract."
        )
    return contract, ""


def _authorized_openclaw_contract(
    scope: OpenClawCapabilityScope,
) -> tuple[Optional[dict[str, Any]], str]:
    """Validate a bridge-owned OpenClaw capability against the active run."""
    from hermes_cli import kanban_db as kb

    if scope.backend_agent_id != "missioncrew-facebook-page-operator":
        return None, "Facebook Page capability requires the dedicated OpenClaw operator."
    with kb.connect_closing(board=scope.board or None) as conn:
        task = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (scope.task_id,),
        ).fetchone()
        if (
            task is None
            or task["status"] != "running"
            or int(task["current_run_id"] or 0) != scope.run_id
        ):
            return None, "Facebook Page capability requires the active Kanban run."
        run = conn.execute(
            "SELECT status, metadata FROM task_runs WHERE id = ? AND task_id = ?",
            (scope.run_id, scope.task_id),
        ).fetchone()
        if run is None or run["status"] != "running":
            return None, "Facebook Page capability worker run is not active."
        try:
            metadata = json.loads(str(run["metadata"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "Facebook Page capability run metadata is invalid."
        preflight = scope.task_type == "facebook_page_publish_preflight"
        expected_tools = (
            {"facebook_page_publish_preflight"}
            if preflight
            else {"facebook_page_graph_status", "facebook_page_graph_publish"}
        )
        if (
            str(metadata.get("delegation_id") or "") != scope.delegation_id
            or str(metadata.get("contract_fingerprint") or "")
            != scope.contract_fingerprint
            or str(metadata.get("approval_grant_id") or "") != scope.approval_grant_id
            or (not preflight and not scope.approval_grant_id)
            or (preflight and bool(scope.approval_grant_id))
            or str(metadata.get("backend_agent_id") or "")
            != scope.backend_agent_id
            or set(metadata.get("allowed_tools") or []) != expected_tools
            or int(metadata.get("external_effect_budget") or 0) != (0 if preflight else 1)
            or str(metadata.get("task_type") or "") != scope.task_type
            or list(metadata.get("credential_refs") or [])
            != ["missioncrew-facebook-page"]
        ):
            return None, "Facebook Page capability does not match the active Loop Contract."
        contract = (
            metadata.get("loop_contract")
            if preflight
            else kb.grace_task_facebook_page_post_contract(conn, scope.task_id)
        )
    if not isinstance(contract, Mapping):
        return None, (
            "Facebook Page capability requires an exact active Loop Contract."
        )
    return dict(contract), ""


def _contract_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _contract_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _contract_strings(child)]
    return []


def _embedded_page_source(contract: Mapping[str, Any]) -> tuple[str, str]:
    original = str(contract.get("original_request") or "")
    match = re.search(
        r"sha256=([0-9a-f]{64})\s*\nBEGIN_FACEBOOK_PAGE_SOURCE_TEXT\n(.*?)\nEND_FACEBOOK_PAGE_SOURCE_TEXT",
        original,
        flags=re.DOTALL,
    )
    if match is None:
        raise ValueError("Loop Contract has no canonical embedded Facebook Page source.")
    return match.group(2), match.group(1)


def _reviewed_full_package_page(
    conn: Any,
    kb: Any,
    execution_id: str,
    review_id: str,
    source_run: Any,
    review_run: Any,
) -> dict[str, Any]:
    """Project the one reviewed Page asset from a complete multi-asset package."""
    source = source_run.metadata or {}
    review = review_run.metadata or {}
    report = source.get("user_facing_report")
    evidence = source.get("acceptance_evidence")
    verdict = review.get("verification")
    if not all(isinstance(value, Mapping) for value in (report, evidence, verdict)):
        raise ValueError("Accepted full package lacks structured Page review evidence.")
    sections = report.get("sections")
    evidence_sections = evidence.get("sections")
    page_post = source.get("facebook_page_post")
    message = sections.get("facebook_page_post") if isinstance(sections, Mapping) else None
    body = report.get("body")
    digests = verdict.get("controller_digests")
    headings = list(re.finditer(
        r"(?m)^##[ \t]+([1-6])[ \t]*[.、|｜][^\r\n]*(?:\r?\n)", body,
    )) if isinstance(body, str) else []
    if not (
        report.get("package_kind") == "full_publication_package"
        and report.get("complete") is True
        and source_run.id == kb.latest_run(conn, execution_id).id
        and verdict.get("package_kind") == "full_publication_package"
        and verdict.get("package_complete") is True
        and verdict.get("facebook_page_post_text_equals_section") is True
        and verdict.get("controller_zero_effect_check") is True
        and verdict.get("external_effect_count") == 0
        and verdict.get("external_effects") == []
        and source.get("external_effect_budget") == 0
        and source.get("external_effects") == []
        and report.get("external_effects") == []
        and not kb.list_external_effects(conn, execution_id)
        and not kb.list_external_effects(conn, review_id)
        and isinstance(message, str) and message.strip()
        and isinstance(digests, Mapping)
        and isinstance(body, str)
        and digests.get("body_utf8_byte_count") == len(body.encode("utf-8"))
        and digests.get("body_sha256") == hashlib.sha256(body.encode("utf-8")).hexdigest()
        and [heading.group(1) for heading in headings] == list("123456")
        and body[headings[0].end():headings[1].start()].strip() == message
        and isinstance(evidence_sections, Mapping)
        and evidence_sections.get("facebook_page_post") == message
        and isinstance(page_post, Mapping) and page_post.get("text") == message
        and source.get("attachment_manifest") == kb.task_attachment_manifest(conn, execution_id)
    ):
        raise ValueError("Accepted full package Page text or review evidence changed.")
    sources = (report.get("assets"), evidence.get("assets"), verdict.get("assets"))
    if any(not isinstance(assets, list) for assets in sources):
        raise ValueError("Accepted full package lacks reviewed Page Hero assets.")
    heroes = [
        [asset for asset in assets if isinstance(asset, Mapping) and asset.get("asset_family") == "page_hero"]
        for assets in sources
    ]
    if any(len(group) != 1 for group in heroes):
        raise ValueError("Accepted package must have one reviewed Page Hero.")
    declared, accepted, reviewed = (group[0] for group in heroes)
    filename = reviewed.get("filename")
    digest = reviewed.get("sha256")
    width, height = reviewed.get("width"), reviewed.get("height")
    if not (
        isinstance(filename, str) and filename and Path(filename).name == filename
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        and type(width) is int and type(height) is int
        and width > 0 and width * 9 == height * 16
        and reviewed.get("aspect_ratio") == "16:9"
        and reviewed.get("byte_for_byte_equal") is True
        and reviewed.get("visual_review") == "pass"
        and reviewed.get("ai_disclosure_visible") is True
        and type(reviewed.get("attachment_id")) is int
        and all(
            asset.get("filename") == filename
            and asset.get("sha256") == digest
            and asset.get("width") == width
            and asset.get("height") == height
            for asset in (declared, accepted)
        )
    ):
        raise ValueError("Accepted Page Hero review conflicts with the complete package.")
    matching = [
        attachment for attachment in kb.list_attachments(conn, execution_id)
        if attachment.id == reviewed["attachment_id"]
        and attachment.filename == filename
    ]
    if len(matching) != 1:
        raise ValueError("Accepted Page Hero has no unique controller attachment.")
    attachment = matching[0]
    path = Path(attachment.stored_path)
    try:
        image_bytes = path.read_bytes()
        dimensions = _png_dimensions(image_bytes)
    except (OSError, ValueError) as exc:
        raise ValueError("Accepted Page Hero attachment is unavailable.") from exc
    if (attachment.size != len(image_bytes)
        or hashlib.sha256(image_bytes).hexdigest() != digest
        or dimensions != (width, height)):
        raise ValueError("Accepted Page Hero attachment bytes changed.")
    return {
        "execution_task_id": execution_id, "execution_run_id": source_run.id,
        "review_task_id": review_id, "review_run_id": review_run.id,
        "source_field": "user_facing_report.sections.facebook_page_post",
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "message_utf8_bytes": len(message.encode("utf-8")),
        "image_path": str(path), "image_sha256": digest,
        "image_bytes": len(image_bytes),
        "image_resolution": {"source": "reviewed_full_package_attachment", "candidate_count": 1,
                             "attachment_id": attachment.id},
        "image_filename": filename,
        "dimensions": f"{width}×{height}",
    }


def _resolve_accepted_page_source(
    contract: Mapping[str, Any],
    *,
    board: Optional[str] = None,
    require_current_visual_safety: bool,
) -> Optional[dict[str, Any]]:
    """Resolve one sealed, same-Topic accepted package from controller state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.grace_review_metadata import (
        grace_review_accepted,
        page_hero_review_evidence,
    )

    prefix = "Use accepted Facebook Page package: "
    entries = [s for s in (contract.get("scope") or {}).get("allowed", [])
               if isinstance(s, str) and s.startswith(prefix)]
    if not entries:
        return None
    match = re.fullmatch(
        re.escape(prefix) + r"execution_task_id=(t_[0-9a-f]+); review_task_id=(t_[0-9a-f]+)",
        entries[0],
    )
    if len(entries) != 1 or match is None:
        raise ValueError("Preflight requires one exact accepted package selection.")
    execution_id, review_id = match.groups()
    identity = contract.get("identity") or {}
    with kb.connect_closing(board=board) as conn:
        links = conn.execute(
            "SELECT d.platform, d.chat_id, d.thread_id, d.contract_snapshot, "
            "d.contract_fingerprint FROM grace_delegations d "
            "JOIN task_links l ON l.parent_id=d.execution_task_id AND l.child_id=d.review_task_id "
            "WHERE d.execution_task_id=? AND d.review_task_id=?",
            (execution_id, review_id),
        ).fetchall()
        if len(links) != 1:
            raise ValueError("Accepted package requires one exact delegation and review link.")
        link = links[0]
        if any(
            not identity.get(key) or str(link[key]) != str(identity[key])
            for key in ("platform", "chat_id", "thread_id")
        ):
            raise ValueError("Accepted package must belong to this exact Topic and review link.")
        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        review_run = kb.latest_run(conn, review_id)
        latest_source_run = kb.latest_run(conn, execution_id)
        try:
            source_run = kb.reviewed_execution_run(conn, review_run, execution_id)
        except ValueError as exc:
            raise ValueError(
                "Selected package review does not bind unchanged execution evidence."
            ) from exc
        if (
            execution is None or review is None
            or execution.status != "done" or review.status != "done"
            or source_run is None or review_run is None
            or latest_source_run is None or source_run.id != latest_source_run.id
            or source_run.status != "done" or review_run.status != "done"
            or not source_run.ended_at or not review_run.started_at
            or review_run.started_at < source_run.ended_at
            or not grace_review_accepted(review_run.metadata)
        ):
            raise ValueError("Selected package has no completed review of its latest execution.")
        source_attachments = kb.list_attachments(conn, execution_id)
        source_metadata = source_run.metadata or {}
        lineage_review_attachments = []
        source_lineage = source_metadata.get("source_lineage")
        if isinstance(source_lineage, Mapping):
            lineage_execution_id = source_lineage.get("execution_task_id")
            lineage_review_id = source_lineage.get("review_task_id")
            if (
                isinstance(lineage_execution_id, str)
                and re.fullmatch(r"t_[0-9a-f]+", lineage_execution_id)
                and isinstance(lineage_review_id, str)
                and re.fullmatch(r"t_[0-9a-f]+", lineage_review_id)
            ):
                lineage_links = conn.execute(
                    "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                    (lineage_execution_id, lineage_review_id),
                ).fetchall()
                lineage_execution = kb.get_task(conn, lineage_execution_id)
                lineage_review = kb.get_task(conn, lineage_review_id)
                lineage_review_run = kb.latest_run(conn, lineage_review_id)
                try:
                    lineage_execution_run = kb.reviewed_execution_run(
                        conn, lineage_review_run, lineage_execution_id
                    )
                except (TypeError, ValueError):
                    lineage_execution_run = None
                if (
                    len(lineage_links) == 1
                    and lineage_execution is not None
                    and lineage_execution.status == "done"
                    and lineage_review is not None
                    and lineage_review.status == "done"
                    and lineage_execution_run is not None
                    and lineage_execution_run.status == "done"
                    and lineage_review_run is not None
                    and lineage_review_run.status == "done"
                    and grace_review_accepted(lineage_review_run.metadata)
                ):
                    lineage_review_attachments = kb.list_attachments(
                        conn, lineage_review_id
                    )
    source_identity = (source_metadata.get("loop_contract") or {}).get("identity") or {}
    sealed_contract: Optional[dict[str, Any]] = None
    # Native Hermes completion replaces run metadata; the sealed delegation
    # remains the controller-owned authority for its original identity.
    snapshot = link["contract_snapshot"]
    if snapshot is not None:
        if (not isinstance(snapshot, str)
            or hashlib.sha256(snapshot.encode("utf-8")).hexdigest() != link["contract_fingerprint"]):
            raise ValueError("Accepted package has an invalid sealed contract fingerprint.")
        sealed_contract = json.loads(snapshot)
        sealed_identity = sealed_contract.get("identity") or {}
        for key in ("platform", "chat_id", "thread_id", "project"):
            if (not sealed_identity.get(key) or str(sealed_identity[key]) != str(identity.get(key) or "")
                or (key in source_identity and str(source_identity[key]) != str(sealed_identity[key]))):
                raise ValueError("Accepted package sealed identity does not match this Topic/project.")
        source_identity = sealed_identity
    if not identity.get("project") or source_identity.get("project") != identity["project"]:
        raise ValueError("Accepted package belongs to another project.")
    report = source_metadata.get("user_facing_report")
    if isinstance(report, Mapping) and report.get("package_kind") == "full_publication_package":
        with kb.connect_closing(board=board) as conn:
            return _reviewed_full_package_page(
                conn, kb, execution_id, review_id, source_run, review_run,
            )
    evidence = source_metadata.get("acceptance_evidence") or {}
    package = evidence.get("inline_content_package") or {}
    candidates = []
    canonical_package = source_metadata.get("canonical_package")
    if canonical_package is not None and not isinstance(canonical_package, Mapping):
        raise ValueError("Accepted package has malformed canonical_package evidence.")
    canonical_page_post = None
    if isinstance(canonical_package, Mapping) and "facebook_page_post" in canonical_package:
        canonical_page_post = canonical_package["facebook_page_post"]
        if not isinstance(canonical_page_post, Mapping) or "text" not in canonical_page_post:
            raise ValueError(
                "Accepted package has malformed canonical facebook_page_post text."
            )
        canonical_text = canonical_page_post["text"]
        if (
            not isinstance(canonical_text, str)
            or canonical_page_post.get("utf8_byte_count")
            != len(canonical_text.encode("utf-8"))
            or canonical_page_post.get("utf8_sha256")
            != hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        ):
            raise ValueError(
                "Accepted package has malformed canonical facebook_page_post text."
            )
        candidates.append(("canonical_package.facebook_page_post.text", canonical_text))
    canonical_page_hero = None
    if isinstance(canonical_package, Mapping) and "page_hero" in canonical_package:
        canonical_page_hero = canonical_package["page_hero"]
        if not isinstance(canonical_page_hero, Mapping):
            raise ValueError("Accepted package has malformed canonical Page Hero evidence.")
        canonical_dimensions = canonical_page_hero.get("dimensions")
        canonical_width = (
            canonical_dimensions.get("width")
            if isinstance(canonical_dimensions, Mapping)
            else None
        )
        canonical_height = (
            canonical_dimensions.get("height")
            if isinstance(canonical_dimensions, Mapping)
            else None
        )
        canonical_path = canonical_page_hero.get("path")
        canonical_filename = canonical_page_hero.get("filename")
        canonical_hash = str(
            canonical_page_hero.get("raw_byte_sha256") or ""
        ).lower()
        if (
            canonical_page_hero.get("asset_family") != "page_hero"
            or not isinstance(canonical_filename, str)
            or not canonical_filename
            or Path(canonical_filename).name != canonical_filename
            or not isinstance(canonical_path, str)
            or not canonical_path
            or re.fullmatch(r"[0-9a-f]{64}", canonical_hash) is None
            or type(canonical_width) is not int
            or type(canonical_height) is not int
            or canonical_width <= 0
            or canonical_height <= 0
            or canonical_width * 9 != canonical_height * 16
            or canonical_page_hero.get("exact_16_9") is not True
        ):
            raise ValueError("Accepted package has malformed canonical Page Hero evidence.")
    if isinstance(package, Mapping) and "facebook_page_post" in package:
        candidates.append(("acceptance_evidence.inline_content_package.facebook_page_post", package["facebook_page_post"]))
    for prefix, metadata in (("", source_metadata), ("acceptance_evidence.", evidence)):
        page_post = metadata.get("facebook_page_post")
        if isinstance(page_post, Mapping) and "text" in page_post:
            candidates.append((prefix + "facebook_page_post.text", page_post["text"]))
    if not candidates or any(not isinstance(text, str) or not text.strip() for _, text in candidates):
        raise ValueError("Accepted package lacks structured facebook_page_post text.")
    source_field, message = candidates[0]
    if any(text != message for _, text in candidates):
        raise ValueError("Accepted package has conflicting structured facebook_page_post text.")
    review_metadata = review_run.metadata or {}
    normalized_review = page_hero_review_evidence(review_metadata)
    raw_images = review_metadata.get("asset_review")
    images = [dict(asset) for asset in raw_images
              if isinstance(asset, Mapping) and asset.get("asset_family") == "page_hero"] if isinstance(raw_images, list) else []
    if len(images) > 1:
        raise ValueError("Accepted package must have one reviewed Page Hero.")
    review_evidence = review_metadata.get("evidence")
    verified_review = review_metadata.get("verified")
    verified_page_hero = (
        verified_review.get("page_hero")
        if isinstance(verified_review, Mapping)
        else None
    )
    image_resolution: dict[str, Any] = {
        "source": "reviewed_path",
        "candidate_count": 1,
    }
    if verified_page_hero is not None:
        if not isinstance(verified_page_hero, Mapping):
            raise ValueError("Accepted package has malformed verified Page Hero evidence.")
        verified_path = verified_page_hero.get("path")
        verified_hash = str(
            verified_page_hero.get("raw_byte_sha256") or ""
        ).lower()
        verified_width = verified_page_hero.get("width")
        verified_height = verified_page_hero.get("height")
        if (
            verified_page_hero.get("asset_family") != "page_hero"
            or verified_page_hero.get("actual_image_opened") is not True
            or verified_page_hero.get("exact_16_9") is not True
            or not isinstance(verified_path, str)
            or not verified_path
            or re.fullmatch(r"[0-9a-f]{64}", verified_hash) is None
            or type(verified_width) is not int
            or type(verified_height) is not int
            or verified_width <= 0
            or verified_height <= 0
            or verified_width * 9 != verified_height * 16
        ):
            raise ValueError("Accepted package has malformed verified Page Hero evidence.")
        verified_file = Path(verified_path)
        try:
            verified_bytes = verified_file.read_bytes()
            actual_dimensions = _png_dimensions(verified_bytes)
        except (OSError, ValueError) as exc:
            matching_lineage_attachments = []
            for attachment in lineage_review_attachments:
                path = Path(attachment.stored_path)
                try:
                    image_bytes = path.read_bytes()
                    image_dimensions = _png_dimensions(image_bytes)
                except (OSError, ValueError):
                    continue
                if (
                    attachment.filename
                    == str((canonical_page_hero or {}).get("filename") or "")
                    and attachment.size == len(image_bytes)
                    and hashlib.sha256(image_bytes).hexdigest() == verified_hash
                    and image_dimensions == (verified_width, verified_height)
                ):
                    matching_lineage_attachments.append(
                        (attachment, path, image_bytes, image_dimensions)
                    )
            if len(matching_lineage_attachments) != 1:
                raise ValueError(
                    "Accepted package verified Page Hero file is unavailable."
                ) from exc
            _, verified_file, verified_bytes, actual_dimensions = (
                matching_lineage_attachments[0]
            )
            matched_attachment = matching_lineage_attachments[0][0]
            image_resolution = {
                "source": "accepted_lineage_review_attachment",
                "candidate_count": len(matching_lineage_attachments),
                "lineage_execution_task_id": source_lineage["execution_task_id"],
                "lineage_review_task_id": source_lineage["review_task_id"],
                "attachment_id": matched_attachment.id,
                "attachment_filename": matched_attachment.filename,
                "attachment_size": matched_attachment.size,
            }
        if (
            hashlib.sha256(verified_bytes).hexdigest() != verified_hash
            or actual_dimensions != (verified_width, verified_height)
        ):
            raise ValueError("Accepted package verified Page Hero bytes changed.")
        if canonical_page_hero is not None:
            dimensions = canonical_page_hero["dimensions"]
            if any((
                canonical_page_hero.get("asset_family") != "page_hero",
                canonical_page_hero.get("path") != verified_path,
                canonical_page_hero.get("raw_byte_sha256") != verified_hash,
                dimensions.get("width") != verified_width,
                dimensions.get("height") != verified_height,
                canonical_page_hero.get("exact_16_9") is not True,
            )):
                raise ValueError("Accepted package has conflicting canonical Page Hero evidence.")
        images.append({
            "asset_family": "page_hero",
            "accepted": True,
            "path": str(verified_file),
            "sha256": verified_hash,
            "width": verified_width,
            "height": verified_height,
            "filename": str(
                (canonical_page_hero or {}).get("filename")
                or verified_file.name
            ),
        })
    for native, page_hero in (
        (False, review_metadata.get("page_hero")),
        (True, (review_metadata.get("acceptance_evidence") or {}).get("page_hero")),
        (True, review_evidence.get("page_hero") if isinstance(review_evidence, Mapping) else None),
    ):
        if page_hero is None:
            continue
        if not isinstance(page_hero, Mapping):
            raise ValueError("Accepted package has malformed Page Hero review evidence.")
        asset = dict(page_hero)
        if native:
            # Canonical Grace evidence uses explicit raw-file/reviewer names.
            # Normalize only corroborating fields; never override disagreement.
            for key, alias in (("sha256", "raw_file_sha256"),
                               ("actual_image_inspected", "actual_image_inspected_by_review")):
                if alias in asset:
                    if key in asset and asset[key] != asset[alias]:
                        raise ValueError("Accepted Page Hero has conflicting review evidence.")
                    if key == "actual_image_inspected" and (asset[alias] is not True
                        or (key in asset and asset[key] is not True)):
                        raise ValueError("Accepted Page Hero requires actual image inspection.")
                    asset[key] = asset[alias]
            if asset.get("actual_image_inspected") is not True:
                raise ValueError("Accepted Page Hero requires actual image inspection.")
            dimensions = asset.get("pixel_dimensions")
            if (not isinstance(dimensions, list) or len(dimensions) != 2
                or any(type(value) is not int or value <= 0 for value in dimensions)):
                raise ValueError("Accepted Page Hero lacks exact path/hash/dimensions.")
            if any(key in asset and asset[key] != value
                   for key, value in zip(("width", "height"), dimensions)):
                raise ValueError("Accepted Page Hero has conflicting dimensions.")
            asset.update(width=dimensions[0], height=dimensions[1])
        asset.setdefault("accepted", True)
        images.append(asset)
    native_visual = (
        review_evidence.get("page_hero_actual_pixel_analysis")
        if isinstance(review_evidence, Mapping)
        else None
    )
    native_hashes = (
        review_evidence.get("hash_verification")
        if isinstance(review_evidence, Mapping)
        else None
    )
    native_receipt = (
        review_evidence.get("controller_content_package_readback")
        if isinstance(review_evidence, Mapping)
        else None
    )
    current_declaration = (
        normalized_review.get("declaration")
        if normalized_review is not None
        else None
    )
    current_attachment = (
        normalized_review.get("controller_attachment")
        if normalized_review is not None
        else None
    )
    current_post_readback = (
        normalized_review.get("facebook_page_post_readback")
        if normalized_review is not None
        else None
    )
    current_inspected = (
        normalized_review.get("actual_controller_attachment_inspected")
        if normalized_review is not None
        else None
    )
    current_schema_claimed = bool(
        current_attachment is not None
        or current_post_readback is not None
        or current_inspected is not None
    )
    native_schema_claimed = bool(
        native_visual is not None
        or native_hashes is not None
        or native_receipt is not None
    )
    if current_schema_claimed:
        if (
            not isinstance(current_declaration, Mapping)
            or not isinstance(current_attachment, Mapping)
            or not isinstance(current_post_readback, Mapping)
            or current_inspected is not True
        ):
            raise ValueError(
                "Accepted package has incomplete controller Page Hero evidence."
            )
        width = current_declaration.get("width")
        height = current_declaration.get("height")
        expected_hash = str(current_declaration.get("sha256") or "").lower()
        expected_size = current_declaration.get("bytes")
        expected_filename = str(current_declaration.get("filename") or "")
        attachment_hash = str(current_attachment.get("sha256") or "").lower()
        post_hash = str(current_post_readback.get("sha256") or "").lower()
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or width * 9 != height * 16
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or type(expected_size) is not int
            or expected_size <= 0
            or not expected_filename
            or current_attachment.get("byte_for_byte_equal") is not True
            or current_attachment.get("filename") != expected_filename
            or attachment_hash != expected_hash
            or current_attachment.get("width") != width
            or current_attachment.get("height") != height
            or current_attachment.get("bytes") != expected_size
            or current_post_readback.get("byte_preserving") is not True
            or current_post_readback.get("utf8_bytes")
            != len(message.encode("utf-8"))
            or post_hash
            != hashlib.sha256(message.encode("utf-8")).hexdigest()
        ):
            raise ValueError(
                "Accepted package has malformed controller Page Hero evidence."
            )
        matching_attachments = []
        for attachment in source_attachments:
            path = Path(attachment.stored_path)
            try:
                image_bytes = path.read_bytes()
                actual_width, actual_height = _png_dimensions(image_bytes)
            except (OSError, ValueError):
                continue
            if (
                attachment.id == current_attachment.get("attachment_id")
                and attachment.filename == expected_filename
                and attachment.stored_path == current_attachment.get("stored_path")
                and attachment.size == expected_size == len(image_bytes)
                and hashlib.sha256(image_bytes).hexdigest() == expected_hash
                and (actual_width, actual_height) == (width, height)
            ):
                matching_attachments.append((attachment, path))
        if len(matching_attachments) != 1:
            raise ValueError(
                "Accepted package controller evidence has no unique attachment."
            )
        attachment, path = matching_attachments[0]
        images.append({
            "asset_family": "page_hero",
            "accepted": True,
            "path": str(path),
            "sha256": expected_hash,
            "width": width,
            "height": height,
            "filename": attachment.filename,
        })
    elif normalized_review is not None and not native_schema_claimed:
        raise ValueError(
            "Accepted package has incomplete controller Page Hero evidence."
        )
    if native_visual is not None or native_hashes is not None:
        asset_declarations = review_metadata.get("asset_declarations")
        page_hero_declared = (
            str(review_metadata.get("asset_family") or "").strip().lower()
            == "page_hero"
            or (
                isinstance(asset_declarations, Mapping)
                and asset_declarations.get("page_hero") is not None
            )
        )
        visual_review = review_metadata.get("visual_review")
        if (
            not isinstance(native_visual, Mapping)
            or not isinstance(native_hashes, Mapping)
            or not isinstance(native_receipt, Mapping)
        ):
            raise ValueError("Accepted package has malformed actual-pixel Page Hero evidence.")
        if require_current_visual_safety and (
            not page_hero_declared
            or not isinstance(visual_review, Mapping)
            or visual_review.get("all_required_text_readable") is not True
            or visual_review.get("text_occlusion_free") is not True
            or visual_review.get("disclosure_non_obstructive") is not True
            or visual_review.get("defects_found") != []
        ):
            raise ValueError(
                "Accepted package lacks current structured Page Hero safety evidence."
            )
        dimensions_match = re.fullmatch(
            r"([1-9][0-9]*)[x×]([1-9][0-9]*)",
            str(native_visual.get("dimensions") or ""),
        )
        expected_hash = str(
            native_hashes.get("page_hero_sha256") or ""
        ).strip().lower()
        if (
            native_visual.get("asset_family") != "page_hero"
            or native_visual.get("status") != "passed"
            or native_visual.get("traditional_chinese_readable") is not True
            or native_visual.get("ai_disclosure_visible") is not True
            or dimensions_match is None
            or native_hashes.get("exact_attachment_bytes_verified") is not True
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or (
                native_receipt.get("package_complete") is not True
                and not (
                    # completion_mode is controller-derived from the Objective
                    # stage before this contract snapshot is fingerprinted.
                    native_receipt.get("package_complete") is False
                    and isinstance(sealed_contract, Mapping)
                    and sealed_contract.get("completion_mode") == "intermediate"
                )
            )
            or type(native_receipt.get("task_attachment_row_count")) is not int
            or native_receipt["task_attachment_row_count"] < 1
        ):
            raise ValueError("Accepted package has malformed actual-pixel Page Hero evidence.")
        width, height = (int(value) for value in dimensions_match.groups())
        matching_attachments = []
        for attachment in source_attachments:
            path = Path(attachment.stored_path)
            try:
                image_bytes = path.read_bytes()
                actual_width, actual_height = _png_dimensions(image_bytes)
            except (OSError, ValueError):
                continue
            if (
                hashlib.sha256(image_bytes).hexdigest() == expected_hash
                and (actual_width, actual_height) == (width, height)
                and attachment.size == len(image_bytes)
            ):
                matching_attachments.append((attachment, path))
        if len(matching_attachments) != 1:
            raise ValueError(
                "Accepted package actual-pixel evidence has no unique controller attachment."
            )
        attachment, path = matching_attachments[0]
        images.append({
            "asset_family": "page_hero",
            "accepted": True,
            "path": str(path),
            "sha256": expected_hash,
            "width": width,
            "height": height,
            "filename": attachment.filename,
        })
    if not images or any(asset.get("asset_family") != "page_hero" or asset.get("accepted") is not True
                         for asset in images):
        raise ValueError("Accepted package must have one reviewed Page Hero.")
    # Multiple representations may corroborate the same image, never hide a
    # rejection or choose between different reviewed assets.
    for asset in images:
        if (not isinstance(asset.get("path"), str) or not asset["path"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or ""))
            or type(asset.get("width")) is not int or type(asset.get("height")) is not int
            or asset["width"] <= 0 or asset["height"] <= 0
            or asset["width"] * 9 != asset["height"] * 16):
            raise ValueError("Accepted Page Hero lacks exact path/hash/dimensions.")
        if any(asset[key] != images[0][key] for key in ("path", "sha256", "width", "height")):
            raise ValueError("Accepted package has conflicting Page Hero review evidence.")
    asset = images[0]
    asset_file = Path(asset["path"])
    try:
        asset_bytes = asset_file.read_bytes()
        asset_dimensions = _png_dimensions(asset_bytes)
    except (OSError, ValueError) as exc:
        raise ValueError("Accepted package Page Hero file is unavailable.") from exc
    if (
        hashlib.sha256(asset_bytes).hexdigest() != asset["sha256"]
        or asset_dimensions != (asset["width"], asset["height"])
    ):
        raise ValueError("Accepted package Page Hero bytes changed.")
    return {
        "execution_task_id": execution_id, "execution_run_id": source_run.id,
        "review_task_id": review_id, "review_run_id": review_run.id,
        "source_field": source_field,
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "message_utf8_bytes": len(message.encode("utf-8")),
        "image_path": asset["path"], "image_sha256": asset["sha256"],
        "image_bytes": len(asset_bytes),
        "image_resolution": image_resolution,
        "image_filename": str(asset.get("filename") or Path(asset["path"]).name),
        "dimensions": f"{asset['width']}×{asset['height']}",
    }


def bind_accepted_page_preflight_source(
    contract: Mapping[str, Any], *, board: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Bind an accepted package that already has current visual-safety proof."""
    return _resolve_accepted_page_source(
        contract,
        board=board,
        require_current_visual_safety=True,
    )


_ACCEPTED_PAGE_RELAY_DELIVERY = {
    "required": True,
    "kind": "content_package",
    "delivery": "inline_with_attachment",
    "body_field": "metadata.facebook_page_post.text",
    "asset_filenames": ["page-hero.png"],
}


def accepted_page_relay_delivery_contract(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Require the delivery shape consumed by controller relay readback."""
    delivery = contract.get("user_facing_delivery")
    if not isinstance(delivery, Mapping) or any(
        delivery.get(key) != value
        for key, value in _ACCEPTED_PAGE_RELAY_DELIVERY.items()
    ):
        raise ValueError(
            "Accepted Page relay requires content_package/inline_with_attachment "
            "delivery from metadata.facebook_page_post.text with only page-hero.png."
        )
    return dict(delivery)


def _write_controller_handoff_file(path: Path, data: bytes) -> None:
    """Create one controller file, or verify an identical retry artifact."""
    if path.is_symlink():
        raise ValueError("Controller source handoff path must not be a symlink.")
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError("Existing controller source handoff does not match accepted bytes.")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def materialize_accepted_page_relay_handoff(
    contract: Mapping[str, Any],
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Put exact accepted Page bytes in a native worker workspace.

    This is intentionally weaker than publish binding only in one respect: a
    historical accepted review may supply the source bytes for a zero-effect
    relay whose output receives a fresh formal visual review.  It cannot make
    that historical package directly publishable.
    """
    completion_handoff = contract.get("completion_handoff")
    resolved = ((contract.get("routing") or {}).get("resolved") or {})
    if (
        not isinstance(completion_handoff, Mapping)
        or completion_handoff.get("metadata_source") != "workspace_file"
        or resolved.get("task_type") != "devops"
    ):
        return None
    accepted = _resolve_accepted_page_source(
        contract,
        board=board,
        require_current_visual_safety=False,
    )
    if accepted is None:
        return None
    accepted_page_relay_delivery_contract(contract)

    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Controller source handoff requires a real worker workspace.")
    handoff_dir = root / ".hermes-controller-handoff"
    if handoff_dir.exists():
        if handoff_dir.is_symlink() or not handoff_dir.is_dir():
            raise ValueError("Controller source handoff directory is not trusted.")
    else:
        handoff_dir.mkdir(mode=0o700)

    image_name = Path(str(accepted["image_filename"])).name
    if not image_name or image_name != str(accepted["image_filename"]):
        raise ValueError("Accepted Page Hero filename is invalid.")
    source_image = Path(str(accepted["image_path"]))
    image_bytes = source_image.read_bytes()
    width, height = _png_dimensions(image_bytes)
    if (
        hashlib.sha256(image_bytes).hexdigest() != accepted["image_sha256"]
        or f"{width}×{height}" != accepted["dimensions"]
    ):
        raise ValueError("Accepted Page Hero changed before controller handoff.")
    # The source filename is evidence, not a controller storage name.  Keep it
    # in the bundle while reserving fixed, non-overlapping handoff paths.
    image_path = handoff_dir / "page-hero.png"
    _write_controller_handoff_file(image_path, image_bytes)

    bundle = {
        "schema_version": "1.0",
        "source_kind": "accepted_facebook_page_package",
        "execution_task_id": accepted["execution_task_id"],
        "execution_run_id": accepted["execution_run_id"],
        "review_task_id": accepted["review_task_id"],
        "review_run_id": accepted["review_run_id"],
        "facebook_page_post": {
            "text": accepted["message"],
            "sha256": accepted["message_sha256"],
            "utf8_bytes": accepted["message_utf8_bytes"],
            "source_field": accepted["source_field"],
        },
        "page_hero": {
            "asset_family": "page_hero",
            "filename": image_name,
            "path": str(image_path),
            "sha256": accepted["image_sha256"],
            "dimensions": accepted["dimensions"],
        },
        "external_effects": [],
    }
    bundle_bytes = json.dumps(
        bundle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    bundle_path = handoff_dir / "accepted-page-source.json"
    _write_controller_handoff_file(bundle_path, bundle_bytes)
    return {
        "schema_version": "1.0",
        "bundle_path": str(bundle_path),
        "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "image_path": str(image_path),
        "image_sha256": accepted["image_sha256"],
        "message_sha256": accepted["message_sha256"],
        "message_utf8_bytes": accepted["message_utf8_bytes"],
        "execution_task_id": accepted["execution_task_id"],
        "execution_run_id": accepted["execution_run_id"],
        "review_task_id": accepted["review_task_id"],
        "review_run_id": accepted["review_run_id"],
    }


def read_materialized_accepted_page_relay_handoff(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Read back exact controller handoff bytes from an active-run receipt."""
    bundle_path = Path(str(receipt.get("bundle_path") or ""))
    image_path = Path(str(receipt.get("image_path") or ""))
    if (
        bundle_path.name != "accepted-page-source.json"
        or bundle_path.parent.name != ".hermes-controller-handoff"
        or image_path != bundle_path.parent / "page-hero.png"
        or bundle_path.is_symlink()
        or image_path.is_symlink()
        or bundle_path.parent.is_symlink()
    ):
        raise ValueError("Controller accepted-source handoff paths are invalid.")

    def read_regular(path: Path, limit: int) -> bytes:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                raise ValueError("Controller accepted-source handoff file is invalid.")
            chunks: list[bytes] = []
            size = 0
            while size <= limit:
                chunk = os.read(descriptor, min(65_536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            if len(data) != info.st_size or len(data) > limit:
                raise ValueError("Controller accepted-source handoff file changed while read.")
            return data
        finally:
            os.close(descriptor)

    bundle_bytes = read_regular(bundle_path, 1_000_000)
    if hashlib.sha256(bundle_bytes).hexdigest() != receipt.get("bundle_sha256"):
        raise ValueError("Controller accepted-source bundle hash changed before completion.")
    try:
        bundle = json.loads(bundle_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Controller accepted-source bundle is invalid JSON.") from exc
    post = bundle.get("facebook_page_post") if isinstance(bundle, Mapping) else None
    hero = bundle.get("page_hero") if isinstance(bundle, Mapping) else None
    message = post.get("text") if isinstance(post, Mapping) else None
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Controller accepted-source bundle has no exact Page text.")
    message_bytes = message.encode("utf-8")
    identity_fields = (
        "execution_task_id", "execution_run_id", "review_task_id", "review_run_id",
    )
    if (
        bundle.get("schema_version") != "1.0"
        or bundle.get("source_kind") != "accepted_facebook_page_package"
        or bundle.get("external_effects") != []
        or any(bundle.get(key) != receipt.get(key) for key in identity_fields)
        or not isinstance(post, Mapping)
        or hashlib.sha256(message_bytes).hexdigest() != post.get("sha256")
        or len(message_bytes) != post.get("utf8_bytes")
        or post.get("sha256") != receipt.get("message_sha256")
        or post.get("utf8_bytes") != receipt.get("message_utf8_bytes")
        or not isinstance(hero, Mapping)
        or hero.get("asset_family") != "page_hero"
        or hero.get("path") != str(image_path)
        or hero.get("sha256") != receipt.get("image_sha256")
    ):
        raise ValueError("Controller accepted-source bundle disagrees with its receipt.")
    image_bytes = read_regular(image_path, 64 * 1024 * 1024)
    width, height = _png_dimensions(image_bytes)
    if (
        hashlib.sha256(image_bytes).hexdigest() != hero.get("sha256")
        or hero.get("dimensions") != f"{width}×{height}"
        or width * 9 != height * 16
    ):
        raise ValueError("Controller accepted-source Page Hero changed before completion.")
    return {
        "message": message,
        "message_sha256": post["sha256"],
        "message_utf8_bytes": post["utf8_bytes"],
        "source_field": post.get("source_field"),
        "image_path": str(image_path),
        "image_sha256": hero["sha256"],
        "image_data": image_bytes,
        "image_bytes": len(image_bytes),
        "width": width,
        "height": height,
        "dimensions": hero["dimensions"],
    }


def _authorized_page_body(source: str, final_message: str) -> dict[str, Any]:
    headings = ("💬 Group 討論題：", "Page → Group 導流：")
    positions = []
    for heading in headings:
        marker = f"\n\n{heading}\n"
        if source.count(marker) != 1:
            raise ValueError(f"Source must contain exactly one authorized section: {heading}")
        positions.append(source.index(marker))
    if positions != sorted(positions):
        raise ValueError("Authorized source sections are out of order.")
    preserved = source[: positions[0]].rstrip()
    if final_message == preserved:
        hashtags = ""
    elif final_message.startswith(preserved + "\n\n"):
        hashtags = final_message[len(preserved) + 2 :]
        if "\n\n" in hashtags or not hashtags.strip():
            raise ValueError("Hashtags must be one final non-empty paragraph.")
        tokens = hashtags.split()
        if not tokens or any(not token.startswith("#") or len(token) == 1 for token in tokens):
            raise ValueError("Only a case-customized hashtag paragraph may be appended.")
    else:
        raise ValueError("Final Page text changes content outside the two authorized removals.")
    return {
        "removed_sections": list(headings),
        "preserved_prefix_sha256": hashlib.sha256(preserved.encode("utf-8")).hexdigest(),
        "hashtags": hashtags,
        "hashtags_are_final_paragraph": bool(hashtags),
    }


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("Image is not a canonical PNG.")
    return struct.unpack(">II", data[16:24])


def _handle_publish_preflight(
    args: dict[str, Any], *, capability_scope: OpenClawCapabilityScope
) -> str:
    contract, scope_error = _authorized_openclaw_contract(capability_scope)
    if contract is None:
        return json.dumps({"success": False, "published": False, "error": scope_error}, ensure_ascii=False)
    final_message = str(args.get("final_message") or "")
    image_path_raw = str(args.get("image_path") or "").strip()
    try:
        accepted = bind_accepted_page_preflight_source(contract, board=capability_scope.board or None)
        if accepted is not None:
            if contract.get("facebook_page_preflight_source") != accepted:
                raise ValueError("Accepted package changed or was not pinned before delegation.")
            if final_message and final_message != accepted["message"]:
                raise ValueError("Worker message differs from the exact accepted Page message.")
            if image_path_raw and image_path_raw != accepted["image_path"]:
                raise ValueError("Worker image path differs from the accepted Page Hero.")
            final_message, image_path_raw = accepted["message"], accepted["image_path"]
            source_hash = accepted["message_sha256"]
            diff = {key: value for key, value in accepted.items() if key != "message"}
            diff["accepted_message_unchanged"] = True
        else:
            if contract.get("facebook_page_preflight_source") is not None:
                raise ValueError("Pinned accepted package has no explicit source selection.")
            source, expected_source_hash = _embedded_page_source(contract)
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source_hash != expected_source_hash:
                raise ValueError("Embedded source SHA-256 does not match its evidence header.")
            diff = _authorized_page_body(source, final_message)
        strings = _contract_strings(contract)
        if not image_path_raw or not any(image_path_raw in item for item in strings):
            raise ValueError("Image path is not bound in the Loop Contract.")
        image_path = Path(image_path_raw).expanduser()
        image_bytes = image_path.read_bytes()
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        if not any(image_hash in item for item in strings):
            raise ValueError("Image SHA-256 is not bound in the Loop Contract.")
        width, height = _png_dimensions(image_bytes)
        dimension_tokens = {f"{width}×{height}", f"{width}x{height}", f"{width} X {height}"}
        if not any(any(token in item for token in dimension_tokens) for item in strings):
            raise ValueError("Actual image dimensions are not bound in the Loop Contract.")
        if width * 9 != height * 16:
            raise ValueError("Page Hero is not exact 16:9.")
        page_status = json.loads(_handle_status({}))
        if not page_status.get("identity_verified"):
            raise ValueError("Configured Facebook Page identity did not pass Graph read-only verification.")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return json.dumps(
            {"success": False, "published": False, "error": str(exc)},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "success": True,
            "published": False,
            "external_actions_performed": False,
            "final_message": final_message,
            "manifest": {
                "source_sha256": source_hash,
                "message_sha256": hashlib.sha256(final_message.encode("utf-8")).hexdigest(),
                "message_utf8_bytes": len(final_message.encode("utf-8")),
                "image_path": str(image_path),
                "image_sha256": image_hash,
                "image_bytes": len(image_bytes),
                "image_format": "PNG",
                "image_width": width,
                "image_height": height,
                "image_ratio": "16:9",
                "page_id": page_status.get("page_id"),
                "page_name": page_status.get("page_name"),
                "page_url": page_status.get("page_url"),
                "graph_page_url": page_status.get("graph_page_url"),
                "page_username": page_status.get("page_username"),
                "canonical_url_verified": page_status.get(
                    "canonical_url_verified"
                ),
                "api_version": page_status.get("api_version"),
                "configured": page_status.get("configured"),
                "identity_verified": page_status.get("identity_verified"),
            },
            "evidence": diff,
            "external_effects": [],
        },
        ensure_ascii=False,
    )


def _handle_status(_args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_page_config()
    if config.missing:
        return json.dumps({
            "success": False,
            "configured": False,
            "missing_configuration": config.missing,
        }, ensure_ascii=False)
    try:
        result = _fetch_page_status(config)
    except FacebookGraphError as exc:
        result = {
            "success": False,
            "configured": True,
            "error": str(exc),
            "api_version": config.api_version,
        }
    result.setdefault("configured", True)
    return json.dumps(result, ensure_ascii=False)


def _readback_attachment(payload: Mapping[str, Any]) -> dict[str, Any]:
    attachments = payload.get("attachments")
    data = attachments.get("data") if isinstance(attachments, Mapping) else []
    first = data[0] if isinstance(data, list) and data else {}
    target = first.get("target") if isinstance(first, Mapping) else {}
    return {
        "media_type": str(first.get("media_type") or "") if isinstance(first, Mapping) else "",
        "target_id": str(target.get("id") or "") if isinstance(target, Mapping) else "",
        "url": str(first.get("url") or "") if isinstance(first, Mapping) else "",
    }


def _accepted_preflight_message(
    conn: Any,
    *,
    message_sha256: str,
    image_sha256: str,
) -> Optional[str]:
    """Resolve exact bytes from a completed, Grace-accepted Page preflight."""
    rows = conn.execute(
        """
        SELECT parent.result AS execution_result,
               review_run.metadata AS review_metadata
          FROM tasks AS parent
          JOIN task_links AS link ON link.parent_id = parent.id
          JOIN tasks AS review ON review.id = link.child_id
          JOIN task_runs AS review_run
            ON review_run.id = (
                SELECT MAX(candidate.id)
                  FROM task_runs AS candidate
                 WHERE candidate.task_id = review.id
            )
         WHERE parent.status = 'done'
           AND review.status = 'done'
           AND INSTR(COALESCE(parent.result, ''), ?) > 0
           AND INSTR(COALESCE(parent.result, ''), ?) > 0
        """,
        (message_sha256, image_sha256),
    ).fetchall()
    accepted_messages: set[str] = set()
    for row in rows:
        try:
            result = json.loads(str(row["execution_result"] or "{}"))
            review = json.loads(str(row["review_metadata"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        review_evidence = review.get("evidence") if isinstance(review, Mapping) else None
        review_copy = (
            review_evidence.get("page_copy")
            if isinstance(review_evidence, Mapping)
            else None
        )
        review_visual = (
            review_evidence.get("visual_review")
            if isinstance(review_evidence, Mapping)
            else None
        )
        if (
            review.get("accepted") is not True
            or review.get("acceptance_criteria_met") is not True
            or not isinstance(review_copy, Mapping)
            or not isinstance(review_visual, Mapping)
            or str(review_copy.get("final_message_sha256") or "") != message_sha256
            or str(review_visual.get("image_sha256") or "") != image_sha256
        ):
            continue
        artifacts = result.get("artifacts") if isinstance(result, Mapping) else None
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            value = artifact.get("value") if isinstance(artifact, Mapping) else None
            delegated = value.get("result") if isinstance(value, Mapping) else None
            evidence = (
                delegated.get("acceptanceEvidence")
                if isinstance(delegated, Mapping)
                else None
            )
            source_and_final = (
                evidence.get("source_and_final_text")
                if isinstance(evidence, Mapping)
                else None
            )
            hero = evidence.get("hero_asset") if isinstance(evidence, Mapping) else None
            admission = evidence.get("admission") if isinstance(evidence, Mapping) else None
            message = (
                str(evidence.get("final_facebook_page_body") or "")
                if isinstance(evidence, Mapping)
                else ""
            )
            if (
                isinstance(source_and_final, Mapping)
                and isinstance(hero, Mapping)
                and isinstance(admission, Mapping)
                and admission.get("published") is False
                and admission.get("external_actions_performed") is False
                and str(source_and_final.get("final_message_sha256") or "")
                == message_sha256
                and str(hero.get("image_sha256") or "") == image_sha256
                and hashlib.sha256(message.encode("utf-8")).hexdigest()
                == message_sha256
            ):
                accepted_messages.add(message)
    if len(accepted_messages) != 1:
        return None
    return next(iter(accepted_messages))


def _handle_publish(
    args: dict[str, Any],
    *,
    capability_scope: Optional[OpenClawCapabilityScope] = None,
    **_kwargs: Any,
) -> str:
    from hermes_cli import kanban_db as kb

    if capability_scope is None:
        contract, scope_error = _authorized_execution_contract()
        task_id, run_id, board = _worker_scope()
    else:
        contract, scope_error = _authorized_openclaw_contract(capability_scope)
        task_id = capability_scope.task_id
        run_id = capability_scope.run_id
        board = capability_scope.board
    if contract is None:
        return json.dumps({"success": False, "published": False, "error": scope_error})
    page_url = _canonical_page_url(str(args.get("page_url") or ""))
    supplied_message = str(args.get("message") or "")
    try:
        image_path = Path(str(args.get("image_path") or "")).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        return json.dumps({
            "success": False,
            "published": False,
            "error": f"Approved image path is invalid: {type(exc).__name__}",
        }, ensure_ascii=False)
    supplied_message_sha256 = hashlib.sha256(
        supplied_message.encode("utf-8")
    ).hexdigest()
    try:
        if not image_path.is_file():
            raise OSError("path is not a regular file")
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        return json.dumps({
            "success": False,
            "published": False,
            "error": f"Approved image is unavailable: {type(exc).__name__}: {exc}",
        }, ensure_ascii=False)
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if page_url != contract["page_url"] or image_sha256 != contract["image_sha256"]:
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Target URL or exact payload hashes do not match the approved contract.",
            "message_sha256": supplied_message_sha256,
            "image_sha256": image_sha256,
        }, ensure_ascii=False)
    message = supplied_message
    if supplied_message_sha256 != contract["message_sha256"]:
        with kb.connect_closing(board=board or None) as conn:
            message = _accepted_preflight_message(
                conn,
                message_sha256=contract["message_sha256"],
                image_sha256=contract["image_sha256"],
            ) or ""
        if not message:
            return json.dumps({
                "success": False,
                "published": False,
                "error": (
                    "Supplied message does not match the approved contract and no unique "
                    "Grace-accepted preflight body could be resolved."
                ),
                "message_sha256": supplied_message_sha256,
                "image_sha256": image_sha256,
            }, ensure_ascii=False)
    message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if message_sha256 != contract["message_sha256"]:
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Resolved Page body does not match the approved message SHA-256.",
        }, ensure_ascii=False)
    config = _load_page_config()
    if config.missing:
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Facebook Page Graph configuration is incomplete.",
            "missing_configuration": config.missing,
        }, ensure_ascii=False)
    approved_page_id = str(contract.get("page_id") or "").strip()
    if (
        (approved_page_id and approved_page_id != config.page_id)
        or (not approved_page_id and page_url != config.page_url)
    ):
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Approved Page identity does not match configured Page identity.",
        })
    try:
        status = _fetch_page_status(config)
    except FacebookGraphError as exc:
        return json.dumps({
            "success": False,
            "published": False,
            "error": str(exc),
        }, ensure_ascii=False)
    if not status.get("identity_verified"):
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Configured Page identity did not pass read-only preflight.",
            "page_status": status,
        }, ensure_ascii=False)

    with kb.connect_closing(board=board or None) as conn:
        reserve_error = kb.reserve_facebook_page_create(
            conn,
            task_id,
            page_url=page_url,
            message_sha256=message_sha256,
            image_sha256=image_sha256,
            expected_run_id=run_id,
        )
    if reserve_error:
        return json.dumps({
            "success": False,
            "published": False,
            "error": reserve_error,
        }, ensure_ascii=False)

    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    try:
        created = _graph_request(
            config,
            "POST",
            f"{config.page_id}/photos",
            data={"message": message, "published": "true"},
            files={"source": (image_path.name, image_bytes, content_type)},
        )
    except FacebookGraphError as exc:
        return json.dumps({
            "success": False,
            "published": None,
            "retry_permitted": False,
            "error": str(exc),
            "warning": "Graph POST was dispatched; durable create_started was preserved.",
        }, ensure_ascii=False)
    photo_id = str(created.get("id") or "").strip()
    post_id = str(created.get("post_id") or "").strip()
    if not post_id:
        return json.dumps({
            "success": False,
            "published": None,
            "retry_permitted": False,
            "photo_id": photo_id or None,
            "warning": "Graph POST returned no post_id; durable create_started was preserved.",
        }, ensure_ascii=False)

    base_details = {
        "page_id": config.page_id,
        "page_name": config.page_name,
        "page_url": config.page_url,
        "post_id": post_id,
        "photo_id": photo_id,
        "transport": "graph_api",
        "message_sha256": message_sha256,
        "image_sha256": image_sha256,
        "published": True,
    }
    try:
        with kb.connect_closing(board=board or None) as conn:
            kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                state="created",
                external_id=post_id,
                details=base_details,
                expected_run_id=run_id,
            )
    except Exception as exc:
        return json.dumps({
            "success": True,
            "published": True,
            "verified": False,
            "post_id": post_id,
            "photo_id": photo_id or None,
            "retry_permitted": False,
            "warning": (
                "Post was created but durable ledger recording failed: "
                f"{type(exc).__name__}. Preserve this post_id and reconcile; "
                "do not retry."
            ),
            "message_sha256": message_sha256,
            "image_sha256": image_sha256,
        }, ensure_ascii=False)
    try:
        readback = _graph_request(
            config,
            "GET",
            post_id,
            params={
                "fields": "id,message,permalink_url,created_time,attachments{media_type,target,url}"
            },
        )
        attachment = _readback_attachment(readback)
        readback_message = str(readback.get("message") or "")
        readback_message_sha256 = hashlib.sha256(
            readback_message.encode("utf-8")
        ).hexdigest()
        readback_message_final_paragraph = (
            readback_message.rstrip().split("\n")[-1]
            if readback_message
            else ""
        )
        message_verified = readback_message == message
        image_verified = bool(photo_id) and attachment["target_id"] == photo_id
        verified = message_verified and image_verified
        warning = None
        if not message_verified:
            warning = "read-back message mismatch"
        elif not image_verified:
            warning = "read-back image attachment mismatch"
        details = {
            **base_details,
            "permalink_url": str(readback.get("permalink_url") or ""),
            "created_time": str(readback.get("created_time") or ""),
            "readback_message": readback_message,
            "readback_message_length": len(readback_message),
            "readback_message_sha256": readback_message_sha256,
            "readback_message_final_paragraph": readback_message_final_paragraph,
            "readback_attachment": attachment,
            "verified": verified,
            "readback_warning": warning,
        }
    except FacebookGraphError as exc:
        verified = False
        warning = f"read-back failed: {exc}"
        details = {**base_details, "verified": False, "readback_warning": warning}
    try:
        with kb.connect_closing(board=board or None) as conn:
            kb.record_external_effect(
                conn,
                task_id,
                platform="facebook",
                state="verified" if verified else "created",
                external_id=post_id,
                details=details,
                expected_run_id=run_id,
            )
    except Exception as exc:
        verified = False
        warning = (
            "Post was created but final ledger recording failed: "
            f"{type(exc).__name__}. Preserve this post_id and reconcile; "
            "do not retry."
        )
    return json.dumps({
        "success": True,
        "published": True,
        "verified": verified,
        "warning": warning,
        "post_id": post_id,
        "photo_id": photo_id or None,
        "permalink_url": details.get("permalink_url"),
        "created_time": details.get("created_time"),
        "attachment": details.get("readback_attachment"),
        "readback_message_length": details.get("readback_message_length"),
        "readback_message_sha256": details.get("readback_message_sha256"),
        "readback_message_final_paragraph": details.get(
            "readback_message_final_paragraph"
        ),
        "message_sha256": message_sha256,
        "image_sha256": image_sha256,
        "retry_permitted": False,
    }, ensure_ascii=False)


def execute_openclaw_facebook_page_capability(
    operation: str,
    args: Mapping[str, Any],
    scope: OpenClawCapabilityScope,
) -> str:
    """Execute one bridge-bound Facebook Page capability without exposing credentials."""
    contract, scope_error = _authorized_openclaw_contract(scope)
    if contract is None:
        return json.dumps(
            {"success": False, "published": False, "error": scope_error},
            ensure_ascii=False,
        )
    if operation == "status":
        return _handle_status(dict(args))
    if operation == "preflight":
        return _handle_publish_preflight(dict(args), capability_scope=scope)
    if operation == "publish":
        return _handle_publish(dict(args), capability_scope=scope)
    return json.dumps(
        {"success": False, "published": False, "error": "Unsupported capability operation."},
        ensure_ascii=False,
    )


FACEBOOK_PAGE_GRAPH_STATUS_SCHEMA = {
    "name": "facebook_page_graph_status",
    "description": (
        "Read-only preflight for the configured Facebook Page Graph API. "
        "Verifies the Page access token resolves to the exact configured "
        "numeric Page ID and Page name; never publishes content or returns the token."
    ),
    "parameters": {"type": "object", "properties": {}},
}

FACEBOOK_PAGE_GRAPH_PUBLISH_SCHEMA = {
    "name": "facebook_page_graph_publish",
    "description": (
        "Publish one approved Facebook Page photo post through Meta Graph API, "
        "not the browser. Requires an active Kanban execution task whose "
        "consumed one-time Loop Contract binds exact message and image hashes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page_url": {"type": "string", "description": "Exact approved Page URL."},
            "message": {"type": "string", "description": "Exact approved UTF-8 post text."},
            "image_path": {"type": "string", "description": "Local path to the exact approved image."},
        },
        "required": ["page_url", "message", "image_path"],
    },
}


registry.register(
    name="facebook_page_graph_status",
    toolset=TOOLSET,
    schema=FACEBOOK_PAGE_GRAPH_STATUS_SCHEMA,
    handler=_handle_status,
    check_fn=_tool_available,
    requires_env=[],
    emoji="📄",
)

registry.register(
    name="facebook_page_graph_publish",
    toolset=TOOLSET,
    schema=FACEBOOK_PAGE_GRAPH_PUBLISH_SCHEMA,
    handler=_handle_publish,
    check_fn=_tool_available,
    requires_env=[],
    emoji="📤",
)
