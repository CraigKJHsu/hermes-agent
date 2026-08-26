from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json

from tools import facebook_page_graph_tool as tool


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
