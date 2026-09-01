import asyncio
import hashlib
from pathlib import Path
import time


from gateway.config import Platform
from gateway.kanban_watchers import _loop_task_context
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.documents = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    def extract_local_files(self, text):
        return [], text

    async def send_document(self, chat_id, file_path, metadata=None):
        self.documents.append({
            "chat_id": chat_id,
            "file_path": file_path,
            "metadata": metadata or {},
        })


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


def _insert_loop_delegation(conn, execution_id, review_id):
    now = int(time.time())
    fingerprint = hashlib.sha256(
        f"{execution_id}:{review_id}".encode("utf-8"),
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', 'chat-1', '2', ?, ?, '{}', 0,
                  'queued', ?, ?, ?, ?)
        """,
        (
            f"gd_{execution_id}",
            fingerprint,
            f"gri_{execution_id}",
            f"loop:{execution_id}",
            f"loop-session:{execution_id}",
            execution_id,
            review_id,
            now,
            now,
        ),
    )
    conn.commit()


def _inline_commerce_report():
    return {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": True,
        "as_of": "2026-08-02 16:00 Asia/Taipei",
        "observed_at": 1_785_657_600,
        "rows": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "destination_id": "902016640291333",
            "destination_name": "【大台北地區】二手家具、二手家電買賣",
            "status": "public",
            "status_label": "公開可見",
            "observed_at": 1_785_657_600,
            "verified_at": "2026-08-02 07:43 Asia/Taipei",
            "evidence": "社團搜尋顯示商品卡。",
        }],
        "coverage": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "complete": True,
            "named_count": 1,
            "gap_count": 0,
            "expected_total": 1,
            "expected_total_label": "1",
            "note": "完整",
        }],
    }


def _disable_model_receipt_gate(monkeypatch):
    from proactive import model_routing

    monkeypatch.setattr(
        model_routing,
        "execution_receipt_from_env",
        lambda _raw: {"test": True},
    )
    monkeypatch.setattr(
        model_routing,
        "validate_grace_acceptance_receipt",
        lambda *_args, **_kwargs: None,
    )


def test_loop_task_context_requires_durable_binding_and_exact_header(tmp_path):
    db_path = tmp_path / "loop-context.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        ordinary_id = kb.create_task(
            conn,
            title="ordinary",
            body="ordinary evidence\nGRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="worker",
        )
        assert _loop_task_context(conn, kb.get_task(conn, ordinary_id)) == {}

        execution_id = kb.create_task(
            conn,
            title="execution",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-content",
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
                "evidence mentions GRACE_LOOP_CONTRACT_STAGE: execution"
            ),
            assignee="default",
            parents=(execution_id,),
        )
        _insert_loop_delegation(conn, execution_id, review_id)
        assert (
            _loop_task_context(conn, kb.get_task(conn, review_id))["stage"]
            == "grace_review"
        )


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_reports_claimed_and_spawned_progress(tmp_path, monkeypatch):
    db_path = tmp_path / "progress.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="progress test", assignee="clawops-test")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="claimed")
        kb._append_event(conn, tid, kind="spawned", payload={"pid": 4321})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 2
    assert "已啟動" in adapter.sent[0]["text"]
    assert "執行中" in adapter.sent[1]["text"]
    assert "pid=4321" in adapter.sent[1]["text"]


def test_kanban_notifier_labels_timeout_give_up_as_timeout(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "gave-up-timeout.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="timeout", assignee="clawops-ops")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
        )
        kb._append_event(
            conn,
            tid,
            kind="gave_up",
            payload={
                "trigger_outcome": "timed_out",
                "error": "elapsed 1800s > limit 1800s",
            },
        )

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "after repeated timeouts" in adapter.sent[0]["text"]
    assert "spawn failures" not in adapter.sent[0]["text"]


def test_kanban_notifier_reports_cancellation_once_and_unsubscribes(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "cancel-notify.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="cancelled work",
            assignee="clawops-review",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="topic-2",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                (tid,),
            )
            kb._append_event(
                conn,
                tid,
                "cancelled",
                {"reason": "KJ 已要求停止執行。"},
            )

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["thread_id"] == "topic-2"
    assert "已依 KJ 指示停止" in adapter.sent[0]["text"]
    assert "不會自動重試" in adapter.sent[0]["text"]
    with kb.connect_closing() as conn:
        assert kb.list_notify_subs(conn, tid) == []


def test_kanban_notifier_delivers_loop_breaker_triage_to_original_chat(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "triage-notify.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="brand decision", assignee="clawops-browser")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="topic-2",
        )
        kb._append_event(
            conn,
            tid,
            kind="block_loop_detected",
            payload={
                "reason": "Shopee 品牌查無結果；請決定是否申請新增品牌。",
                "kind": "needs_input",
                "recurrences": 2,
                "limit": 2,
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-1"
    assert adapter.sent[0]["metadata"]["thread_id"] == "topic-2"
    assert "需要你確認" in adapter.sent[0]["text"]
    assert "申請新增品牌" in adapter.sent[0]["text"]


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_completed_message_preserves_long_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "long-summary.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    summary = (
        "已完成 Facebook 二手刊登實際狀態只讀核對："
        + "前置說明" * 35
        + "Listed on Marketplace and at least 1 group（5 clicks on listing）。"
    )
    _create_completed_subscription(summary=summary)

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "Listed on Marketplace and at least 1 group" in text


def test_accepted_grace_review_delivers_parent_execution_artifact(
    tmp_path, monkeypatch,
):
    _disable_model_receipt_gate(monkeypatch)
    db_path = tmp_path / "accepted-artifact.db"
    artifact_path = tmp_path / "verified-report.txt"
    artifact_path.write_text("verified", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        execution_id = kb.create_task(
            conn,
            title="execution",
            assignee="clawops-content",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _insert_loop_delegation(conn, execution_id, review_id)
        kb.add_notify_sub(
            conn,
            task_id=review_id,
            platform="telegram",
            chat_id="chat-1",
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="execution complete",
            metadata={
                "artifacts": [str(artifact_path)],
                # A worker-authored kind alone must not suppress the durable
                # artifact when no content-package delivery contract exists.
                "user_facing_report": {
                    "kind": "content_package",
                    "delivery": "inline_with_attachment",
                    "complete": True,
                    "title": "Untrusted package claim",
                    "body": "Not contract-backed",
                    "observed_at": int(time.time()),
                    "assets": [{
                        "filename": "untrusted.png",
                        "label": "untrusted",
                        "path": "/tmp/untrusted.png",
                        "sha256": "0" * 64,
                    }],
                },
            },
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review accepted",
            metadata={"review_outcome": "accepted"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "已完成驗收" in adapter.sent[0]["text"]
    assert "正在整理證據與下一步核准項目" not in adapter.sent[0]["text"]
    assert "若確實需要後續核准" in adapter.sent[0]["text"]
    assert len(adapter.documents) == 1
    delivered_path = Path(adapter.documents[0]["file_path"])
    assert delivered_path.name == artifact_path.name
    assert delivered_path.read_text(encoding="utf-8") == "verified"


def test_inline_user_facing_report_keeps_markdown_artifact_audit_only(
    tmp_path, monkeypatch,
):
    _disable_model_receipt_gate(monkeypatch)
    db_path = tmp_path / "inline-only-artifact.db"
    artifact_path = tmp_path / "audit-only.md"
    artifact_path.write_text("# audit", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        execution_id = kb.create_task(
            conn,
            title="execution",
            assignee="clawops-browser",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _insert_loop_delegation(conn, execution_id, review_id)
        kb.add_notify_sub(
            conn,
            task_id=review_id,
            platform="telegram",
            chat_id="chat-1",
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="inline table ready",
            metadata={
                "artifacts": [str(artifact_path)],
                "user_facing_report": _inline_commerce_report(),
            },
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review accepted",
            metadata={"review_outcome": "accepted"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "已完成驗收" in adapter.sent[0]["text"]
    assert adapter.documents == []


def test_contract_backed_content_package_never_falls_back_to_partial_artifact(
    tmp_path, monkeypatch,
):
    _disable_model_receipt_gate(monkeypatch)
    db_path = tmp_path / "incomplete-content-package.db"
    partial = tmp_path / "partial.txt"
    partial.write_text("partial", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                '{"user_facing_delivery":{"required":true,'
                '"kind":"content_package",'
                '"delivery":"inline_with_attachment",'
                '"asset_filenames":["missing.png"]}}',
                "```",
            ]),
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _insert_loop_delegation(conn, execution_id, review_id)
        kb.add_notify_sub(
            conn, task_id=review_id, platform="telegram", chat_id="chat-1",
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="partial package",
            metadata={"artifacts": [str(partial)]},
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.documents == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()
