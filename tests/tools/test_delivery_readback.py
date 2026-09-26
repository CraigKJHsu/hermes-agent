import hashlib
import json
import sqlite3
import time

import pytest
from PIL import Image

from hermes_cli import kanban_db as kb
from hermes_cli.user_facing_report import render_user_facing_report_chunks, user_facing_report_digest
from tools import kanban_tools as tool


def seal(conn, execution, review, contract):
    raw = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    identity = contract["identity"]
    conn.execute(
        "INSERT INTO grace_delegations (delegation_id,request_instance_id,contract_fingerprint,platform,chat_id,thread_id,"
        "session_key,session_id,resolved_route,state,execution_task_id,review_task_id,"
        "contract_snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,'session-key','session','{}',"
        "'queued',?,?,?,1,1)",
        ("gd-" + execution, "request-" + execution, fingerprint, identity["platform"], identity["chat_id"],
         identity["thread_id"], execution, review, raw),
    )
    for task_id, stage in ((execution, "execution"), (review, "review")):
        body = f"GRACE_LOOP_CONTRACT_STAGE: {stage}\n```json\n{raw}\n```"
        conn.execute("UPDATE tasks SET body=?,project_namespace=? WHERE id=?", (body, identity["project"], task_id))
    conn.commit()
    return fingerprint


@pytest.fixture(params=["2", "4641"])
def package(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    identity = {"platform": "telegram", "chat_id": "chat-1", "thread_id": request.param,
                "project": "project-" + request.param, "board": "default"}
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="source", assignee="clawops-ops")
        assert kb.claim_task(conn, source)
        paths = []
        for name in ("hero.png", "audio.png"):
            path = tmp_path / name
            Image.new("RGB", (16, 9), "blue").save(path)
            kb.add_attachment(conn, source, filename=name, stored_path=str(path),
                              content_type="image/png", size=path.stat().st_size)
            paths.append(path)
        report = {"kind": "content_package", "delivery": "inline_with_attachment",
                  "complete": True, "title": "Approved package", "body": "完整正文\n" * 900,
                  "observed_at": int(time.time()), "assets": [
                      {"filename": p.name, "label": p.name, "path": str(p),
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}
        assert kb.complete_task(conn, source, metadata={"user_facing_report": report})
        review = kb.create_task(conn, title="review", parents=[source])
        assert kb.claim_task(conn, review)
        assert kb.complete_task(conn, review, metadata={"accepted": True, "review_outcome": "accepted"})
        source_contract = {"identity": identity, "user_facing_delivery": {
            "required": True, "kind": "content_package", "delivery": "inline_with_attachment",
            "body_field": "body", "asset_filenames": [p.name for p in paths],
        }}
        fingerprint = seal(conn, source, review, source_contract)
        report = kb.grace_inline_content_package_report(conn, source)
        assert report
        chunks = render_user_facing_report_chunks(report)
        assert len(chunks) == 2
        digest = user_facing_report_digest(report)
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='completed'", (review,)).fetchone()[0]
        conn.execute(
            "INSERT INTO grace_loop_callbacks (review_task_id,execution_task_id,platform,chat_id,thread_id,"
            "contract_fingerprint,created_at,state,last_event_id,user_report_event_id,user_report_digest,"
            "user_report_next_chunk,user_report_total_chunks,outcome_kind,outcome_event_id) "
            "VALUES (?,?,'telegram','chat-1',?,?,1,'delivered',?,?,?,4,4,'closed',?)",
            (review, source, request.param, fingerprint, event, event, digest, event),
        )
        conn.execute("UPDATE grace_loop_callbacks SET user_report_delivered_at=1 WHERE review_task_id=?", (review,))
        for index in range(4):
            conn.execute(
                "INSERT INTO grace_user_report_chunk_deliveries (review_task_id,event_id,report_digest,"
                "chunk_index,total_chunks,state,message_id,created_at,updated_at) VALUES (?,?,?,?,4,'sent',?,1,1)",
                (review, event, digest, index, str(100 + index)),
            )
        conn.commit()
        current = kb.create_task(conn, title="verify", assignee="clawops-ops")
        claimed = kb.claim_task(conn, current)
        assert claimed
        current_review = kb.create_task(conn, title="verification review", assignee="default", parents=[current])
        caller_contract = {"identity": identity, "scope": {"allowed": [f"Verify {source} / {review} delivery"]}}
        seal(conn, current, current_review, caller_contract)
        monkeypatch.setenv("HERMES_KANBAN_TASK", current)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)
        monkeypatch.setenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", claimed.worker_auth_token)
    return {"args": {"view": "delivery", "source_review_task_id": review,
                      "message_ids": ["100", "101", "102", "103"]},
            "source": source, "review": review, "current": current,
            "paths": paths, "report": report, "digest": digest, "thread": request.param}


def test_readback(package, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No writable/default connection or provider call allowed")
    monkeypatch.setattr(kb, "connect", forbidden)
    import requests
    monkeypatch.setattr(requests, "get", forbidden)
    result = json.loads(tool._handle_show(package["args"]))
    assert result["success"] and result["whole_report_delivery_complete"]
    assert result["requested_messages_confirmed_sent"]
    assert result["report_digest"] == package["digest"]
    assert result["body"] == package["report"]["body"]
    assert [row["kind"] for row in result["messages"]] == ["text", "text", "image", "image"]
    assert all(row["thread_id"] == package["thread"] for row in result["messages"])
    assert all(row["submitted_asset"]["sha256"] for row in result["messages"][2:])
    assert result["current_telegram_ui_verified"] is False
    assert result["human_read_verified"] is False
    assert result["verification_external_effect_count"] == 0


@pytest.mark.parametrize("fault", ["auth", "scope", "seal", "topic", "project", "callback_lane",
                                  "digest", "missing", "mapping", "image", "image_valid", "owner", "event", "superseded", "not_closed"])
def test_reject_bad_evidence(package, monkeypatch, fault):
    with kb.connect_closing() as conn:
        if fault == "auth":
            monkeypatch.setenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", "invalid")
        elif fault in ("scope", "topic", "project", "seal"):
            tid = package["current"] if fault == "scope" else package["source"]
            row = conn.execute("SELECT contract_snapshot FROM grace_delegations WHERE execution_task_id=?", (tid,)).fetchone()
            contract = json.loads(row[0])
            if fault == "scope": contract["scope"]["allowed"] = []
            if fault == "topic": contract["identity"]["thread_id"] = "another-topic"
            if fault == "project": contract["identity"]["project"] = "another-project"
            raw = json.dumps(contract)
            fingerprint = "bad" if fault == "seal" else hashlib.sha256(raw.encode()).hexdigest()
            conn.execute("UPDATE grace_delegations SET contract_snapshot=?,contract_fingerprint=?,thread_id=? "
                         "WHERE execution_task_id=?", (raw, fingerprint, contract["identity"]["thread_id"], tid))
        elif fault == "callback_lane":
            conn.execute("UPDATE grace_loop_callbacks SET chat_id='other' WHERE review_task_id=?", (package["review"],))
        elif fault == "digest":
            conn.execute("UPDATE grace_loop_callbacks SET user_report_digest='wrong' WHERE review_task_id=?", (package["review"],))
        elif fault == "missing":
            conn.execute("DELETE FROM grace_user_report_chunk_deliveries WHERE review_task_id=? AND chunk_index=0", (package["review"],))
        elif fault == "mapping":
            conn.execute("UPDATE grace_user_report_chunk_deliveries SET total_chunks=7 WHERE review_task_id=?", (package["review"],))
        elif fault == "image":
            package["paths"][0].write_bytes(b"changed")
        elif fault == "image_valid":
            Image.new("RGB", (16, 9), "red").save(package["paths"][0])
        elif fault == "owner":
            conn.execute("UPDATE tasks SET project_namespace='different-owner' WHERE id=?", (package["source"],))
        elif fault == "superseded":
            conn.execute("UPDATE grace_loop_callbacks SET last_event_id=last_event_id+1 WHERE review_task_id=?", (package["review"],))
        elif fault == "not_closed":
            conn.execute("UPDATE grace_loop_callbacks SET outcome_kind='continued' WHERE review_task_id=?", (package["review"],))
        elif fault == "event":
            conn.execute("UPDATE grace_loop_callbacks SET user_report_event_id=-1 WHERE review_task_id=?", (package["review"],))
        conn.commit()
    result = json.loads(tool._handle_show(package["args"]))
    assert result.get("success") is not True
    assert "messages" not in result and "body" not in result


def test_pending_is_not_sent(package):
    with kb.connect_closing() as conn:
        conn.execute("UPDATE grace_user_report_chunk_deliveries SET state='pending' WHERE review_task_id=? AND chunk_index=1", (package["review"],))
        conn.commit()
    result = json.loads(tool._handle_show(package["args"]))
    assert result.get("success") is not True
    assert "messages" not in result and "body" not in result


@pytest.mark.parametrize("args", [{"board": "other"}, {"task_id": "t_00000000"},
                                  {"message_ids": ["100", "100"]}, {"message_ids": ["999"]}])
def test_scope_parameters(package, args):
    result = json.loads(tool._handle_show({**package["args"], **args}))
    assert result.get("success") is not True


def test_native_delivery_in_predelegation_snapshot(package):
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot
    with kb.connect_closing() as conn:
        contract = json.loads(conn.execute(
            "SELECT contract_snapshot FROM grace_delegations WHERE execution_task_id=?",
            (package["current"],),
        ).fetchone()[0])
        # Native completion has no OpenClaw loop_contract metadata.
        assert "loop_contract" not in kb.latest_run(conn, package["source"]).metadata
        snapshot = _objective_durable_evidence_snapshot(conn, contract)
        delivery = next(d for d in snapshot["accepted_source_deliveries"]
                        if d["source_review_task_id"] == package["review"])
        assert delivery["success"]
        assert delivery["body"] == package["report"]["body"]
        assert delivery["report_digest"] == package["digest"]
        assert [m["message_id"] for m in delivery["messages"]] == ["100", "101", "102", "103"]
        assert delivery["callback_closed_for_report"]
        contract["identity"]["thread_id"] = "other-topic"
        denied = _objective_durable_evidence_snapshot(conn, contract)["accepted_source_deliveries"][0]
        assert not denied["success"] and "body" not in denied and "messages" not in denied
