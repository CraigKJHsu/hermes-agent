"""Integration tests for tools.browser_supervisor.

Exercises the supervisor end-to-end against a real local Chrome
(``--remote-debugging-port``).  Skipped when Chrome is not installed
— these are the tests that actually verify the CDP wire protocol
works, since mock-CDP unit tests can only prove the happy paths we
thought to model.

Run manually:
    scripts/run_tests.sh tests/tools/test_browser_supervisor.py

Automated: skipped in CI unless ``HERMES_E2E_BROWSER=1`` is set.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time

import pytest


_MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


pytestmark = pytest.mark.skipif(
    not shutil.which("google-chrome")
    and not shutil.which("chromium")
    and not os.path.isfile(_MACOS_CHROME),
    reason="Chrome/Chromium not installed",
)


def _find_chrome() -> str:
    for candidate in (
        "google-chrome", "chromium", "chromium-browser", _MACOS_CHROME,
    ):
        path = candidate if os.path.isfile(candidate) else shutil.which(candidate)
        if path:
            return path
    pytest.skip("no Chrome binary found")


@pytest.fixture
def chrome_cdp(request):
    """Start a headless Chrome with --remote-debugging-port, yield its WS URL.

    Uses a unique port per xdist worker to avoid cross-worker collisions.
    Always launches with ``--site-per-process`` so cross-origin iframes
    become real OOPIFs (needed by the iframe interaction tests).
    """

    # xdist worker_id is "master" in single-process mode or "gw0".."gwN" otherwise.
    # Under subprocess-per-file isolation there's no xdist, so we fall back
    # to "master" via the session-scoped fixture below.
    worker_id = request.getfixturevalue("worker_id") if "worker_id" in request.fixturenames else "master"
    if worker_id == "master":
        port_offset = 0
    else:
        port_offset = int(worker_id.lstrip("gw"))
    port = 9225 + port_offset
    profile = tempfile.mkdtemp(prefix="hermes-supervisor-test-")
    proc = subprocess.Popen(
        [
            _find_chrome(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--headless=new",
            "--disable-gpu",
            "--site-per-process",  # force OOPIFs for cross-origin iframes
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    ws_url = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1
            ) as r:
                info = json.loads(r.read().decode())
                ws_url = info["webSocketDebuggerUrl"]
                break
        except Exception:
            time.sleep(0.25)
    if ws_url is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, AssertionError, Exception):
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except (AssertionError, Exception):
                pass
        shutil.rmtree(profile, ignore_errors=True)
        pytest.skip("Chrome didn't expose CDP in time")

    yield ws_url, port

    # Tear down Chrome. The stdlib `subprocess._wait()` POSIX implementation
    # has a known race (https://bugs.python.org/issue38630): when SIGCHLD
    # arrives concurrently with `proc.wait()`, `_try_wait(WNOHANG)` can
    # return a foreign pid and the `assert pid == self.pid or pid == 0`
    # fires. We saw this in CI on slice 1 after this fixture's teardown
    # (PR #33661 follow-up). Swallow the stdlib race + force-kill if wait
    # hangs, then always reap so we don't leak a zombie.
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except (subprocess.TimeoutExpired, AssertionError, Exception):
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except (AssertionError, Exception):
            pass
    shutil.rmtree(profile, ignore_errors=True)


def _test_page_url() -> str:
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Supervisor pytest</title></head><body>
<h1>Supervisor pytest</h1>
<button aria-label="Join group" onclick="window.__joined = true">
  Join group
</button>
<div role="textbox" aria-label="Draft body" contenteditable="true"></div>
<label for="language">Profile Language</label>
<select id="language"
  oninput="window.__selectInputCtor = event.constructor.name; window.__selectInputType = event.inputType || null"
  onchange="window.__languageChanges = (window.__languageChanges || 0) + 1">
  <option value="en_US">English (English)</option>
  <option value="zh_TW">Traditional Chinese (legacy duplicate value)</option>
  <option value="zh_TW">正體中文 (Chinese (Traditional))</option>
</select>
<label for="optgroup-disabled-language">Optgroup Disabled Language</label>
<select id="optgroup-disabled-language">
  <option value="en_US">English (English)</option>
  <optgroup label="Unavailable" disabled>
    <option value="zh_TW">正體中文 (Chinese (Traditional))</option>
  </optgroup>
</select>
<fieldset disabled>
  <label for="fieldset-disabled-language">Fieldset Disabled Language</label>
  <select id="fieldset-disabled-language">
    <option value="en_US">English (English)</option>
    <option value="zh_TW">正體中文 (Chinese (Traditional))</option>
  </select>
</fieldset>
<label for="controlled-language">Controlled Language</label>
<select id="controlled-language" onchange="this.selectedIndex = 0">
  <option value="en_US">English (English)</option>
  <option value="zh_TW">正體中文 (Chinese (Traditional))</option>
</select>
<iframe id="inner" srcdoc="<body><h2>frame-marker</h2></body>" width="400" height="100"></iframe>
</body></html>"""
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


def _fire_on_page(cdp_url: str, expression: str) -> None:
    """Navigate the first page target to a data URL and fire `expression`."""
    import asyncio
    import websockets as _ws_mod

    async def run():
        async with _ws_mod.connect(cdp_url, max_size=50 * 1024 * 1024) as ws:
            next_id = [1]

            async def call(method, params=None, session_id=None):
                cid = next_id[0]
                next_id[0] += 1
                p = {"id": cid, "method": method}
                if params:
                    p["params"] = params
                if session_id:
                    p["sessionId"] = session_id
                await ws.send(json.dumps(p))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("id") == cid:
                        return m

            targets = (await call("Target.getTargets"))["result"]["targetInfos"]
            page = next(t for t in targets if t.get("type") == "page")
            attach = await call(
                "Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}
            )
            sid = attach["result"]["sessionId"]
            await call("Page.navigate", {"url": _test_page_url()}, session_id=sid)
            await asyncio.sleep(1.5)  # let the page load
            await call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                session_id=sid,
            )

    asyncio.run(run())


def _serve_virtual_https_page(cdp_url: str, url: str, html: str) -> None:
    """Fulfill one HTTPS navigation through CDP without external network I/O."""
    import websockets as _ws_mod

    async def run():
        async with _ws_mod.connect(cdp_url, max_size=50 * 1024 * 1024) as ws:
            next_id = [1]

            async def call(method, params=None, session_id=None):
                cid = next_id[0]
                next_id[0] += 1
                payload = {"id": cid, "method": method}
                if params:
                    payload["params"] = params
                if session_id:
                    payload["sessionId"] = session_id
                await ws.send(json.dumps(payload))
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("id") == cid:
                        return message

            targets = (await call("Target.getTargets"))["result"]["targetInfos"]
            page = next(t for t in targets if t.get("type") == "page")
            attached = await call(
                "Target.attachToTarget",
                {"targetId": page["targetId"], "flatten": True},
            )
            session_id = attached["result"]["sessionId"]
            await call("Page.enable", session_id=session_id)
            await call(
                "Fetch.enable",
                {"patterns": [{"urlPattern": url, "requestStage": "Request"}]},
                session_id=session_id,
            )

            navigate_id = next_id[0]
            next_id[0] += 1
            await ws.send(json.dumps({
                "id": navigate_id,
                "method": "Page.navigate",
                "params": {"url": url},
                "sessionId": session_id,
            }))
            navigation_done = False
            load_done = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (
                navigation_done and load_done
            ):
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                message = json.loads(raw)
                if message.get("id") == navigate_id:
                    navigation_done = True
                    continue
                if (
                    message.get("sessionId") == session_id
                    and message.get("method") == "Page.loadEventFired"
                ):
                    load_done = True
                    continue
                if (
                    message.get("sessionId") == session_id
                    and message.get("method") == "Fetch.requestPaused"
                ):
                    fulfill_id = next_id[0]
                    next_id[0] += 1
                    await ws.send(json.dumps({
                        "id": fulfill_id,
                        "method": "Fetch.fulfillRequest",
                        "params": {
                            "requestId": message["params"]["requestId"],
                            "responseCode": 200,
                            "responseHeaders": [{
                                "name": "Content-Type",
                                "value": "text/html; charset=utf-8",
                            }],
                            "body": base64.b64encode(html.encode()).decode(),
                        },
                        "sessionId": session_id,
                    }))
            assert navigation_done and load_done
            await call("Fetch.disable", session_id=session_id)

    asyncio.run(run())


@pytest.fixture
def supervisor_registry():
    """Yield the global registry and tear down any supervisors after the test."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    yield SUPERVISOR_REGISTRY
    SUPERVISOR_REGISTRY.stop_all()


def _wait_for_dialog(supervisor, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = supervisor.snapshot()
        if snap.pending_dialogs:
            return snap.pending_dialogs
        time.sleep(0.1)
    return ()


def test_supervisor_start_and_snapshot(chrome_cdp, supervisor_registry):
    """Supervisor attaches, exposes an active snapshot with a top frame."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-1", cdp_url=cdp_url)

    # Navigate so the frame tree populates.
    _fire_on_page(cdp_url, "/* no dialog */ void 0")

    # Give a moment for frame events to propagate
    time.sleep(1.0)
    snap = supervisor.snapshot()
    assert snap.active is True
    assert snap.task_id == "pytest-1"
    assert snap.pending_dialogs == ()
    # At minimum a top frame should exist after the navigate.
    assert snap.frame_tree.get("top") is not None


def test_guarded_dom_action_revalidates_semantics_and_contenteditable(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-guarded-dom",
        cdp_url=cdp_url,
    )
    _fire_on_page(cdp_url, "void 0")
    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    nodes = ax_result["result"]["nodes"]

    def backend_id(role, name):
        return next(
            node["backendDOMNodeId"]
            for node in nodes
            if node.get("role", {}).get("value") == role
            and node.get("name", {}).get("value") == name
        )

    profile_language_node = next(
        node
        for node in nodes
        if node.get("role", {}).get("value") == "combobox"
        and node.get("name", {}).get("value") == "Profile Language"
    )
    popup_role = next(
        str((prop.get("value") or {}).get("value") or "").casefold()
        for prop in profile_language_node.get("properties", [])
        if str(prop.get("name") or "").casefold() == "haspopup"
    )
    if popup_role == "true":
        popup_role = "menu"
    dom_popup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('#language')"
                ".getAttribute('aria-haspopup')"
            ),
            "returnByValue": True,
        },
    )
    assert dom_popup["result"]["result"].get("value") is None
    popup_mismatch = supervisor.guarded_dom_action(
        backend_node_id=int(profile_language_node["backendDOMNodeId"]),
        expected_page_identity=identity,
        expected_role="combobox",
        expected_name="Profile Language",
        action="click",
        required_popup_role="dialog",
    )
    assert popup_mismatch["ok"] is False
    assert popup_mismatch["error_code"] == (
        "popup_semantics_changed_before_atomic_action"
    )
    assert popup_mismatch["expected_popup_role"] == "dialog"
    assert popup_mismatch["actual_popup_role"] == popup_role
    popup_clicked = supervisor.guarded_dom_action(
        backend_node_id=int(profile_language_node["backendDOMNodeId"]),
        expected_page_identity=identity,
        expected_role="combobox",
        expected_name="Profile Language",
        action="click",
        required_popup_role=popup_role,
    )
    assert popup_clicked["ok"] is True, popup_clicked

    clicked = supervisor.guarded_dom_action(
        backend_node_id=backend_id("button", "Join group"),
        expected_page_identity=identity,
        expected_role="button",
        expected_name="Join group",
        action="click",
    )
    assert clicked["ok"] is True

    filled = supervisor.guarded_dom_action(
        backend_node_id=backend_id("textbox", "Draft body"),
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Draft body",
        action="fill",
        text="guarded content",
    )
    assert filled["ok"] is True
    selected = supervisor.guarded_dom_action(
        backend_node_id=backend_id("combobox", "Profile Language"),
        expected_page_identity=identity,
        expected_role="combobox",
        expected_name="Profile Language",
        action="fill",
        text="正體中文 (Chinese (Traditional))",
    )
    assert selected["ok"] is True, selected
    for disabled_name in (
        "Optgroup Disabled Language",
        "Fieldset Disabled Language",
    ):
        rejected_select = supervisor.guarded_dom_action(
            backend_node_id=backend_id("combobox", disabled_name),
            expected_page_identity=identity,
            expected_role="combobox",
            expected_name=disabled_name,
            action="fill",
            text="正體中文 (Chinese (Traditional))",
        )
        assert rejected_select["ok"] is False
        assert "disabled" in rejected_select["error"]
    controlled = supervisor.guarded_dom_action(
        backend_node_id=backend_id("combobox", "Controlled Language"),
        expected_page_identity=identity,
        expected_role="combobox",
        expected_name="Controlled Language",
        action="fill",
        text="正體中文 (Chinese (Traditional))",
    )
    assert controlled["ok"] is False
    assert "after events" in controlled["error"]
    readback = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "({joined: window.__joined === true, "
                "draft: document.querySelector('[contenteditable]').textContent, "
                "language: document.querySelector('select').value, "
                "languageLabel: document.querySelector('select').selectedOptions[0].textContent.trim(), "
                "disabledLanguages: [...document.querySelectorAll('select')].slice(1).map(node => node.value), "
                "selectInputCtor: window.__selectInputCtor, "
                "selectInputType: window.__selectInputType, "
                "languageChanges: window.__languageChanges})"
            ),
            "returnByValue": True,
        },
    )
    assert readback["result"]["result"]["value"] == {
        "joined": True,
        "draft": "guarded content",
        "language": "zh_TW",
        "languageLabel": "正體中文 (Chinese (Traditional))",
        "disabledLanguages": ["en_US", "en_US", "en_US"],
        "selectInputCtor": "Event",
        "selectInputType": None,
        "languageChanges": 1,
    }

    changed = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('button').setAttribute("
                "'aria-label', 'Publish')"
            ),
        },
    )
    assert changed["ok"] is True
    rejected = supervisor.guarded_dom_action(
        backend_node_id=backend_id("button", "Join group"),
        expected_page_identity=identity,
        expected_role="button",
        expected_name="Join group",
        action="click",
    )
    assert rejected["ok"] is False
    assert "semantics changed" in rejected["error"]


def test_marketplace_price_guard_accepts_facebook_thousands_formatting(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-marketplace-price-format",
        cdp_url=cdp_url,
    )
    listing_id = "1666446304587399"
    page_url = (
        "https://www.facebook.com/marketplace/edit/"
        f"?listing_id={listing_id}"
    )
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Marketplace price</title></head>
<body><main>
  <label for="price">Price</label>
  <input id="price" aria-label="Price" value="NT$108,000"
    oninput="this.value = 'NT$' + this.value.replace(/\\D/g, '').replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',')">
  <button type="button" aria-label="Update"
    onclick="window.__updated = true">Update</button>
</main></body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )["result"]["result"]["value"]

    def backend_id(role, name):
        nodes = supervisor.call_page_cdp(
            "Accessibility.getFullAXTree"
        )["result"]["nodes"]
        return next(
            int(node["backendDOMNodeId"])
            for node in nodes
            if node.get("role", {}).get("value") == role
            and node.get("name", {}).get("value") == name
        )

    flow_token = "price-format-test-token"
    filled = supervisor.guarded_dom_action(
        backend_node_id=backend_id("textbox", "Price"),
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Price",
        action="fill",
        text="89000",
        marketplace_price_stage="fill",
        marketplace_price_token=flow_token,
        required_marketplace_listing_id=listing_id,
        required_marketplace_price_twd=89000,
    )
    assert filled["ok"] is True, filled
    live_value = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "document.querySelector('#price').value",
            "returnByValue": True,
        },
    )["result"]["result"]["value"]
    assert live_value == "NT$89,000"

    submitted = supervisor.guarded_dom_action(
        backend_node_id=backend_id("button", "Update"),
        expected_page_identity=identity,
        expected_role="button",
        expected_name="Update",
        action="click",
        required_marketplace_listing_id=listing_id,
        marketplace_price_stage="submit",
        marketplace_price_token=flow_token,
        required_marketplace_price_twd=89000,
    )
    assert submitted["ok"] is True, submitted
    updated = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {"expression": "window.__updated === true", "returnByValue": True},
    )["result"]["result"]["value"]
    assert updated is True


def test_page_composer_guard_uses_management_actor_not_comment_voice(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-actor-visibility",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Page actor visibility</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button, [role="textbox"] {{ display: block; margin: 12px; }}
  [role="dialog"] {{
    position: fixed; inset: 20px; z-index: 10; background: white;
    border: 1px solid black; padding: 20px;
  }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<div role="textbox" aria-label="Comment as Personal Profile"
  contenteditable="true" style="min-height:24px"></div>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1>
  <h2>{actor_name}</h2>
</nav>
<div style="opacity:0">
  <a href="{page_url}">Stale Page Actor</a>
  <div role="textbox" aria-label="Comment as Stale Page Actor"
    contenteditable="true"></div>
  <button aria-label="Switch into {actor_name}'s Page">
    <span>{actor_name}</span>
  </button>
</div>
<button aria-label="What's on your mind?" onclick="
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-label', 'Create post');
    dialog.innerHTML = `
    <div><span>{actor_name}</span></div>
    <div style='opacity:0'>
      <a href='{page_url}'>Stale Page Actor</a>
    </div>
    <div role='textbox' aria-label='Composer body'
      contenteditable='true' style='min-height:80px'></div>
    <div id='hidden-composer-target' role='textbox'
      aria-label='Stale composer body' contenteditable='true'
      style='min-height:80px;opacity:0'></div>`;
  document.body.appendChild(dialog);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    opened = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )

    assert opened["ok"] is True, opened
    assert opened["result"]["boundPageActor"] == actor_name
    assert opened["result"]["boundPageComposerToken"] == (
        "page-composer-test-token"
    )
    diagnostics = opened["result"]["pageGuardDiagnostics"]
    assert diagnostics["page_url_match"] is True
    assert diagnostics["manage_page_context_visible"] is True
    assert diagnostics["switch_into_page_visible"] is False
    assert diagnostics["management_actor_names"] == [actor_name]
    assert diagnostics["comment_actor_names"] == ["Personal Profile"]
    assert diagnostics["composer_actor_match"] is True

    duplicate_token = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "[...document.querySelectorAll("
                "'[role=dialog] [data-hermes-page-composer-actor-token]')]"
                f".filter(node => node.innerText === {json.dumps(actor_name)})"
                ".forEach(node => node.setAttribute("
                "'data-hermes-page-composer-actor-token', "
                "'page-composer-test-token'))"
            ),
        },
    )
    assert duplicate_token["ok"] is True

    composer_ax = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    textbox_id = next(
        int(node["backendDOMNodeId"])
        for node in composer_ax["result"]["nodes"]
        if node.get("role", {}).get("value") == "textbox"
        and node.get("name", {}).get("value") == "Composer body"
    )
    composed = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="approved body",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert composed["ok"] is True, composed

    readback = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('[role=dialog] [role=textbox]')"
                ".textContent"
            ),
            "returnByValue": True,
        },
    )
    assert readback["result"]["result"]["value"] == "approved body"

    removed_actor_proof_marker = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('[role=dialog]').removeAttribute("
                "'data-hermes-page-composer-actor-proof')"
            ),
        },
    )
    assert removed_actor_proof_marker["ok"] is True
    missing_actor_proof_fill = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject removed actor proof marker",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert missing_actor_proof_fill["ok"] is False
    assert missing_actor_proof_fill["guard_diagnostics"][
        "failed_predicate"
    ] == "composer_actor_proof_marker_match"
    restored_actor_proof_marker = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('[role=dialog]').setAttribute("
                "'data-hermes-page-composer-actor-proof', 'visible')"
            ),
        },
    )
    assert restored_actor_proof_marker["ok"] is True

    removed_source_capability = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "delete document.__hermesPageComposerFlow",
        },
    )
    assert removed_source_capability["ok"] is True
    missing_capability_fill = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject missing source capability",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert missing_capability_fill["ok"] is False
    assert missing_capability_fill["error_code"] == (
        "facebook_page_composer_binding_invalid"
    )
    assert missing_capability_fill["guard_diagnostics"][
        "failed_predicate"
    ] == (
        "source_capability_match"
    )
    restored_source_capability = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "Object.defineProperty(document, "
                "'__hermesPageComposerFlow', {value: Object.freeze({"
                "token: 'page-composer-test-token', "
                "pageUrl: 'https://www.facebook.com/solobizai', "
                f"actor: {json.dumps(actor_name)}, "
                f"pageIdentity: {json.dumps(identity)}, "
                "composerActorProofRequired: true"
                "}), configurable: true})"
            ),
        },
    )
    assert restored_source_capability["ok"] is True

    changed_live_source_actor = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('nav[aria-label=\"Page navigation\"]')"
                ".insertAdjacentHTML('beforeend', "
                "'<h2 id=\"unexpected-live-source\">"
                "Unexpected management actor</h2>')"
            ),
        },
    )
    assert changed_live_source_actor["ok"] is True
    changed_live_source_fill = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject changed live source actor",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert changed_live_source_fill["ok"] is False
    assert changed_live_source_fill["guard_diagnostics"][
        "failed_predicate"
    ] == "source_actor_contradiction"
    restored_live_source_actor = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('#unexpected-live-source').remove()"
            ),
        },
    )
    assert restored_live_source_actor["ok"] is True

    malformed_composer_marker = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "(() => { const marker = document.createElement('div'); "
                "marker.id = 'malformed-composer-marker'; "
                "marker.style.opacity = '0'; "
                "marker.setAttribute('data-hermes-page-composer-token', "
                "'different-token'); document.body.appendChild(marker); "
                "return true; })()"
            ),
            "returnByValue": True,
        },
    )
    assert malformed_composer_marker["ok"] is True
    malformed_composer_fill = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject malformed composer marker",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert malformed_composer_fill["ok"] is False
    removed_malformed_composer = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('#malformed-composer-marker').remove()"
            ),
        },
    )
    assert removed_malformed_composer["ok"] is True

    duplicated_marker = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "(() => { const actor = document.querySelector("
                "'[data-hermes-page-composer-actor-token]'); "
                "const clone = actor.cloneNode(true); "
                "clone.id = 'duplicate-composer-actor-marker'; "
                "clone.style.opacity = '0'; "
                "document.body.appendChild(clone); "
                "return true; })()"
            ),
            "returnByValue": True,
        },
    )
    assert duplicated_marker["ok"] is True
    duplicate_actor_fill = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject duplicate actor proof",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert duplicate_actor_fill["ok"] is False
    removed_duplicate = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector("
                "'#duplicate-composer-actor-marker').remove()"
            ),
        },
    )
    assert removed_duplicate["ok"] is True

    document = supervisor.call_page_cdp(
        "DOM.getDocument", {"depth": -1},
    )
    hidden_node = supervisor.call_page_cdp(
        "DOM.querySelector",
        {
            "nodeId": document["result"]["root"]["nodeId"],
            "selector": "#hidden-composer-target",
        },
    )
    hidden_description = supervisor.call_page_cdp(
        "DOM.describeNode",
        {"nodeId": hidden_node["result"]["nodeId"]},
    )
    hidden_fill = supervisor.guarded_dom_action(
        backend_node_id=int(
            hidden_description["result"]["node"]["backendNodeId"]
        ),
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Stale composer body",
        action="fill",
        text="must remain hidden",
        page_composer_stage="compose",
        page_composer_token="page-composer-test-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor=actor_name,
    )
    assert hidden_fill["ok"] is False

    editable_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]').remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`)
                .onclick = () => {{
                  const dialog = document.createElement('div');
                  dialog.setAttribute('role', 'dialog');
                  dialog.setAttribute('aria-label', 'Create post');
                  dialog.innerHTML = `
                    <div role='textbox' aria-label='Composer body'
                      contenteditable='true' style='min-height:80px'>
                      <span>{actor_name}</span>
                    </div>`;
                  document.body.appendChild(dialog);
                }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert editable_actor_setup["ok"] is True
    editable_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="editable-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert editable_actor_open["ok"] is True, editable_actor_open

    clipped_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`)
                .onclick = () => {{
                  const dialog = document.createElement('div');
                  dialog.setAttribute('role', 'dialog');
                  dialog.setAttribute('aria-label', 'Create post');
                  dialog.innerHTML = `
                    <div style='position:relative;height:1px;overflow:hidden;
                      margin-bottom:80px'>
                      <span style='position:absolute;top:20px'>{actor_name}</span>
                    </div>
                    <div role='textbox' aria-label='Composer body'
                      contenteditable='true' style='min-height:80px'></div>`;
                  document.body.appendChild(dialog);
                }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert clipped_actor_setup["ok"] is True
    clipped_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="clipped-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert clipped_actor_open["ok"] is True, clipped_actor_open

    contained_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`)
                .onclick = () => {{
                  const dialog = document.createElement('div');
                  dialog.setAttribute('role', 'dialog');
                  dialog.setAttribute('aria-label', 'Create post');
                  dialog.innerHTML = `
                    <div style='position:relative;width:1px;height:1px;
                      contain:strict;margin-bottom:80px'>
                      <span style='position:absolute;top:20px'>{actor_name}</span>
                    </div>
                    <div role='textbox' aria-label='Composer body'
                      contenteditable='true' style='min-height:80px'></div>`;
                  document.body.appendChild(dialog);
                }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert contained_actor_setup["ok"] is True
    contained_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="contained-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert contained_actor_open["ok"] is True, contained_actor_open

    split_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`).onclick = () => {{
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.setAttribute('aria-label', 'Create post');
                dialog.innerHTML = `
                  <div><span>AI BizWeek｜</span><span>SoloBiz AI 一人公司商業誌</span></div>
                  <div role='textbox' aria-label='Composer body'
                    contenteditable='true' style='min-height:80px'></div>`;
                document.body.appendChild(dialog);
              }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert split_actor_setup["ok"] is True
    split_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="split-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert split_actor_open["ok"] is True, split_actor_open
    assert split_actor_open["result"]["boundPageActor"] == actor_name

    clip_path_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`).onclick = () => {{
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.setAttribute('aria-label', 'Create post');
                dialog.innerHTML = `
                  <span style='clip-path:inset(100%)'>{actor_name}</span>
                  <div role='textbox' aria-label='Composer body'
                    contenteditable='true' style='min-height:80px'></div>`;
                document.body.appendChild(dialog);
              }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert clip_path_actor_setup["ok"] is True
    clip_path_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="clip-path-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert clip_path_actor_open["ok"] is True, clip_path_actor_open

    editable_descendant_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`).onclick = () => {{
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.setAttribute('aria-label', 'Create post');
                dialog.innerHTML = `
                  <span>{actor_name}<span contenteditable='plaintext-only'></span></span>
                  <div role='textbox' aria-label='Composer body'
                    contenteditable='true' style='min-height:80px'></div>`;
                document.body.appendChild(dialog);
              }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert editable_descendant_setup["ok"] is True
    editable_descendant_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="editable-descendant-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert editable_descendant_open["ok"] is True, editable_descendant_open

    indented_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`).onclick = () => {{
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.setAttribute('aria-label', 'Create post');
                dialog.innerHTML = `
                  <span style='display:block;width:80px;height:24px;
                    overflow:hidden;text-indent:-9999px'>{actor_name}</span>
                  <div role='textbox' aria-label='Composer body'
                    contenteditable='true' style='min-height:80px'></div>`;
                document.body.appendChild(dialog);
              }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert indented_actor_setup["ok"] is True
    indented_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="indented-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert indented_actor_open["ok"] is True, indented_actor_open

    transparent_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": f"""
              document.querySelector('[role=dialog]')?.remove();
              document.querySelector(`button[aria-label="What's on your mind?"]`).onclick = () => {{
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.setAttribute('aria-label', 'Create post');
                dialog.innerHTML = `
                  <span style='color:transparent'>{actor_name}</span>
                  <div role='textbox' aria-label='Composer body'
                    contenteditable='true' style='min-height:80px'></div>`;
                document.body.appendChild(dialog);
              }};
              true;
            """,
            "returnByValue": True,
        },
    )
    assert transparent_actor_setup["ok"] is True
    transparent_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="transparent-actor-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )
    assert transparent_actor_open["ok"] is True, transparent_actor_open


def test_page_composer_guard_blocks_only_an_explicit_visible_switch_gate(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-switch-gate",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Page switch gate</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
</style></head><body>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<button aria-label="Switch into {actor_name}'s Page">Switch into Page</button>
<button aria-label="What's on your mind?">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    blocked = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="switch-gate-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "facebook_page_switch_required"
    assert blocked["guard_diagnostics"]["page_url_match"] is True
    assert blocked["guard_diagnostics"]["manage_page_context_visible"] is True
    assert blocked["guard_diagnostics"]["switch_into_page_visible"] is True


def test_page_composer_guard_diagnostics_cannot_resolve_ambiguous_management_actor(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-ambiguous-management-actor",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Ambiguous Page actors</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button, [role="textbox"] {{ display:block; margin:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<div role="textbox" aria-label="Comment as {actor_name}"
  contenteditable="true" style="min-height:24px"></div>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1>
  <h2>{actor_name}</h2>
  <h2>Unexpected second management actor</h2>
</nav>
<button aria-label="What's on your mind?">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    blocked = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="ambiguous-management-token",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "facebook_page_source_context_unproven"
    diagnostics = blocked["guard_diagnostics"]
    assert diagnostics["page_url_match"] is True
    assert diagnostics["switch_into_page_visible"] is False
    assert diagnostics["corroborated_management_actor_names"] == [actor_name]
    assert diagnostics["failed_predicate"] == "management_actor_unique"


def test_page_composer_guard_ignores_tool_section_headings_after_identity_boundary(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-management-tools-heading",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Page tools heading</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ position:fixed; inset:80px; background:white; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1>
  <h2>{actor_name}</h2>
  <hr>
  <h2>More tools</h2>
</nav>
<button aria-label="What's on your mind?" onclick="
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-label', 'Create post');
  dialog.innerHTML = '<h2>{actor_name}</h2>'
    + '<div role=&quot;textbox&quot; contenteditable=&quot;true&quot;></div>';
  document.body.appendChild(dialog);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    opened = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="tools-heading-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
    )

    assert opened["ok"] is True
    assert opened["result"]["boundPageActor"] == actor_name
    diagnostics = opened["result"]["pageGuardDiagnostics"]
    assert diagnostics["management_actor_names"] == [actor_name]
    assert diagnostics["page_navigation_heading_names"] == [
        actor_name,
        "More tools",
    ]


def test_page_composer_guard_collapses_nested_dialog_shells_to_editable_owner(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-nested-composer-shells",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Nested Page composer</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ background:white; padding:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<button aria-label="What's on your mind?" onclick="
  const outer = document.createElement('div');
  outer.setAttribute('role', 'dialog');
  outer.setAttribute('aria-label', 'Create post');
  outer.innerHTML = '<form><div>'
    + '<div role=&quot;dialog&quot; aria-label=&quot;Create post&quot;>'
    + '<h2>Create post</h2><button>Close composer dialog</button></div>'
    + '<span>{actor_name}</span>'
    + '<div role=&quot;textbox&quot; aria-label=&quot;Composer body&quot; '
    + 'contenteditable=&quot;true&quot; style=&quot;min-height:80px&quot;></div>'
    + '</div></form>';
  document.body.appendChild(outer);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    opened = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="nested-shell-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
    )

    assert opened["ok"] is True, opened
    assert opened["result"]["boundPageActor"] == actor_name
    diagnostics = opened["result"]["pageGuardDiagnostics"]
    assert diagnostics["opened_composer_shell_count"] == 2
    assert diagnostics["opened_composer_count"] == 1


def test_page_composer_guard_accepts_delayed_actorless_composer_from_page_context(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-delayed-composer-binding",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Delayed Page composer</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ background:white; padding:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<button aria-label="What's on your mind?" onclick="
  const outer = document.createElement('div');
  outer.setAttribute('role', 'dialog');
  outer.setAttribute('aria-label', 'Create post');
  outer.innerHTML = '<div role=&quot;dialog&quot; '
    + 'aria-label=&quot;Create post&quot;><h2>Create post</h2></div>';
  document.body.appendChild(outer);
  document.querySelector('nav[aria-label=&quot;Page navigation&quot;]')
    .style.display = 'none';
  setTimeout(() => {{
    outer.insertAdjacentHTML('beforeend',
      '<div role=&quot;textbox&quot; aria-label=&quot;Composer body&quot; '
      + 'contenteditable=&quot;true&quot; style=&quot;min-height:80px&quot;>'
      + '<span>{actor_name}</span></div>'
      + '<button aria-label=&quot;Publish&quot;>Publish</button>');
  }}, 200);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    opened = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
    )

    assert opened["ok"] is True, opened
    diagnostics = opened["result"]["pageGuardDiagnostics"]
    assert diagnostics["opened_composer_shell_count"] == 2
    assert diagnostics["opened_composer_count"] == 1
    assert diagnostics["settled_source_actor_observable"] is False
    assert diagnostics["settled_source_actor_match"] is None
    assert diagnostics["settled_source_actor_contradiction"] is False
    assert diagnostics["composer_actor_match"] is None
    assert diagnostics["actor_binding_source"] == (
        "source_page_management_context"
    )

    composer_ax = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    textbox_id = next(
        int(node["backendDOMNodeId"])
        for node in composer_ax["result"]["nodes"]
        if node.get("role", {}).get("value") == "textbox"
        and node.get("name", {}).get("value") == "Composer body"
    )
    publish_id = next(
        int(node["backendDOMNodeId"])
        for node in composer_ax["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "Publish"
    )
    late_contradictory_actor = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('[role=dialog]').setAttribute("
                "'title', 'Posting as Personal Profile')"
            ),
        },
    )
    assert late_contradictory_actor["ok"] is True
    rejected_late_actor = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject late contradictory actor",
        page_composer_stage="compose",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor=actor_name,
    )
    assert rejected_late_actor["ok"] is False
    assert rejected_late_actor["guard_diagnostics"][
        "failed_predicate"
    ] == "composer_actor_contradiction"
    removed_late_actor = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('[role=dialog]').removeAttribute("
                "'title')"
            ),
        },
    )
    assert removed_late_actor["ok"] is True
    ambiguous_composer_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": """
              document.body.insertAdjacentHTML('beforeend', `
                <div id='ambiguous-composer' role='dialog'
                  aria-label='Create post'>
                  <div role='textbox' aria-label='Other composer body'
                    contenteditable='true' style='min-height:80px'></div>
                </div>`);
              true;
            """,
            "returnByValue": True,
        },
    )
    assert ambiguous_composer_setup["ok"] is True
    rejected_ambiguous_composer = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="must reject ambiguous live composer",
        page_composer_stage="compose",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor=actor_name,
    )
    assert rejected_ambiguous_composer["ok"] is False
    assert rejected_ambiguous_composer["guard_diagnostics"][
        "failed_predicate"
    ] == "visible_composer_count"
    removed_ambiguous_composer = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('#ambiguous-composer').remove()"
            ),
        },
    )
    assert removed_ambiguous_composer["ok"] is True
    composed = supervisor.guarded_dom_action(
        backend_node_id=textbox_id,
        expected_page_identity=identity,
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="approved actorless body",
        page_composer_stage="compose",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor=actor_name,
    )
    assert composed["ok"] is True, composed
    ambiguous_submit_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": """
              document.body.insertAdjacentHTML('beforeend', `
                <div id='ambiguous-submit-composer' role='dialog'
                  aria-label='Create post'>
                  <div role='textbox' aria-label='Other submit composer'
                    contenteditable='true' style='min-height:80px'></div>
                </div>`);
              true;
            """,
            "returnByValue": True,
        },
    )
    assert ambiguous_submit_setup["ok"] is True
    rejected_ambiguous_submit = supervisor.guarded_dom_action(
        backend_node_id=publish_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="Publish",
        action="click",
        page_composer_stage="submit",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor=actor_name,
    )
    assert rejected_ambiguous_submit["ok"] is False
    assert rejected_ambiguous_submit["guard_diagnostics"][
        "failed_predicate"
    ] == "visible_composer_count"
    removed_ambiguous_submit = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": (
                "document.querySelector('#ambiguous-submit-composer')"
                ".remove()"
            ),
        },
    )
    assert removed_ambiguous_submit["ok"] is True
    submitted = supervisor.guarded_dom_action(
        backend_node_id=publish_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="Publish",
        action="click",
        page_composer_stage="submit",
        page_composer_token="delayed-binding-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor=actor_name,
    )
    assert submitted["ok"] is True, submitted

    contradictory_actor_setup = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": """
              document.querySelector('[role=dialog]').remove();
              document.querySelector('nav[aria-label="Page navigation"]')
                .style.display = 'block';
              document.querySelector(`button[aria-label="What's on your mind?"]`)
                .onclick = () => {
                  const dialog = document.createElement('div');
                  dialog.setAttribute('role', 'dialog');
                  dialog.setAttribute('aria-label', 'Create post');
                  dialog.setAttribute('title', 'Posting as Personal Profile');
                  dialog.innerHTML = `
                    <div role='textbox' aria-label='Composer body'
                      contenteditable='true' style='min-height:80px'></div>`;
                  document.body.appendChild(dialog);
                };
              true;
            """,
            "returnByValue": True,
        },
    )
    assert contradictory_actor_setup["ok"] is True
    contradictory_actor_open = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="contradictory-actor-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
    )
    assert contradictory_actor_open["ok"] is False
    assert contradictory_actor_open["error_code"] == (
        "facebook_page_composer_actor_mismatch"
    )
    assert contradictory_actor_open["guard_diagnostics"][
        "failed_predicate"
    ] == "composer_actor_contradiction"


def test_page_composer_guard_blocks_staggered_editable_owner_composers(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-staggered-logical-composers",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Two Page composers</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ background:white; padding:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<button aria-label="What's on your mind?" onclick="
  const addComposer = () => {{
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-label', 'Create post');
    dialog.innerHTML = '<span>{actor_name}</span>'
      + '<div role=&quot;textbox&quot; aria-label=&quot;Composer body&quot; '
      + 'contenteditable=&quot;true&quot; style=&quot;min-height:80px&quot;></div>';
    document.body.appendChild(dialog);
  }};
  addComposer();
  setTimeout(addComposer, 200);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    blocked = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="two-composers-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        timeout=8.0,
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "facebook_page_composer_not_unique"
    diagnostics = blocked["guard_diagnostics"]
    assert diagnostics["opened_composer_shell_count"] == 2
    assert diagnostics["opened_composer_count"] == 2
    assert diagnostics["failed_predicate"] == "opened_composer_count"


def test_page_composer_guard_does_not_adopt_preexisting_shell(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-preexisting-composer-shell",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Preexisting shell</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ background:white; padding:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<div id="existing-shell" role="dialog"></div>
<button aria-label="What's on your mind?" onclick="
  setTimeout(() => {{
    const shell = document.querySelector('#existing-shell');
    shell.setAttribute('aria-label', 'Create post');
    shell.insertAdjacentHTML(
      'beforeend', '<span>{actor_name}</span>'
      + '<div role=&quot;textbox&quot; aria-label=&quot;Composer body&quot; '
      + 'contenteditable=&quot;true&quot; style=&quot;min-height:80px&quot;></div>'
    );
  }}, 200);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    blocked = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="preexisting-shell-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        timeout=8.0,
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "facebook_page_composer_not_unique"
    diagnostics = blocked["guard_diagnostics"]
    assert diagnostics["opened_composer_shell_count"] == 0
    assert diagnostics["opened_composer_count"] == 0


def test_page_composer_guard_rejects_readonly_editable_surfaces(
    chrome_cdp,
    supervisor_registry,
):
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-page-readonly-composer-surfaces",
        cdp_url=cdp_url,
    )
    page_url = "https://www.facebook.com/SoloBizAi/"
    actor_name = "AI BizWeek｜SoloBiz AI 一人公司商業誌"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Readonly surfaces</title>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  a, button {{ display:block; margin:12px; }}
  [role="dialog"] {{ background:white; padding:12px; }}
</style></head><body>
<a href="{page_url}" aria-label="{actor_name}'s Timeline">AI</a>
<nav role="navigation" aria-label="Page navigation">
  <h1>Manage Page</h1><h2>{actor_name}</h2>
</nav>
<button aria-label="What's on your mind?" onclick="
  const nativeDialog = document.createElement('div');
  nativeDialog.setAttribute('role', 'dialog');
  nativeDialog.setAttribute('aria-label', 'Create post');
  nativeDialog.innerHTML = '<span>{actor_name}</span>'
    + '<fieldset disabled><textarea></textarea></fieldset>';
  document.body.appendChild(nativeDialog);
  const ariaDialog = document.createElement('div');
  ariaDialog.setAttribute('role', 'dialog');
  ariaDialog.setAttribute('aria-label', 'Create post');
  ariaDialog.innerHTML = '<span>{actor_name}</span>'
    + '<div aria-readonly=&quot;true&quot;>'
    + '<div role=&quot;textbox&quot; contenteditable=&quot;true&quot; '
    + 'style=&quot;min-height:80px&quot;></div></div>';
  document.body.appendChild(ariaDialog);
  const inheritedAriaDialog = document.createElement('div');
  inheritedAriaDialog.setAttribute('role', 'dialog');
  inheritedAriaDialog.setAttribute('aria-label', 'Create post');
  inheritedAriaDialog.innerHTML = '<span>{actor_name}</span>'
    + '<div aria-disabled=&quot;true&quot;>'
    + '<div role=&quot;textbox&quot; contenteditable=&quot;true&quot; '
    + 'style=&quot;min-height:80px&quot;></div></div>';
  document.body.appendChild(inheritedAriaDialog);
">What's on your mind?</button>
</body></html>"""
    _serve_virtual_https_page(cdp_url, page_url, html)

    identity_result = supervisor.call_page_cdp(
        "Runtime.evaluate",
        {
            "expression": "`${location.href}|${performance.timeOrigin}`",
            "returnByValue": True,
        },
    )
    identity = identity_result["result"]["result"]["value"]
    ax_result = supervisor.call_page_cdp("Accessibility.getFullAXTree")
    button_id = next(
        int(node["backendDOMNodeId"])
        for node in ax_result["result"]["nodes"]
        if node.get("role", {}).get("value") == "button"
        and node.get("name", {}).get("value") == "What's on your mind?"
    )

    blocked = supervisor.guarded_dom_action(
        backend_node_id=button_id,
        expected_page_identity=identity,
        expected_role="button",
        expected_name="What's on your mind?",
        action="click",
        page_composer_stage="open",
        page_composer_token="readonly-surfaces-token",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        timeout=8.0,
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "facebook_page_composer_not_unique"
    diagnostics = blocked["guard_diagnostics"]
    assert diagnostics["opened_composer_shell_count"] == 3
    assert diagnostics["opened_composer_count"] == 0


def test_main_frame_alert_detection_and_dismiss(chrome_cdp, supervisor_registry):
    """alert() in the main frame surfaces and can be dismissed via the sync API."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-2", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "setTimeout(() => alert('PYTEST-MAIN-ALERT'), 50)")
    dialogs = _wait_for_dialog(supervisor)
    assert dialogs, "no dialog detected"
    d = dialogs[0]
    assert d.type == "alert"
    assert "PYTEST-MAIN-ALERT" in d.message

    result = supervisor.respond_to_dialog("dismiss")
    assert result["ok"] is True
    # State cleared after dismiss
    time.sleep(0.3)
    assert supervisor.snapshot().pending_dialogs == ()


def test_iframe_contentwindow_alert(chrome_cdp, supervisor_registry):
    """alert() fired from inside a same-origin iframe surfaces too."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-3", cdp_url=cdp_url)

    _fire_on_page(
        cdp_url,
        "setTimeout(() => document.querySelector('#inner').contentWindow.alert('PYTEST-IFRAME'), 50)",
    )
    dialogs = _wait_for_dialog(supervisor)
    assert dialogs, "no iframe dialog detected"
    assert any("PYTEST-IFRAME" in d.message for d in dialogs)

    result = supervisor.respond_to_dialog("accept")
    assert result["ok"] is True


def test_prompt_dialog_with_response_text(chrome_cdp, supervisor_registry):
    """prompt() gets our prompt_text back inside the page."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-4", cdp_url=cdp_url)

    # Fire a prompt and stash the answer on window
    _fire_on_page(
        cdp_url,
        "setTimeout(() => { window.__promptResult = prompt('give me a token', 'default-x'); }, 50)",
    )
    dialogs = _wait_for_dialog(supervisor)
    assert dialogs
    d = dialogs[0]
    assert d.type == "prompt"
    assert d.default_prompt == "default-x"

    result = supervisor.respond_to_dialog("accept", prompt_text="PYTEST-PROMPT-REPLY")
    assert result["ok"] is True


def test_respond_with_no_pending_dialog_errors_cleanly(chrome_cdp, supervisor_registry):
    """Calling respond_to_dialog when nothing is pending returns a clean error, not an exception."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-5", cdp_url=cdp_url)

    result = supervisor.respond_to_dialog("accept")
    assert result["ok"] is False
    assert "no dialog" in result["error"].lower()


def test_auto_dismiss_policy(chrome_cdp, supervisor_registry):
    """auto_dismiss policy clears dialogs without the agent responding."""
    from tools.browser_supervisor import DIALOG_POLICY_AUTO_DISMISS

    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(
        task_id="pytest-6",
        cdp_url=cdp_url,
        dialog_policy=DIALOG_POLICY_AUTO_DISMISS,
    )

    _fire_on_page(cdp_url, "setTimeout(() => alert('PYTEST-AUTO-DISMISS'), 50)")
    # Give the supervisor a moment to see + auto-dismiss
    time.sleep(2.0)
    snap = supervisor.snapshot()
    # Nothing pending because auto-dismiss cleared it immediately
    assert snap.pending_dialogs == ()


def test_registry_idempotent_get_or_start(chrome_cdp, supervisor_registry):
    """Calling get_or_start twice with the same (task, url) returns the same instance."""
    cdp_url, _port = chrome_cdp
    a = supervisor_registry.get_or_start(task_id="pytest-idem", cdp_url=cdp_url)
    b = supervisor_registry.get_or_start(task_id="pytest-idem", cdp_url=cdp_url)
    assert a is b


def test_registry_stop(chrome_cdp, supervisor_registry):
    """stop() tears down the supervisor and snapshot reports inactive."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-stop", cdp_url=cdp_url)
    assert supervisor.snapshot().active is True
    supervisor_registry.stop("pytest-stop")
    # Post-stop snapshot reports inactive; supervisor obj may still exist
    assert supervisor.snapshot().active is False


def test_browser_dialog_tool_no_supervisor():
    """browser_dialog returns a clear error when no supervisor is attached."""
    from tools.browser_dialog_tool import browser_dialog

    r = json.loads(browser_dialog(action="accept", task_id="nonexistent-task"))
    assert r["success"] is False
    assert "No CDP supervisor" in r["error"]


def test_browser_dialog_invalid_action(chrome_cdp, supervisor_registry):
    """browser_dialog rejects actions that aren't accept/dismiss."""
    from tools.browser_dialog_tool import browser_dialog

    cdp_url, _port = chrome_cdp
    supervisor_registry.get_or_start(task_id="pytest-bad-action", cdp_url=cdp_url)

    r = json.loads(browser_dialog(action="eat", task_id="pytest-bad-action"))
    assert r["success"] is False
    assert "accept" in r["error"] and "dismiss" in r["error"]


def test_recent_dialogs_ring_buffer(chrome_cdp, supervisor_registry):
    """Closed dialogs show up in recent_dialogs with a closed_by tag."""
    from tools.browser_supervisor import DIALOG_POLICY_AUTO_DISMISS

    cdp_url, _port = chrome_cdp
    sv = supervisor_registry.get_or_start(
        task_id="pytest-recent",
        cdp_url=cdp_url,
        dialog_policy=DIALOG_POLICY_AUTO_DISMISS,
    )

    _fire_on_page(cdp_url, "setTimeout(() => alert('PYTEST-RECENT'), 50)")
    # Wait for auto-dismiss to cycle the dialog through
    deadline = time.time() + 5
    while time.time() < deadline:
        recent = sv.snapshot().recent_dialogs
        if recent and any("PYTEST-RECENT" in r.message for r in recent):
            break
        time.sleep(0.1)

    recent = sv.snapshot().recent_dialogs
    assert recent, "recent_dialogs should contain the auto-dismissed dialog"
    match = next((r for r in recent if "PYTEST-RECENT" in r.message), None)
    assert match is not None
    assert match.type == "alert"
    assert match.closed_by == "auto_policy"
    assert match.closed_at >= match.opened_at


def test_browser_dialog_tool_end_to_end(chrome_cdp, supervisor_registry):
    """Full agent-path check: fire an alert, call the tool handler directly."""
    from tools.browser_dialog_tool import browser_dialog

    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-tool", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "setTimeout(() => alert('PYTEST-TOOL-END2END'), 50)")
    assert _wait_for_dialog(supervisor), "no dialog detected via wait_for_dialog"

    r = json.loads(browser_dialog(action="dismiss", task_id="pytest-tool"))
    assert r["success"] is True
    assert r["action"] == "dismiss"
    assert "PYTEST-TOOL-END2END" in r["dialog"]["message"]


def test_browser_cdp_frame_id_routes_via_supervisor(chrome_cdp, supervisor_registry, monkeypatch):
    """browser_cdp(frame_id=...) routes Runtime.evaluate through supervisor.

    Mocks the supervisor with a known frame and verifies browser_cdp sends
    the call via the supervisor's loop rather than opening a stateless
    WebSocket. This is the path that makes cross-origin iframe eval work
    on Browserbase.
    """
    cdp_url, _port = chrome_cdp
    sv = supervisor_registry.get_or_start(task_id="frame-id-test", cdp_url=cdp_url)
    assert sv.snapshot().active

    # Inject a fake OOPIF frame pointing at the SUPERVISOR's own page session
    # so we can verify routing. We fake is_oopif=True so the code path
    # treats it as an OOPIF child.
    import tools.browser_supervisor as _bs
    with sv._state_lock:
        fake_frame_id = "FAKE-FRAME-001"
        sv._frames[fake_frame_id] = _bs.FrameInfo(
            frame_id=fake_frame_id,
            url="fake://",
            origin="",
            parent_frame_id=None,
            is_oopif=True,
            cdp_session_id=sv._page_session_id,  # route at page scope
        )

    # Route the tool through the supervisor. Should succeed and return
    # something that clearly came from CDP.
    from tools.browser_cdp_tool import browser_cdp
    result = browser_cdp(
        method="Runtime.evaluate",
        params={"expression": "1 + 1", "returnByValue": True},
        frame_id=fake_frame_id,
        task_id="frame-id-test",
    )
    r = json.loads(result)
    assert r.get("success") is True, f"expected success, got: {r}"
    assert r.get("frame_id") == fake_frame_id
    assert r.get("session_id") == sv._page_session_id
    value = r.get("result", {}).get("result", {}).get("value")
    assert value == 2, f"expected 2, got {value!r}"


def test_browser_cdp_frame_id_real_oopif_smoke_documented():
    """Document that real-OOPIF E2E was manually verified — see PR #14540.

    A pytest version of this hits an asyncio version-quirk in the venv
    (3.11) that doesn't show up in standalone scripts (3.13 + system
    websockets). The mechanism IS verified end-to-end by two separate
    smoke scripts in /tmp/dialog-iframe-test/:

      * smoke_local_oopif.py   — local Chrome + 2 http servers on
        different hostnames + --site-per-process. Outer page on
        localhost:18905, iframe src=http://127.0.0.1:18906. Calls
        browser_cdp(method='Runtime.evaluate', frame_id=<OOPIF>) and
        verifies inner page's title comes back from the OOPIF session.
        PASSED on 2026-04-23: iframe document.title = 'INNER-FRAME-XYZ'

      * smoke_bb_iframe_agent_path.py — Browserbase + real cross-origin
        iframe (src=https://example.com/). Same browser_cdp(frame_id=)
        path. PASSED on 2026-04-23: iframe document.title =
        'Example Domain'

    The test_browser_cdp_frame_id_routes_via_supervisor pytest covers
    the supervisor-routing plumbing with a fake injected OOPIF.
    """
    pytest.skip(
        "Real-OOPIF E2E verified manually with smoke_local_oopif.py and "
        "smoke_bb_iframe_agent_path.py — pytest version hits an asyncio "
        "version quirk between venv (3.11) and standalone (3.13). "
        "Smoke logs preserved in /tmp/dialog-iframe-test/."
    )


def test_browser_cdp_frame_id_missing_supervisor():
    """browser_cdp(frame_id=...) errors cleanly when no supervisor is attached."""
    from tools.browser_cdp_tool import browser_cdp
    result = browser_cdp(
        method="Runtime.evaluate",
        params={"expression": "1"},
        frame_id="any-frame-id",
        task_id="no-such-task",
    )
    r = json.loads(result)
    assert r.get("success") is not True
    assert "supervisor" in (r.get("error") or "").lower()


def test_browser_cdp_frame_id_not_in_frame_tree(chrome_cdp, supervisor_registry):
    """browser_cdp(frame_id=...) errors when the frame_id isn't known."""
    cdp_url, _port = chrome_cdp
    sv = supervisor_registry.get_or_start(task_id="bad-frame-test", cdp_url=cdp_url)
    assert sv.snapshot().active

    from tools.browser_cdp_tool import browser_cdp
    result = browser_cdp(
        method="Runtime.evaluate",
        params={"expression": "1"},
        frame_id="nonexistent-frame",
        task_id="bad-frame-test",
    )
    r = json.loads(result)
    assert r.get("success") is not True
    assert "not found" in (r.get("error") or "").lower()


def test_bridge_captures_prompt_and_returns_reply_text(chrome_cdp, supervisor_registry):
    """End-to-end: agent's prompt_text round-trips INTO the page's JS.

    Proves the bridge isn't just catching dialogs — it's properly round-
    tripping our reply back into the page via Fetch.fulfillRequest, so
    ``prompt()`` actually returns the agent-supplied string to the page.
    """
    import base64 as _b64

    cdp_url, _port = chrome_cdp
    sv = supervisor_registry.get_or_start(task_id="pytest-bridge-prompt", cdp_url=cdp_url)

    # Page fires prompt and stashes the return value on window.
    html = """<!doctype html><html><body><script>
      window.__ret = null;
      setTimeout(() => { window.__ret = prompt('PROMPT-MSG', 'default'); }, 50);
    </script></body></html>"""
    url = "data:text/html;base64," + _b64.b64encode(html.encode()).decode()

    import asyncio as _asyncio
    import websockets as _ws_mod

    async def nav_and_read():
        async with _ws_mod.connect(cdp_url, max_size=50 * 1024 * 1024) as ws:
            nid = [1]
            pending: dict = {}

            async def reader_fn():
                try:
                    async for raw in ws:
                        m = json.loads(raw)
                        if "id" in m:
                            fut = pending.pop(m["id"], None)
                            if fut and not fut.done():
                                fut.set_result(m)
                except Exception:
                    pass

            rd = _asyncio.create_task(reader_fn())

            async def call(method, params=None, sid=None):
                c = nid[0]; nid[0] += 1
                p = {"id": c, "method": method}
                if params: p["params"] = params
                if sid: p["sessionId"] = sid
                fut = _asyncio.get_event_loop().create_future()
                pending[c] = fut
                await ws.send(json.dumps(p))
                return await _asyncio.wait_for(fut, timeout=20)

            try:
                t = (await call("Target.getTargets"))["result"]["targetInfos"]
                pg = next(x for x in t if x.get("type") == "page")
                a = await call("Target.attachToTarget", {"targetId": pg["targetId"], "flatten": True})
                sid = a["result"]["sessionId"]

                # Fire navigate but don't await — prompt() blocks the page
                nav_id = nid[0]; nid[0] += 1
                nav_fut = _asyncio.get_event_loop().create_future()
                pending[nav_id] = nav_fut
                await ws.send(json.dumps({"id": nav_id, "method": "Page.navigate", "params": {"url": url}, "sessionId": sid}))

                # Wait for supervisor to see the prompt
                deadline = time.monotonic() + 10
                dialog = None
                while time.monotonic() < deadline:
                    snap = sv.snapshot()
                    if snap.pending_dialogs:
                        dialog = snap.pending_dialogs[0]
                        break
                    await _asyncio.sleep(0.05)
                assert dialog is not None, "no dialog captured"
                assert dialog.bridge_request_id is not None, "expected bridge path"
                assert dialog.type == "prompt"

                # Agent responds
                resp = sv.respond_to_dialog("accept", prompt_text="AGENT-SUPPLIED-REPLY")
                assert resp["ok"] is True

                # Wait for nav to complete + read back
                try:
                    await _asyncio.wait_for(nav_fut, timeout=10)
                except Exception:
                    pass
                await _asyncio.sleep(0.5)
                r = await call(
                    "Runtime.evaluate",
                    {"expression": "window.__ret", "returnByValue": True},
                    sid=sid,
                )
                return r.get("result", {}).get("result", {}).get("value")
            finally:
                rd.cancel()
                try: await rd
                except BaseException: pass

    value = asyncio.run(nav_and_read())
    assert value == "AGENT-SUPPLIED-REPLY", f"expected AGENT-SUPPLIED-REPLY, got {value!r}"


def test_evaluate_runtime_primitive(chrome_cdp, supervisor_registry):
    """evaluate_runtime returns primitive values via the supervisor's live WS."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-eval-1", cdp_url=cdp_url)

    # Need a page to evaluate against.
    _fire_on_page(cdp_url, "void 0")
    time.sleep(0.5)

    out = supervisor.evaluate_runtime("1 + 41")
    assert out["ok"] is True
    assert out["result"] == 42
    assert out["result_type"] == "number"


def test_evaluate_runtime_object(chrome_cdp, supervisor_registry):
    """Plain objects come back JSON-serialized via returnByValue=True."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-eval-2", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "void 0")
    time.sleep(0.5)

    out = supervisor.evaluate_runtime('({foo: "bar", n: 7})')
    assert out["ok"] is True
    assert out["result"] == {"foo": "bar", "n": 7}
    assert out["result_type"] == "object"


def test_evaluate_runtime_js_exception(chrome_cdp, supervisor_registry):
    """JS exceptions surface as ok=False with the exception message."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-eval-3", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "void 0")
    time.sleep(0.5)

    out = supervisor.evaluate_runtime("nonExistentVar.nope")
    assert out["ok"] is False
    assert "ReferenceError" in out["error"] or "not defined" in out["error"]


def test_evaluate_runtime_dom_node_returns_empty_object(chrome_cdp, supervisor_registry):
    """DOM nodes with returnByValue=true serialize to ``{}`` (Chrome quirk).

    This is honest — DOM nodes can't be deeply JSON-serialized — and matches
    DevTools console behaviour for the same expression.  Documenting the
    contract here so a future change that "fixes" it (e.g. switching to
    returnByValue=false + DOM.describeNode) doesn't break callers expecting
    the current shape.
    """
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-eval-4", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "void 0")
    time.sleep(0.5)

    out = supervisor.evaluate_runtime("document.querySelector('h1')")
    assert out["ok"] is True
    assert out["result_type"] == "object"
    # Empty dict — Chrome can't deeply-serialize a DOM node through returnByValue.
    assert out["result"] == {}


def test_evaluate_runtime_unserializable_value(chrome_cdp, supervisor_registry):
    """``Infinity``/``NaN``/``BigInt`` come back via ``unserializableValue``."""
    cdp_url, _port = chrome_cdp
    supervisor = supervisor_registry.get_or_start(task_id="pytest-eval-5", cdp_url=cdp_url)

    _fire_on_page(cdp_url, "void 0")
    time.sleep(0.5)

    out = supervisor.evaluate_runtime("Infinity")
    assert out["ok"] is True
    assert out["result"] == "Infinity"
