from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str, body=None):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=body,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_TOOL_NAMES", "stale_parent_scope")
    monkeypatch.setenv("HERMES_KANBAN_ACCEPTED_SOURCE_BUNDLE", "/stale/bundle.json")
    monkeypatch.setenv("HERMES_KANBAN_ACCEPTED_SOURCE_SHA256", "f" * 64)

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned
    assert "HERMES_KANBAN_TOOL_NAMES" not in captured["env"]
    assert "HERMES_KANBAN_ACCEPTED_SOURCE_BUNDLE" not in captured["env"]
    assert "HERMES_KANBAN_ACCEPTED_SOURCE_SHA256" not in captured["env"]


def test_compiled_worker_narrows_profile_toolsets_and_discovery(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - browser\n    - file\n    - session_search\n"
        "    - skills\n    - terminal\n    - web\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_ACCEPTED_SOURCE_BUNDLE", "/stale/bundle.json")
    monkeypatch.setenv("HERMES_KANBAN_ACCEPTED_SOURCE_SHA256", "f" * 64)

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb,
        "_probe_worker_capabilities",
        lambda **_kwargs: {
            "ok": True,
            "declared_tools": [],
            "required_runtime_tools": [],
            "available_tools": [],
            "missing_required_tools": [],
        },
    )
    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        '{"routing":{"resolved":{"assignment":{"allowed_tools":'
        '["kanban","status_check","logs_read","report_generate"]}}}}'
        "\n```"
    )

    kb._default_spawn(
        _make_task(kb, assignee="elias", body=body), str(workspace)
    )

    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    assert pinned == ["kanban"]
    assert not {"browser", "browser-cdp", "session_search", "skills", "web"}.intersection(pinned)
    discovery_names = set(captured["env"]["HERMES_KANBAN_TOOL_NAMES"].split(","))
    assert discovery_names == {
        "kanban_show", "kanban_comment", "kanban_complete", "kanban_block",
    }


def test_grace_review_worker_uses_minimal_cold_start_surface(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "default"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - browser\n    - kanban\n"
        "    - managed_policy\n    - memory\n    - skills\n    - vision\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: grace_review\n```json\n"
        '{"stop_rules":{"max_runtime_seconds":900},"policy_snapshots":[]}'
        "\n```"
    )

    kb._default_spawn(
        _make_task(kb, assignee="default", body=body), str(workspace)
    )

    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    assert pinned == ["kanban", "managed_policy", "vision"]
    assert "--accept-hooks" not in captured["cmd"]
    assert captured["env"]["HERMES_SAFE_MODE"] == "1"
    assert captured["env"]["HERMES_KANBAN_REVIEW_SKIP_MEMORY"] == "1"
    assert set(captured["env"]["HERMES_KANBAN_TOOL_NAMES"].split(",")) == {
        "kanban_show", "kanban_comment", "kanban_complete", "kanban_block",
        "managed_policy_read", "vision_analyze",
    }
    assert "FORMAL_REVIEW_EVIDENCE_ROUTE" in captured["cmd"][-1]


def test_default_spawn_announces_controller_accepted_source_handoff(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb
    from tools import facebook_page_graph_tool as page_tool

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb,
        "_probe_worker_capabilities",
        lambda **_kwargs: {
            "ok": True,
            "declared_tools": ["terminal"],
            "required_runtime_tools": ["terminal"],
            "available_tools": ["terminal"],
            "missing_required_tools": [],
        },
    )
    receipt = {
        "bundle_path": str(tmp_path / "workspace" / "accepted-page-source.json"),
        "bundle_sha256": "a" * 64,
        "message_sha256": "b" * 64,
        "message_utf8_bytes": 4497,
    }
    monkeypatch.setattr(
        page_tool,
        "materialize_accepted_page_relay_handoff",
        lambda *_args, **_kwargs: receipt,
    )
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        '{"completion_handoff":{"metadata_source":"workspace_file"},'
        '"routing":{"resolved":{"task_type":"devops","assignment":'
        '{"allowed_tools":["terminal"],"required_callable_tools":["terminal"]}}},'
        '"scope":{"allowed":["Use accepted Facebook Page package: '
        'execution_task_id=t_11111111; review_task_id=t_22222222"]}}'
        "\n```"
    )

    kb._default_spawn(
        _make_task(kb, assignee="elias", body=body), str(workspace)
    )

    prompt = captured["cmd"][-1]
    assert "CONTROLLER_ACCEPTED_SOURCE_HANDOFF" in prompt
    assert receipt["bundle_path"] in prompt
    assert receipt["bundle_sha256"] in prompt
    assert captured["env"]["HERMES_KANBAN_ACCEPTED_SOURCE_BUNDLE"] == receipt[
        "bundle_path"
    ]
    assert captured["env"]["HERMES_KANBAN_ACCEPTED_SOURCE_SHA256"] == receipt[
        "bundle_sha256"
    ]


def test_compiled_worker_with_empty_tools_keeps_only_lifecycle_surface():
    from hermes_cli import kanban_db as kb

    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        '{"routing":{"resolved":{"assignment":{"allowed_tools":[]}}}}'
        "\n```"
    )

    assert kb._task_scoped_worker_toolsets(
        body, ["file", "kanban", "terminal", "web"],
    ) == ["kanban"]
    assert kb._worker_tool_names([]) == [
        "kanban_block", "kanban_comment", "kanban_complete", "kanban_show",
    ]


def test_compiled_worker_discovers_only_declared_callable_not_toolset_siblings():
    from hermes_cli import kanban_db as kb

    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        '{"routing":{"resolved":{"assignment":{"allowed_tools":'
        '["browser_snapshot"]}}}}\n```'
    )

    toolsets = kb._task_scoped_worker_toolsets(
        body, ["browser", "browser-cdp", "kanban"],
    )
    names = set(kb._worker_tool_names(["browser_snapshot"]))
    assert "browser" in toolsets
    assert names == {
        "browser_snapshot", "kanban_show", "kanban_comment",
        "kanban_complete", "kanban_block",
    }
    assert "browser_click" not in names

def test_compiled_worker_adds_concrete_callable_toolset():
    from hermes_cli import kanban_db as kb

    body = (
        "GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
        '{"routing":{"resolved":{"assignment":{"required_callable_tools":'
        '["browser_upload_files"]}}}}\n```'
    )

    assert "browser-cdp" not in kb._task_scoped_worker_toolsets(body, ["web"])
    assert "browser-cdp" in kb._task_scoped_worker_toolsets(
        body, ["web", "browser-cdp"]
    )


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_resolve_worker_cli_toolsets_adds_browser_cdp_when_browser_enabled(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "default"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - browser
    - terminal
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "browser" in resolved
    assert "browser-cdp" in resolved
