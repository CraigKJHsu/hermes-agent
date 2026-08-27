from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli.policy import build_parser, policy_command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    policy = build_parser(subparsers)
    policy.set_defaults(func=policy_command)
    return parser


def test_policy_cli_create_status_and_topic_binding(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    source = tmp_path / "policy.md"
    source.write_text("# Complete policy\n", encoding="utf-8")
    parser = _parser()

    args = parser.parse_args(
        [
            "policy",
            "create",
            "example-policy",
            "v1",
            "--file",
            str(source),
            "--owner-scope",
            "brand",
            "--owner-id",
            "Example",
            "--activate",
            "--expect-no-active",
        ]
    )
    assert args.func(args) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["active_version"] == "v1"

    args = parser.parse_args(
        [
            "policy",
            "bind-topic",
            "topic:5000",
            "--policy",
            "example-policy",
        ]
    )
    assert args.func(args) == 0
    binding = json.loads(capsys.readouterr().out)
    assert binding["requirements"][0]["resolution"] == "latest_active"

    args = parser.parse_args(["policy", "topic", "topic:5000"])
    assert args.func(args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["requirements"] == binding["requirements"]


def test_policy_cli_requires_explicit_bind_or_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    args = _parser().parse_args(["policy", "bind-topic", "topic:5000"])
    with pytest.raises(ValueError, match="--policy or --clear"):
        args.func(args)
