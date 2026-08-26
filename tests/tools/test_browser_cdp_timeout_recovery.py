"""Regression tests for shared-CDP timeout recovery."""

import json
import subprocess

import websockets.sync.client

import tools.browser_tool as browser_tool


class _NoopLock:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _prepare_navigation(monkeypatch):
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "check_website_access", lambda _url: None)


def test_cdp_health_failure_stops_before_session_creation(monkeypatch):
    _prepare_navigation(monkeypatch)
    monkeypatch.setattr(
        browser_tool,
        "_get_cdp_override",
        lambda: "ws://127.0.0.1:9222/devtools/browser/test",
    )
    monkeypatch.setattr(
        browser_tool,
        "_preflight_cdp_page_health",
        lambda _url: "TimeoutError: renderer did not respond",
    )
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda _task_id: (_ for _ in ()).throw(
            AssertionError("failed preflight must not create a logical session")
        ),
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://example.com",
            task_id="cdp-health-failure",
        )
    )

    assert result["success"] is False
    assert result["browser_recovery_required"] is True
    assert "No destination page was opened" in result["error"]
    assert browser_tool._last_active_session_key == {}


def test_cdp_lock_contention_is_retryable_without_recovery(monkeypatch):
    _prepare_navigation(monkeypatch)
    monkeypatch.setattr(
        browser_tool,
        "_get_cdp_override",
        lambda: "ws://127.0.0.1:9222/devtools/browser/test",
    )
    monkeypatch.setattr(
        browser_tool,
        "_preflight_cdp_page_health",
        lambda _url: (_ for _ in ()).throw(
            browser_tool._CDPBusyError("shared CDP browser is busy")
        ),
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://example.com",
            task_id="cdp-busy",
        )
    )

    assert result == {
        "success": False,
        "error": "shared CDP browser is busy",
        "browser_busy": True,
        "retryable": True,
    }


def test_blocked_url_does_not_run_cdp_health_probe(monkeypatch):
    _prepare_navigation(monkeypatch)
    monkeypatch.setattr(
        browser_tool,
        "_get_cdp_override",
        lambda: "ws://127.0.0.1:9222/devtools/browser/test",
    )
    monkeypatch.setattr(
        browser_tool,
        "check_website_access",
        lambda _url: {
            "message": "Blocked by website policy",
            "host": "blocked.example",
            "rule": "blocked.example",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        browser_tool,
        "_preflight_cdp_page_health",
        lambda _url: (_ for _ in ()).throw(
            AssertionError("blocked URL must not mutate the browser")
        ),
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://blocked.example",
            task_id="blocked-url",
        )
    )

    assert result["success"] is False
    assert result["blocked_by_policy"]["host"] == "blocked.example"


def test_health_probe_closes_only_its_exact_target(monkeypatch):
    class _HealthSocket:
        def __init__(self):
            self.responses = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, raw):
            request = json.loads(raw)
            method = request["method"]
            if method == "Target.createTarget":
                result = {"targetId": "hermes-health-target"}
            elif method == "Target.attachToTarget":
                result = {"sessionId": "health-session"}
            elif method == "Runtime.evaluate":
                result = {"result": {"value": 2}}
            else:
                raise AssertionError(f"unexpected CDP method: {method}")
            self.responses.append(json.dumps({"id": request["id"], "result": result}))

        def recv(self, timeout=None):
            assert timeout is not None
            return self.responses.pop(0)

    monkeypatch.setattr(
        websockets.sync.client,
        "connect",
        lambda *_args, **_kwargs: _HealthSocket(),
    )
    closed = []

    def _close(_url, method, params=None, **_kwargs):
        assert method == "Target.closeTarget"
        closed.append(params["targetId"])
        return {"success": True}

    monkeypatch.setattr(browser_tool, "_cdp_browser_call", _close)

    error = browser_tool._probe_cdp_page_health("ws://cdp")

    assert error is None
    assert closed == ["hermes-health-target"]


def test_health_probe_fails_when_exact_target_cannot_close(monkeypatch):
    class _HealthSocket:
        def __init__(self):
            self.responses = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, raw):
            request = json.loads(raw)
            results = {
                "Target.createTarget": {"targetId": "owned-target"},
                "Target.attachToTarget": {"sessionId": "owned-session"},
                "Runtime.evaluate": {"result": {"value": 2}},
            }
            self.responses.append(
                json.dumps({"id": request["id"], "result": results[request["method"]]})
            )

        def recv(self, timeout=None):
            assert timeout is not None
            return self.responses.pop(0)

    monkeypatch.setattr(
        websockets.sync.client,
        "connect",
        lambda *_args, **_kwargs: _HealthSocket(),
    )
    attempts = []

    def _cannot_close(_url, method, params=None, **_kwargs):
        attempts.append((method, params["targetId"]))
        raise TimeoutError("close did not complete")

    monkeypatch.setattr(browser_tool, "_cdp_browser_call", _cannot_close)

    error = browser_tool._probe_cdp_page_health("ws://cdp")

    assert "health target cleanup failed" in error
    assert attempts == [
        ("Target.closeTarget", "owned-target"),
        ("Target.closeTarget", "owned-target"),
    ]


def test_quarantine_persists_until_explicit_managed_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_tool, "_socket_safe_tmpdir", lambda: str(tmp_path))
    browser_tool._write_cdp_quarantine("ws://cdp", "timed-out open")

    assert "quarantined" in browser_tool._cdp_quarantine_reason("ws://cdp")
    assert "timed-out open" in browser_tool._cdp_quarantine_reason("ws://cdp")

    browser_tool._clear_cdp_quarantine("ws://cdp")
    assert browser_tool._cdp_quarantine_reason("ws://cdp") is None


def test_failed_navigation_is_not_recorded_as_last_active(monkeypatch):
    _prepare_navigation(monkeypatch)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda _task_id: {
            "session_name": "failed-nav",
            "_first_nav": False,
            "features": {"local": True, "proxies": False},
        },
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *_args, **_kwargs: {
            "success": False,
            "error": "Browser command 'open' timed out",
        },
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://example.com",
            task_id="failed-nav",
        )
    )

    assert result["success"] is False
    assert "failed-nav" not in browser_tool._last_active_session_key


def test_open_timeout_quarantines_failed_cleanup_before_unlock(monkeypatch, tmp_path):
    session = {
        "session_name": "timeout-session",
        "cdp_url": "ws://127.0.0.1:9222/devtools/browser/test",
        "features": {"cdp_override": True},
    }
    monkeypatch.setattr(browser_tool, "_find_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(browser_tool, "_requires_real_termux_browser_install", lambda _cmd: False)
    monkeypatch.setattr(browser_tool, "_is_local_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_get_session_info", lambda _task_id: session)
    monkeypatch.setattr(browser_tool, "_get_browser_engine", lambda: "auto")
    events = []

    class _RecordingLock(_NoopLock):
        def __exit__(self, *_args):
            events.append("unlocked")
            return False

    monkeypatch.setattr(browser_tool, "_CDPOperationLock", _RecordingLock)
    monkeypatch.setattr(
        browser_tool,
        "_socket_safe_tmpdir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(browser_tool, "_write_owner_pid", lambda *_args: None)
    monkeypatch.setattr(browser_tool, "_build_browser_env", lambda: {})
    monkeypatch.setattr(browser_tool, "_merge_browser_path", lambda path: path)
    monkeypatch.setattr(browser_tool, "_needs_chromium_sandbox_bypass", lambda: False)
    invalidated = []

    def _invalidate(task_id, info):
        events.append("invalidated")
        invalidated.append((task_id, info))

    monkeypatch.setattr(
        browser_tool,
        "_invalidate_timed_out_browser_session",
        _invalidate,
    )
    monkeypatch.setattr(
        browser_tool,
        "_write_cdp_quarantine",
        lambda *_args: events.append("leased"),
    )

    class _TimedOutProcess:
        returncode = None

        def __init__(self):
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("agent-browser", timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        browser_tool.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _TimedOutProcess(),
    )

    result = browser_tool._run_browser_command(
        "timeout-task",
        "open",
        ["https://example.com"],
        timeout=1,
    )

    assert result["success"] is False
    assert result["browser_recovery_required"] is True
    assert events == ["leased", "invalidated", "unlocked"]
    assert len(invalidated) == 1
    assert invalidated[0][0] == "timeout-task"
    assert invalidated[0][1] is session
