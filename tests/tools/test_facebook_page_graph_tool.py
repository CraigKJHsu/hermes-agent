from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import struct

from tools import facebook_page_graph_tool as tool
from hermes_cli import kanban_db as kb


def _config() -> tool.FacebookPageConfig:
    return tool.FacebookPageConfig(
        api_version="v26.0",
        page_id="123",
        page_name="Test Page",
        page_url="https://www.facebook.com/testpage",
        access_token="secret-token",
        app_secret="secret-app",
    )


def test_status_returns_verified_identity_without_token(monkeypatch):
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: {
            "id": "123",
            "name": "Test Page",
            "link": "https://www.facebook.com/testpage",
        },
    )

    result = json.loads(tool._handle_status({}))

    assert result["success"] is True
    assert result["identity_verified"] is True
    assert "secret-token" not in json.dumps(result)


def test_status_rejects_identity_when_graph_omits_page_link(monkeypatch):
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: {"id": "123", "name": "Test Page"},
    )

    result = json.loads(tool._handle_status({}))

    assert result["success"] is False
    assert result["identity_verified"] is False
    assert result["page_url"] == ""


def test_legacy_sealed_page_publish_scope_compiles_exact_manifest():
    message_hash = "a" * 64
    image_hash = "b" * 64
    contract = {
        "external_targets": ["https://www.facebook.com/testpage"],
        "scope": {
            "allowed": [
                "唯一目標：Facebook Page https://www.facebook.com/testpage（Page ID 123）",
                f"僅使用已驗證的精確正文，SHA-256={message_hash}",
                f"僅使用 /tmp/hero.png，SHA-256={image_hash}",
            ]
        },
    }

    assert kb._facebook_page_post_manifest(contract) == {
        "action": "create_post",
        "transport": "graph_api",
        "page_url": "https://www.facebook.com/testpage",
        "message_sha256": message_hash,
        "image_sha256": image_hash,
        "page_id": "123",
    }


def test_legacy_sealed_page_publish_scope_rejects_ambiguous_hashes():
    contract = {
        "external_targets": ["https://www.facebook.com/testpage"],
        "scope": {
            "allowed": [
                "唯一目標：Facebook Page https://www.facebook.com/testpage（Page ID 123）",
                f"僅使用已驗證的精確正文，SHA-256={'a' * 64}",
                f"僅使用已驗證的精確正文，SHA-256={'c' * 64}",
                f"僅使用 /tmp/hero.png，SHA-256={'b' * 64}",
            ]
        },
    }

    assert kb._facebook_page_post_manifest(contract) is None


def test_preflight_verifies_source_diff_png_hash_and_page_identity(monkeypatch, tmp_path):
    source = (
        "案例正文\n\n今日可做：建立報價計算器。\n\n"
        "💬 Group 討論題：\n討論內容\n\n"
        "Page → Group 導流：\n導流內容"
    )
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    image_path = tmp_path / "hero.png"
    image_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 1600, 900)
        + b"\x08\x06\x00\x00\x00"
    )
    image_path.write_bytes(image_bytes)
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    contract = {
        "original_request": (
            f"sha256={source_hash}\nBEGIN_FACEBOOK_PAGE_SOURCE_TEXT\n"
            f"{source}\nEND_FACEBOOK_PAGE_SOURCE_TEXT"
        ),
        "scope": {
            "allowed": [f"{image_path} PNG 1600×900 SHA-256 {image_hash}"]
        },
    }
    scope = tool.OpenClawCapabilityScope(
        task_id="t_preflight",
        run_id=1,
        delegation_id="delegation",
        contract_fingerprint="f" * 64,
        approval_grant_id="",
        backend_agent_id="missioncrew-facebook-page-operator",
        task_type="facebook_page_publish_preflight",
    )
    monkeypatch.setattr(
        tool, "_authorized_openclaw_contract", lambda _scope: (contract, "")
    )
    monkeypatch.setattr(
        tool,
        "_handle_status",
        lambda _args: json.dumps({
            "identity_verified": True,
            "page_id": "123",
            "page_name": "Test Page",
            "page_url": "https://www.facebook.com/testpage",
        }),
    )

    final_message = "案例正文\n\n今日可做：建立報價計算器。\n\n#一人公司 #報價系統"
    result = json.loads(tool._handle_publish_preflight(
        {"final_message": final_message, "image_path": str(image_path)},
        capability_scope=scope,
    ))

    assert result["success"] is True
    assert result["published"] is False
    assert result["external_actions_performed"] is False
    assert result["manifest"]["image_ratio"] == "16:9"
    assert result["manifest"]["message_sha256"] == hashlib.sha256(
        final_message.encode("utf-8")
    ).hexdigest()
    assert result["evidence"]["hashtags_are_final_paragraph"] is True


def test_openclaw_status_rejects_unbound_capability_before_graph(monkeypatch):
    scope = tool.OpenClawCapabilityScope(
        task_id="t_missing",
        run_id=1,
        delegation_id="delegation",
        contract_fingerprint="f" * 64,
        approval_grant_id="approval",
        backend_agent_id="missioncrew-facebook-page-operator",
    )
    monkeypatch.setattr(
        tool,
        "_authorized_openclaw_contract",
        lambda _scope: (None, "no approved contract"),
    )
    monkeypatch.setattr(
        tool,
        "_handle_status",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("Graph status must not run for an unbound capability")
        ),
    )

    result = json.loads(
        tool.execute_openclaw_facebook_page_capability("status", {}, scope)
    )

    assert result == {
        "success": False,
        "published": False,
        "error": "no approved contract",
    }


def test_page_url_parser_rejects_lookalike_hosts_and_extra_routes():
    assert tool._canonical_page_url(
        "https://www.facebook.com/testpage/"
    ) == "https://www.facebook.com/testpage"
    assert tool._canonical_page_url(
        "https://www.facebook.com.evil.example/testpage"
    ) == ""
    assert tool._canonical_page_url(
        "https://www.facebook.com/testpage/posts/1"
    ) == ""
    assert tool._canonical_page_url(
        "https://www.facebook.com:bad/testpage"
    ) == ""


def test_publish_rejects_hash_mismatch_before_reservation(monkeypatch, tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"approved-image")
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": "0" * 64,
            "image_sha256": hashlib.sha256(b"approved-image").hexdigest(),
        }, ""),
    )
    graph_called = False

    def fail_graph(*_args, **_kwargs):
        nonlocal graph_called
        graph_called = True
        raise AssertionError("Graph API must not run after a hash mismatch")

    monkeypatch.setattr(tool, "_graph_request", fail_graph)

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": "wrong message",
        "image_path": str(image_path),
    }))

    assert result["success"] is False
    assert result["published"] is False
    assert graph_called is False


def test_publish_uses_unique_grace_accepted_preflight_body(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    approved_message = "Exact approved message"
    supplied_message = "Wrong source body"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(approved_message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(
        tool,
        "_accepted_preflight_message",
        lambda *_args, **_kwargs: approved_message,
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    posted_messages = []

    def graph(_cfg, method, _path, **kwargs):
        if method == "POST":
            posted_messages.append(kwargs["data"]["message"])
            return {"id": "photo-1", "post_id": "123_post-1"}
        return {
            "id": "123_post-1",
            "message": approved_message,
            "permalink_url": "https://www.facebook.com/test/posts/post-1",
            "attachments": {"data": [{
                "media_type": "photo",
                "target": {"id": "photo-1"},
                "url": "https://www.facebook.com/photo.php?id=photo-1",
            }]},
        }

    monkeypatch.setattr(tool, "_graph_request", graph)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(kb, "reserve_facebook_page_create", lambda *_a, **_k: None)
    monkeypatch.setattr(kb, "record_external_effect", lambda *_a, **_k: {})

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": supplied_message,
        "image_path": str(image_path),
    }))

    assert result["success"] is True
    assert result["verified"] is True
    assert posted_messages == [approved_message]
    assert result["message_sha256"] == hashlib.sha256(
        approved_message.encode()
    ).hexdigest()


def test_publish_reserves_records_and_verifies_readback(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    message = "Exact approved message"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    responses = iter([
        {"id": "photo-1", "post_id": "123_post-1"},
        {
            "id": "123_post-1",
            "message": message,
            "permalink_url": "https://www.facebook.com/test/posts/post-1",
            "created_time": "2026-08-26T00:00:00+0000",
            "attachments": {"data": [{
                "media_type": "photo",
                "target": {"id": "photo-1"},
                "url": "https://www.facebook.com/photo.php?id=photo-1",
            }]},
        },
    ])
    monkeypatch.setattr(
        tool, "_graph_request", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    calls = []

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(
        kb,
        "reserve_facebook_page_create",
        lambda *_args, **kwargs: calls.append(("reserve", kwargs)) or None,
    )
    monkeypatch.setattr(
        kb,
        "record_external_effect",
        lambda *_args, **kwargs: calls.append(("record", kwargs)) or {},
    )

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": message,
        "image_path": str(image_path),
    }))

    assert result["success"] is True
    assert result["published"] is True
    assert result["verified"] is True
    assert result["post_id"] == "123_post-1"
    assert [name for name, _ in calls] == ["reserve", "record", "record"]
    assert calls[-1][1]["state"] == "verified"
    details = calls[-1][1]["details"]
    assert details["readback_message"] == message
    assert details["readback_message_length"] == len(message)
    assert details["readback_message_sha256"] == hashlib.sha256(
        message.encode("utf-8")
    ).hexdigest()
    assert details["readback_message_final_paragraph"] == message
    assert result["readback_message_sha256"] == details["readback_message_sha256"]


def test_publish_preserves_reservation_after_transport_error(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    message = "Exact approved message"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tool.FacebookGraphError("ReadTimeout")
        ),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    calls = []

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(
        kb,
        "reserve_facebook_page_create",
        lambda *_args, **kwargs: calls.append(("reserve", kwargs)) or None,
    )
    monkeypatch.setattr(
        kb,
        "record_external_effect",
        lambda *_args, **kwargs: calls.append(("record", kwargs)) or {},
    )

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": message,
        "image_path": str(image_path),
    }))

    assert result["success"] is False
    assert result["published"] is None
    assert result["retry_permitted"] is False
    assert [name for name, _ in calls] == ["reserve"]
