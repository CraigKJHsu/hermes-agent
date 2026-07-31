import asyncio
import json
import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


class CallbackAdapter:
    def __init__(self, *, record_outcome=True, send_result=None):
        self.handled = []
        self.sent = []
        self._active_sessions = set()
        self.record_outcome = record_outcome
        self.send_result = send_result

    async def handle_message(self, event):
        self.handled.append(event)
        if (
            self.record_outcome
            and "validated_outcome=accepted" in event.text
        ):
            fields = {}
            for line in event.text.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    fields[key] = value
            with kb.connect_closing() as conn:
                callback = kb.get_grace_loop_callback(
                    conn, fields["grace_review_task_id"],
                )
                kb.record_grace_loop_callback_outcome(
                    conn,
                    review_task_id=fields["grace_review_task_id"],
                    event_id=int(fields["callback_event_id"]),
                    platform="telegram",
                    chat_id="chat-1",
                    thread_id="2",
                    session_id=callback["session_id"],
                    lease_owner=callback["lease_owner"],
                    outcome_kind="closed",
                    payload={"summary": "originating outcome satisfied"},
                )

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata or {}))
        return self.send_result


class DeferredCallbackAdapter(CallbackAdapter):
    """Model BasePlatformAdapter's dispatch-now, process-later contract."""

    def __init__(self):
        super().__init__()
        self._session_tasks = {}

    async def handle_message(self, event):
        async def _process():
            try:
                await asyncio.sleep(0.01)
                await CallbackAdapter.handle_message(self, event)
            finally:
                completion = (
                    event.internal_context or {}
                ).get("processing_completion_future")
                if completion is not None and not completion.done():
                    completion.set_result(None)

        task = asyncio.create_task(_process())
        self._session_tasks["callback"] = task


class FailedDeferredCallbackAdapter(CallbackAdapter):
    def __init__(self):
        super().__init__()
        self._session_tasks = {}

    async def handle_message(self, event):
        async def _fail():
            completion = (
                event.internal_context or {}
            ).get("processing_completion_future")
            if completion is not None and not completion.done():
                completion.set_result(False)

        task = asyncio.create_task(_fail())
        self._session_tasks["callback"] = task


class DroppedCallbackAdapter(CallbackAdapter):
    def __init__(self):
        super().__init__()
        self._session_tasks = {}

    async def handle_message(self, event):
        return None


class PendingApprovalWithoutOutcomeAdapter(CallbackAdapter):
    def __init__(self, *, expire_challenge=False, raise_after_create=False):
        super().__init__()
        self.expire_challenge = expire_challenge
        self.raise_after_create = raise_after_create

    async def handle_message(self, event):
        self.handled.append(event)
        fields = {}
        for line in event.text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        with kb.connect_closing() as conn:
            callback = kb.get_grace_loop_callback(
                conn, fields["grace_review_task_id"],
            )
            challenge = kb.create_grace_approval_challenge(
                conn,
                contract_fingerprint="e" * 64,
                request_instance_id="execution-blocker-approval",
                platform="telegram",
                chat_id="chat-1",
                thread_id="2",
                session_key="agent:main:telegram:group:chat-1:2",
                session_id=callback["session_id"],
                user_id_sha256="owner-hash",
                requested_message_id="42",
                action_summary="navigate Facebook Marketplace read-only",
                approval_platform="Facebook Marketplace",
                approval_scope='{"allowed":["navigate","click"],"forbidden":["write"]}',
                origin_review_task_id=fields["grace_review_task_id"],
                origin_event_id=int(fields["callback_event_id"]),
                callback_lease_owner=callback["lease_owner"],
            )
            if self.expire_challenge:
                with kb.write_txn(conn):
                    conn.execute(
                        """
                        UPDATE grace_approval_challenges
                           SET expires_at = 0
                         WHERE token = ?
                        """,
                        (challenge["token"],),
                    )
        if self.raise_after_create:
            raise RuntimeError("synthetic failure after durable challenge")


def _runner(
    adapter,
    *,
    session_key,
    session_id,
    chat_type="group",
    thread_id="2",
    compression_tip=None,
):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type=chat_type,
        thread_id=thread_id,
        user_id="kj",
    )
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                session_id=session_id,
                origin=source,
            )
        }
    )
    if compression_tip is not None:
        class _SessionDb:
            async def get_compression_tip(self, _session_id):
                return compression_tip

        runner._session_db = _SessionDb()
    else:
        runner._session_db = None
    return runner


def _bind_delegation(conn, execution_id, review_id, *, suffix):
    now = int(time.time())
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
            f"gd-{suffix}",
            (suffix.encode().hex() + "0" * 64)[:64],
            f"request-{suffix}",
            "agent:main:telegram:group:chat-1:2",
            "grace-session-1",
            execution_id,
            review_id,
            now,
            now,
        ),
    )


def test_orphan_callback_cannot_be_listed_or_claimed(tmp_path, monkeypatch):
    db_path = tmp_path / "orphan-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(conn, title="execution")
        assert kb.complete_task(conn, execution_id, summary="done")
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="f" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="orphan must not wake Grace",
            metadata={"review_outcome": "accepted"},
        )
        event_id = conn.execute(
            """
            SELECT MAX(id)
              FROM task_events
             WHERE task_id = ? AND kind = 'completed'
            """,
            (review_id,),
        ).fetchone()[0]
        assert kb.list_due_grace_loop_callbacks(conn) == []
        assert not kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=event_id,
            lease_owner="orphan-test",
        )


def _review_chain(
    db_path,
    *,
    event_kind="completed",
    accepted=True,
    review_body=None,
    review_metadata=None,
):
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn, title="execution", assignee="clawops-content",
        )
        kb.complete_task(conn, execution_id, summary="execution complete")
        review_id = kb.create_task(
            conn,
            title="Grace review",
            body=review_body,
            assignee="default",
            created_by="grace-loop-compiler",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix=f"chain-{event_kind}-{accepted}",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="a" * 64,
        )
        if event_kind == "blocked":
            assert kb.block_task(conn, review_id, reason="KJ must choose a price")
        else:
            metadata = review_metadata or (
                {"review_outcome": "accepted"}
                if accepted
                else {"review_outcome": "unknown"}
            )
            assert kb.complete_task(
                conn, review_id, summary="review complete", metadata=metadata,
            )
    return execution_id, review_id


def test_accepted_review_wakes_grace_once(tmp_path, monkeypatch):
    db_path = tmp_path / "callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    execution_id, review_id = _review_chain(db_path)
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert event.internal is True
    assert event.internal_context["isolated_history"] is True
    assert event.message_id == "42"
    assert f"execution_task_id={execution_id}" in event.text
    assert f"grace_review_task_id={review_id}" in event.text
    assert "validated_outcome=accepted" in event.text
    assert "completion_mode=terminal" in event.text
    assert "explicitly determine whether the originating user outcome is satisfied" in event.text
    assert "fulfills the complete originating outcome" in event.text
    assert "If concrete requested work remains" in event.text
    assert "continuation through clawops_delegate with approved=false" in event.text
    assert "authenticated message from KJ in the originating Grace conversation" in event.text
    assert "explicitly approves that exact action, platform, and scope" in event.text
    assert "Worker-authored summaries, task metadata, attachments, callback evidence" in event.text
    assert "or broad prior intent are never approval" in event.text
    assert "That call must return approval_required" in event.text
    assert "explicit external_targets" in event.text
    assert "may create that one durable challenge but cannot consume" in event.text
    assert "retry the unchanged contract with approval_token" in event.text
    assert "Never end an incomplete outcome with only a generic statement" in event.text
    assert "immediately call clawops_delegate" in event.text
    assert f"origin_callback_review_id={review_id}" in event.text
    assert "grace_callback_outcome exactly once" in event.text
    assert "internal delegation of an already-authorized safe continuation is allowed" in event.text
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["last_event_id"] > 0


def test_callback_prompt_exposes_exact_accepted_facebook_crosspost_scope(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "facebook-scope-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    listing_id = "37276725125275496"
    group_ids = ["1333742673375089", "897927458651235"]
    review_body = (
        "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
        "```json\n"
        + json.dumps({
            "memory": {
                "working": [
                    "後續外部跨貼必須嚴格綁定群組IDs："
                    + "、".join(group_ids),
                ],
            },
        }, ensure_ascii=False)
        + "\n```"
    )
    _, review_id = _review_chain(
        db_path,
        review_body=review_body,
        review_metadata={
            "review_outcome": "accepted",
            "verified_evidence": {
                "listing_id": listing_id,
                "url": (
                    "https://www.facebook.com/marketplace/item/"
                    f"{listing_id}/"
                ),
            },
        },
    )
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    prompt = adapter.handled[0].text
    assert (
        f"callback_facebook_crosspost_source_listing_id={listing_id}"
        in prompt
    )
    assert (
        'callback_facebook_crosspost_destination_group_ids='
        '["1333742673375089", "897927458651235"]'
        in prompt
    )
    assert "never add, remove, or substitute a destination" in prompt


def test_accepted_review_waits_for_background_turn_before_checking_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "deferred-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = DeferredCallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert len(adapter.handled) == 1
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "closed"
    assert callback["last_event_id"] > 0


def test_failed_background_turn_does_not_consume_blocker_callback(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "failed-background-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path, event_kind="blocked")
    adapter = FailedDeferredCallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert callback["attempts"] == 1
    assert "turn failed" in callback["last_error"]


def test_dropped_background_turn_times_out_and_releases_callback(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "dropped-background-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_GRACE_CALLBACK_TURN_TIMEOUT_SECONDS", "0.05")
    kb.init_db()
    _, review_id = _review_chain(db_path, event_kind="blocked")
    runner = _runner(
        DroppedCallbackAdapter(),
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert callback["attempts"] == 1
    assert "TimeoutError" in callback["last_error"]


def test_callback_delivery_is_supervised_without_blocking_notifier_polling():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._background_tasks = set()
    started = []

    async def _exercise():
        gate = asyncio.Event()

        async def _deliver(_kb, *, notifier_profile=None):
            started.append(notifier_profile)
            await gate.wait()

        runner._deliver_due_grace_loop_callbacks = _deliver
        runner._schedule_due_grace_loop_callbacks(
            object(),
            notifier_profile="default",
        )
        await asyncio.sleep(0)
        assert started == ["default"]
        assert len(runner._background_tasks) == 1

        runner._schedule_due_grace_loop_callbacks(
            object(),
            notifier_profile="default",
        )
        await asyncio.sleep(0)
        assert started == ["default"]

        gate.set()
        await asyncio.gather(*tuple(runner._background_tasks))
        await asyncio.sleep(0)
        assert runner._background_tasks == set()

    asyncio.run(_exercise())


def test_accepted_review_without_structured_outcome_escalates_without_cursor(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "missing-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = CallbackAdapter(record_outcome=False)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    for _ in range(3):
        asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert len(adapter.handled) == 3
    assert callback["state"] == "attention"
    assert callback["last_event_id"] == 0
    assert callback["delivered_at"] is None
    assert "structured outcome" in callback["last_error"]
    assert any("callback 已重試 3 次" in sent[1] for sent in adapter.sent)


def test_failed_fallback_notice_does_not_consume_callback(tmp_path, monkeypatch):
    db_path = tmp_path / "failed-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = CallbackAdapter(
        record_outcome=False,
        send_result=SimpleNamespace(success=False, error="telegram unavailable"),
    )
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    for _ in range(3):
        asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert callback["delivered_at"] is None
    assert "structured outcome" in callback["last_error"]


def test_blocked_review_wakes_grace_for_user_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "blocked-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path, event_kind="blocked")
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert "review_event=blocked" in adapter.handled[0].text
    assert "validated_outcome=blocked" in adapter.handled[0].text
    assert "ask only for the specific missing decision" in adapter.handled[0].text


def test_dependency_review_correction_does_not_ask_kj_for_input(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "dependency-correction.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            assignee="clawops-content",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        kb.complete_task(conn, execution_id, summary="needs correction")
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="dependency-correction",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="a" * 64,
        )
        assert kb.block_task(
            conn,
            review_id,
            reason="Correct the image proportions",
            kind="dependency",
        )
        due = kb.list_due_grace_loop_callbacks(conn)

    assert due == []


def test_blocked_execution_wakes_grace_once_without_claiming_review(tmp_path, monkeypatch):
    db_path = tmp_path / "execution-blocked-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn, title="execution", assignee="clawops-content",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="execution-blocked",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="a" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="KJ must choose the final product name",
            kind="needs_input",
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert "callback_stage=execution" in event.text
    assert "validated_outcome=needs_input" in event.text
    assert "KJ must choose the final product name" in event.text
    assert "do not claim that Grace reviewed or accepted" in event.text
    trigger_line = next(
        line for line in event.text.splitlines() if line.startswith("trigger_event=")
    )
    trigger_event = json.loads(trigger_line.split("=", 1)[1])
    assert trigger_event["kind"] == "blocked"
    payload_preview = json.loads(trigger_event["payload_preview"])
    assert payload_preview["reason"] == "KJ must choose the final product name"
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["last_event_id"] > 0

    with kb.connect_closing(db_path) as conn:
        assert kb.unblock_task(conn, execution_id)
        assert kb.complete_task(conn, execution_id, summary="execution complete")
        assert kb.complete_task(
            conn,
            review_id,
            summary="review accepted",
            metadata={"review_outcome": "accepted"},
        )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 2
    review_event = adapter.handled[1]
    assert "callback_stage=grace_review" in review_event.text
    assert "validated_outcome=accepted" in review_event.text


def test_execution_approval_challenge_requires_structured_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "execution-approval-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook read-only verification",
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
        _bind_delegation(
            conn,
            execution_id,
            review_id,
            suffix="execution-approval-outcome",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="e" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="Facebook navigation needs read-only approval",
            kind="needs_input",
        )

    adapter = PendingApprovalWithoutOutcomeAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
        challenge_count = conn.execute(
            """
            SELECT COUNT(*)
              FROM grace_approval_challenges
             WHERE origin_review_task_id = ?
               AND origin_event_id = ?
               AND state = 'pending'
            """,
            (review_id, adapter.handled[0].text.split(
                "callback_event_id=", 1,
            )[1].splitlines()[0]),
        ).fetchone()[0]
    assert len(adapter.handled) == 1
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert "durable checkpoint" in callback["last_error"]
    assert challenge_count == 1


def test_expired_execution_approval_challenge_still_requires_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "expired-execution-approval-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook read-only verification",
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
        _bind_delegation(
            conn,
            execution_id,
            review_id,
            suffix="expired-execution-approval-outcome",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="e" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="Facebook navigation needs read-only approval",
            kind="needs_input",
        )

    adapter = PendingApprovalWithoutOutcomeAdapter(expire_challenge=True)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert "durable checkpoint" in callback["last_error"]


def test_exception_after_execution_challenge_preserves_checkpoint_requirement(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "failed-execution-approval-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook read-only verification",
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
        _bind_delegation(
            conn,
            execution_id,
            review_id,
            suffix="failed-execution-approval-outcome",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="e" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="Facebook navigation needs read-only approval",
            kind="needs_input",
        )

    adapter = PendingApprovalWithoutOutcomeAdapter(raise_after_create=True)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    for _ in range(3):
        asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "attention"
    assert callback["last_event_id"] == 0
    assert callback["delivered_at"] is None
    assert "synthetic failure after durable challenge" in callback["last_error"]


def test_callback_bounds_worker_authored_trigger_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "bounded-trigger.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(conn, title="execution")
        review_id = kb.create_task(
            conn,
            title="review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="bounded-trigger",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="a" * 64,
        )
        assert kb.block_task(
            conn,
            execution_id,
            reason="x" * 50000,
            kind="needs_input",
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    event = adapter.handled[0]
    trigger_line = next(
        line for line in event.text.splitlines() if line.startswith("trigger_event=")
    )
    trigger_event = json.loads(trigger_line.split("=", 1)[1])
    assert len(trigger_event["payload_preview"]) < 4100
    assert trigger_event["payload_preview"].endswith("...[truncated]")
    assert len(event.text) < 30000


def test_invalid_review_metadata_never_claims_acceptance(tmp_path, monkeypatch):
    db_path = tmp_path / "invalid-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, _ = _review_chain(db_path, accepted=False)
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert "validated_outcome=invalid_completion_metadata" in adapter.handled[0].text
    assert "validated_outcome=accepted" not in adapter.handled[0].text


def test_session_reset_sends_safe_handoff_without_injecting_old_turn(tmp_path, monkeypatch):
    db_path = tmp_path / "reset-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="new-session-after-reset",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert adapter.handled == []
    assert len(adapter.sent) == 1
    assert review_id in adapter.sent[0][1]
    assert "原對話已被重設" in adapter.sent[0][1]
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "attention"
    assert callback["last_event_id"] == 0


def test_compression_child_rebinds_and_delivers_callback(tmp_path, monkeypatch):
    db_path = tmp_path / "compression-child-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-after-compression",
        compression_tip="grace-session-after-compression",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert adapter.sent == []
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["session_id"] == "grace-session-after-compression"
    assert callback["last_event_id"] > 0


def test_session_reset_retries_when_handoff_notice_reports_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "reset-send-failed.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    adapter = CallbackAdapter(
        send_result=SimpleNamespace(success=False, error="telegram unavailable"),
    )
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="new-session-after-reset",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert "was not delivered" in callback["last_error"]


def test_busy_origin_session_defers_callback_until_next_tick(tmp_path, monkeypatch):
    db_path = tmp_path / "busy-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    session_key = "agent:main:telegram:group:chat-1:2"
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE grace_loop_callbacks SET session_key = NULL "
            "WHERE review_task_id = ?",
            (review_id,),
        )
        conn.commit()
    adapter = CallbackAdapter()
    adapter._active_sessions.add(session_key)
    runner = _runner(
        adapter,
        session_key=session_key,
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert adapter.handled == []
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "pending"
    assert callback["last_event_id"] == 0
    assert callback["attempts"] == 0
    assert "origin session busy" in callback["last_error"]

    adapter._active_sessions.clear()
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["last_event_id"] > 0


def test_legacy_non_threaded_group_callback_recovers_group_session(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "legacy-group-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE grace_loop_callbacks "
            "SET session_key = NULL, chat_type = NULL, thread_id = '' "
            "WHERE review_task_id = ?",
            (review_id,),
        )
        conn.commit()

    session_key = "agent:main:telegram:group:chat-1:kj"
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key=session_key,
        session_id="grace-session-1",
        chat_type="group",
        thread_id=None,
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "group"


def test_legacy_callback_detects_session_reset_after_key_recovery(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "legacy-reset-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE grace_loop_callbacks SET session_key = NULL, chat_type = NULL "
            "WHERE review_task_id = ?",
            (review_id,),
        )
        conn.commit()

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="new-session-after-reset",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert adapter.handled == []
    assert len(adapter.sent) == 1
    assert review_id in adapter.sent[0][1]
    assert "原對話已被重設" in adapter.sent[0][1]
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "attention"
    assert callback["last_event_id"] == 0


def test_superseded_execution_blocker_coalesces_to_accepted_review(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "coalesced-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
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
        _bind_delegation(
            conn, execution_id, review_id, suffix="superseded-blocker",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            message_id="42",
            contract_fingerprint="a" * 64,
        )
        assert kb.block_task(
            conn, execution_id, reason="temporary decision", kind="needs_input",
        )
        assert kb.unblock_task(conn, execution_id)
        assert kb.complete_task(conn, execution_id, summary="execution complete")
        assert kb.complete_task(
            conn,
            review_id,
            summary="review accepted",
            metadata={"review_outcome": "accepted"},
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert "callback_stage=grace_review" in adapter.handled[0].text
    assert "validated_outcome=accepted" in adapter.handled[0].text
    assert "validated_outcome=needs_input" not in adapter.handled[0].text


def test_resolved_execution_blocker_is_not_delivered_while_review_pending(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "resolved-blocker-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
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
        _bind_delegation(
            conn, execution_id, review_id, suffix="resolved-blocker",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="2",
            user_id="kj",
            session_key="agent:main:telegram:group:chat-1:2",
            session_id="grace-session-1",
            contract_fingerprint="a" * 64,
        )
        assert kb.block_task(
            conn, execution_id, reason="temporary decision", kind="needs_input",
        )
        assert kb.unblock_task(conn, execution_id)
        assert kb.complete_task(conn, execution_id, summary="execution complete")
        assert kb.list_due_grace_loop_callbacks(conn) == []

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))
    assert adapter.handled == []


def test_expired_callback_lease_is_recoverable_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "lease-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    now = int(time.time())
    with kb.connect_closing(db_path) as conn:
        due = kb.list_due_grace_loop_callbacks(conn, now=now)
        assert len(due) == 1
        event_id = due[0]["event_id"]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=event_id,
            lease_owner="dead-gateway",
            lease_seconds=30,
        )
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["attempts"] == 0
        assert kb.list_due_grace_loop_callbacks(conn, now=now + 10) == []
        recovered = kb.list_due_grace_loop_callbacks(conn, now=now + 31)
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert len(recovered) == 1
    assert recovered[0]["event_id"] == event_id
    assert callback["attempts"] == 0
