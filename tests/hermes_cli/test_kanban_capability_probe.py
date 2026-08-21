"""Focused regression tests for isolated Kanban worker capability probes."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb


def test_capability_probe_receives_disabled_toolsets_without_importing_config(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["payload"] = json.loads(kwargs["input"])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "declared_tools": ["browser_snapshot"],
                    "required_runtime_tools": ["browser_snapshot"],
                    "abstract_contract_tools": [],
                    "available_tools": ["browser_snapshot"],
                    "missing_required_tools": [],
                    "tool_checks": {},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "proactive.hubops_routing.registered_worker_capabilities",
        lambda: (),
    )

    result = kb._probe_worker_capabilities(
        declared_tools=["browser_snapshot"],
        toolsets=["browser-cdp"],
        disabled_toolsets=["browser"],
        env={"HERMES_HOME": str(tmp_path)},
        workspace=str(tmp_path),
    )

    assert result["ok"] is True
    assert captured["payload"] == {
        "declared_tools": ["browser_snapshot"],
        "toolsets": ["browser-cdp"],
        "disabled_toolsets": ["browser"],
    }
    assert "hermes_cli.config" not in kb._WORKER_CAPABILITY_PROBE


def test_worker_cli_configuration_returns_toolsets_and_disabled_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "platform_toolsets": {"cli": ["browser"]},
            "agent": {"disabled_toolsets": ["computer", " vision "]},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda config, platform: config["platform_toolsets"][platform],
    )

    toolsets, disabled = kb._resolve_worker_cli_configuration("/tmp/profile")

    assert toolsets == ["browser", "browser-cdp"]
    assert disabled == ["computer", "vision"]


def test_worker_cli_configuration_loads_default_home_when_override_is_absent(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_constants.set_hermes_home_override",
        lambda _home: pytest.fail("default home must not install an override"),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "platform_toolsets": {"cli": ["terminal"]},
            "agent": {"disabled_toolsets": ["computer"]},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda config, platform: config["platform_toolsets"][platform],
    )

    toolsets, disabled = kb._resolve_worker_cli_configuration(None)

    assert toolsets == ["terminal"]
    assert disabled == ["computer"]


def test_worker_cli_configuration_failure_is_not_an_empty_snapshot(monkeypatch):
    def fail_load():
        raise ValueError("broken profile config")

    monkeypatch.setattr("hermes_cli.config.load_config", fail_load)

    with pytest.raises(kb.WorkerCapabilityConfigError):
        kb._resolve_worker_cli_configuration("/tmp/profile")


def test_capability_fingerprint_ignores_unrelated_tool_module_changes(
    monkeypatch,
    tmp_path,
):
    runtime_root = tmp_path / "runtime"
    tools_dir = runtime_root / "tools"
    hermes_cli_dir = runtime_root / "hermes_cli"
    profile_home = tmp_path / "profile"
    tools_dir.mkdir(parents=True)
    hermes_cli_dir.mkdir()
    profile_home.mkdir()
    (runtime_root / "toolsets.py").write_text("TOOLSETS = {}\n")
    (tools_dir / "registry.py").write_text("# registry\n")
    (profile_home / "config.yaml").write_text("agent: {}\n")
    relevant = tools_dir / "browser_tool.py"
    relevant.write_text(
        "registry.register(name='browser_click', toolset='browser')\n"
    )
    unrelated = tools_dir / "email_tool.py"
    unrelated.write_text(
        "registry.register(name='email_send', toolset='email')\n"
    )
    fake_module = hermes_cli_dir / "kanban_db.py"
    fake_module.write_text("# orchestration\n")
    monkeypatch.setattr(kb, "__file__", str(fake_module))
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_home)

    first = kb._worker_capability_fingerprint(
        runtime_declared=["browser_click"],
        toolsets=["browser"],
        disabled_toolsets=[],
        env=env,
    )
    unrelated.write_text(
        "registry.register(name='email_send', toolset='email')\n# changed\n"
    )
    assert kb._worker_capability_fingerprint(
        runtime_declared=["browser_click"],
        toolsets=["browser"],
        disabled_toolsets=[],
        env=env,
    ) == first

    relevant.write_text(
        "registry.register(name='browser_click', toolset='browser')\n# changed\n"
    )
    assert kb._worker_capability_fingerprint(
        runtime_declared=["browser_click"],
        toolsets=["browser"],
        disabled_toolsets=[],
        env=env,
    ) != first
