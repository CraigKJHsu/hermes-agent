import hashlib
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from tests.plugins.test_clawops_delegate import (
    _configure_secondhand_context, _nested_args, _isolated_openclaw_loop_backend,
)

@pytest.mark.parametrize("topic", ["2", "4641"])
@pytest.mark.parametrize("mode", ["telegram", "codex"])
@pytest.mark.parametrize("fault", [None, "pending", "revoked", "owner", "target", "message", "image", "source", "auth_digest", "thread", "requested_by", "request_id", "scope", "internal", "fingerprint"])
def test_page_consumer_checks_consumed_exact_approval(tmp_path, monkeypatch, topic, mode, fault):
    values = {
        "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "chat-1",
        "HERMES_SESSION_THREAD_ID": topic, "HERMES_SESSION_USER_ID": "kj",
        "HERMES_SESSION_OWNER_USER_ID": "kj", "HERMES_SESSION_KEY": f"agent:main:telegram:group:chat-1:{topic}",
        "HERMES_SESSION_ID": "grace-session-1", "HERMES_SESSION_MESSAGE_ID": "prepare-page",
        "HERMES_SESSION_MESSAGE_TEXT": "請準備 Facebook Page 發布核准", "HERMES_SESSION_INTERNAL": "false",
    }
    _configure_secondhand_context(tmp_path, monkeypatch, values)
    registry = tmp_path / "registry.yaml"
    registry.write_text(registry.read_text().replace("thread_id: '2'", f"thread_id: '{topic}'") + "    aliases: [testpage]\n")
    message = "  🇦🇺 Videoguys 正文\r\n\r\nCTA → https://example.com/\n#案例\n"
    pin = {
        "execution_task_id": "t_1234", "execution_run_id": 1,
        "review_task_id": "t_5678", "review_run_id": 2,
        "source_field": "acceptance_evidence.inline_content_package.facebook_page_post",
        "message": message, "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "message_utf8_bytes": len(message.encode()), "image_path": str(tmp_path / "hero.png"),
        "image_sha256": "b" * 64, "dimensions": "1664×936",
    }
    def bind(contract, *, board):
        return dict(pin)
    monkeypatch.setattr("tools.facebook_page_graph_tool.bind_accepted_page_preflight_source", bind)
    args = _nested_args()
    args["task_type"], args["risk_level"] = "facebook_page_api_publish", "medium"
    args["external_effect_budget"] = 1
    args["original_request"] = args["goal"]["objective"] = "將既定正文與主圖發布到 Facebook Page"
    args["goal"]["non_goals"] = ["不發布至 Group"]
    args["external_targets"] = ["https://www.facebook.com/12345"]
    args["scope"]["allowed"] = [
        "僅使用已驗證的精確正文，SHA-256=" + pin["message_sha256"],
        "僅使用 hero.png，SHA-256=" + pin["image_sha256"],
        "Page ID 12345",
        "Use accepted Facebook Page package: execution_task_id=t_1234; review_task_id=t_5678",
    ]
    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
    if mode == "codex":
        values.update({"HERMES_SESSION_PLATFORM": "codex", "HERMES_SESSION_SOURCE": "codex_local_operator",
                       "HERMES_SESSION_CHAT_ID": "", "HERMES_SESSION_THREAD_ID": "",
                       "HERMES_SESSION_KEY": "codex:thread:test", "HERMES_SESSION_ID": "codex-session",
                       "HERMES_CODEX_AUTHORIZATION_ID": "auth-1", "HERMES_CODEX_THREAD_ID": "thread-1"})
        args.update(context_alias="testpage", approved=True)
    else:
        challenge = json.loads(handle_clawops_delegate(args))
        assert challenge["status"] == "approval_required", challenge
        values["HERMES_SESSION_MESSAGE_ID"] = "approve-page"
        values["HERMES_SESSION_MESSAGE_TEXT"] = f"核准 {challenge['approval_token']}"
        args["approval_token"] = challenge["approval_token"]
    result = json.loads(handle_clawops_delegate(args))
    assert result["status"] == "queued", result
    with kb.connect_closing() as conn:
        task_id = result["execution_task_id"]
        task = kb.get_task(conn, task_id)
        contract = kb._grace_compiled_contract(task.body)
        if fault in {"pending", "revoked"}:
            conn.execute("UPDATE grace_approval_challenges SET state=?", (fault,))
        elif fault == "owner":
            conn.execute("UPDATE grace_approval_challenges SET user_id_sha256='wrong'")
        elif fault == "request_id":
            conn.execute("UPDATE grace_approval_challenges SET requested_message_id='wrong'")
        elif fault == "requested_by":
            rows = conn.execute("SELECT token,delegation_args FROM grace_approval_challenges").fetchall()
            for row in rows:
                sealed = json.loads(row["delegation_args"])
                sealed["_approval_compiled_contract"]["identity"]["requested_by"] = "wrong"
                conn.execute("UPDATE grace_approval_challenges SET delegation_args=? WHERE token=?", (json.dumps(sealed), row["token"]))
        else:
            provenance = contract["approval_provenance"]
            if fault == "target": contract["external_targets"] = ["https://www.facebook.com/99999"]
            elif fault in {"message", "image"}: contract["facebook_page_post"][fault + "_sha256"] = "c" * 64
            elif fault == "source": provenance["source"] = "unknown"
            elif fault == "auth_digest": provenance["codex_authorization_id_sha256" if mode == "codex" else "challenge_token_sha256"] = "c" * 64
            elif fault == "thread":
                if mode == "codex": provenance["codex_thread_id"] = ""
                else: conn.execute("UPDATE grace_approval_challenges SET thread_id='wrong'")
            elif fault == "scope": provenance["scope_binding"] = "wrong"
            elif fault == "internal": provenance["internal"] = True
            elif fault == "fingerprint": provenance["contract_fingerprint"] = "c" * 64
            original = kb._grace_compiled_contract(task.body)
            # Replace only the JSON contract, preserving its execution-stage header.
            marker = task.body.index("```json\n") + len("```json\n")
            stop = task.body.index("\n```", marker)
            conn.execute("UPDATE tasks SET body=? WHERE id=?", (task.body[:marker] + json.dumps(contract) + task.body[stop:], task_id))
        approved = kb.grace_task_facebook_page_post_contract(conn, task_id)
        if fault is None or (fault == "requested_by" and mode == "telegram"):
            assert approved and approved["message_sha256"] == pin["message_sha256"]
        else:
            assert approved is None
