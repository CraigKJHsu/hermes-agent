"""CLI for versioned managed policies and Topic bindings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from proactive.policy_registry import (
    activate_policy,
    bind_topic_policies,
    create_policy_version,
    policy_status,
    topic_policy_binding,
)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_create(args: argparse.Namespace) -> int:
    content = Path(args.file).expanduser().resolve().read_text(encoding="utf-8")
    active_expectation = {}
    if args.expect_no_active:
        active_expectation["expected_active_version"] = None
    elif args.expected_active_version is not None:
        active_expectation["expected_active_version"] = args.expected_active_version
    result = create_policy_version(
        args.policy_id,
        args.version,
        content,
        owner_scope=args.owner_scope,
        owner_id=args.owner_id,
        supersedes=args.supersedes,
        activate=args.activate,
        **active_expectation,
    )
    _print_json(result)
    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    active_expectation = {}
    if args.expect_no_active:
        active_expectation["expected_active_version"] = None
    elif args.expected_active_version is not None:
        active_expectation["expected_active_version"] = args.expected_active_version
    _print_json(
        activate_policy(
            args.policy_id,
            args.version,
            **active_expectation,
        )
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _print_json(policy_status(args.policy_id))
    return 0


def _requirement(value: str) -> dict[str, object]:
    policy_id, separator, version = str(value or "").strip().partition("@")
    if separator:
        return {
            "policy_id": policy_id,
            "resolution": "fixed",
            "version": version,
        }
    return {"policy_id": policy_id, "resolution": "latest_active"}


def _cmd_bind_topic(args: argparse.Namespace) -> int:
    if not args.clear and not args.policy:
        raise ValueError("bind-topic requires --policy or --clear")
    requirements = [] if args.clear else [_requirement(item) for item in args.policy]
    current = topic_policy_binding(args.namespace)
    _print_json(
        bind_topic_policies(
            args.namespace,
            requirements,
            expected_binding_sha256=current["binding_sha256"],
        )
    )
    return 0


def _cmd_topic(args: argparse.Namespace) -> int:
    _print_json(topic_policy_binding(args.namespace))
    return 0


def policy_command(args: argparse.Namespace) -> int:
    handler = getattr(args, "policy_handler", None)
    if handler is None:
        args.policy_parser.print_help()
        return 1
    return int(handler(args))


def build_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "policy",
        help="Manage versioned policies and Topic bindings",
    )
    parser.set_defaults(policy_parser=parser)
    commands = parser.add_subparsers(dest="policy_command")

    create = commands.add_parser("create", aliases=["update"], help="Create an immutable version")
    create.add_argument("policy_id")
    create.add_argument("version")
    create.add_argument("--file", required=True)
    create.add_argument(
        "--owner-scope",
        required=True,
        choices=["global", "brand", "channel", "topic"],
    )
    create.add_argument("--owner-id", required=True)
    create.add_argument("--supersedes")
    create.add_argument("--activate", action="store_true")
    create_expectation = create.add_mutually_exclusive_group()
    create_expectation.add_argument("--expected-active-version")
    create_expectation.add_argument("--expect-no-active", action="store_true")
    create.set_defaults(policy_handler=_cmd_create)

    activate = commands.add_parser("activate", help="Activate an existing version")
    activate.add_argument("policy_id")
    activate.add_argument("version")
    activate_expectation = activate.add_mutually_exclusive_group()
    activate_expectation.add_argument("--expected-active-version")
    activate_expectation.add_argument("--expect-no-active", action="store_true")
    activate.set_defaults(policy_handler=_cmd_activate)

    status = commands.add_parser("status", help="Show a policy manifest")
    status.add_argument("policy_id")
    status.set_defaults(policy_handler=_cmd_status)

    bind = commands.add_parser(
        "bind-topic",
        help="Replace one Topic's policy bindings",
    )
    bind.add_argument("namespace")
    bind.add_argument(
        "--policy",
        action="append",
        default=[],
        help="Policy id for latest active, or policy_id@version for a fixed binding",
    )
    bind.add_argument("--clear", action="store_true")
    bind.set_defaults(policy_handler=_cmd_bind_topic)

    topic = commands.add_parser("topic", help="Show one Topic's bindings")
    topic.add_argument("namespace")
    topic.set_defaults(policy_handler=_cmd_topic)
    return parser
