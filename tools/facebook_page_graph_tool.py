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
        params={"fields": "id,name,link"},
    )
    observed_id = str(payload.get("id") or "").strip()
    observed_name = str(payload.get("name") or "").strip()
    observed_link = _canonical_page_url(str(payload.get("link") or ""))
    identity_verified = (
        observed_id == config.page_id
        and observed_name == config.page_name
        and observed_link == config.page_url
    )
    return {
        "success": identity_verified,
        "identity_verified": identity_verified,
        "page_id": observed_id,
        "page_name": observed_name,
        "page_url": observed_link,
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
                "image_path": str(image_path),
                "image_sha256": image_hash,
                "image_format": "PNG",
                "image_width": width,
                "image_height": height,
                "image_ratio": "16:9",
                "page_id": page_status.get("page_id"),
                "page_name": page_status.get("page_name"),
                "page_url": page_status.get("page_url"),
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
