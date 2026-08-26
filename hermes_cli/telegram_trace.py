"""Read-only ``hermes trace telegram`` command."""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace, _SubParsersAction
from typing import Any

from hermes_cli.interaction_index import InteractionIndex


def build_parser(subparsers: _SubParsersAction[Any]) -> ArgumentParser:
    trace = subparsers.add_parser(
        "trace",
        help="Inspect durable cross-agent correlation traces",
    )
    trace_subparsers = trace.add_subparsers(dest="trace_kind", required=True)
    telegram = trace_subparsers.add_parser(
        "telegram",
        help="Resolve Telegram inbound to delegation, tasks, OpenClaw and callback",
    )
    selectors = telegram.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--trace-id")
    selectors.add_argument("--message-id")
    selectors.add_argument("--delegation-id")
    selectors.add_argument("--task-id")
    selectors.add_argument("--run-id")
    telegram.add_argument(
        "--chat-id",
        help="Required with --message-id because Telegram message IDs are chat-scoped",
    )
    telegram.add_argument(
        "--json",
        action="store_true",
        help="Print the full machine-readable trace",
    )
    trace.set_defaults(func=cmd_trace)
    return trace


def cmd_trace(args: Namespace) -> int:
    if args.trace_kind != "telegram":
        raise ValueError("Unknown trace kind")
    result = InteractionIndex().trace_telegram(
        trace_id=args.trace_id or "",
        chat_id=args.chat_id or "",
        message_id=args.message_id or "",
        delegation_id=args.delegation_id or "",
        task_id=args.task_id or "",
        run_id=args.run_id or "",
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not result["traces"]:
        print("找不到符合條件的 Telegram 訊息路徑。")
        return 1
    for item in result["traces"]:
        path = item["telegram_message_path"]
        print(
            f"{path['trace_id']}  delegation={item['delegation_id']}  "
            f"state={item['state']}"
        )
        for hop in path.get("hops", []):
            source = (hop.get("from_actor") or {}).get("display_name") or "?"
            target = (hop.get("to_actor") or {}).get("display_name") or "?"
            identifiers = ", ".join(
                f"{key}={value}"
                for key, value in (hop.get("identifiers") or {}).items()
            )
            suffix = f" ({identifiers})" if identifiers else ""
            print(
                f"  {hop.get('stage', '?')}: {source} → {target} "
                f"[{hop.get('status', '?')}]{suffix}"
            )
    return 0
