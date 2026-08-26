from __future__ import annotations

import json

import pytest


def test_delegated_task_and_result_schema_validation_accepts_required_payloads():
    from plugins.openclaw_bridge.schemas import (
        validate_delegated_result,
        validate_delegated_task,
    )

    task = {
        "task_id": "task-1",
        "requested_by": "hermes",
        "objective": "Read project status",
        "context_refs": ["obsidian:System/Tasks.md"],
        "allowed_tools": ["status_check"],
        "denied_tools": ["external_message"],
        "risk_level": "low",
        "requires_confirmation": False,
        "max_runtime_seconds": 60,
        "output_format": "markdown",
        "audit_required": True,
    }
    result = {
        "task_id": "task-1",
        "status": "succeeded",
        "summary": "Status checked",
        "artifacts": [],
        "tool_calls": [],
        "audit_log": [],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "none",
    }

    assert validate_delegated_task(task) == task
    assert validate_delegated_result(result) == result


def test_delegated_task_schema_rejects_missing_required_field():
    from plugins.openclaw_bridge.schemas import validate_delegated_task

    with pytest.raises(ValueError, match="objective"):
        validate_delegated_task({"task_id": "task-1"})


def test_protocol_v2_schemas_require_identity_and_success_evidence():
    from plugins.openclaw_bridge.schemas import (
        validate_delegated_result,
        validate_delegated_task,
    )

    base_task = {
        "task_id": "task-v2",
        "requested_by": "hermes",
        "objective": "Read status",
        "context_refs": [],
        "allowed_tools": ["browser.read"],
        "denied_tools": [],
        "risk_level": "low",
        "requires_confirmation": False,
        "max_runtime_seconds": 60,
        "output_format": "json",
        "audit_required": True,
        "protocol_version": "2.0",
    }
    with pytest.raises(ValueError):
        validate_delegated_task(base_task)

    base_result = {
        "task_id": "task-v2",
        "status": "succeeded",
        "summary": "claimed success",
        "artifacts": [],
        "tool_calls": [],
        "audit_log": [],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "none",
        "protocol_version": "2.0",
        "delegation_id": "delegation-v2",
        "attempt_id": "attempt-v2",
        "contract_fingerprint": "sha256:contract",
        "identity_correlated": True,
    }
    with pytest.raises(ValueError):
        validate_delegated_result(base_result)


def test_protocol_v3_task_schema_requires_context_and_confirmation_contracts():
    from plugins.openclaw_bridge.schemas import validate_delegated_task

    task = {
        "task_id": "task-v3",
        "requested_by": "hermes",
        "objective": "Publish only after approval.",
        "context_refs": [],
        "allowed_tools": ["browser.read"],
        "denied_tools": [],
        "risk_level": "medium",
        "requires_confirmation": False,
        "max_runtime_seconds": 300,
        "output_format": "json",
        "audit_required": True,
        "protocol_version": "3.0-draft",
        "delegation_id": "delegation-v3",
        "attempt_id": "attempt-v3",
        "contract_fingerprint": "sha256:v3",
        "project": "secondhand_commerce",
        "topic_id": "telegram/thread/2",
        "task_type": "browser_publish",
        "executor_backend": "openclaw",
        "executor_profile": "browser",
        "backend_agent_id": "openclaw-browser-operator",
        "external_effect_budget": 1,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": [],
        "idempotency_key": "attempt-v3",
        "dry_run": True,
        "scope": {"allowed_actions": ["browser.read"]},
        "context_packet": {"summary": "approved scoped context"},
        "confirmation_policy": {"required_before": ["publish"]},
        "result_contract": {"required_fields": ["status", "audit_log"]},
    }

    assert validate_delegated_task(task) == task

    missing = dict(task)
    missing.pop("context_packet")
    with pytest.raises(ValueError):
        validate_delegated_task(missing)


def test_high_risk_task_stops_at_approval_gate():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    result = delegate_to_openclaw(
        {
            "objective": "Deploy production",
            "risk_level": "high",
            "allowed_tools": ["deploy"],
            "requested_by": "hermes",
        },
        transport=lambda _task: {"status": "succeeded"},
    )

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True
    assert "approval" in result["recommended_next_action"].lower()


def test_critical_risk_task_stops_at_approval_gate():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    result = delegate_to_openclaw(
        {
            "objective": "Rotate production credentials",
            "risk_level": "critical",
            "allowed_tools": ["credentials"],
            "requested_by": "hermes",
        },
        transport=lambda _task: {"status": "succeeded"},
    )

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True
    assert "risk_level=critical" in result["summary"]


def test_requires_confirmation_stops_before_transport():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    def fail_transport(_task):
        raise AssertionError("requires_confirmation tasks must not reach OpenClaw")

    result = delegate_to_openclaw(
        {
            "objective": "Send a Telegram message",
            "risk_level": "low",
            "requires_confirmation": True,
            "allowed_tools": ["telegram.send"],
            "requested_by": "hermes",
        },
        transport=fail_transport,
    )

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True


def test_scoped_loop_contract_approval_passes_browser_confirmation_policy():
    from plugins.openclaw_bridge import tools

    seen = []

    def transport(task):
        seen.append(task)
        return {
            "task_id": task["task_id"],
            "status": "queued",
            "summary": "OpenClaw accepted the Loop Contract execution.",
            "artifacts": [],
            "tool_calls": [],
            "audit_log": [],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "Poll the OpenClaw run.",
            "protocol_version": "2.0",
            "delegation_id": "delegation-loop",
            "attempt_id": "attempt-loop",
            "contract_fingerprint": "fingerprint-loop",
            "identity_correlated": True,
            "protocol_correlated": True,
            "backend_run_id": "run-loop",
            "backend_agent_id": "missioncrew-executor",
            "backend_session_key": "session-loop",
        }

    result = tools.delegate_loop_contract_to_openclaw(
        {
            "task_id": "task-loop",
            "objective": "Publish within an approved Loop Contract.",
            "risk_level": "high",
            "allowed_tools": ["browser"],
            "requires_confirmation": False,
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-loop",
            "attempt_id": "attempt-loop",
            "contract_fingerprint": "fingerprint-loop",
            "project": "secondhand_commerce",
            "topic_id": "2",
            "task_type": "browser_publish",
            "executor_backend": "openclaw",
            "executor_profile": "loop-contract",
            "backend_agent_id": "missioncrew-executor",
            "approval_grant_id": "delegation-loop",
            "external_effect_budget": 1,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": ["hermes-controlled-browser"],
            "idempotency_key": "attempt-loop:start",
            "openclaw_task_id": "openclaw.agent.loop_contract_start",
            "dry_run": False,
            "loop_contract": {
                "trace": {
                    "telegram_message_path": {
                        "trace_id": "tgtrace-loop",
                        "platform": "telegram",
                    }
                },
                "routing": {"task_type": "browser_publish"},
                "external_targets": ["https://www.facebook.com/groups/1/"],
                "approval_provenance": {
                    "contract_fingerprint": "fingerprint-loop",
                    "scope_binding": "exact_loop_contract_fingerprint",
                },
            },
            "message_path": {
                "trace_id": "tgtrace-loop",
                "platform": "telegram",
                "chat_id": "chat_opaque",
            },
        },
        transport=transport,
    )

    assert result["status"] == "queued"
    assert result["backend_run_id"] == "run-loop"
    assert result["backend_agent_id"] == "missioncrew-executor"
    assert result["protocol_correlated"] is True
    assert len(seen) == 1
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )
    payload = tools._openclaw_payload(
        seen[0],
        config,
        live_async_capability=tools._LOOP_CONTRACT_ASYNC_CAPABILITY,
    )
    assert payload["input"]["messagePath"]["trace_id"] == "tgtrace-loop"


def test_low_risk_task_builds_valid_delegated_task_and_uses_transport():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    seen = []

    def transport(task):
        seen.append(task)
        return {
            "task_id": task["task_id"],
            "status": "succeeded",
            "summary": "ok",
            "artifacts": [],
            "tool_calls": [],
            "audit_log": [],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "none",
        }

    result = delegate_to_openclaw(
        {
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["obsidian:System/Tasks.md"],
        },
        transport=transport,
    )

    assert result["status"] == "succeeded"
    assert len(seen) == 1
    assert seen[0]["objective"] == "Read status"
    assert seen[0]["requires_confirmation"] is False


def test_protocol_v3_payload_carries_context_confirmation_and_result_contracts():
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v3-dry-run",
            "objective": "Prepare a publish preview but stop before publish.",
            "risk_level": "medium",
            "allowed_tools": ["browser.read"],
            "requires_confirmation": False,
            "protocol_version": "3.0-draft",
            "delegation_id": "delegation-v3",
            "attempt_id": "attempt-v3",
            "contract_fingerprint": "sha256:v3",
            "project": "secondhand_commerce",
            "topic_id": "telegram/thread/2",
            "task_type": "browser_publish",
            "executor_backend": "openclaw",
            "executor_profile": "browser",
            "backend_agent_id": "openclaw-browser-operator",
            "external_effect_budget": 1,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-v3",
            "dry_run": True,
            "scope": {"allowed_actions": ["browser.read"], "external_effect_budget": 1},
            "context_packet": {"summary": "scoped context"},
            "confirmation_policy": {"required_before": ["publish"]},
            "result_contract": {"required_fields": ["status", "audit_log"]},
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    payload = tools._openclaw_payload(task, config)

    assert payload["protocolVersion"] == "3.0-draft"
    assert payload["identity"]["taskType"] == "browser_publish"
    assert payload["contract"] == {
        "scope": {"allowed_actions": ["browser.read"], "external_effect_budget": 1},
        "contextPacket": {"summary": "scoped context"},
        "confirmationPolicy": {"required_before": ["publish"]},
        "resultContract": {"required_fields": ["status", "audit_log"]},
    }
    assert payload["dryRun"] is True


def test_protocol_v3_live_execution_still_fails_closed_until_template_is_verified():
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v3-live",
            "objective": "Try unsupported live publish.",
            "risk_level": "medium",
            "allowed_tools": ["browser.read"],
            "requires_confirmation": False,
            "protocol_version": "3.0-draft",
            "delegation_id": "delegation-v3",
            "attempt_id": "attempt-v3",
            "contract_fingerprint": "sha256:v3",
            "project": "secondhand_commerce",
            "topic_id": "telegram/thread/2",
            "task_type": "browser_publish",
            "executor_backend": "openclaw",
            "executor_profile": "browser",
            "backend_agent_id": "openclaw-browser-operator",
            "external_effect_budget": 1,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-v3",
            "dry_run": False,
            "scope": {"allowed_actions": ["browser.read"]},
            "context_packet": {"summary": "scoped context"},
            "confirmation_policy": {"required_before": ["publish"]},
            "result_contract": {"required_fields": ["status", "audit_log"]},
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    with pytest.raises(ValueError, match="Protocol v3 live templates must be verified"):
        tools._openclaw_payload(task, config)


def test_external_facebook_work_is_not_sent_to_dry_run_bridge():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    def fail_transport(_task):
        raise AssertionError("dry-run bridge must not receive real Facebook work")

    result = delegate_to_openclaw(
        {
            "objective": "檢查 Facebook 20 個社團，可刊登就實際刊登貼文",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["kanban:t_16d6dfe3"],
        },
        transport=fail_transport,
    )

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True
    assert "Facebook" in result["summary"]
    assert "browser-capable executor" in result["recommended_next_action"]


def test_clawops_runtime_work_is_not_sent_to_dry_run_bridge():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    def fail_transport(_task):
        raise AssertionError("ClawOps runtime work must not reach OpenClaw dry-run")

    result = delegate_to_openclaw(
        {
            "objective": "請交給 ClawOps Content Creator 生成二手拍賣需要的圖片素材",
            "risk_level": "low",
            "allowed_tools": ["image_generate"],
            "requested_by": "hermes",
            "context_refs": ["telegram:secondhand-topic"],
        },
        transport=fail_transport,
    )

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True
    assert "ClawOps runtime queue" in result["summary"]
    assert "/clawops" in result["recommended_next_action"]


def test_explicit_openclaw_dry_run_still_uses_transport():
    from plugins.openclaw_bridge.tools import delegate_to_openclaw

    seen = []

    def transport(task):
        seen.append(task)
        return {
            "task_id": task["task_id"],
            "status": "succeeded",
            "summary": "dry-run ok",
            "artifacts": [],
            "tool_calls": [],
            "audit_log": [],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "none",
        }

    result = delegate_to_openclaw(
        {
            "objective": "Validate bridge health",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["telegram:openclaw-dry-run"],
        },
        transport=transport,
    )

    assert result["status"] == "succeeded"
    assert seen[0]["objective"] == "Validate bridge health"


def test_openclaw_delegate_handler_accepts_registry_keyword_args(monkeypatch):
    import json

    from plugins.openclaw_bridge import tools

    def fake_delegate(args):
        return {
            "task_id": args["task_id"],
            "status": "succeeded",
            "summary": args["objective"],
            "artifacts": [],
            "tool_calls": [],
            "audit_log": [],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "none",
        }

    monkeypatch.setattr(tools, "delegate_to_openclaw", fake_delegate)

    output = tools.handle_openclaw_delegate(
        {"objective": "Check Facebook status"},
        task_id="kanban-t-1",
    )

    result = json.loads(output)
    assert result["task_id"] == "kanban-t-1"
    assert result["summary"] == "Check Facebook status"


def test_plugin_registers_tool_command_and_gateway_hook():
    from plugins.openclaw_bridge import register

    calls = {"tools": [], "commands": [], "hooks": [], "middleware": []}

    class Ctx:
        def register_tool(self, **kwargs):
            calls["tools"].append(kwargs["name"])

        def register_command(self, name, **_kwargs):
            calls["commands"].append(name)

        def register_hook(self, name, _handler):
            calls["hooks"].append(name)

        def register_middleware(self, name, _handler):
            calls["middleware"].append(name)

    register(Ctx())

    assert calls == {
        "tools": [
            "clawops_delegate",
            "clawops_cancel",
            "grace_callback_outcome",
            "openclaw_delegate",
        ],
        "commands": ["openclaw-dry-run"],
        "hooks": ["pre_gateway_dispatch"],
        "middleware": ["tool_execution"],
    }


def test_pre_gateway_dispatch_keeps_clawops_requests_with_grace():
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    event = SimpleNamespace(text="ClawOps: check bridge wiring")
    assert pre_gateway_dispatch(event=event) is None

    spaced = SimpleNamespace(text="ClawOps check queue callback")
    assert pre_gateway_dispatch(event=spaced) is None


def test_pre_gateway_dispatch_keeps_facebook_execution_with_grace():
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    event = SimpleNamespace(text="請繼續 #7 咖啡器材新舊交流團的刊登流程，只允許點 Next，不要送出")
    assert pre_gateway_dispatch(event=event) is None

    upload = SimpleNamespace(text="Facebook 社團商品表單照片上傳後檢查 Next 是否解除鎖定")
    assert pre_gateway_dispatch(event=upload) is None


def test_pre_gateway_dispatch_does_not_route_facebook_explanation_to_clawops():
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    event = SimpleNamespace(text="請說明 Facebook 社團刊登有哪些風險")
    assert pre_gateway_dispatch(event=event) is None


def test_pre_gateway_dispatch_keeps_openclaw_requests_as_bridge_preview():
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    event = SimpleNamespace(text="OpenClaw: check bridge wiring")
    assert pre_gateway_dispatch(event=event) == {
        "action": "rewrite",
        "text": "/openclaw-dry-run check bridge wiring",
    }

    normal = SimpleNamespace(text="請說明 OpenClaw 是什麼")
    assert pre_gateway_dispatch(event=normal) is None


def test_pre_gateway_dispatch_never_auto_routes_execution_work(monkeypatch):
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "openclaw_bridge": {
                "auto_route_clawops": {
                    "enabled": True,
                    "platforms": ["telegram"],
                }
            }
        },
    )
    source = SimpleNamespace(platform="telegram")
    event = SimpleNamespace(
        text="請檢查 Hermes gateway log 並修正錯誤",
        source=source,
    )

    assert pre_gateway_dispatch(event=event) is None


def test_pre_gateway_dispatch_auto_route_keeps_advice_with_grace(monkeypatch):
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "openclaw_bridge": {
                "auto_route_clawops": {
                    "enabled": True,
                    "platforms": ["telegram"],
                }
            }
        },
    )
    source = SimpleNamespace(platform="telegram")

    advice = SimpleNamespace(
        text="目前系統回覆緩慢，是否因為 GPT-5.5？請提出解決方案",
        source=source,
    )
    explanation = SimpleNamespace(
        text="請說明如何修正 gateway timeout",
        source=source,
    )

    assert pre_gateway_dispatch(event=advice) is None
    assert pre_gateway_dispatch(event=explanation) is None


def test_pre_gateway_dispatch_auto_route_respects_platform_allowlist(monkeypatch):
    from types import SimpleNamespace

    from plugins.openclaw_bridge.tools import pre_gateway_dispatch

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "openclaw_bridge": {
                "auto_route_clawops": {
                    "enabled": True,
                    "platforms": ["telegram"],
                }
            }
        },
    )
    event = SimpleNamespace(
        text="請檢查 Hermes gateway log 並修正錯誤",
        source=SimpleNamespace(platform="feishu"),
    )

    assert pre_gateway_dispatch(event=event) is None


def test_openclaw_dry_run_slash_does_not_enqueue_kanban_by_default(monkeypatch):
    from plugins.openclaw_bridge.tools import handle_openclaw_dry_run

    def fail_run_slash(_command):
        raise AssertionError("kanban should not be called unless explicitly requested")

    monkeypatch.setattr("hermes_cli.kanban.run_slash", fail_run_slash)
    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_HERMES_BRIDGE_TOKEN", raising=False)

    output = handle_openclaw_dry_run("check bridge wiring")

    assert "OpenClaw bridge result" in output
    assert "status: blocked" in output
    assert "Kanban enqueue result" not in output


def test_openclaw_dry_run_can_explicitly_enqueue_kanban(monkeypatch):
    from plugins.openclaw_bridge.tools import handle_openclaw_dry_run

    seen = []

    def fake_run_slash(command):
        seen.append(command)
        return "Created t_abc123  (ready, assignee=-)"

    monkeypatch.setattr("hermes_cli.kanban.run_slash", fake_run_slash)
    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_HERMES_BRIDGE_TOKEN", raising=False)

    output = handle_openclaw_dry_run("kanban check runtime queue")

    assert "Kanban enqueue result" in output
    assert "Created t_abc123" in output
    assert len(seen) == 1
    assert seen[0].startswith("create ")
    assert "--created-by hermes-openclaw-bridge" in seen[0]


def test_openclaw_dry_run_local_mode_keeps_old_validation_only_path(monkeypatch):
    from plugins.openclaw_bridge.tools import handle_openclaw_dry_run

    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_HERMES_BRIDGE_TOKEN", raising=False)

    output = handle_openclaw_dry_run("local check bridge wiring")

    assert "status: queued" in output
    assert "no OpenClaw transport configured in this process" in output


def test_low_risk_task_posts_to_openclaw_bridge_when_configured(monkeypatch):
    import json

    from plugins.openclaw_bridge import tools

    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token")
    monkeypatch.setenv("OPENCLAW_HERMES_BRIDGE_TOKEN", "bridge-token")

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "taskId": "agents.ask_team",
                    "status": "succeeded",
                    "summary": "Dry-run completed. No OpenClaw agents were started.",
                    "auditLog": [{"step": "accepted"}],
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(tools, "urlopen", fake_urlopen)

    result = tools.delegate_to_openclaw(
        {
            "objective": "Ask team for status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["telegram:test"],
        }
    )

    assert result["status"] == "succeeded"
    assert result["summary"] == "Dry-run completed. No OpenClaw agents were started."
    assert seen["url"] == "http://127.0.0.1:18789/api/plugins/hermes-bridge/tasks"
    assert seen["headers"]["Authorization"] == "Bearer gateway-token"
    assert seen["headers"]["X-openclaw-hermes-token"] == "bridge-token"
    assert seen["payload"]["taskId"] == "agents.ask_team"
    assert seen["payload"]["dryRun"] is True
    assert seen["payload"]["requestedBy"] == "hermes"
    assert seen["payload"]["input"]["objective"] == "Ask team for status"


def test_http_mapper_fails_closed_when_artifacts_is_not_an_array(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "malformed-artifacts",
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "summary": "Malformed result",
                    "artifacts": 7,
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert result["errors"] == ["OpenClaw response artifacts must be an array."]


def test_http_mapper_rejects_contradictory_success_with_ok_false(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "contradictory-result",
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": False,
                    "status": "succeeded",
                    "summary": "contradictory",
                    "requiresHumanReview": False,
                    "error": {"message": "backend rejected request"},
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert result["errors"] == ["backend rejected request"]


def test_http_mapper_rejects_non_boolean_ok(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "non-boolean-ok",
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": "false",
                    "status": "succeeded",
                    "summary": "Malformed success",
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert result["errors"] == ["OpenClaw response ok must be a boolean."]


@pytest.mark.parametrize("status", ["accepted", "queued", "running"])
def test_http_mapper_rejects_explicit_ok_false_active_status(monkeypatch, status):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": f"contradictory-active-{status}",
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": False,
                    "status": status,
                    "summary": "backend rejected active request",
                    "requiresHumanReview": False,
                    "error": {"message": "backend rejected request"},
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert result["errors"] == ["backend rejected request"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_http_mapper_preserves_nonterminal_backend_status_without_ok(
    monkeypatch,
    status,
):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": f"async-{status}",
            "objective": "Read status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "status": status,
                    "summary": f"backend is {status}",
                    "requiresHumanReview": False,
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == status
    assert result["errors"] == []
    assert result["requires_human_review"] is False


@pytest.mark.parametrize(
    ("response_payload", "expected_artifact_type", "expected_next_action"),
    [
        (
            {
                "ok": True,
                "status": "request_context",
                "protocolVersion": "3.0-draft",
                "summary": "Need destination.",
                "executionIdentity": {
                    "delegationId": "delegation-v3",
                    "attemptId": "attempt-v3",
                    "contractFingerprint": "sha256:v3",
                },
                "question": "Which destination should I use?",
                "neededFields": ["target_url"],
                "reason": "No approved destination was disclosed.",
            },
            "request_context",
            "Ask Grace for scoped context",
        ),
        (
            {
                "ok": True,
                "status": "request_confirmation",
                "protocolVersion": "3.0-draft",
                "summary": "Ready to publish.",
                "executionIdentity": {
                    "delegationId": "delegation-v3",
                    "attemptId": "attempt-v3",
                    "contractFingerprint": "sha256:v3",
                },
                "action": "publish_facebook_group_post",
                "riskLevel": "L3",
                "externalEffects": ["public_post"],
            },
            "request_confirmation",
            "Ask KJ for explicit approval",
        ),
    ],
)
def test_protocol_v3_mapper_turns_openclaw_interruptions_into_grace_review_blocks(
    monkeypatch,
    response_payload,
    expected_artifact_type,
    expected_next_action,
):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v3-interruption",
            "objective": "Prepare a browser task.",
            "risk_level": "medium",
            "allowed_tools": ["browser.read"],
            "requires_confirmation": False,
            "protocol_version": "3.0-draft",
            "delegation_id": "delegation-v3",
            "attempt_id": "attempt-v3",
            "contract_fingerprint": "sha256:v3",
            "project": "secondhand_commerce",
            "topic_id": "telegram/thread/2",
            "task_type": "browser_publish",
            "executor_backend": "openclaw",
            "executor_profile": "browser",
            "backend_agent_id": "openclaw-browser-operator",
            "external_effect_budget": 1,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-v3",
            "dry_run": True,
            "scope": {"allowed_actions": ["browser.read"]},
            "context_packet": {"summary": "scoped context"},
            "confirmation_policy": {"required_before": ["publish"]},
            "result_contract": {"required_fields": ["status", "audit_log"]},
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(response_payload).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "blocked"
    assert result["requires_human_review"] is True
    assert result["protocol_version"] == "3.0-draft"
    assert result["identity_correlated"] is True
    assert result["errors"] == [expected_artifact_type]
    assert expected_next_action in result["recommended_next_action"]
    assert any(
        artifact["type"] == expected_artifact_type
        for artifact in result["artifacts"]
    )


def test_protocol_v3_mapper_preserves_standardized_success_result(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v3-standard-result",
            "objective": "Run a standardized OpenClaw result.",
            "risk_level": "medium",
            "allowed_tools": ["browser.read"],
            "requires_confirmation": False,
            "protocol_version": "3.0-draft",
            "delegation_id": "delegation-v3",
            "attempt_id": "attempt-v3",
            "contract_fingerprint": "sha256:v3",
            "project": "hub_ops",
            "topic_id": "telegram/thread/2",
            "task_type": "research",
            "executor_backend": "openclaw",
            "executor_profile": "research",
            "backend_agent_id": "openclaw-researcher",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-v3",
            "dry_run": True,
            "scope": {"allowed_actions": ["browser.read"]},
            "context_packet": {"summary": "scoped context"},
            "confirmation_policy": {"required_before": ["publish"]},
            "result_contract": {"required_fields": ["status", "audit_log"]},
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "protocolVersion": "3.0-draft",
                    "summary": "Standardized result complete.",
                    "executionIdentity": {
                        "delegationId": "delegation-v3",
                        "attemptId": "attempt-v3",
                        "contractFingerprint": "sha256:v3",
                    },
                    "backendExecution": {
                        "backendRunId": "backend-v3",
                        "backendAgentId": "openclaw-researcher",
                        "sessionKey": "agent:openclaw-researcher:session:v3",
                    },
                    "actionsTaken": [
                        {"action": "browser.read", "target": "https://example.com/"}
                    ],
                    "evidence": [
                        {"kind": "snapshot", "ref": "kanban-attachment://snap"}
                    ],
                    "filesChanged": [],
                    "externalEffects": [],
                    "needsReview": True,
                    "auditLog": [{"event": "task_completed"}],
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "succeeded"
    assert result["requires_human_review"] is True
    assert result["actions_taken"] == [
        {"action": "browser.read", "target": "https://example.com/"}
    ]
    assert result["evidence"] == [
        {"kind": "snapshot", "ref": "kanban-attachment://snap"}
    ]
    assert result["files_changed"] == []
    assert result["external_effects"] == []
    assert result["needs_review"] is True
    assert result["backend_run_id"] == "backend-v3"


def test_protocol_v3_success_without_standardized_result_fails_closed(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v3-missing-standard-result",
            "objective": "Claim success without standardized fields.",
            "risk_level": "medium",
            "allowed_tools": ["browser.read"],
            "requires_confirmation": False,
            "protocol_version": "3.0-draft",
            "delegation_id": "delegation-v3",
            "attempt_id": "attempt-v3",
            "contract_fingerprint": "sha256:v3",
            "project": "hub_ops",
            "topic_id": "telegram/thread/2",
            "task_type": "research",
            "executor_backend": "openclaw",
            "executor_profile": "research",
            "backend_agent_id": "openclaw-researcher",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-v3",
            "dry_run": True,
            "scope": {"allowed_actions": ["browser.read"]},
            "context_packet": {"summary": "scoped context"},
            "confirmation_policy": {"required_before": ["publish"]},
            "result_contract": {"required_fields": ["status", "audit_log"]},
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "protocolVersion": "3.0-draft",
                    "summary": "Incomplete success.",
                    "executionIdentity": {
                        "delegationId": "delegation-v3",
                        "attemptId": "attempt-v3",
                        "contractFingerprint": "sha256:v3",
                    },
                    "backendExecution": {
                        "backendRunId": "backend-v3",
                        "backendAgentId": "openclaw-researcher",
                        "sessionKey": "agent:openclaw-researcher:session:v3",
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert "OpenClaw response omitted standardized result field actions_taken." in result["errors"]
    assert "OpenClaw response omitted standardized result field needs_review." in result["errors"]


@pytest.mark.parametrize("error_body", [b"[]", b"null", b'"upstream failed"'])
def test_http_mapper_handles_non_object_json_error_bodies(monkeypatch, error_body):
    from io import BytesIO
    from urllib.error import HTTPError

    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "non-object-http-error",
            "objective": "Read status",
            "risk_level": "low",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    def raise_http_error(request, timeout):
        raise HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            {},
            BytesIO(error_body),
        )

    monkeypatch.setattr(tools, "urlopen", raise_http_error)

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["errors"] == ["http_502"]
    assert result["requires_human_review"] is True


def test_http_mapper_preserves_status_fallback_when_error_body_read_fails(
    monkeypatch,
):
    from urllib.error import HTTPError

    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "unreadable-http-error",
            "objective": "Read status",
            "risk_level": "low",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class UnreadableHTTPError(HTTPError):
        def read(self, *args, **kwargs):
            raise OSError("truncated error response")

    def raise_http_error(request, timeout):
        raise UnreadableHTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            None,
        )

    monkeypatch.setattr(tools, "urlopen", raise_http_error)

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["summary"] == "OpenClaw bridge HTTP error: 503"
    assert result["errors"] == ["http_503"]


def test_protocol_v2_http_failure_preserves_request_identity_without_echo_claim(
    monkeypatch,
):
    from io import BytesIO
    from urllib.error import HTTPError

    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "v2-http-error",
            "objective": "Read the Example Domain page.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "protocol_version": "2.0",
            "delegation_id": "delegation-http-error",
            "attempt_id": "attempt-http-error",
            "contract_fingerprint": "sha256:http-error",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-http-error",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    def raise_http_error(request, timeout):
        raise HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            {},
            BytesIO(b'{"error":{"message":"upstream failed"}}'),
        )

    monkeypatch.setattr(tools, "urlopen", raise_http_error)

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["protocol_version"] == "2.0"
    assert result["delegation_id"] == "delegation-http-error"
    assert result["attempt_id"] == "attempt-http-error"
    assert result["contract_fingerprint"] == "sha256:http-error"
    assert result["identity_correlated"] is False
    assert result["protocol_correlated"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dry_run", ""),
        ("requires_confirmation", ""),
        ("external_effect_budget", -0.5),
        ("external_effect_budget", True),
        ("credential_refs", ""),
        ("allowed_tools", ""),
    ],
)
def test_protocol_v2_rejects_policy_values_before_coercion(field, value):
    from plugins.openclaw_bridge import tools

    args = {
        "task_id": "strict-v2-policy",
        "objective": "Validate the Protocol v2 trust boundary.",
        "risk_level": "low",
        "allowed_tools": [],
        "requires_confirmation": False,
        "protocol_version": "2.0",
        "delegation_id": "delegation-strict",
        "attempt_id": "attempt-strict",
        "contract_fingerprint": "sha256:strict",
        "project": "hub_ops",
        "topic_id": "async",
        "executor_backend": "openclaw",
        "executor_profile": "zero-effect-async",
        "backend_agent_id": "missioncrew-browser-readonly",
        "external_effect_budget": 0,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": [],
        "idempotency_key": "strict-v2-policy",
        "dry_run": False,
    }
    args[field] = value

    with pytest.raises(ValueError, match=field):
        tools.build_delegated_task(args)


def test_http_mapper_normalizes_ok_true_failed_status_to_failure(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "contradictory-http-result",
            "objective": "Read status",
            "risk_level": "low",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "failed",
                    "error": {"message": "backend rejected task"},
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["errors"] == ["backend rejected task"]
    assert result["requires_human_review"] is True


def test_openclaw_payload_contract_forces_dry_run_and_fixed_route():
    from urllib.parse import urljoin

    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "contract-1",
            "objective": "Check bridge status",
            "risk_level": "low",
            "allowed_tools": ["status_check"],
            "requested_by": "hermes",
            "context_refs": ["telegram:test"],
            "output_format": "markdown",
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    payload = tools._openclaw_payload(task, config)

    assert urljoin(config.base_url + "/", tools.DEFAULT_OPENCLAW_BRIDGE_PATH.lstrip("/")) == (
        "http://127.0.0.1:18789/api/plugins/hermes-bridge/tasks"
    )
    assert payload["taskId"] == "agents.ask_team"
    assert payload["dryRun"] is True
    assert payload["allowedTools"] == ["status_check"]
    assert payload["requiresConfirmation"] is False
    assert payload["idempotencyKey"] == "contract-1"


def test_protocol_v2_readonly_browser_payload_allows_one_zero_effect_live_template():
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-1",
            "objective": "Read the Example Domain page and return snapshot evidence.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-1",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-1",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    payload = tools._openclaw_payload(task, config)

    assert payload["protocolVersion"] == "2.0"
    assert payload["taskId"] == "openclaw.browser.read_snapshot"
    assert payload["dryRun"] is False
    assert payload["input"]["url"] == "https://example.com/"
    assert payload["identity"]["delegationId"] == "delegation-1"
    assert payload["identity"]["topicId"] == "readonly-browser"
    assert payload["routing"]["executorBackend"] == "openclaw"
    assert payload["policy"] == {
        "approvalGrantId": None,
        "externalEffectBudget": 0,
        "workspacePolicy": "dedicated",
        "sessionPolicy": "ephemeral",
        "credentialRefs": [],
    }


@pytest.mark.parametrize(
    "template",
    [
        "openclaw.browser.read_snapshot_poll",
        "openclaw.browser.read_snapshot_cancel",
    ],
)
def test_protocol_v2_readonly_browser_lifecycle_payload_requires_exact_run(
    template,
):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-lifecycle-1",
            "objective": "Control the exact admitted browser run.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-1",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": f"attempt-1:{template.rsplit('_', 1)[-1]}",
            "start_idempotency_key": "attempt-1",
            "backend_run_id": "backend-run-1",
            "openclaw_task_id": template,
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    payload = tools._openclaw_payload(task, config)

    assert payload["taskId"] == template
    assert payload["input"]["startIdempotencyKey"] == "attempt-1"
    assert payload["input"]["backendRunId"] == "backend-run-1"
    assert payload["dryRun"] is False


@pytest.mark.parametrize(
    ("template", "extra", "expected_input"),
    [
        ("openclaw.agent.zero_effect_async_start", {}, {}),
        (
            "openclaw.agent.zero_effect_async_poll",
            {
                "start_idempotency_key": "start-1",
                "backend_run_id": "backend-1",
            },
            {
                "startIdempotencyKey": "start-1",
                "backendRunId": "backend-1",
            },
        ),
    ],
)
def test_protocol_v2_zero_effect_async_payloads_are_fixed_and_toolless(
    template,
    extra,
    expected_input,
):
    from plugins.openclaw_bridge import tools

    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway",
        bridge_token="bridge",
    )
    task = tools.build_delegated_task(
        {
            "task_id": "async-task",
            "objective": "Zero-effect async acceptance",
            "allowed_tools": [],
            "risk_level": "low",
            "requires_confirmation": False,
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-1",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "async",
            "executor_backend": "openclaw",
            "executor_profile": "zero-effect-async",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": f"{template}:request",
            "openclaw_task_id": template,
            "dry_run": False,
            **extra,
        }
    )

    payload = tools._openclaw_payload(
        task,
        config,
        live_async_capability=tools._ZERO_EFFECT_ASYNC_CAPABILITY,
    )

    assert payload["taskId"] == template
    assert payload["allowedTools"] == []
    assert payload["dryRun"] is False
    assert payload["input"] | expected_input == payload["input"]


def test_generic_delegate_cannot_admit_live_zero_effect_async():
    from plugins.openclaw_bridge import tools

    transport_called = False

    def transport(_task):
        nonlocal transport_called
        transport_called = True
        return {"status": "queued"}

    result = tools.delegate_to_openclaw(
        {
            "task_id": "generic-live-async",
            "objective": "Try to bypass durable admission.",
            "allowed_tools": [],
            "risk_level": "low",
            "requires_confirmation": False,
            "protocol_version": "2.0",
            "delegation_id": "delegation-generic",
            "attempt_id": "attempt-generic",
            "contract_fingerprint": "sha256:generic",
            "project": "hub_ops",
            "topic_id": "async",
            "executor_backend": "openclaw",
            "executor_profile": "zero-effect-async",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "generic-live-async",
            "openclaw_task_id": "openclaw.agent.zero_effect_async_start",
            "dry_run": False,
        },
        transport=transport,
    )

    assert result["status"] == "blocked"
    assert result["errors"] == ["external_capability_unavailable"]
    assert transport_called is False


def test_protocol_v2_rejects_arbitrary_non_dry_run_template():
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "objective": "Do live work",
            "risk_level": "low",
            "allowed_tools": [],
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-1",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    with pytest.raises(ValueError, match="Only the Protocol v2 zero-effect"):
        tools._openclaw_payload(task, config)


@pytest.mark.parametrize("protocol_version", ["2.1", "3.0", "", 2])
def test_delegated_task_rejects_unsupported_protocol_versions(protocol_version):
    from plugins.openclaw_bridge import tools

    with pytest.raises(ValueError, match="protocol_version"):
        tools.build_delegated_task(
            {
                "objective": "Do not downgrade this protocol.",
                "protocol_version": protocol_version,
            }
        )


def test_protocol_v2_rejects_arbitrary_live_browser_target():
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-private",
            "objective": "Attempt an unapproved target.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-private",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-private",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "http://[fc00::1]/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    with pytest.raises(ValueError, match="Only the Protocol v2 zero-effect"):
        tools._openclaw_payload(task, config)


@pytest.mark.parametrize("returned_protocol", [None, "1.0"])
def test_protocol_v2_http_response_must_explicitly_echo_v2(
    monkeypatch,
    returned_protocol,
):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-response",
            "objective": "Read the Example Domain page.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-1",
            "attempt_id": "attempt-response",
            "contract_fingerprint": "sha256:contract",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-response",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )
    response_payload = {
        "ok": True,
        "status": "succeeded",
        "summary": "Backend claimed success.",
        "executionIdentity": {
            "delegationId": "delegation-1",
            "attemptId": "attempt-response",
            "contractFingerprint": "sha256:contract",
        },
        "backendExecution": {
            "backendRunId": "backend-run-1",
            "backendAgentId": "missioncrew-browser-readonly",
            "sessionKey": "agent:missioncrew-browser-readonly:subagent:test",
        },
    }
    if returned_protocol is not None:
        response_payload["protocolVersion"] = returned_protocol

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(response_payload).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["requires_human_review"] is True
    assert result["protocol_version"] == "2.0"
    assert result["protocol_correlated"] is False
    assert result["delegation_id"] == "delegation-1"
    assert result["attempt_id"] == "attempt-response"
    assert result["contract_fingerprint"] == "sha256:contract"
    assert result["errors"] == [
        "OpenClaw response did not explicitly echo Protocol v2."
    ]


def test_protocol_v2_http_response_maps_matching_execution_identity(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-identity",
            "objective": "Read the Example Domain page.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-identity",
            "attempt_id": "attempt-identity",
            "contract_fingerprint": "sha256:identity",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-identity",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "protocolVersion": "2.0",
                    "executionIdentity": {
                        "delegationId": "delegation-identity",
                        "attemptId": "attempt-identity",
                        "contractFingerprint": "sha256:identity",
                    },
                    "backendExecution": {
                        "backendRunId": "backend-run-identity",
                        "backendAgentId": "missioncrew-browser-readonly",
                        "sessionKey": "agent:missioncrew-browser-readonly:subagent:test",
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "succeeded"
    assert result["protocol_version"] == "2.0"
    assert result["delegation_id"] == "delegation-identity"
    assert result["attempt_id"] == "attempt-identity"
    assert result["contract_fingerprint"] == "sha256:identity"
    assert result["identity_correlated"] is True


def test_protocol_v2_http_response_preserves_backend_token_usage(monkeypatch):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-usage",
            "objective": "Read the Example Domain page.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "requested_by": "hermes",
            "protocol_version": "2.0",
            "delegation_id": "delegation-usage",
            "attempt_id": "attempt-usage",
            "contract_fingerprint": "sha256:usage",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_backend": "openclaw",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "external_effect_budget": 0,
            "workspace_policy": "dedicated",
            "session_policy": "ephemeral",
            "credential_refs": [],
            "idempotency_key": "attempt-usage",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "protocolVersion": "2.0",
                    "executionIdentity": {
                        "delegationId": "delegation-usage",
                        "attemptId": "attempt-usage",
                        "contractFingerprint": "sha256:usage",
                    },
                    "backendExecution": {
                        "backendRunId": "backend-run-usage",
                        "backendAgentId": "missioncrew-browser-readonly",
                        "sessionKey": "agent:missioncrew-browser-readonly:subagent:test",
                        "tokenUsage": {
                            "inputTokens": 10,
                            "cachedInputTokens": 7,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 2,
                            "model": "gpt-test",
                        },
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "succeeded"
    assert result["token_usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_tokens": 7,
        "reasoning_tokens": 2,
        "total_tokens": 22,
        "model": "gpt-test",
    }


@pytest.mark.parametrize(
    ("response_patch", "expected_error"),
    [
        (
            {"protocolVersion": 2.0},
            "OpenClaw response did not explicitly echo Protocol v2.",
        ),
        (
            {
                "executionIdentity": {
                    "delegationId": "delegation-from-another-run",
                    "attemptId": "attempt-identity",
                    "contractFingerprint": "sha256:identity",
                }
            },
            "OpenClaw response executionIdentity.delegationId did not match the request.",
        ),
        (
            {
                "executionIdentity": {
                    "delegationId": 123,
                    "attemptId": "attempt-identity",
                    "contractFingerprint": "sha256:identity",
                }
            },
            "OpenClaw response executionIdentity.delegationId did not match the request.",
        ),
        (
            {"backendExecution": None},
            "OpenClaw Protocol v2 success response omitted backendExecution.backendRunId.",
        ),
        (
            {
                "backendExecution": {
                    "backendRunId": 123,
                    "backendAgentId": "missioncrew-browser-readonly",
                    "sessionKey": "agent:test",
                }
            },
            "OpenClaw Protocol v2 success response omitted backendExecution.backendRunId.",
        ),
        (
            {
                "backendExecution": {
                    "backendRunId": "backend-run-identity",
                    "backendAgentId": "another-agent",
                    "sessionKey": "agent:another-agent:subagent:test",
                }
            },
            "OpenClaw response backendExecution.backendAgentId did not match the request.",
        ),
    ],
)
def test_protocol_v2_http_response_rejects_uncorrelated_or_evidence_free_success(
    monkeypatch,
    response_patch,
    expected_error,
):
    from plugins.openclaw_bridge import tools

    task = tools.build_delegated_task(
        {
            "task_id": "browser-contract-correlation",
            "objective": "Read the Example Domain page.",
            "risk_level": "low",
            "allowed_tools": ["browser.read"],
            "protocol_version": "2.0",
            "delegation_id": "delegation-identity",
            "attempt_id": "attempt-identity",
            "contract_fingerprint": "sha256:identity",
            "project": "hub_ops",
            "topic_id": "readonly-browser",
            "executor_profile": "browser-readonly",
            "backend_agent_id": "missioncrew-browser-readonly",
            "openclaw_task_id": "openclaw.browser.read_snapshot",
            "target_url": "https://example.com/",
            "dry_run": False,
        }
    )
    config = tools.OpenClawBridgeConfig(
        base_url="http://127.0.0.1:18789",
        gateway_token="gateway-token",
        bridge_token="bridge-token",
    )
    response_payload = {
        "ok": True,
        "status": "succeeded",
        "protocolVersion": "2.0",
        "executionIdentity": {
            "delegationId": "delegation-identity",
            "attemptId": "attempt-identity",
            "contractFingerprint": "sha256:identity",
        },
        "backendExecution": {
            "backendRunId": "backend-run-identity",
            "backendAgentId": "missioncrew-browser-readonly",
            "sessionKey": "agent:missioncrew-browser-readonly:subagent:test",
        },
        **response_patch,
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(response_payload).encode("utf-8")

    monkeypatch.setattr(tools, "urlopen", lambda _request, timeout: Response())

    result = tools.post_to_openclaw_bridge(task, config)

    assert result["status"] == "failed"
    assert result["protocol_version"] == "2.0"
    assert result["requires_human_review"] is True
    assert expected_error in result["errors"]
    if "executionIdentity" in response_patch:
        assert result["identity_correlated"] is False
    else:
        assert result["identity_correlated"] is True
    for field in ("backend_run_id", "backend_agent_id", "backend_session_key"):
        assert result.get(field) != ""


def test_openclaw_bridge_config_can_read_tokens_from_env_file(monkeypatch, tmp_path):
    from plugins.openclaw_bridge import tools

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789",
                "OPENCLAW_GATEWAY_TOKEN=gateway-token",
                "OPENCLAW_HERMES_BRIDGE_TOKEN=bridge-token",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_HERMES_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"openclaw_bridge": {"env_file": str(env_file)}},
    )

    config = tools.load_openclaw_bridge_config()

    assert config is not None
    assert config.base_url == "http://127.0.0.1:18789"
    assert config.gateway_token == "gateway-token"
    assert config.bridge_token == "bridge-token"
