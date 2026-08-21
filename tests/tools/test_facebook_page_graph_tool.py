from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from tools import facebook_page_graph_tool as graph_tool


def _config() -> graph_tool.FacebookPageGraphConfig:
    return graph_tool.FacebookPageGraphConfig(
        api_version="v26.0",
        page_id="123456",
        page_name="AI BizWeek｜SoloBiz AI 一人公司商業誌",
        page_url="https://www.facebook.com/solobizai",
        page_access_token="page-token-secret",
        app_secret="app-secret-value",
    )


def test_graph_client_uses_bearer_header_and_publishes_single_photo(tmp_path):
    image = tmp_path / "questgen-v2.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer page-token-secret"
        assert "page-token-secret" not in str(request.url)
        if request.method == "GET" and request.url.path.endswith("/123456"):
            return httpx.Response(
                200,
                json={
                    "id": "123456",
                    "name": "AI BizWeek｜SoloBiz AI 一人公司商業誌",
                    "link": "https://www.facebook.com/SoloBizAi/",
                },
            )
        if request.method == "POST":
            body = request.content
            assert b'name="caption"' in body
            assert "Questgen 最終正文".encode() in body
            assert b'name="published"' in body
            assert b"true" in body
            assert b'name="source"' in body
            return httpx.Response(
                200,
                json={"id": "photo-1", "post_id": "123456_987"},
            )
        return httpx.Response(
            200,
            json={
                "id": "123456_987",
                "message": "Questgen 最終正文",
                "created_time": "2026-08-15T12:00:00+0000",
                "permalink_url": (
                    "https://www.facebook.com/123456/posts/987"
                ),
                "attachments": {"data": [{
                    "media_type": "photo",
                    "target": {"id": "photo-1"},
                }]},
            },
        )

    with graph_tool.FacebookPageGraphClient(
        _config(),
        transport=httpx.MockTransport(handler),
    ) as client:
        identity = graph_tool._verified_page_identity(client, _config())
        published = client.publish_photo(
            "Questgen 最終正文",
            image,
            image.read_bytes(),
        )
        readback = client.read_post(published["post_id"])

    assert identity["id"] == "123456"
    assert published == {"id": "photo-1", "post_id": "123456_987"}
    assert graph_tool._verified_readback(
        readback,
        post_id="123456_987",
        message="Questgen 最終正文",
        page_id="123456",
        photo_id="photo-1",
    ) == (True, "")
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_payload_must_match_both_approved_hashes(tmp_path):
    message = "Questgen 最終正文\n#SoloBizAI"
    image = tmp_path / "questgen-v2.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nverified-image")
    binding = {
        "page_url": "https://www.facebook.com/solobizai",
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
    }

    validated = graph_tool._validated_payload(
        binding,
        _config(),
        page_url="https://www.facebook.com/solobizai",
        message=message,
        image_path=str(image),
    )
    assert validated[0] == image.resolve()
    assert validated[1] == image.read_bytes()

    with pytest.raises(ValueError, match="message bytes"):
        graph_tool._validated_payload(
            binding,
            _config(),
            page_url="https://www.facebook.com/solobizai",
            message=message + " changed",
            image_path=str(image),
        )


def test_status_reports_missing_configuration_without_exposing_token(
    monkeypatch,
):
    for name in (
        "FACEBOOK_PAGE_ID",
        "FACEBOOK_PAGE_NAME",
        "FACEBOOK_PAGE_URL",
        "FACEBOOK_PAGE_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    result = json.loads(graph_tool.facebook_page_graph_status())

    assert result["success"] is False
    assert result["configured"] is False
    assert "FACEBOOK_PAGE_ID" in result["error"]
    assert "access_token" not in json.dumps(result).lower()


def test_config_rejects_page_id_not_fixed_to_authorized_url(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "999999")
    monkeypatch.setenv(
        "FACEBOOK_PAGE_NAME",
        "AI BizWeek｜SoloBiz AI 一人公司商業誌",
    )
    monkeypatch.setenv(
        "FACEBOOK_PAGE_URL",
        "https://www.facebook.com/solobizai",
    )
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "secret")

    with pytest.raises(ValueError, match="fixed authorized ID"):
        graph_tool._load_config()


def test_graph_error_redacts_token_and_app_secret():
    config = _config()
    request = httpx.Request("POST", "https://graph.facebook.com/v26.0/123")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "code": 190,
                "type": "OAuthException",
                "message": (
                    "bad page-token-secret and app-secret-value"
                ),
            }
        },
    )

    sanitized = str(graph_tool._sanitize_graph_error(response, config))

    assert "page-token-secret" not in sanitized
    assert "app-secret-value" not in sanitized
    assert sanitized.count("[REDACTED]") == 2


def test_page_identity_must_bind_numeric_id_name_and_canonical_link():
    config = _config()

    class WrongPageClient:
        def page_identity(self):
            return {
                "id": config.page_id,
                "name": config.page_name,
                "link": "https://www.facebook.com/anotherpage",
            }

    with pytest.raises(graph_tool.FacebookGraphError, match="does not match"):
        graph_tool._verified_page_identity(WrongPageClient(), config)

    class NumericPageLinkClient:
        def page_identity(self):
            return {
                "id": config.page_id,
                "name": config.page_name,
                "link": f"https://www.facebook.com/{config.page_id}",
            }

    assert graph_tool._verified_page_identity(
        NumericPageLinkClient(),
        config,
    )["id"] == config.page_id


def test_readback_uses_verified_post_id_not_untrusted_graph_permalink():
    readback = {
        "id": "123456_987",
        "message": "approved message",
        "permalink_url": "https://www.facebook.com/anotherpage/posts/987",
        "attachments": {"data": [{
            "media_type": "photo",
            "target": {"id": "photo-1"},
        }]},
    }

    assert graph_tool._verified_readback(
        readback,
        post_id="123456_987",
        message="approved message",
        page_id="123456",
        photo_id="photo-1",
    ) == (True, "")
    assert graph_tool._canonical_post_permalink(
        "123456_987",
        "123456",
    ) == "https://www.facebook.com/123456/posts/987"


def test_readback_accepts_photo_attachment_with_opaque_graph_permalink():
    readback = {
        "id": "123456_987",
        "message": "approved message",
        "permalink_url": "https://www.facebook.com/photo.php?fbid=654321",
        "attachments": {"data": [{
            "media_type": "photo",
            "target": {"id": "photo-1"},
        }]},
    }

    assert graph_tool._verified_readback(
        readback,
        post_id="123456_987",
        message="approved message",
        page_id="123456",
        photo_id="photo-1",
    ) == (True, "")

    readback["permalink_url"] = (
        "https://www.facebook.com/solobizai/posts/pfbidUnbound"
    )
    assert graph_tool._verified_readback(
        readback,
        post_id="123456_987",
        message="approved message",
        page_id="123456",
        photo_id="photo-1",
    ) == (True, "")


def test_readback_rejects_a_different_photo_attachment():
    readback = {
        "id": "123456_987",
        "message": "approved message",
        "attachments": {"data": [{
            "media_type": "photo",
            "target": {"id": "different-photo"},
        }]},
    }

    assert graph_tool._verified_readback(
        readback,
        post_id="123456_987",
        message="approved message",
        page_id="123456",
        photo_id="photo-1",
    ) == (
        False,
        "read-back image attachment does not match the uploaded photo",
    )


def test_read_only_transport_error_does_not_claim_ambiguous_publication():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with graph_tool.FacebookPageGraphClient(
        _config(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(graph_tool.FacebookGraphError) as error:
            client.page_identity()

    assert error.value.dispatch_ambiguous is False
    assert "no Graph write was dispatched" in str(error.value)
    assert "publication result is unknown" not in str(error.value)


def test_read_only_non_json_response_does_not_claim_ambiguous_publication():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with graph_tool.FacebookPageGraphClient(
        _config(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(graph_tool.FacebookGraphError) as error:
            client.page_identity()

    assert error.value.dispatch_ambiguous is False
    assert "no Graph write was dispatched" in str(error.value)
    assert "publication result must be reconciled" not in str(error.value)


@pytest.mark.parametrize(
    ("status_code", "dispatch_ambiguous"),
    [(400, False), (403, False), (500, True), (503, True)],
)
def test_graph_http_error_ambiguity_matches_response_class(
    status_code,
    dispatch_ambiguous,
):
    response = httpx.Response(
        status_code,
        json={"error": {"code": 200, "message": "rejected"}},
    )

    error = graph_tool._sanitize_graph_error(response, _config())

    assert error.dispatch_ambiguous is dispatch_ambiguous


@pytest.mark.parametrize(
    "post_id",
    ["", "opaque", "654321_987", "123456_pfbidOpaque"],
)
def test_canonical_post_permalink_rejects_unbound_post_id(post_id):
    with pytest.raises(ValueError, match="not bound"):
        graph_tool._canonical_post_permalink(post_id, "123456")
