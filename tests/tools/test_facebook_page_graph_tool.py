from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import struct
import pytest

from tools import facebook_page_graph_tool as tool
from hermes_cli import kanban_db as kb


@pytest.fixture
def accepted_page_package(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    image = tmp_path / "hero.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                      + struct.pack(">II", 1664, 936) + b"\x08\x06\x00\x00\x00")
    message = "  已驗收正文\r\n\r\n保留 Page → Group CTA。\n\n#案例\n"
    contract = {"identity": {"platform": "telegram", "chat_id": "chat-1",
                             "thread_id": "2", "project": "case-project"}}
    with kb.connect_closing() as conn:
        source_id = kb.create_task(conn, title="accepted package")
        assert kb.claim_task(conn, source_id)
        assert kb.complete_task(conn, source_id, metadata={
            "loop_contract": contract,
            "acceptance_evidence": {"inline_content_package": {"facebook_page_post": message}},
        })
        review_id = kb.create_task(conn, title="accepted review", parents=(source_id,))
        assert kb.claim_task(conn, review_id)
        assert kb.complete_task(conn, review_id, metadata={
            "review_outcome": "accepted", "accepted": True,
            "asset_review": [{"asset_family": "page_hero", "accepted": True,
                              "path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                              "width": 1664, "height": 936}],
        })
        conn.execute(
            "INSERT INTO grace_delegations (delegation_id, contract_fingerprint, request_instance_id, "
            "platform, chat_id, thread_id, session_key, session_id, resolved_route, approval_required, "
            "state, execution_task_id, review_task_id, created_at, updated_at) "
            "VALUES ('gd-source', ?, 'request-source', 'telegram', 'chat-1', '2', 'session-key', "
            "'session', '{}', 0, 'queued', ?, ?, 1, 1)",
            ("a" * 64, source_id, review_id),
        )
    contract["scope"] = {"allowed": [f"Use accepted Facebook Page package: execution_task_id={source_id}; review_task_id={review_id}"]}
    contract["facebook_page_preflight_source"] = tool.bind_accepted_page_preflight_source(contract)
    return contract, message, source_id, review_id


@pytest.mark.parametrize("fault", [None, "wrong_topic", "wrong_project", "rejected", "new_execution",
                                       "wrong_review", "ambiguous", "unselected"])
def test_accepted_page_source_requires_exact_review_and_topic(accepted_page_package, fault):
    contract, message, source_id, review_id = accepted_page_package
    if fault == "wrong_topic":
        contract["identity"]["thread_id"] = "3"
    elif fault == "wrong_project":
        contract["identity"]["project"] = "other-case"
    elif fault == "rejected":
        with kb.connect_closing() as conn:
            row = kb.latest_run(conn, review_id)
            conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"review_outcome":"rejected"}', row.id))
    elif fault == "new_execution":
        with kb.connect_closing() as conn:
            conn.execute("UPDATE task_runs SET ended_at=ended_at+100 WHERE task_id=?", (source_id,))
    elif fault == "wrong_review":
        contract["scope"]["allowed"][0] = contract["scope"]["allowed"][0].replace(review_id, "t_ffffffff")
    elif fault == "ambiguous":
        contract["scope"]["allowed"] *= 2
    elif fault == "unselected":
        contract["scope"]["allowed"] = []
        assert tool.bind_accepted_page_preflight_source(contract) is None
        return
    if fault:
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"] == message
        assert resolved["message_utf8_bytes"] == len(message.encode("utf-8"))
        assert resolved["message_sha256"] == hashlib.sha256(message.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("fault", [None, "changed_pin", "changed_message", "wrong_image", "inactive"])
def test_preflight_reads_accepted_bytes_through_active_capability(
    accepted_page_package, monkeypatch, fault,
):
    contract, message, _, _ = accepted_page_package
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="preflight")
        task = kb.claim_task(conn, task_id)
        run_id = task.current_run_id
        scope = tool.OpenClawCapabilityScope(task_id=task_id, run_id=run_id,
            delegation_id="gd-preflight", contract_fingerprint="b" * 64, approval_grant_id="",
            backend_agent_id="missioncrew-facebook-page-operator", task_type="facebook_page_publish_preflight")
        if fault == "changed_pin":
            contract["facebook_page_preflight_source"]["message"] = "different case"
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({
            "delegation_id": scope.delegation_id, "contract_fingerprint": scope.contract_fingerprint,
            "approval_grant_id": "", "backend_agent_id": scope.backend_agent_id,
            "allowed_tools": ["facebook_page_publish_preflight"], "external_effect_budget": 0,
            "task_type": scope.task_type, "credential_refs": ["missioncrew-facebook-page"],
            "loop_contract": contract,
        }), run_id))
        if fault == "inactive":
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    calls = []
    def status(_args):
        calls.append("GET")
        return json.dumps({"identity_verified": True, "configured": True, "page_id": "123",
                           "page_name": "Test", "page_url": "https://www.facebook.com/test", "api_version": "v26.0"})
    monkeypatch.setattr(tool, "_handle_status", status)
    result = json.loads(tool._handle_publish_preflight({
        "final_message": "changed" if fault == "changed_message" else "",
        "image_path": "/unbound/private.txt" if fault == "wrong_image" else "",
    }, capability_scope=scope))
    assert result["success"] is (fault is None)
    assert result["published"] is False
    assert calls == ([] if fault else ["GET"])
    if not fault:
        assert result["final_message"] == message
        assert result["manifest"]["message_utf8_bytes"] == len(message.encode("utf-8"))
        assert result["external_effects"] == []


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
