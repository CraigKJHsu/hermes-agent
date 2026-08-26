"""Task-scoped Facebook Page photo publishing through Meta Graph API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
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


def _handle_publish(args: dict[str, Any], **_kwargs: Any) -> str:
    from hermes_cli import kanban_db as kb

    contract, scope_error = _authorized_execution_contract()
    if contract is None:
        return json.dumps({"success": False, "published": False, "error": scope_error})
    page_url = _canonical_page_url(str(args.get("page_url") or ""))
    message = str(args.get("message") or "")
    try:
        image_path = Path(str(args.get("image_path") or "")).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        return json.dumps({
            "success": False,
            "published": False,
            "error": f"Approved image path is invalid: {type(exc).__name__}",
        }, ensure_ascii=False)
    message_bytes = message.encode("utf-8")
    message_sha256 = hashlib.sha256(message_bytes).hexdigest()
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
    if (
        page_url != contract["page_url"]
        or message_sha256 != contract["message_sha256"]
        or image_sha256 != contract["image_sha256"]
    ):
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Target URL or exact payload hashes do not match the approved contract.",
            "message_sha256": message_sha256,
            "image_sha256": image_sha256,
        }, ensure_ascii=False)
    config = _load_page_config()
    if config.missing:
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Facebook Page Graph configuration is incomplete.",
            "missing_configuration": config.missing,
        }, ensure_ascii=False)
    if page_url != config.page_url:
        return json.dumps({
            "success": False,
            "published": False,
            "error": "Approved Page URL does not match configured Page URL.",
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

    task_id, run_id, board = _worker_scope()
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
        message_verified = str(readback.get("message") or "") == message
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
        "message_sha256": message_sha256,
        "image_sha256": image_sha256,
        "retry_permitted": False,
    }, ensure_ascii=False)


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
