"""Unit tests for _SupervisorRegistry cache-hit healthcheck.

Verifies that get_or_start() does NOT return a cached supervisor whose
thread has exited or whose event loop has stopped. Avoids a real Chrome —
the only thing under test is the registry's cache decision.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from tools import browser_supervisor as bs


class _FakeLoop:
    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


def _make_fake_supervisor(cdp_url: str, *, thread_alive: bool, loop_running: bool):
    """Build a minimal stand-in for a CDPSupervisor entry in the registry.

    Only the attributes touched by the healthcheck (_thread, _loop, cdp_url)
    and by the teardown path (stop()) need to exist.
    """

    if thread_alive:
        # A thread that is actually running — parks on an Event we never set.
        hold = threading.Event()
        t = threading.Thread(target=hold.wait, daemon=True)
        t.start()
        # Attach the release hook so the test can let the thread exit.
        setattr(t, "_release", hold.set)
    else:
        # An un-started thread — is_alive() returns False.
        t = threading.Thread(target=lambda: None)

    stop_calls: list[bool] = []

    fake = SimpleNamespace(
        cdp_url=cdp_url,
        _thread=t,
        _loop=_FakeLoop(loop_running),
        stop=lambda: stop_calls.append(True),
    )
    fake._stop_calls = stop_calls  # type: ignore[attr-defined]
    return fake


@pytest.mark.asyncio
async def test_crosspost_gate_recovers_missing_post_body_without_consuming_gate(
    monkeypatch,
):
    supervisor = bs.CDPSupervisor("t-crosspost-body", "ws://unused")
    calls = []

    async def fake_cdp(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "Fetch.getRequestPostData":
            return {
                "result": {
                    "postData": "fb_api_req_friendly_name=UnrelatedQuery",
                },
            }
        return {}

    monkeypatch.setattr(supervisor, "_cdp", fake_cdp)
    gate = bs.CrosspostRequestGate(
        listing_id="1666446304587399",
        group_ids=("123",),
        session_id="session-1",
        graphql_url_pattern="https://www.facebook.com/api/graphql/*",
        page_identity="https://www.facebook.com/marketplace/item/1666446304587399|1",
        armed_at=0.0,
    )

    await supervisor._handle_crosspost_fetch_paused(
        {
            "requestId": "request-1",
            "request": {
                "url": "https://www.facebook.com/api/graphql/",
                "method": "POST",
            },
        },
        "session-1",
        gate,
        gate.graphql_url_pattern,
    )

    assert gate.consumed is False
    assert [method for method, _params in calls] == [
        "Fetch.getRequestPostData",
        "Fetch.continueRequest",
    ]


@pytest.mark.asyncio
async def test_crosspost_gate_retires_ambiguously_when_unknown_body_is_blocked(
    monkeypatch,
):
    supervisor = bs.CDPSupervisor("t-crosspost-body-blocked", "ws://unused")
    calls = []

    async def fake_cdp(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "Fetch.getRequestPostData":
            return {"result": {}}
        return {}

    monkeypatch.setattr(supervisor, "_cdp", fake_cdp)
    gate = bs.CrosspostRequestGate(
        listing_id="1666446304587399",
        group_ids=("123",),
        session_id="session-1",
        graphql_url_pattern="https://www.facebook.com/api/graphql/*",
        page_identity=(
            "https://www.facebook.com/marketplace/item/1666446304587399|1"
        ),
        armed_at=0.0,
    )
    supervisor._crosspost_request_gate = gate

    await supervisor._handle_crosspost_fetch_paused(
        {
            "requestId": "request-1",
            "request": {
                "url": "https://www.facebook.com/api/graphql/",
                "method": "POST",
            },
        },
        "session-1",
        gate,
        gate.graphql_url_pattern,
    )

    assert [method for method, _params in calls] == [
        "Fetch.getRequestPostData",
        "Fetch.failRequest",
    ]
    assert gate.completed.is_set()
    assert gate.consumed is True
    assert gate.result["dispatch_ambiguous"] is True
    assert gate.result["request_released"] is False
    assert supervisor._crosspost_request_gate is None


@pytest.fixture
def isolated_registry():
    """A fresh registry instance, independent of the global SUPERVISOR_REGISTRY."""
    return bs._SupervisorRegistry()


@pytest.fixture
def stub_cdp_supervisor(monkeypatch):
    """Replace CDPSupervisor in the module so recreate paths don't touch Chrome.

    Returns a callable that reads the last-constructed fake out.
    """
    created: list[SimpleNamespace] = []

    class _StubSupervisor:
        def __init__(
            self,
            *,
            task_id,
            cdp_url,
            expected_page_url=None,
            dialog_policy,
            dialog_timeout_s,
        ):
            self.task_id = task_id
            self.cdp_url = cdp_url
            self.expected_page_url = expected_page_url
            self.dialog_policy = dialog_policy
            self.dialog_timeout_s = dialog_timeout_s
            # Healthy by default — real thread, running "loop".
            hold = threading.Event()
            self._thread = threading.Thread(target=hold.wait, daemon=True)
            self._thread.start()
            self._thread_release = hold.set  # type: ignore[attr-defined]
            self._loop = _FakeLoop(True)
            self.start_called = False
            self.stop_called = False
            created.append(self)

        def start(self, timeout: float = 15.0) -> None:
            self.start_called = True

        def stop(self) -> None:
            self.stop_called = True
            # Release the parked thread so the process exits cleanly.
            release = getattr(self, "_thread_release", None)
            if release is not None:
                release()

    monkeypatch.setattr(bs, "CDPSupervisor", _StubSupervisor)
    yield created
    # Teardown: release any parked threads in stubs the test left behind.
    for s in created:
        release = getattr(s, "_thread_release", None)
        if release is not None:
            release()


def test_cache_hit_returns_same_instance_when_healthy(
    isolated_registry, stub_cdp_supervisor
):
    """Sanity: healthy cached supervisor is returned without recreate."""
    first = isolated_registry.get_or_start(task_id="t1", cdp_url="http://h/1")
    second = isolated_registry.get_or_start(task_id="t1", cdp_url="http://h/1")
    assert first is second
    # Only one CDPSupervisor was ever constructed.
    assert len(stub_cdp_supervisor) == 1
    first.stop()


def test_exact_page_binding_change_restarts_cached_supervisor(
    isolated_registry, stub_cdp_supervisor
):
    first = isolated_registry.get_or_start(
        task_id="t-exact",
        cdp_url="http://h/exact",
        expected_page_url="https://www.facebook.com/marketplace/item/1/",
    )
    second = isolated_registry.get_or_start(
        task_id="t-exact",
        cdp_url="http://h/exact",
        expected_page_url="https://www.facebook.com/marketplace/item/2/",
    )

    assert second is not first
    assert first.stop_called is True
    assert second.expected_page_url.endswith("/item/2/")
    second.stop()


def test_initial_binding_allows_duplicate_url_until_identity_capture(monkeypatch):
    url = "https://www.facebook.com/marketplace/item/1/"
    supervisor = bs.CDPSupervisor(
        "t-duplicates",
        "ws://unused",
        expected_page_url=url,
    )

    async def fake_cdp(method, params=None, **kwargs):
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {"type": "page", "url": url, "targetId": "first"},
                        {"type": "page", "url": url, "targetId": "second"},
                    ],
                },
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "session-first"}}
        raise AssertionError(method)

    async def fake_configure(_session_id):
        return None

    monkeypatch.setattr(supervisor, "_cdp", fake_cdp)
    monkeypatch.setattr(supervisor, "_configure_page_session", fake_configure)

    asyncio.run(supervisor._attach_initial_page())

    assert supervisor._page_target_id == "first"


def test_dead_thread_triggers_recreate(isolated_registry, stub_cdp_supervisor):
    """Cached supervisor with a non-live thread must not be reused."""
    cdp_url = "http://h/2"
    dead = _make_fake_supervisor(cdp_url, thread_alive=False, loop_running=True)
    isolated_registry._by_task["t2"] = dead  # pre-seed cache with a dead entry

    fresh = isolated_registry.get_or_start(task_id="t2", cdp_url=cdp_url)

    assert fresh is not dead, "dead-thread supervisor must be replaced"
    assert dead._stop_calls == [True], "dead supervisor must be torn down"
    assert isolated_registry._by_task["t2"] is fresh
    assert len(stub_cdp_supervisor) == 1
    assert stub_cdp_supervisor[0].start_called
    fresh.stop()


def test_stopped_loop_triggers_recreate(isolated_registry, stub_cdp_supervisor):
    """Cached supervisor whose event loop is no longer running is recreated."""
    cdp_url = "http://h/3"
    broken = _make_fake_supervisor(cdp_url, thread_alive=True, loop_running=False)
    isolated_registry._by_task["t3"] = broken

    fresh = isolated_registry.get_or_start(task_id="t3", cdp_url=cdp_url)

    assert fresh is not broken
    assert broken._stop_calls == [True]
    # Release the still-live thread from the pre-seeded fake so we don't leak.
    release = getattr(broken._thread, "_release", None)
    if release is not None:
        release()
    assert isolated_registry._by_task["t3"] is fresh
    fresh.stop()


def test_missing_thread_and_loop_attrs_trigger_recreate(
    isolated_registry, stub_cdp_supervisor
):
    """Defensive: None _thread or None _loop counts as unhealthy."""
    cdp_url = "http://h/4"
    broken = SimpleNamespace(
        cdp_url=cdp_url,
        _thread=None,
        _loop=None,
        stop=lambda: None,
    )
    isolated_registry._by_task["t4"] = broken

    fresh = isolated_registry.get_or_start(task_id="t4", cdp_url=cdp_url)
    assert fresh is not broken
    assert isolated_registry._by_task["t4"] is fresh
    fresh.stop()


def test_guarded_action_uses_browser_computed_accessible_name(monkeypatch):
    supervisor = bs.CDPSupervisor("t-ax", "ws://unused")
    calls: list[str] = []

    def fake_call(method, params=None, timeout=10.0):
        calls.append(method)
        if method == "DOM.resolveNode":
            return {"ok": True, "result": {"object": {"objectId": "node-1"}}}
        if method == "Accessibility.getPartialAXTree":
            return {
                "ok": True,
                "result": {
                    "nodes": [{
                        "backendDOMNodeId": 44,
                        "role": {"value": "textbox"},
                        # This can come from <label for>, aria-labelledby, etc.
                        "name": {"value": "Email address"},
                    }],
                },
            }
        return {
            "ok": True,
            "result": {"result": {"value": {"ok": True}}},
        }

    monkeypatch.setattr(supervisor, "call_page_cdp", fake_call)

    result = supervisor.guarded_dom_action(
        backend_node_id=44,
        expected_page_identity="https://example.com/form|100",
        expected_role="textbox",
        expected_name="Email address",
        action="fill",
        text="person@example.com",
    )

    assert result["ok"] is True
    assert calls == [
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "Accessibility.getPartialAXTree",
        "Runtime.callFunctionOn",
    ]


def test_guarded_popup_revalidation_uses_snapshot_full_ax_source(monkeypatch):
    supervisor = bs.CDPSupervisor("t-popup-ax", "ws://unused")
    calls: list[tuple[str, dict]] = []

    def fake_call(method, params=None, timeout=10.0):
        del timeout
        calls.append((method, params or {}))
        if method == "DOM.resolveNode":
            return {"ok": True, "result": {"object": {"objectId": "node-1"}}}
        if method == "Accessibility.getFullAXTree":
            return {
                "ok": True,
                "result": {
                    "nodes": [{
                        "backendDOMNodeId": 44,
                        "role": {"value": "button"},
                        "name": {"value": "Options"},
                        "properties": [{
                            "name": "hasPopup",
                            "value": {"value": "menu"},
                        }],
                    }],
                },
            }
        return {
            "ok": True,
            "result": {"result": {"value": {"ok": True}}},
        }

    monkeypatch.setattr(supervisor, "call_page_cdp", fake_call)

    result = supervisor.guarded_dom_action(
        backend_node_id=44,
        expected_page_identity=(
            "https://www.facebook.com/marketplace/item/36803832485927906"
            "|100"
        ),
        expected_role="button",
        expected_name="Options",
        required_popup_role="menu",
        expected_popup_semantics_source="ax_full",
        action="click",
    )

    assert result["ok"] is True
    methods = [method for method, _params in calls]
    assert methods == [
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "Accessibility.getFullAXTree",
        "Runtime.callFunctionOn",
    ]
    click_params = calls[-1][1]
    assert {"value": "ax_full"} in click_params["arguments"]


def test_guarded_page_composer_action_carries_exact_atomic_token(monkeypatch):
    supervisor = bs.CDPSupervisor("t-page-composer", "ws://unused")
    calls: list[tuple[str, dict]] = []

    def fake_call(method, params=None, timeout=10.0):
        del timeout
        calls.append((method, params or {}))
        if method == "DOM.resolveNode":
            return {"ok": True, "result": {"object": {"objectId": "node-1"}}}
        if method == "Accessibility.getPartialAXTree":
            return {
                "ok": True,
                "result": {
                    "nodes": [{
                        "backendDOMNodeId": 45,
                        "role": {"value": "textbox"},
                        "name": {"value": "What's on your mind?"},
                    }],
                },
            }
        return {
            "ok": True,
            "result": {"result": {"value": {"ok": True}}},
        }

    monkeypatch.setattr(supervisor, "call_page_cdp", fake_call)

    result = supervisor.guarded_dom_action(
        backend_node_id=45,
        expected_page_identity=(
            "https://www.facebook.com/solobizai|100"
        ),
        expected_role="textbox",
        expected_name="What's on your mind?",
        action="fill",
        text="approved body",
        page_composer_stage="compose",
        page_composer_token="composer-token-123",
        required_facebook_page_url=(
            "https://www.facebook.com/solobizai"
        ),
        required_facebook_page_actor="SoloBizAi",
    )

    assert result["ok"] is True
    action_params = calls[-1][1]
    assert {"value": "compose"} in action_params["arguments"]
    assert {"value": "composer-token-123"} in action_params["arguments"]
    assert {"value": "SoloBizAi"} in action_params["arguments"]
    assert "data-hermes-page-composer-token" in (
        action_params["functionDeclaration"]
    )
    assert "data-hermes-page-composer-actor-token" in (
        action_params["functionDeclaration"]
    )
    assert "canonicalPageUrlOf(requiredFacebookPageUrl)" in (
        action_params["functionDeclaration"]
    )
    assert "composerActorProofRequired" in (
        action_params["functionDeclaration"]
    )
    assert "composerActorBinding?.name" in (
        action_params["functionDeclaration"]
    )
    assert "textboxTop - rect.bottom > 240" not in (
        action_params["functionDeclaration"]
    )
    assert "facebook_page_composer_binding_invalid" in (
        action_params["functionDeclaration"]
    )
    assert "bindings.length === 1" in (
        action_params["functionDeclaration"]
    )


def test_guarded_page_composer_failure_returns_predicate_diagnostics(monkeypatch):
    supervisor = bs.CDPSupervisor("t-page-diagnostics", "ws://unused")
    runtime_calls = 0

    def fake_call(method, params=None, timeout=10.0):
        nonlocal runtime_calls
        del params, timeout
        if method == "DOM.resolveNode":
            return {"ok": True, "result": {"object": {"objectId": "node-1"}}}
        if method == "Accessibility.getPartialAXTree":
            return {
                "ok": True,
                "result": {
                    "nodes": [{
                        "backendDOMNodeId": 45,
                        "role": {"value": "textbox"},
                        "name": {"value": "Composer body"},
                    }],
                },
            }
        if method == "Runtime.callFunctionOn":
            runtime_calls += 1
            if runtime_calls == 1:
                return {
                    "ok": True,
                    "result": {"result": {"value": True}},
                }
            return {
                "ok": True,
                "result": {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {
                            "description": (
                                "Error: HERMES_PAGE_GUARD|"
                                "facebook_page_composer_binding_invalid|"
                                '{"page_url_match":true,'
                                '"composer_actor_match":false}'
                            ),
                        },
                    },
                },
            }
        raise AssertionError(method)

    monkeypatch.setattr(supervisor, "call_page_cdp", fake_call)

    result = supervisor.guarded_dom_action(
        backend_node_id=45,
        expected_page_identity="https://www.facebook.com/solobizai|100",
        expected_role="textbox",
        expected_name="Composer body",
        action="fill",
        text="approved body",
        page_composer_stage="compose",
        page_composer_token="composer-token-123",
        required_facebook_page_url="https://www.facebook.com/solobizai",
        required_facebook_page_actor="SoloBizAi",
    )

    assert result["ok"] is False
    assert result["dispatch_ambiguous"] is False
    assert result["error_code"] == "facebook_page_composer_binding_invalid"
    assert result["guard_diagnostics"] == {
        "page_url_match": True,
        "composer_actor_match": False,
    }
    assert result["error"] == (
        "Facebook Page action blocked: the target is not bound to the "
        "approved Page composer capability."
    )
