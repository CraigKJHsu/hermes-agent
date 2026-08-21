import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from proactive.loop_contract import canonical_marketplace_readonly_sections


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
    execution_metadata=None,
    execution_body=None,
    review_body=None,
    review_metadata=None,
):
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body=execution_body,
            assignee="clawops-content",
        )
        kb.complete_task(
            conn,
            execution_id,
            summary="execution complete",
            metadata=execution_metadata,
        )
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
        review_run = kb.claim_task(conn, review_id, claimer="reviewer")
        assert review_run is not None
        if event_kind == "blocked":
            assert kb.block_task(
                conn,
                review_id,
                reason="KJ must choose a price",
                expected_run_id=review_run.current_run_id,
            )
        else:
            metadata = review_metadata or (
                {"review_outcome": "accepted"}
                if accepted
                else {"review_outcome": "unknown"}
            )
            assert kb.complete_task(
                conn,
                review_id,
                summary="review complete",
                metadata=metadata,
                expected_run_id=review_run.current_run_id,
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


def test_timeout_callback_carries_compiled_contract_scope(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "callback-contract-scope.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    execution_body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "contract_version": "1.0",
            "goal": {
                "objective": "唯讀查核 Kolin Marketplace 刊登狀態",
                "deliverables": ["社團完整名稱與目前狀態"],
            },
            "scope": {
                "allowed": ["Facebook Marketplace 唯讀檢視"],
                "forbidden": ["勾選社團", "發布"],
            },
            "external_targets": [
                "Facebook Marketplace item 37217119148451132",
            ],
            "completion_mode": "intermediate",
        }, ensure_ascii=False)
        + "\n```"
    )
    execution_id, review_id = _review_chain(
        db_path,
        execution_body=execution_body,
    )
    with kb.connect_closing(db_path) as conn:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                execution_id,
                "browser_evidence_recorded",
                {
                    "operation": "snapshot",
                    "observed_at": int(time.time()),
                    "visible_text": "Kolin｜In stock｜至少刊登於 1 個社團",
                },
            )
            kb._append_event(
                conn,
                execution_id,
                "browser_blocker_recorded",
                {
                    "blocker_code": "facebook_readonly_guard_mismatch",
                    "component": "controlled_facebook_browser",
                    "operation": "open_more_options",
                    "listing_id": "37217119148451132",
                    "tool": "browser_click",
                    "tool_error_code": (
                        "popup_semantics_changed_before_atomic_action"
                    ),
                    "exact_error": (
                        "Captured snapshot popup semantics changed before "
                        "atomic action"
                    ),
                    "observed_at": int(time.time()),
                    "external_state_changed": False,
                    "raw_cdp_or_dom_used": False,
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
    assert '"contract_scope"' in prompt
    assert "37217119148451132" in prompt
    assert "社團完整名稱與目前狀態" in prompt
    assert "browser_evidence_recorded" in prompt
    assert "browser_blocker_recorded" in prompt
    assert "popup_semantics_changed_before_atomic_action" in prompt
    assert "Kolin｜In stock｜至少刊登於 1 個社團" in prompt
    assert "never ask KJ to paste or restate the original task" in prompt
    assert "localhost CDP connection-refused" in prompt
    assert "immediately create one fresh read-only continuation" in prompt
    assert "Infrastructure recovery never authorizes a Facebook click" in prompt
    assert "facebook_page_actor_guard_mismatch" in prompt
    assert "facebook_page_guard_rejected" in prompt
    assert "switch_into_page_visible=false is a passed negative gate" in prompt
    assert "facebook_page_switch_required" in prompt
    assert "report the exact failed predicate" in prompt
    assert "no profile switch is requested" in prompt
    assert "Hermes-controlled Facebook Page window" in prompt
    assert f"execution_task_id={execution_id}" in prompt
    assert f"grace_review_task_id={review_id}" in prompt


def test_timeout_callback_hard_bounds_pathological_contract_scope(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "callback-bounded-scope.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    execution_body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n"
        "```json\n"
        + json.dumps({
            "goal": {"objective": "x" * 40_000},
            "user_facing_delivery": {
                "required": True,
                "kind": "commerce_group_status",
            },
        })
        + "\n```"
    )
    _review_chain(db_path, execution_body=execution_body)
    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    prompt = adapter.handled[0].text
    evidence_json = prompt.split("evidence_snapshot=", 1)[1].split("\n", 1)[0]
    assert len(evidence_json) <= 16_000
    bounded = json.loads(evidence_json)
    assert bounded["note"].startswith("callback evidence was structurally clipped")


def test_uncontracted_user_report_is_evidence_not_callback_poison(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "uncontracted-user-report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(
        db_path,
        execution_metadata={
            "user_facing_report": {
                "kind": "commerce_group_status",
                "delivery": "inline_only",
                "complete": False,
                "as_of": "2026-08-03T13:31:46Z",
                "observed_at": int(time.time()),
                "rows": [],
                "coverage": [{
                    "subject_key": "carimali-armonia-soft-plus",
                    "subject_label": "Carimali Armonia Soft Plus",
                    "complete": False,
                    "named_count": 0,
                    "gap_count": None,
                    "expected_total": None,
                    "note": (
                        "reviewed evidence without an exact delivery contract"
                    ),
                }],
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
    assert adapter.sent == []
    prompt = adapter.handled[0].text
    assert "contracted_user_facing_report=false" in prompt
    assert "Gateway has not auto-delivered it" in prompt
    assert "no accepted Kanban completion" in prompt
    assert "never say no deliverable was produced" in prompt
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "closed"
    assert callback["user_report_delivered_at"] is None


def test_saved_evidence_report_delivers_without_another_grace_turn(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "saved-evidence-delivery.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    listing_id = "36803832485927906"
    subject_key = f"facebook_marketplace:{listing_id}"
    contract = {
        "routing": {"task_type": "secondhand_commerce_group_status"},
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": [subject_key],
        },
    }
    body = json.dumps(contract)
    observed_at = int(time.time())
    report = {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": False,
        "as_of": "2026-08-09T00:00:00+08:00",
        "observed_at": observed_at,
        "rows": [{
            "subject_key": subject_key,
            "subject_label": "Carimali",
            "destination_name": "二手咖啡器材交流",
            "status": "not_posted",
            "status_label": "未刊登",
            "observed_at": observed_at,
            "verified_at": "2026-08-09T00:00:00+08:00",
            "evidence": "visible unchecked checkbox",
            "source_listing_id": listing_id,
        }],
        "coverage": [{
            "subject_key": subject_key,
            "subject_label": "Carimali",
            "complete": False,
            "named_count": 1,
            "gap_count": None,
            "expected_total": None,
            "note": "one unnamed Join group control remains",
        }],
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="saved commerce evidence",
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
                f"{body}\n```"
            ),
        )
        review_id = kb.create_task(
            conn,
            title="Grace review",
            body=(
                "GRACE_LOOP_CONTRACT_STAGE: review\n```json\n"
                f"{body}\n```"
            ),
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="saved-evidence-delivery",
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
            contract_fingerprint="b" * 64,
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                execution_id,
                "runtime_finalization_requested",
                {"source": "saved_commerce_evidence_schema_resume"},
            )
        execution = kb.claim_task(conn, execution_id)
        assert execution is not None
        assert kb.complete_task(
            conn,
            execution_id,
            summary="saved evidence finalized",
            metadata={"user_facing_report": report},
            expected_run_id=execution.current_run_id,
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
            expected_run_id=review.current_run_id,
        )
    adapter = CallbackAdapter(
        send_result=SimpleNamespace(success=True, message_id="telegram-42"),
    )
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )

    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.sent) == 1
    assert "二手咖啡器材交流：尚未驗證" in adapter.sent[0][1]
    assert "candidate checkbox does not prove" in adapter.sent[0][1]
    assert adapter.handled == []
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
        continuations = conn.execute(
            "SELECT COUNT(*) FROM grace_delegations "
            "WHERE origin_review_task_id = ?",
            (review_id,),
        ).fetchone()[0]
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "evidence_delivered"
    assert callback["user_report_delivered_at"] is not None
    assert continuations == 0


@pytest.mark.parametrize(
    "group_ids",
    [
        ["897927458651235"],
        ["1333742673375089", "897927458651235"],
    ],
)
def test_callback_prompt_exposes_exact_accepted_facebook_crosspost_scope(
    tmp_path,
    monkeypatch,
    group_ids,
):
    db_path = tmp_path / "facebook-scope-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    listing_id = "37276725125275496"
    review_body = (
        "GRACE_LOOP_CONTRACT_STAGE: grace_review\n"
        "```json\n"
        + json.dumps({
            "grace_interpretation": (
                f"來源 listing {listing_id} 待確認，並明確選擇"
                "跨貼目標為 Facebook 群組 "
                + " 與 ".join(group_ids)
            ),
            "memory": {
                "working": [
                    "KJ 指定未來目的地僅限群組 "
                    + " 與 ".join(group_ids),
                    "歷史候選來源為 37276725125275496。",
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
                "canonical_listing_id": listing_id,
                "canonical_url": (
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
        + json.dumps(sorted(group_ids), ensure_ascii=False)
        in prompt
    )
    assert (
        "set user_facing_delivery.subject_keys to exactly "
        "['facebook_marketplace:<numeric-id>']"
        in prompt
    )
    assert "never add, remove, or substitute a destination" in prompt


def test_retry_delivered_decision_callback_is_exact_and_single_use(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "retry-stale-decision.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(db_path)
    with kb.connect_closing(db_path) as conn:
        event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (review_id,),
        ).fetchone()
        assert event is not None
        event_id = int(event["id"])
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                review_id,
                "memory_promotion_pending",
                {"state": "pending"},
            )
            conn.execute(
                """
                UPDATE grace_loop_callbacks
                   SET state = 'delivered', last_event_id = ?,
                       outcome_event_id = ?, outcome_kind = 'decision_blocked',
                       outcome_payload = '{"exact_question":"stale"}'
                 WHERE review_task_id = ?
                """,
                (event_id, event_id, review_id),
            )
        assert kb.retry_delivered_decision_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=event_id,
        )
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "pending"
        replay_event_id = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (review_id,),
        ).fetchone()[0]
        assert replay_event_id > event_id
        assert callback["last_event_id"] == replay_event_id - 1
        assert callback["outcome_kind"] is None
        assert not kb.retry_delivered_decision_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=event_id,
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                review_id,
                "operator_retry_requested",
                {"source": "test"},
            )
            conn.execute(
                """
                UPDATE grace_loop_callbacks
                   SET state = 'delivered', last_event_id = ?,
                       outcome_event_id = ?, outcome_kind = 'decision_blocked'
                 WHERE review_task_id = ?
                """,
                (event_id, event_id, review_id),
            )
        assert not kb.retry_delivered_decision_grace_loop_callback(
            conn,
            review_task_id=review_id,
            event_id=event_id,
        )


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


def test_validated_execution_browser_blocker_records_capability_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "execution-capability-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    listing_id = "915975414881937"
    observed_at = int(time.time())
    contract = {
        **canonical_marketplace_readonly_sections(listing_id),
        "external_targets": [f"Facebook Marketplace listing ID {listing_id}"],
        "routing": {"task_type": "secondhand_commerce_group_status"},
        "user_facing_delivery": {
            "required": True,
            "kind": "commerce_group_status",
            "delivery": "inline_only",
            "subject_keys": [f"facebook_marketplace:{listing_id}"],
        },
    }
    execution_body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    blocker = {
        "blocker_code": "facebook_readonly_guard_mismatch",
        "component": "controlled_facebook_browser",
        "operation": "open_more_options",
        "listing_id": listing_id,
        "tool": "browser_click",
        "tool_error_code": "facebook_readonly_scope_denied",
        "exact_error": (
            "Facebook mutation blocked: the current page is neither an "
            "authorized numeric group destination nor a reserved create route."
        ),
        "observed_at": observed_at,
        "external_state_changed": False,
        "raw_cdp_or_dom_used": False,
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook read-only inspection",
            assignee="clawops-browser",
            body=execution_body,
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
            suffix="execution-capability-outcome",
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
            reason="controlled browser read-only guard mismatch",
            kind="capability",
            blocker=blocker,
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert len(adapter.handled) == 1
    assert "validated_outcome=needs_input" in adapter.handled[0].text
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "capability_blocked"
    outcome_payload = json.loads(callback["outcome_payload"])
    assert outcome_payload["capability_key"] == (
        f"facebook:marketplace-group-status:{listing_id}"
    )
    assert outcome_payload["retry_after"] == observed_at + 900


def test_validated_marketplace_price_blocker_records_capability_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "price-capability-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    listing_id = "1666446304587399"
    observed_at = int(time.time())
    contract = {
        "identity": {
            "project": "secondhand_commerce",
            "topic_name": "price update",
            "request_instance_id": "price-callback-test",
        },
        "goal": {"objective": "update exact listing price"},
        "scope": {"allowed": ["price only"], "forbidden": ["other fields"]},
        "routing": {"task_type": "facebook_marketplace_price_update"},
        "external_targets": [f"Facebook Marketplace item {listing_id}"],
        "facebook_marketplace_price_update": {
            "action": "update_price",
            "transport": "browser",
            "marketplace_listing_id": listing_id,
            "currency": "TWD",
            "price_twd": 89000,
        },
    }
    execution_body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n```"
    )
    blocker = {
        "blocker_code": "facebook_marketplace_price_guard_mismatch",
        "component": "controlled_facebook_browser",
        "operation": "update_price",
        "listing_id": listing_id,
        "price_twd": 89000,
        "tool": "browser_type",
        "tool_error_code": "facebook_readonly_scope_denied",
        "exact_error": (
            "Facebook mutation blocked: the current page is neither an "
            "authorized numeric group destination, an approved Page "
            "composer, nor a reserved create route."
        ),
        "observed_at": observed_at,
        "external_state_changed": False,
        "raw_cdp_or_dom_used": False,
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn, title="Marketplace price update",
            assignee="clawops-browser", body=execution_body,
        )
        review_id = kb.create_task(
            conn, title="Grace review", assignee="default",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id,
            suffix="price-capability-outcome",
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
            contract_fingerprint="b" * 64,
        )
        assert kb.block_task(
            conn, execution_id, reason="price guard mismatch",
            kind="capability", blocker=blocker,
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "capability_blocked"
    outcome_payload = json.loads(callback["outcome_payload"])
    assert outcome_payload["capability_key"] == (
        f"facebook:marketplace-price-update:{listing_id}"
    )
    assert outcome_payload["retry_after"] == observed_at + 900


def test_execution_approval_challenge_records_exact_structured_outcome(
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
        challenge_rows = conn.execute(
            """
            SELECT token
              FROM grace_approval_challenges
             WHERE origin_review_task_id = ?
               AND origin_event_id = ?
               AND state = 'pending'
            """,
            (review_id, adapter.handled[0].text.split(
                "callback_event_id=", 1,
            )[1].splitlines()[0]),
        ).fetchall()
    assert len(adapter.handled) == 1
    assert callback["state"] == "delivered"
    assert callback["last_event_id"] > 0
    assert callback["last_error"] is None
    assert callback["outcome_kind"] == "approval_blocked"
    outcome = json.loads(callback["outcome_payload"])
    assert outcome == {
        "action": "navigate Facebook Marketplace read-only",
        "platform": "Facebook Marketplace",
        "scope": {
            "allowed": ["navigate", "click"],
            "forbidden": ["write"],
        },
        "exact_question": f"核准 {challenge_rows[0]['token']}",
    }
    assert len(challenge_rows) == 1


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
