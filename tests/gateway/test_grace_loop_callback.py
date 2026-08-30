import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.kanban_watchers import (
    _confirmed_grace_provider_message_id,
    _grace_review_accepted,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


class CallbackAdapter:
    def __init__(self, *, record_outcome=True, send_result=None):
        self.handled = []
        self.sent = []
        self.sent_images = []
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
        if self.send_result is not None:
            return self.send_result
        return SimpleNamespace(
            success=True,
            message_id=str(len(self.sent)),
        )

    async def send_image_file(
        self, chat_id, image_path, caption=None, metadata=None,
    ):
        self.sent_images.append((chat_id, image_path, caption, metadata or {}))
        return SimpleNamespace(
            success=True,
            message_id=f"image-{len(self.sent_images)}",
        )


def test_legacy_telegram_content_package_is_delivered_before_callback_closes(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "content-package.db"
    markdown = tmp_path / "package.md"
    page = tmp_path / "page.png"
    cover = tmp_path / "cover.png"
    unrelated_page = tmp_path / "unrelated-page.png"
    unrelated_cover = tmp_path / "unrelated-cover.png"
    markdown.write_text("# Complete package\n\n" + "Body. " * 900, encoding="utf-8")
    page.write_bytes(b"page-image")
    cover.write_bytes(b"cover-image")
    unrelated_page.write_bytes(b"private-page")
    unrelated_cover.write_bytes(b"private-cover")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    contract = {
        "external_targets": ["local://telegram-topic-release-package"],
        "routing": {"task_type": "content_draft"},
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Complete Telegram package",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                json.dumps(contract),
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="package ready",
            metadata={
                "artifacts": [str(markdown), str(page), str(cover)],
                "user_facing_report": {
                    "kind": "content_package",
                    "delivery": "inline_with_attachment",
                    "complete": True,
                    "title": "Untrusted package",
                    "body": "UNTRUSTED BODY",
                    "observed_at": int(time.time()),
                    "assets": [
                        {
                            "filename": "page.png",
                            "label": "untrusted page",
                            "path": str(unrelated_page),
                            "sha256": hashlib.sha256(
                                unrelated_page.read_bytes()
                            ).hexdigest(),
                        },
                        {
                            "filename": "cover.png",
                            "label": "untrusted cover",
                            "path": str(unrelated_cover),
                            "sha256": hashlib.sha256(
                                unrelated_cover.read_bytes()
                            ).hexdigest(),
                        },
                    ],
                },
            },
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="content-package",
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
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )

    adapter = CallbackAdapter()
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert "Complete package" in "".join(text for _, text, _ in adapter.sent)
    assert "UNTRUSTED BODY" not in "".join(text for _, text, _ in adapter.sent)
    assert [item[2] for item in adapter.sent_images] == ["page.png", "cover.png"]
    assert all("attachments" in item[1] for item in adapter.sent_images)
    assert all("unrelated" not in item[1] for item in adapter.sent_images)
    with kb.connect_closing(db_path) as conn:
        receipt = kb.get_grace_loop_callback(conn, review_id)
    assert receipt["state"] == "delivered"
    assert receipt["user_report_delivered_at"] is not None
    assert receipt["user_report_chunk_count"] == (
        len(adapter.sent) + len(adapter.sent_images)
    )


def test_complete_content_package_auto_closes_after_verified_inline_delivery(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "content-package-auto-close.db"
    markdown = tmp_path / "package.md"
    page = tmp_path / "page.png"
    cover = tmp_path / "cover.png"
    markdown.write_text("# Complete package\n\nBody.", encoding="utf-8")
    page.write_bytes(b"page-image")
    cover.write_bytes(b"cover-image")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "UPDATE commerce_group_migration_state SET reconciled = 0 "
            "WHERE singleton_id = 1"
        )
        conn.commit()
    contract = {
        "routing": {"task_type": "content_draft"},
        "user_facing_delivery": {
            "required": True,
            "kind": "content_package",
            "delivery": "inline_with_attachment",
            "subject_keys": ["page-body", "page-hero", "audio-brief"],
            "asset_filenames": ["page.png", "cover.png"],
        },
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Complete Telegram package",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                json.dumps(contract),
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="package ready",
            metadata={"artifacts": [str(markdown), str(page), str(cover)]},
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="content-package-auto-close",
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
            contract_fingerprint="c" * 64,
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )

    adapter = CallbackAdapter(record_outcome=False)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    with kb.connect_closing(db_path) as conn:
        receipt = kb.get_grace_loop_callback(conn, review_id)
    assert receipt["state"] == "delivered"
    assert receipt["outcome_kind"] == "closed"
    assert receipt["outcome_event_id"] == receipt["last_event_id"]
    assert receipt["user_report_delivered_at"] is not None
    assert [item[2] for item in adapter.sent_images] == ["page.png", "cover.png"]
    assert not any("callback 已重試 3 次" in sent[1] for sent in adapter.sent)


def test_page_preflight_evidence_delivers_body_and_verified_hero(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "page-preflight-package.db"
    hero = tmp_path / "page.png"
    hero.write_bytes(b"verified-page-image")
    body = "完整 Page 正文\n\n#AIBizWeek"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    contract = {
        "user_facing_delivery": {
            "required": True,
            "kind": "content_package",
            "delivery": "inline_with_attachment",
            "subject_keys": ["page-preflight"],
            "asset_filenames": [hero.name],
        },
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook Page preflight",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                json.dumps(contract),
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="preflight complete",
            metadata={
                "acceptance_evidence": {
                    "admission": {
                        "task_type": "facebook_page_publish_preflight",
                        "allowed_tool_used": "facebook_page_publish_preflight",
                        "external_effect_budget": 0,
                        "published": False,
                        "external_actions_performed": False,
                    },
                    "source_and_final_text": {
                        "final_message_sha256": hashlib.sha256(
                            body.encode("utf-8")
                        ).hexdigest(),
                    },
                    "final_facebook_page_body": body,
                    "hero_asset": {
                        "image_path": str(hero),
                        "image_sha256": hashlib.sha256(
                            hero.read_bytes()
                        ).hexdigest(),
                    },
                    "page_identity": {
                        "page_id": "531289396730654",
                        "page_name": "AI BizWeek",
                        "page_url": "https://www.facebook.com/531289396730654",
                    },
                    "canonical_one_time_approval_text": "確認發布。",
                },
            },
        )
        report = kb.grace_inline_content_package_report(conn, execution_id)

    assert report is not None
    assert report["body"] == body
    assert report["assets"] == [{
        "filename": hero.name,
        "label": "Facebook Page Hero 主圖",
        "path": str(hero.resolve()),
        "sha256": hashlib.sha256(hero.read_bytes()).hexdigest(),
    }]


def test_page_preflight_evidence_rejects_changed_hero(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "page-preflight-changed-hero.db"
    hero = tmp_path / "page.png"
    hero.write_bytes(b"changed")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    contract = {
        "user_facing_delivery": {
            "required": True,
            "kind": "content_package",
            "delivery": "inline_with_attachment",
            "asset_filenames": [hero.name],
        },
    }
    body = "Page body"
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Facebook Page preflight",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                json.dumps(contract),
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="preflight complete",
            metadata={
                "acceptance_evidence": {
                    "admission": {
                        "task_type": "facebook_page_publish_preflight",
                        "allowed_tool_used": "facebook_page_publish_preflight",
                        "external_effect_budget": 0,
                        "published": False,
                        "external_actions_performed": False,
                    },
                    "source_and_final_text": {
                        "final_message_sha256": hashlib.sha256(
                            body.encode("utf-8")
                        ).hexdigest(),
                    },
                    "final_facebook_page_body": body,
                    "hero_asset": {
                        "image_path": str(hero),
                        "image_sha256": "0" * 64,
                    },
                    "page_identity": {
                        "page_id": "1",
                        "page_name": "Page",
                        "page_url": "https://www.facebook.com/1",
                    },
                    "canonical_one_time_approval_text": "確認發布。",
                },
            },
        )
        assert kb.grace_inline_content_package_report(
            conn, execution_id,
        ) is None


def test_legacy_text_only_telegram_draft_does_not_require_package_delivery(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "text-only-content.db"
    markdown = tmp_path / "draft.md"
    markdown.write_text("Text only draft", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    contract = {
        "external_targets": ["local://telegram-topic-draft"],
        "routing": {"task_type": "content_draft"},
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="Text-only Telegram draft",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                json.dumps(contract),
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="draft ready",
            metadata={"artifacts": [str(markdown)]},
        )
        assert kb.grace_user_facing_delivery_contract(
            conn, execution_id,
        ) is None
        assert kb.grace_inline_content_package_report(
            conn, execution_id,
        ) is None


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


def test_grace_provider_receipt_rejects_success_marked_ambiguous():
    assert _confirmed_grace_provider_message_id(SimpleNamespace(
        success=True,
        message_id="123",
        delivery_ambiguous=True,
    )) is None
    assert _confirmed_grace_provider_message_id(SimpleNamespace(
        success=True,
        message_id="123",
        delivery_ambiguous=False,
    )) == "123"


def test_grace_review_accepts_nonempty_criteria_list():
    assert _grace_review_accepted({
        "approved": True,
        "acceptance_criteria_met": [
            "PNG attachment exists",
            "SHA-256 readback matches",
        ],
    }) is True


def test_grace_review_accepts_approved_metadata_with_evidence():
    assert _grace_review_accepted({
        "approved": True,
        "evidence": {
            "package_assets_count": 7,
            "cover_claimed_sha256": "30b87e090cdda4f66215339f4dfb969b9a04dfbbd8bf389b9ec17688c18ca98f",
        },
        "verification_notes": ["visual review completed"],
    }) is True


def test_grace_review_accepts_visual_review_with_file_readback():
    assert _grace_review_accepted({
        "visual_review": {
            "approved": True,
            "observed_elements": ["square cover", "EP03"],
            "defects_found": [],
        },
        "parent_verified_file": {
            "format": "PNG",
            "width": 1254,
            "height": 1254,
            "sha256": "966aef8157560a217bed9d4e823499cd5830064704f6e73ea465af4242ac8cf0",
        },
        "external_actions": False,
    }) is True


def test_grace_review_accepts_instruction_readback_metadata():
    assert _grace_review_accepted({
        "reviewed_file": "/tmp/INSTRUCTIONS.md",
        "review_result": "accepted",
        "verified_lines": {"seven_deliverables": "7-37"},
        "verified_checks": {
            "seven_deliverables": True,
            "external_actions_performed": False,
        },
        "approved": True,
    }) is True


def test_grace_review_accepts_ep04_verified_check_list():
    assert _grace_review_accepted({
        "approved": True,
        "verified_checks": [
            "seven deliverables verified",
            "page hero and audio brief visually inspected",
            "external actions were not performed",
        ],
        "asset_declarations": {
            "page_hero": {"dimensions": "1600x900"},
            "audio_brief": {"dimensions": "1254x1254"},
        },
        "visual_review": {
            "all_required_text_readable": True,
            "text_occlusion_free": True,
            "disclosure_non_obstructive": True,
            "defects_found": [],
        },
        "publication_approved": False,
    }) is True


def test_grace_review_rejects_ep04_wrong_or_missing_asset_dimensions():
    assert _grace_review_accepted({
        "approved": True,
        "verified_checks": [
            "seven deliverables verified",
            "page hero and audio brief visually inspected",
            "external actions were not performed",
        ],
        "asset_declarations": {
            "page_hero": {"dimensions": "1003x1568"},
            "audio_brief": {"dimensions": "1254x1254"},
        },
    }) is False
    assert _grace_review_accepted({
        "approved": True,
        "verified_checks": [
            "seven deliverables verified",
            "page hero and audio brief visually inspected",
            "external actions were not performed",
        ],
        "asset_declarations": {
            "page_hero": {"dimensions_px": "未直接讀回"},
            "audio_brief": {"dimensions": "1254x1254"},
        },
    }) is False


def test_grace_review_rejects_page_hero_with_obstructive_disclosure():
    assert _grace_review_accepted({
        "review_outcome": "accepted",
        "asset_family": "page_hero",
        "reviewed_image": {"dimensions": "1664x936"},
        "visual_review": {
            "all_required_text_readable": False,
            "text_occlusion_free": False,
            "disclosure_non_obstructive": False,
            "defects_found": ["AI disclosure overlay obscures the action area"],
        },
    }) is False


def test_grace_review_canonical_acceptance_allows_non_rejecting_legacy_alias():
    assert _grace_review_accepted({
        "review_outcome": "accepted",
        "review_result": "pass",
        "asset_family": "page_hero",
        "visual_review": {
            "all_required_text_readable": True,
            "text_occlusion_free": True,
            "disclosure_non_obstructive": True,
            "defects_found": [],
        },
    }) is True
    assert _grace_review_accepted({
        "review_outcome": "accepted",
        "review_verdict": "blocked",
        "asset_family": "page_hero",
        "visual_review": {
            "all_required_text_readable": True,
            "text_occlusion_free": True,
            "disclosure_non_obstructive": True,
            "defects_found": [],
        },
    }) is False


def test_grace_review_accepts_evidence_backed_accepted_alias():
    assert _grace_review_accepted({
        "accepted": True,
        "reviewed_artifacts": {"page_hero": "/tmp/page-hero.png"},
        "verified_facts": {
            "dimensions": "1600x900",
            "external_actions_performed": False,
        },
    }) is True


def test_grace_review_rejects_unsupported_accepted_alias():
    assert _grace_review_accepted({"accepted": True}) is False
    assert _grace_review_accepted({
        "accepted": True,
        "review_outcome": "rejected",
        "verified_facts": {"dimensions": "1600x900"},
    }) is False


def test_grace_review_rejects_malformed_verified_check_list():
    assert _grace_review_accepted({
        "approved": True,
        "verified_checks": [False],
    }) is False
    assert _grace_review_accepted({
        "approved": True,
        "verified_checks": ["visual inspection passed", None],
    }) is False


def test_grace_review_explicit_rejection_overrides_approved_evidence():
    assert _grace_review_accepted({
        "review_outcome": "rejected",
        "approved": True,
        "verified_checks": ["visual inspection completed"],
    }) is False
    assert _grace_review_accepted({
        "review_outcome": "accepted",
        "approved": False,
    }) is False


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
    completion_mode="terminal",
):
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn, title="execution", assignee="clawops-content",
        )
        kb.complete_task(conn, execution_id, summary="execution complete")
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
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
            completion_mode=completion_mode,
        )
        if event_kind == "blocked":
            assert kb.block_task(conn, review_id, reason="KJ must choose a price")
        else:
            metadata = {"review_outcome": "accepted"} if accepted else {"review_outcome": "unknown"}
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


def test_intermediate_accepted_review_without_structured_outcome_delivers_without_retry(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "intermediate-missing-outcome.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _, review_id = _review_chain(
        db_path,
        completion_mode="intermediate",
    )
    adapter = CallbackAdapter(record_outcome=False)
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
    assert callback["last_event_id"] > 0
    assert callback["outcome_kind"] is None
    assert callback["outcome_payload"] is None
    assert "intermediate callback delivered without structured continuation" in (
        callback["last_error"] or ""
    )
    assert adapter.sent == []


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
    assert "MUST first call clawops_delegate during this callback" not in adapter.handled[0].text


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
    assert "MUST first call clawops_delegate during this callback" not in event.text
    assert "grace_callback_outcome exactly once" not in event.text
    trigger_line = next(
        line for line in event.text.splitlines() if line.startswith("trigger_event=")
    )
    trigger_event = json.loads(trigger_line.split("=", 1)[1])
    assert trigger_event["kind"] == "blocked"
    payload_preview = json.loads(trigger_event["payload_preview"])
    assert payload_preview["reason"] == "KJ must choose the final product name"
    evidence_line = next(
        line for line in event.text.splitlines() if line.startswith("evidence_snapshot=")
    )
    evidence = json.loads(evidence_line.split("=", 1)[1])
    assert "metadata" not in evidence["execution"]
    assert "metadata_preview" not in evidence["execution"]
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


def test_quota_blocked_execution_records_structured_callback_outcome(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "execution-quota-blocked-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    usage_message = (
        "OpenClaw execution is blocked by codex_usage_limit: "
        "You've reached your Codex subscription usage limit."
    )
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            assignee="openclaw",
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
            suffix="execution-quota-blocked",
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
            reason=usage_message,
            kind="capability",
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
        review = kb.get_task(conn, review_id)
    assert len(adapter.handled) == 1
    assert "validated_outcome=quota_blocked" in adapter.handled[0].text
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "quota_blocked"
    payload = json.loads(callback["outcome_payload"])
    assert payload["reason"] == "codex_usage_limit"
    assert "Codex subscription usage limit" in payload["message"]
    assert review is not None and review.status == "todo"


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
    evidence_line = next(
        line for line in event.text.splitlines() if line.startswith("evidence_snapshot=")
    )
    evidence = json.loads(evidence_line.split("=", 1)[1])
    assert "metadata" not in evidence["execution"]
    assert "metadata_preview" not in evidence["execution"]
    assert "MUST first call clawops_delegate during this callback" not in event.text
    assert len(event.text) < 9000


def test_large_snapshot_preserves_bounded_user_facing_report(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "bounded-user-facing-report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    report = {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": False,
        "as_of": "2026-08-02 16:00 Asia/Taipei",
        "observed_at": int(time.time()),
        "rows": [],
        "coverage": [{
            "subject_key": "carimali-armonia-soft-plus",
            "subject_label": "Carimali Armonia Soft Plus",
            "complete": False,
            "named_count": 0,
            "gap_count": None,
            "expected_total": None,
            "expected_total_label": "目的地未知",
            "note": "N" * 8_000,
        }],
    }
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body="\n".join([
                "GRACE_LOOP_CONTRACT_STAGE: execution",
                "```json",
                '{"user_facing_delivery":{"required":true,'
                '"kind":"commerce_group_status","delivery":"inline_only",'
                '"subject_keys":["carimali-armonia-soft-plus"]}}',
                "```",
            ]),
        )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="report ready",
            metadata={
                "user_facing_report": report,
                "padding": "DROP-ME-" * 4_000,
            },
        )
        review_id = kb.create_task(
            conn, title="review", parents=(execution_id,),
        )
        _bind_delegation(
            conn, execution_id, review_id, suffix="bounded-report",
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
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )

    adapter = CallbackAdapter(record_outcome=False)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    event = adapter.handled[0]
    evidence_line = next(
        line
        for line in event.text.splitlines()
        if line.startswith("evidence_snapshot=")
    )
    evidence = json.loads(evidence_line.split("=", 1)[1])
    preserved = evidence["execution"]["user_facing_report"]
    assert preserved["coverage"][0]["note"] == "N" * 8_000
    assert "...[truncated]" in evidence_line
    assert len(evidence_line) < 16_000
    assert "Gateway has already delivered that exact structured payload" in event.text
    assert "MUST first call clawops_delegate during this callback" not in event.text
    assert len(event.text) < 20_000
    delivered_text = "".join(text for _, text, _ in adapter.sent)
    assert "Carimali Armonia Soft Plus" in delivered_text
    assert "目前沒有可具名的社團紀錄" in delivered_text
    with kb.connect_closing(db_path) as conn:
        receipt = kb.get_grace_loop_callback(conn, review_id)
    assert receipt["user_report_delivered_at"] is not None
    assert receipt["user_report_chunk_count"] == len(adapter.sent)


def test_complete_report_is_not_sent_before_historical_reconciliation(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "blocked-complete-user-facing-report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    now = int(time.time())
    report = {
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "complete": True,
        "as_of": "2026-08-02 20:00 Asia/Taipei",
        "observed_at": now,
        "rows": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "destination_id": "902016640291333",
            "destination_name": "【大台北地區】二手家具、二手家電買賣",
            "status": "public",
            "status_label": "公開可見",
            "observed_at": now,
            "verified_at": "2026-08-02 20:00 Asia/Taipei",
            "evidence": "社團頁面已讀回商品卡。",
        }],
        "coverage": [{
            "subject_key": "kolin-kd291m06",
            "subject_label": "Kolin KD-291M06",
            "complete": True,
            "named_count": 1,
            "gap_count": 0,
            "expected_total": 1,
            "expected_total_label": "目前共一個目的地",
            "note": "清單完整。",
        }],
    }
    body = "\n".join([
        "GRACE_LOOP_CONTRACT_STAGE: execution",
        "```json",
        '{"user_facing_delivery":{"required":true,'
        '"kind":"commerce_group_status","delivery":"inline_only",'
        '"subject_keys":["kolin-kd291m06"]}}',
        "```",
    ])
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(conn, title="execution", body=body)
        assert kb.complete_task(
            conn,
            execution_id,
            summary="complete report",
            metadata={"user_facing_report": report},
        )
        review_id = kb.create_task(conn, title="review", parents=(execution_id,))
        _bind_delegation(conn, execution_id, review_id, suffix="blocked-complete")
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
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        conn.execute(
            "UPDATE commerce_group_migration_state SET reconciled = 0 "
            "WHERE singleton_id = 1"
        )

    adapter = CallbackAdapter(record_outcome=False)
    runner = _runner(
        adapter,
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    asyncio.run(runner._deliver_due_grace_loop_callbacks(kb))

    assert adapter.sent == []
    assert adapter.handled == []
    with kb.connect_closing(db_path) as conn:
        callback = kb.get_grace_loop_callback(conn, review_id)
    assert "requires current" in callback["last_error"]


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


def test_rejected_review_metadata_wakes_grace_as_blocked(tmp_path, monkeypatch):
    db_path = tmp_path / "rejected-callback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn, title="execution", assignee="clawops-content",
        )
        kb.complete_task(conn, execution_id, summary="capability blocked")
        review_id = kb.create_task(
            conn,
            title="Grace review",
            assignee="default",
            parents=(execution_id,),
        )
        _bind_delegation(conn, execution_id, review_id, suffix="rejected")
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
        assert kb.complete_task(
            conn,
            review_id,
            summary="review rejected incomplete",
            metadata={
                "approved": False,
                "review_verdict": "rejected_incomplete",
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
    assert "validated_outcome=blocked" in adapter.handled[0].text
    assert "validated_outcome=invalid_completion_metadata" not in adapter.handled[0].text
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
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "closed"
    assert callback["outcome_event_id"] == callback["last_event_id"]


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
    assert callback["state"] == "delivered"
    assert callback["outcome_kind"] == "closed"
    assert callback["outcome_event_id"] == callback["last_event_id"]


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
