import json
from unittest.mock import Mock

import pytest
import requests

from tools import browser_tool as browser


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(browser, "_active_sessions", {})
    monkeypatch.setattr(browser, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser, "_update_session_activity", lambda *a: None)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_is_safe_url", lambda *a: True)
    monkeypatch.setattr(browser, "check_website_access", lambda *a: None)
    return "ws://127.0.0.1:9222/devtools/browser/actual"


@pytest.mark.parametrize("configured", ["http://127.0.0.1:9222", "ws://127.0.0.1:9222/", "ws://127.0.0.1:9222/json/version"])
def test_disconnected_navigation(endpoint, monkeypatch, configured):
    monkeypatch.setenv("BROWSER_CDP_URL", configured)
    monkeypatch.setattr(browser.requests, "get", Mock(side_effect=requests.ConnectionError("refused")))
    health = Mock(side_effect=AssertionError("No WebSocket health call after failed discovery"))
    local = Mock(side_effect=AssertionError("No replacement browser"))
    monkeypatch.setattr(browser, "_preflight_cdp_page_health", health)
    monkeypatch.setattr(browser, "_create_local_session", local)
    result = json.loads(browser.browser_navigate("https://example.org", "verify"))
    assert result["error_code"] == "cdp_endpoint_unavailable"
    assert "ConnectionError" in result["error"]
    assert "InvalidURI" not in result["error"]
    assert not browser._active_sessions
    health.assert_not_called()
    local.assert_not_called()


def test_config_does_not_fall_back(endpoint, monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_URL")
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": {"cdp_url": "http://127.0.0.1:9222"}})
    monkeypatch.setattr(browser.requests, "get", Mock(side_effect=requests.ConnectionError("refused")))
    with pytest.raises(browser._CDPEndpointUnavailableError):
        browser._get_session_info("verify")
    assert not browser._active_sessions


@pytest.mark.parametrize("payload", [{}, [], {"webSocketDebuggerUrl": "http://127.0.0.1:9222"},
                                     {"webSocketDebuggerUrl": "ws://"}, {"webSocketDebuggerUrl": "ws://host:99999"},
                                     {"webSocketDebuggerUrl": "ws://host/devtools/browser/id#fragment"}])
def test_bad_discovery_payload(endpoint, monkeypatch, payload):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr(browser.requests, "get", Mock(return_value=response))
    with pytest.raises(browser._CDPEndpointUnavailableError):
        browser._get_cdp_override()


def test_endpoint_recovers(endpoint, monkeypatch):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": endpoint}
    monkeypatch.setattr(browser.requests, "get", Mock(side_effect=[requests.ConnectionError("refused"), response]))
    with pytest.raises(browser._CDPEndpointUnavailableError):
        browser._get_session_info("verify")
    assert not browser._active_sessions
    session = browser._get_session_info("verify")
    assert session["cdp_url"] == endpoint
    assert session["features"]["cdp_override"] is True


def test_presence_has_no_network(endpoint, monkeypatch):
    fetch = Mock(side_effect=AssertionError("Configuration checks must not do network I/O"))
    monkeypatch.setattr(browser.requests, "get", fetch)
    assert browser.check_browser_requirements()
    assert not browser._is_local_mode()
    assert browser._navigation_session_key("verify", "http://localhost") == "verify"
    fetch.assert_not_called()


def test_query_and_redaction(endpoint, monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "https://host/json/version?token=private-value")
    fetch = Mock(side_effect=requests.ConnectionError("refused"))
    monkeypatch.setattr(browser.requests, "get", fetch)
    with pytest.raises(browser._CDPEndpointUnavailableError) as result:
        browser._get_cdp_override()
    assert "private-value" not in str(result.value)
    fetch.assert_called_once_with("https://host/json/version?token=private-value", timeout=10)


@pytest.mark.parametrize("configured,expected", [
    ("ws://127.0.0.1:9222/", "http://127.0.0.1:9222/json/version"),
    ("ws://127.0.0.1:9222/json/version", "http://127.0.0.1:9222/json/version"),
    ("ws://[::1]:9222/", "http://[::1]:9222/json/version"),
    ("wss://browser.example/", "https://browser.example/json/version"),
    ("wss://browser.example/?route=unit", "https://browser.example/json/version?route=unit"),
])
def test_ws_discovery_paths(endpoint, monkeypatch, configured, expected):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": endpoint}
    fetch = Mock(return_value=response)
    monkeypatch.setattr(browser.requests, "get", fetch)
    assert browser._resolve_cdp_override(configured, require_websocket=True) == endpoint
    fetch.assert_called_once_with(expected, timeout=10)


def test_ws_fragment_rejected(monkeypatch):
    fetch = Mock(side_effect=AssertionError("No discovery for malformed concrete WebSocket"))
    monkeypatch.setattr(browser.requests, "get", fetch)
    with pytest.raises(browser._CDPEndpointUnavailableError):
        browser._resolve_cdp_override("ws://host/devtools/browser/id#fragment", require_websocket=True)
    fetch.assert_not_called()
