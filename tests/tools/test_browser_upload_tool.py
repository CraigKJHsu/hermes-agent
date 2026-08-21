from __future__ import annotations

import json
import subprocess

from hermes_cli import kanban_db as kb
from tools import browser_tool
from tools import browser_upload_tool


def test_missing_files_returns_error(monkeypatch):
    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    result = json.loads(browser_upload_tool.browser_upload_files(files=[]))
    assert "error" in result
    assert "files" in result["error"]


def test_nonexistent_file_returns_error(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    missing = tmp_path / "missing.jpg"
    result = json.loads(browser_upload_tool.browser_upload_files(files=[str(missing)]))
    assert "error" in result
    assert "does not exist" in result["error"]


def test_no_cdp_endpoint_returns_error(monkeypatch, tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "")
    result = json.loads(browser_upload_tool.browser_upload_files(files=[str(image)]))
    assert "error" in result
    assert "CDP endpoint" in result["error"]


def test_success_invokes_upload_helper(monkeypatch, tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    captured = {}

    def fake_run(payload, timeout):
        captured["payload"] = payload
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "uploadedFiles": 1,
                    "selector": payload["selector"],
                    "targetUrl": "https://www.facebook.com/groups/1",
                    "state": {"fileInputs": [{"files": 1}]},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(browser_upload_tool, "_run_playwright_upload", fake_run)

    result = json.loads(
        browser_upload_tool.browser_upload_files(
            files=[str(image)],
            selector='input[type="file"][accept*="image"]',
            target_url_contains="facebook.com",
            page_index=1,
            input_index=2,
            verify_text_contains="1/9",
            verify_timeout_ms=4321,
            timeout=12,
            settle_ms=1234,
        )
    )
    assert result["success"] is True
    assert result["uploadedFiles"] == 1
    assert captured["payload"]["files"] == [str(image.resolve())]
    assert captured["payload"]["selector"] == 'input[type="file"][accept*="image"]'
    assert captured["payload"]["targetUrlContains"] == "facebook.com"
    assert captured["payload"]["pageIndex"] == 1
    assert captured["payload"]["inputIndex"] == 2
    assert captured["payload"]["verifyTextContains"] == "1/9"
    assert captured["payload"]["verifyTimeoutMs"] == 4321
    assert captured["payload"]["settleMs"] == 1234
    assert captured["timeout"] == 12


def test_ordinary_kanban_upload_skips_grace_identity_probe(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary upload",
            body="Upload an internal report",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_upload_tool,
        "_resolve_cdp_endpoint",
        lambda: "http://127.0.0.1:9222",
    )
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    calls = []

    def single_upload(payload, timeout):
        calls.append(payload)
        assert payload["inspectOnly"] is False
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "uploadedFiles": 1,
                "targetUrl": "https://example.com/upload",
            }),
            stderr="",
        )

    monkeypatch.setattr(
        browser_upload_tool,
        "_run_playwright_upload",
        single_upload,
    )
    result = json.loads(
        browser_upload_tool.browser_upload_files(files=[str(image)])
    )

    assert result["success"] is True
    assert len(calls) == 1


def test_helper_failure_returns_error(monkeypatch, tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")

    def fake_run(payload, timeout):
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=1,
            stdout=json.dumps({"success": False, "error": "playwright missing"}),
            stderr="stack trace",
        )

    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(browser_upload_tool, "_run_playwright_upload", fake_run)

    result = json.loads(browser_upload_tool.browser_upload_files(files=[str(image)]))
    assert "error" in result
    assert "playwright missing" in result["error"]
    assert result["stderr"] == "stack trace"


def test_check_keeps_schema_available_for_call_time_checks(monkeypatch):
    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(browser_upload_tool, "_node_command", lambda: "/usr/bin/node")
    assert browser_upload_tool._browser_upload_files_check() is True

    monkeypatch.setattr(browser_upload_tool, "_resolve_cdp_endpoint", lambda: "")
    assert browser_upload_tool._browser_upload_files_check() is True

    monkeypatch.setattr(browser_upload_tool, "_node_command", lambda: None)
    assert browser_upload_tool._browser_upload_files_check() is True


def test_helper_script_fails_closed_on_ambiguous_pages_and_inputs():
    script = browser_upload_tool._playwright_node_script()
    assert "Multiple pages" in script
    assert "provide page_index" in script
    assert "Multiple file inputs" in script
    assert "provide input_index" in script
    assert "Upload postcondition was not observed" in script
    assert "verification marker was already present before upload" in script
    assert "guardedPageIdentity" in script
    assert "performance.timeOrigin" in script
    assert "selectedInput" in script
    assert "locator.evaluate((selectedInput, inputIndex)" in script
    assert "data-hermes-page-composer-token" in script
    assert "data-hermes-page-composer-actor-token" in script
    assert "pageComposerFlow?.composerActorProofRequired" in script
    assert "actorMarkers.length === 0" in script
    assert "canonicalPageUrlOf(binding.pageUrl) !== binding.pageUrl" in script
    assert "canonicalPageUrlOf(location.href) !== binding.pageUrl" in script
    assert "location.href !== binding.pageUrl" not in script
    assert "const strictlyVisible = (node) =>" in script
    assert "current = current.parentElement" in script
    assert 'current.hasAttribute("hidden")' in script
    assert 'current.hasAttribute("inert")' in script
    assert 'current.getAttribute("aria-hidden") || ""' in script
    assert 'style.visibility !== "visible"' in script
    assert "Number(style.opacity) === 0" in script
    assert "const currentRects = [...current.getClientRects()]" in script
    assert ")].filter(strictlyVisible)" in script
    assert "|| !strictlyVisible(actorElement)" in script
    assert "actorElement.matches('a[href]')" in script
    assert ": binding.pageUrl" in script
    assert "textbox => textbox.contains(actorElement)" in script
    assert "rect.bottom > textboxTop" in script
    assert "rect.bottom > textboxTop + 24" not in script
    assert "return bindings.length === 1 ? bindings[0] : null" in script
    assert "actorMarkers.length !== 1" in script
    assert "tokenDialogs.length === 1" in script
    assert "tokenDialogs[0] === composer" in script
    assert "composer.contains(actorMarkers[0])" in script
    assert "directActorTextVisible" in script
    assert "includeDescendants = false" in script
    assert "NodeFilter.SHOW_TEXT" in script
    assert "actorElement, binding.actor," in script
    assert "textRectGroups.some((rects) => !rects.length)" in script
    assert "url: actorUrl, name: binding.actor" in script
    assert "visibleCandidateTextNodes" in script
    assert ').join("")) !== expectedActor' in script
    assert "createRange()" in script
    assert "range.getClientRects()" in script
    assert "textRects.every(({rect: textRect, textNode}) =>" in script
    assert "const colorPainted = value =>" in script
    assert 'style.filter || "none"' in script
    assert "right >= textRect.right - epsilon" in script
    assert "hasEditableSurface(actorElement)" in script
    assert "element?.isContentEditable" in script
    assert "child.isContentEditable" in script
    assert "Boolean(normalizeActor(" in script
    assert 'style.overflowX !== "visible"' in script
    assert 'style.overflowY !== "visible"' in script
    assert 'clipPath && clipPath !== "none"' in script
    assert 'legacyClip && legacyClip !== "auto"' in script
    assert 'value === "paint"' in script
    assert 'value === "strict"' in script
    assert 'value === "content"' in script
    assert "current.clientLeft" in script
    assert "current.clientTop" in script
    assert "current.clientWidth" in script
    assert "current.clientHeight" in script
    assert "actorBinding?.name === binding.actor" in script
    assert "textboxTop - rect.bottom > 240" in script
    assert "const fileInputBound = Boolean(" in script
    assert "element instanceof HTMLInputElement" in script
    assert 'element.type === "file"' in script
    assert '!element.matches(":disabled")' in script
    assert "strictlyVisible(element.parentElement || composer)" in script


def test_upload_to_protected_create_page_requires_task_reservation(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="external draft",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_upload_tool,
        "_resolve_cdp_endpoint",
        lambda: "http://127.0.0.1:9222",
    )
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")

    def inspect_target(payload, timeout):
        assert payload["inspectOnly"] is True
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "inspectOnly": True,
                "targetUrl": "https://seller.shopee.tw/portal/product/new",
                "pageIdentity": (
                    "https://seller.shopee.tw/portal/product/new|123"
                ),
            }),
            stderr="",
        )

    monkeypatch.setattr(
        browser_upload_tool, "_run_playwright_upload", inspect_target,
    )
    result = json.loads(
        browser_upload_tool.browser_upload_files(
            files=[str(image)],
            target_url_contains="seller.shopee.tw",
        )
    )

    assert "error" in result
    assert "no active task-scoped reservation" in result["error"]
    assert "exact page load" in result["error"]


def test_facebook_page_upload_requires_opened_page_composer(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="page post",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_upload_tool,
        "_resolve_cdp_endpoint",
        lambda: "http://127.0.0.1:9222",
    )
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    business_url = (
        "https://business.facebook.com/latest/composer?"
        "asset_id=531289396730654"
    )

    def inspect_target(payload, timeout):
        assert payload["inspectOnly"] is True
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "inspectOnly": True,
                "targetUrl": business_url,
                "pageIdentity": f"{business_url}|123",
            }),
            stderr="",
        )

    guard_calls = []

    def reject_unopened(url, identity, **kwargs):
        guard_calls.append((url, identity, kwargs))
        return None, "Facebook Page upload blocked: trusted composer is absent."

    monkeypatch.setattr(
        browser_upload_tool,
        "_run_playwright_upload",
        inspect_target,
    )
    monkeypatch.setattr(
        browser_tool,
        "facebook_page_post_upload_guard",
        reject_unopened,
    )
    monkeypatch.setattr(
        kb,
        "external_platform_for_url",
        lambda _url: "facebook",
    )
    monkeypatch.setattr(
        kb,
        "grace_task_facebook_page_post_permission",
        lambda _conn, _task_id: "https://www.facebook.com/solobizai",
    )

    result = json.loads(
        browser_upload_tool.browser_upload_files(files=[str(image)])
    )

    assert "trusted composer is absent" in result["error"]
    assert guard_calls == [(
        business_url,
        f"{business_url}|123",
        {
            "kanban_task_id": task_id,
            "expected_run_id": run.current_run_id,
        },
    )]


def test_facebook_page_upload_revalidates_exact_composer_token(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="page post",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_upload_tool,
        "_resolve_cdp_endpoint",
        lambda: "http://127.0.0.1:9222",
    )
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    page_url = "https://www.facebook.com/solobizai"
    page_identity = f"{page_url}|123"
    calls = []

    def run_upload(payload, timeout):
        del timeout
        calls.append(dict(payload))
        if payload["inspectOnly"]:
            output = {
                "success": True,
                "inspectOnly": True,
                "targetUrl": page_url,
                "pageIdentity": page_identity,
            }
        else:
            assert payload["guardedComposerToken"] == "composer-token-123"
            assert payload["guardedComposerPageUrl"] == page_url
            assert payload["guardedComposerPageActor"] == "SoloBizAi"
            output = {"success": True, "uploadedFiles": 1}
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps(output),
            stderr="",
        )

    monkeypatch.setattr(
        browser_upload_tool,
        "_run_playwright_upload",
        run_upload,
    )
    monkeypatch.setattr(
        browser_tool,
        "facebook_page_post_upload_guard",
        lambda *_args, **_kwargs: ({
            "composer_token": "composer-token-123",
            "page_url": page_url,
            "page_actor": "SoloBizAi",
        }, None),
    )
    monkeypatch.setattr(
        kb,
        "grace_task_facebook_page_post_permission",
        lambda _conn, _task_id: page_url,
    )

    result = json.loads(
        browser_upload_tool.browser_upload_files(files=[str(image)])
    )

    assert result["success"] is True
    assert len(calls) == 2


def test_unrelated_facebook_upload_does_not_consume_page_composer_guard(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="unrelated facebook flow",
            body="GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(
        browser_upload_tool,
        "_resolve_cdp_endpoint",
        lambda: "http://127.0.0.1:9222",
    )
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    event_url = "https://www.facebook.com/events/123"
    calls = []

    def run_upload(payload, timeout):
        del timeout
        calls.append(dict(payload))
        output = (
            {
                "success": True,
                "inspectOnly": True,
                "targetUrl": event_url,
                "pageIdentity": f"{event_url}|123",
            }
            if payload["inspectOnly"]
            else {"success": True, "uploadedFiles": 1}
        )
        return subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps(output),
            stderr="",
        )

    monkeypatch.setattr(
        browser_upload_tool,
        "_run_playwright_upload",
        run_upload,
    )
    monkeypatch.setattr(
        browser_tool,
        "facebook_page_post_upload_guard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Page composer guard must not run")
        ),
    )

    result = json.loads(
        browser_upload_tool.browser_upload_files(files=[str(image)])
    )

    assert result["success"] is True
    assert len(calls) == 2
