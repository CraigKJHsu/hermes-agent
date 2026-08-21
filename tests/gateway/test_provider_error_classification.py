import logging

import pytest

import gateway.run as gateway_run
from hermes_cli import runtime_provider
from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE


def test_quota_exhaustion_wins_over_generic_auth_envelope():
    reply = gateway_run._gateway_provider_error_reply(
        "Provider authentication failed: HTTP 429 quota usage limit reached"
    )

    assert "quota is exhausted" in reply
    assert "Credentials are still valid" in reply
    assert "authentication failed" not in reply


def test_explicit_invalid_key_wins_over_incidental_quota_wording():
    reply = gateway_run._gateway_provider_error_reply(
        "HTTP 401: invalid API key; check your quota settings"
    )

    assert "authentication failed" in reply
    assert "Credentials are still valid" not in reply


def test_generic_rate_limit_does_not_claim_quota_exhaustion():
    reply = gateway_run._gateway_provider_error_reply(
        "HTTP 429: rate limit exceeded; retry after 10 seconds"
    )

    assert "temporarily rate-limited" in reply
    assert "quota is exhausted" not in reply


def test_auth_failure_without_fallback_does_not_claim_trying_fallback(
    monkeypatch,
    caplog,
):
    error = AuthError(
        "Codex provider quota exhausted (429); credentials are still valid.",
        provider="openai-codex",
        code=CODEX_RATE_LIMITED_CODE,
        relogin_required=False,
    )

    def fail_primary(**_kwargs):
        raise error

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fail_primary)
    monkeypatch.setattr(gateway_run, "_try_resolve_fallback_provider", lambda: None)
    caplog.set_level(logging.WARNING, logger="gateway.run")

    with pytest.raises(RuntimeError, match="quota exhausted"):
        gateway_run._resolve_runtime_agent_kwargs()

    assert "no fallback configured" in caplog.text
    assert "trying fallback" not in caplog.text
