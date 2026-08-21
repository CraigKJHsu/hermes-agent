"""Task-scoped Facebook Page publishing through Meta's Graph API.

This is the supported alternative to driving Facebook's changing Page composer
DOM.  The write path stays behind the same one-time Grace approval and durable
external-effect ledger as browser publishing, while binding the exact message
and image bytes into the approved Loop Contract with SHA-256 digests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from hermes_cli import kanban_db as kb
from tools.registry import registry


DEFAULT_GRAPH_API_VERSION = "v26.0"
MAX_PHOTO_BYTES = 4 * 1024 * 1024
SUPPORTED_PHOTO_MIME_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
}
AUTHORIZED_PAGE_IDS_BY_URL = {
    "https://www.facebook.com/solobizai": "531289396730654",
}


@dataclass(frozen=True)
class FacebookPageGraphConfig:
    api_version: str
    page_id: str
    page_name: str
    page_url: str
    page_access_token: str
    app_secret: str = ""


class FacebookGraphError(RuntimeError):
    """A sanitized Meta Graph API failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        dispatch_ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.dispatch_ambiguous = dispatch_ambiguous


def _json_result(success: bool, **payload: Any) -> str:
    return json.dumps(
        {"success": success, **payload},
        ensure_ascii=False,
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_config() -> FacebookPageGraphConfig:
    api_version = (
        os.environ.get("FACEBOOK_GRAPH_API_VERSION", "").strip()
        or DEFAULT_GRAPH_API_VERSION
    )
    if not re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", api_version):
        raise ValueError("FACEBOOK_GRAPH_API_VERSION must look like v26.0")

    page_id = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", page_id):
        raise ValueError("FACEBOOK_PAGE_ID must be a numeric Page ID")

    page_name = os.environ.get("FACEBOOK_PAGE_NAME", "").strip()
    if not page_name:
        raise ValueError("FACEBOOK_PAGE_NAME is required")

    supplied_page_url = os.environ.get("FACEBOOK_PAGE_URL", "").strip()
    page_url = kb.canonical_facebook_page_url(supplied_page_url)
    if page_url is None or supplied_page_url != page_url:
        raise ValueError(
            "FACEBOOK_PAGE_URL must be one canonical public Page URL"
        )
    if AUTHORIZED_PAGE_IDS_BY_URL.get(page_url) != page_id:
        raise ValueError(
            "FACEBOOK_PAGE_ID is not the fixed authorized ID for "
            "FACEBOOK_PAGE_URL"
        )

    page_access_token = os.environ.get(
        "FACEBOOK_PAGE_ACCESS_TOKEN", ""
    ).strip()
    if not page_access_token:
        raise ValueError("FACEBOOK_PAGE_ACCESS_TOKEN is required")

    return FacebookPageGraphConfig(
        api_version=api_version,
        page_id=page_id,
        page_name=page_name,
        page_url=page_url,
        page_access_token=page_access_token,
        app_secret=os.environ.get("FACEBOOK_APP_SECRET", "").strip(),
    )


def _appsecret_proof(config: FacebookPageGraphConfig) -> str:
    if not config.app_secret:
        return ""
    return hmac.new(
        config.app_secret.encode("utf-8"),
        config.page_access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _sanitize_graph_error(
    response: httpx.Response,
    config: FacebookPageGraphConfig,
) -> FacebookGraphError:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {}
    error = body.get("error") if isinstance(body, Mapping) else None
    error = error if isinstance(error, Mapping) else {}
    graph_code = error.get("code")
    graph_type = str(error.get("type") or "").strip()
    graph_message = str(error.get("message") or "Graph API request failed")
    for secret in (config.page_access_token, config.app_secret):
        if secret:
            graph_message = graph_message.replace(secret, "[REDACTED]")
    label = f"Meta Graph API HTTP {response.status_code}"
    if graph_code is not None:
        label += f" code={graph_code}"
    if graph_type:
        label += f" type={graph_type}"
    return FacebookGraphError(
        f"{label}: {graph_message}",
        status_code=response.status_code,
        dispatch_ambiguous=response.status_code >= 500,
    )


class FacebookPageGraphClient:
    """Small synchronous client that never places tokens in request URLs."""

    def __init__(
        self,
        config: FacebookPageGraphConfig,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
    ) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=(
                f"https://graph.facebook.com/{config.api_version}"
            ),
            headers={
                "Authorization": f"Bearer {config.page_access_token}",
                "User-Agent": "Hermes-ClawOps-FacebookPage/1.0",
            },
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "FacebookPageGraphClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._client.close()

    def _proof_params(self) -> dict[str, str]:
        proof = _appsecret_proof(self.config)
        return {"appsecret_proof": proof} if proof else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        write: bool = False,
    ) -> dict[str, Any]:
        safe_params = dict(params or {})
        safe_params.update(self._proof_params())
        try:
            response = self._client.request(
                method,
                path,
                params=safe_params,
                data=data,
                files=files,
            )
        except httpx.RequestError as exc:
            if write:
                failure_message = (
                    "Meta Graph API transport failure after write dispatch; "
                    "publication result is unknown and must be reconciled "
                    "before retrying"
                )
            else:
                failure_message = (
                    "Meta Graph API read-only transport failure; no Graph "
                    "write was dispatched"
                )
            raise FacebookGraphError(
                f"{failure_message}: {type(exc).__name__}",
                dispatch_ambiguous=write,
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise _sanitize_graph_error(response, self.config)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            if write:
                failure_message = (
                    "Meta Graph API returned a non-JSON success response "
                    "after write dispatch; publication result must be "
                    "reconciled before retrying"
                )
            else:
                failure_message = (
                    "Meta Graph API returned a non-JSON read-only response; "
                    "no Graph write was dispatched"
                )
            raise FacebookGraphError(
                failure_message,
                status_code=response.status_code,
                dispatch_ambiguous=write,
            ) from exc
        if not isinstance(body, Mapping):
            raise FacebookGraphError(
                "Meta Graph API returned an invalid response object",
                status_code=response.status_code,
                dispatch_ambiguous=write,
            )
        return dict(body)

    def page_identity(self) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/{self.config.page_id}",
            params={"fields": "id,name,link"},
        )

    def publish_photo(
        self,
        message: str,
        image_path: Path,
        image_bytes: bytes,
    ) -> dict[str, Any]:
        mime_type = mimetypes.guess_type(image_path.name)[0] or ""
        return self._request(
            "POST",
            f"/{self.config.page_id}/photos",
            data={"caption": message, "published": "true"},
            files={
                "source": (
                    image_path.name,
                    image_bytes,
                    mime_type,
                )
            },
            write=True,
        )

    def read_post(self, post_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/{post_id}",
            params={
                "fields": (
                    "id,message,created_time,permalink_url,"
                    "attachments{media_type,target,type,url}"
                )
            },
        )


def _verified_page_identity(
    client: FacebookPageGraphClient,
    config: FacebookPageGraphConfig,
) -> dict[str, Any]:
    identity = client.page_identity()
    observed_id = str(identity.get("id") or "").strip()
    observed_name = str(identity.get("name") or "").strip()
    observed_link = str(identity.get("link") or "").strip()
    observed_page_url = kb.canonical_facebook_page_url(observed_link)
    try:
        parsed_link = urlsplit(observed_link)
        numeric_link_port = parsed_link.port
    except ValueError:
        parsed_link = urlsplit("")
        numeric_link_port = None
    numeric_page_link_matches = bool(
        parsed_link.scheme.casefold() == "https"
        and str(parsed_link.hostname or "").casefold()
        in {"facebook.com", "www.facebook.com"}
        and numeric_link_port in {None, 443}
        and parsed_link.username is None
        and parsed_link.password is None
        and not parsed_link.query
        and not parsed_link.fragment
        and [part for part in parsed_link.path.split("/") if part]
        == [config.page_id]
    )
    if (
        observed_id != config.page_id
        or observed_name != config.page_name
        or (
            observed_page_url != config.page_url
            and not numeric_page_link_matches
        )
    ):
        raise FacebookGraphError(
            "Configured Facebook Page identity does not match the token: "
            f"expected id={config.page_id} name={config.page_name!r}, "
            f"url={config.page_url}, "
            f"observed id={observed_id or '<empty>'} "
            f"name={observed_name or '<empty>'!r} "
            f"url={observed_link or '<invalid>'}"
        )
    return identity


def facebook_page_graph_status() -> str:
    """Validate configuration and Page-token identity without publishing."""
    try:
        config = _load_config()
        with FacebookPageGraphClient(config) as client:
            identity = _verified_page_identity(client, config)
        return _json_result(
            True,
            configured=True,
            api_version=config.api_version,
            page_id=config.page_id,
            page_name=config.page_name,
            page_url=config.page_url,
            observed_link=str(identity.get("link") or ""),
            token_present=True,
            appsecret_proof_enabled=bool(config.app_secret),
        )
    except (ValueError, FacebookGraphError) as exc:
        return _json_result(
            False,
            configured=False,
            error=str(exc),
        )


def _current_task_scope() -> tuple[str, int]:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if not task_id or not raw_run_id:
        raise ValueError(
            "facebook_page_graph_publish requires an active Kanban worker run"
        )
    try:
        run_id = int(raw_run_id)
    except ValueError as exc:
        raise ValueError("HERMES_KANBAN_RUN_ID must be an integer") from exc
    return task_id, run_id


def _approved_graph_binding(
    conn: Any,
    task_id: str,
) -> Mapping[str, str]:
    binding = kb.grace_task_facebook_page_api_permission(conn, task_id)
    if not binding:
        raise ValueError(
            "Task is not bound to a consumed graph_api Facebook Page "
            "publishing approval"
        )
    return binding


def _validated_payload(
    binding: Mapping[str, str],
    config: FacebookPageGraphConfig,
    *,
    page_url: str,
    message: str,
    image_path: str,
) -> tuple[Path, bytes, str, str]:
    canonical_url = kb.canonical_facebook_page_url(page_url)
    if canonical_url is None or canonical_url != page_url:
        raise ValueError("page_url must be one canonical public Page URL")
    if canonical_url != binding.get("page_url") or canonical_url != config.page_url:
        raise ValueError(
            "Requested Page URL does not match both the approved contract "
            "and configured Page"
        )
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    message_sha256 = _sha256_text(message)
    if message_sha256 != binding.get("message_sha256"):
        raise ValueError("message bytes do not match the approved SHA-256")

    image = Path(str(image_path or "")).expanduser().resolve()
    if not image.is_file():
        raise ValueError("image_path must identify one existing local file")
    mime_type = mimetypes.guess_type(image.name)[0] or ""
    if mime_type not in SUPPORTED_PHOTO_MIME_TYPES:
        raise ValueError(
            "Facebook Page photo must be JPEG, PNG, GIF, BMP, or TIFF"
        )
    with image.open("rb") as image_handle:
        image_bytes = image_handle.read(MAX_PHOTO_BYTES + 1)
    if not image_bytes or len(image_bytes) > MAX_PHOTO_BYTES:
        raise ValueError("Facebook Page photo must be between 1 byte and 4 MiB")
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if image_sha256 != binding.get("image_sha256"):
        raise ValueError("image bytes do not match the approved SHA-256")
    return image, image_bytes, message_sha256, image_sha256


def _verified_readback(
    readback: Mapping[str, Any],
    *,
    post_id: str,
    message: str,
    page_id: str,
    photo_id: str,
) -> tuple[bool, str]:
    post_match = re.fullmatch(r"([1-9][0-9]*)_([1-9][0-9]*)", post_id)
    if post_match is None or post_match.group(1) != page_id:
        return False, "post id is not bound to the configured Page"
    post_object_id = post_match.group(2)
    if str(readback.get("id") or "") != post_id:
        return False, "read-back post id mismatch"
    if str(readback.get("message") or "") != message:
        return False, "read-back message mismatch"
    attachments = readback.get("attachments")
    attachment_data = (
        attachments.get("data")
        if isinstance(attachments, Mapping)
        else None
    )
    if not isinstance(attachment_data, list) or not attachment_data:
        return False, "read-back image attachment is missing"
    attachment_matches = any(
        isinstance(attachment, Mapping)
        and str(attachment.get("media_type") or "").casefold() == "photo"
        and isinstance(attachment.get("target"), Mapping)
        and str(attachment["target"].get("id") or "") == photo_id
        for attachment in attachment_data
    )
    if not attachment_matches:
        return False, "read-back image attachment does not match the uploaded photo"
    return True, ""


def _canonical_post_permalink(post_id: str, page_id: str) -> str:
    """Build a credential-free permalink from the verified composite ID."""
    match = re.fullmatch(r"([1-9][0-9]*)_([1-9][0-9]*)", post_id)
    if match is None or match.group(1) != page_id:
        raise ValueError("post id is not bound to the configured Page")
    return f"https://www.facebook.com/{page_id}/posts/{match.group(2)}"


def facebook_page_graph_publish(
    *,
    page_url: str,
    message: str,
    image_path: str,
) -> str:
    """Publish one contract-bound photo post and read it back by post ID."""
    try:
        task_id, run_id = _current_task_scope()
        config = _load_config()
        with kb.connect_closing() as conn:
            binding = _approved_graph_binding(conn, task_id)
            image, image_bytes, message_sha256, image_sha256 = _validated_payload(
                binding,
                config,
                page_url=page_url,
                message=message,
                image_path=image_path,
            )

            with FacebookPageGraphClient(config) as client:
                identity = _verified_page_identity(client, config)
                reservation_error = kb.reserve_external_facebook_page_post(
                    conn,
                    task_id,
                    page_url,
                    expected_run_id=run_id,
                    transport="graph_api",
                    reservation_details={
                        "transport": "graph_api",
                        "api_version": config.api_version,
                        "page_id": config.page_id,
                        "page_name": config.page_name,
                        "page_url": config.page_url,
                        "message_sha256": message_sha256,
                        "image_sha256": image_sha256,
                    },
                )
                if reservation_error:
                    raise ValueError(reservation_error)

                try:
                    published = client.publish_photo(
                        message,
                        image,
                        image_bytes,
                    )
                except FacebookGraphError as exc:
                    if not exc.dispatch_ambiguous:
                        kb.release_external_facebook_page_post_reservation(
                            conn,
                            task_id,
                            page_url,
                            expected_run_id=run_id,
                            reason="graph_api_definitive_rejection",
                        )
                    return _json_result(
                        False,
                        error=str(exc),
                        dispatch_ambiguous=exc.dispatch_ambiguous,
                        reconciliation_required=exc.dispatch_ambiguous,
                        task_id=task_id,
                    )

                post_id = str(published.get("post_id") or "").strip()
                photo_id = str(published.get("id") or "").strip()
                if not post_id:
                    return _json_result(
                        False,
                        error=(
                            "Meta accepted the photo request without returning "
                            "post_id; reconcile before any retry"
                        ),
                        dispatch_ambiguous=True,
                        reconciliation_required=True,
                        task_id=task_id,
                    )
                try:
                    permalink_url = _canonical_post_permalink(
                        post_id,
                        config.page_id,
                    )
                except ValueError:
                    return _json_result(
                        False,
                        error=(
                            "Meta returned a post_id that is not bound to "
                            "the configured Page; reconcile before any retry"
                        ),
                        dispatch_ambiguous=True,
                        reconciliation_required=True,
                        task_id=task_id,
                        post_id=post_id,
                        photo_id=photo_id or None,
                    )

                try:
                    created_effect = kb.record_external_effect(
                        conn,
                        task_id,
                        platform="facebook",
                        state="created",
                        external_id=post_id,
                        details={
                            "transport": "graph_api",
                            "api_version": config.api_version,
                            "page_id": config.page_id,
                            "page_name": config.page_name,
                            "page_url": config.page_url,
                            "message_sha256": message_sha256,
                            "image_sha256": image_sha256,
                            "photo_id": photo_id or None,
                        },
                        expected_run_id=run_id,
                    )
                except Exception as exc:
                    # The reserved create_started row still prevents retries.
                    # Preserve the post ID in the result so an operator can
                    # reconcile the real external effect.
                    return _json_result(
                        True,
                        published=True,
                        verified=False,
                        durable_verified=False,
                        reconciliation_required=True,
                        warning=(
                            "durable created-state write failed: "
                            f"{type(exc).__name__}"
                        ),
                        task_id=task_id,
                        post_id=post_id,
                        photo_id=photo_id or None,
                    )
                try:
                    readback = client.read_post(post_id)
                except FacebookGraphError as exc:
                    return _json_result(
                        True,
                        published=True,
                        verified=False,
                        reconciliation_required=True,
                        warning=str(exc),
                        task_id=task_id,
                        post_id=post_id,
                        photo_id=photo_id or None,
                        external_effect=created_effect,
                    )

                verified, mismatch = _verified_readback(
                    readback,
                    post_id=post_id,
                    message=message,
                    page_id=config.page_id,
                    photo_id=photo_id,
                )
                if not verified:
                    return _json_result(
                        True,
                        published=True,
                        verified=False,
                        reconciliation_required=True,
                        warning=mismatch,
                        task_id=task_id,
                        post_id=post_id,
                        photo_id=photo_id or None,
                        readback=dict(readback),
                        external_effect=created_effect,
                    )

                try:
                    verified_effect = kb.record_external_effect(
                        conn,
                        task_id,
                        platform="facebook",
                        state="verified",
                        external_id=post_id,
                        details={
                            "transport": "graph_api",
                            "api_version": config.api_version,
                            "page_id": config.page_id,
                            "page_name": config.page_name,
                            "page_url": config.page_url,
                            "message_sha256": message_sha256,
                            "image_sha256": image_sha256,
                            "photo_id": photo_id or None,
                            "permalink_url": permalink_url,
                            "created_time": readback.get("created_time"),
                            "observed_page_link": identity.get("link"),
                        },
                        expected_run_id=run_id,
                    )
                except Exception as exc:
                    return _json_result(
                        True,
                        published=True,
                        verified=True,
                        durable_verified=False,
                        reconciliation_required=True,
                        warning=(
                            "durable verified-state write failed: "
                            f"{type(exc).__name__}"
                        ),
                        task_id=task_id,
                        post_id=post_id,
                        photo_id=photo_id or None,
                        permalink_url=permalink_url,
                        created_time=readback.get("created_time"),
                    )
                return _json_result(
                    True,
                    published=True,
                    verified=True,
                    durable_verified=True,
                    task_id=task_id,
                    page_id=config.page_id,
                    page_name=config.page_name,
                    post_id=post_id,
                    photo_id=photo_id or None,
                    permalink_url=permalink_url,
                    created_time=readback.get("created_time"),
                    message_sha256=message_sha256,
                    image_sha256=image_sha256,
                    external_effect=verified_effect,
                )
    except (ValueError, FacebookGraphError) as exc:
        return _json_result(False, error=str(exc))
    except Exception as exc:
        return _json_result(
            False,
            error=(
                "Facebook Page Graph API tool failed closed: "
                f"{type(exc).__name__}"
            ),
        )


FACEBOOK_PAGE_GRAPH_STATUS_SCHEMA = {
    "name": "facebook_page_graph_status",
    "description": (
        "Read-only preflight for the configured Facebook Page Graph API. "
        "Verifies the Page access token resolves to the exact configured "
        "numeric Page ID and Page name; never publishes content or returns "
        "the token. Run this before requesting approval for a Page post."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


FACEBOOK_PAGE_GRAPH_PUBLISH_SCHEMA = {
    "name": "facebook_page_graph_publish",
    "description": (
        "Publish one approved Facebook Page photo post through Meta Graph "
        "API, not the browser. Requires an active Kanban execution task whose "
        "consumed one-time Loop Contract sets facebook_page_post.transport "
        "to graph_api and binds exact message_sha256/image_sha256 values. "
        "The tool reserves the durable external effect before POST, records "
        "post_id immediately, reads back permalink/message/image evidence, "
        "and never retries an ambiguous request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page_url": {
                "type": "string",
                "description": "Exact canonical approved Facebook Page URL.",
            },
            "message": {
                "type": "string",
                "description": "Exact approved post text, including hashtags.",
            },
            "image_path": {
                "type": "string",
                "description": "Local path to the exact approved image file.",
            },
        },
        "required": ["page_url", "message", "image_path"],
    },
}


def _tool_available() -> bool:
    # Keep the schemas visible before setup so ClawOps can run the read-only
    # status tool and return the exact missing configuration instead of
    # silently falling back to browser publishing.
    return True


registry.register(
    name="facebook_page_graph_status",
    toolset="facebook-pages-api",
    schema=FACEBOOK_PAGE_GRAPH_STATUS_SCHEMA,
    handler=lambda _args, **_kw: facebook_page_graph_status(),
    check_fn=_tool_available,
    emoji="📘",
)

registry.register(
    name="facebook_page_graph_publish",
    toolset="facebook-pages-api",
    schema=FACEBOOK_PAGE_GRAPH_PUBLISH_SCHEMA,
    handler=lambda args, **_kw: facebook_page_graph_publish(
        page_url=str(args.get("page_url") or ""),
        message=args.get("message"),
        image_path=str(args.get("image_path") or ""),
    ),
    check_fn=_tool_available,
    emoji="📤",
)
