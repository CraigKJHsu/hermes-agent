"""Operator-only profile inspection, new-Objective canary and explicit migration.

python -m hermes_cli.behavior_profiles show OBJECTIVE_ID
python -m hermes_cli.behavior_profiles select selection.json
python -m hermes_cli.behavior_profiles migrate migration.json [--apply]
python -m hermes_cli.behavior_profiles health [--profile PROFILE --version VERSION]
"""
import argparse
from contextlib import closing
import sqlite3
import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from proactive.behavior_profiles import registry as br


def _pin_health(pin):
    result = br.inspect_pin(pin)
    result.pop("manifest")
    result["pin"] = pin
    return result


def _profile_health(profile_id, version):
    try:
        return _pin_health(br._make_pin(profile_id, version, {}))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "profile_id": profile_id, "version": version,
                "errors": [str(exc)]}


def health(*, board=None, profile_id=None, version=None):
    """Inspect either one candidate or the board; never create or migrate it."""
    result = {"ok": False, "source_root": str(br.CODE_ROOT),
              "runtime": br.runtime_health(), "errors": []}
    if profile_id is not None:
        result["candidate"] = _profile_health(profile_id, version)
        result["ok"] = result["runtime"]["ok"] and result["candidate"]["ok"]
        return result
    path = kb.kanban_db_path(board=board).resolve()
    result.update(board_path=str(path), selections=[], objectives=[])
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            for row in conn.execute(
                "SELECT * FROM grace_behavior_selections WHERE profile_id IS NOT NULL "
                "ORDER BY platform,chat_id,thread_id,project"
            ):
                result["selections"].append({**dict(row),
                    "health": _profile_health(row["profile_id"], row["profile_version"])})
            statuses = sorted(kb._ACTIVE_GRACE_OBJECTIVE_STATUSES)
            for row in conn.execute(
                "SELECT o.objective_id,o.platform,o.chat_id,o.thread_id,o.status,o.revision,p.pin "
                "FROM grace_objectives o JOIN grace_objective_behavior_pins p USING(objective_id) "
                f"WHERE o.status IN ({','.join('?' for _ in statuses)}) ORDER BY o.objective_id",
                statuses,
            ):
                entry = dict(row)
                try:
                    entry["health"] = _pin_health(json.loads(entry.pop("pin")))
                except (ValueError, KeyError, TypeError) as exc:
                    entry["health"] = {"ok": False, "errors": [str(exc)]}
                result["objectives"].append(entry)
    except (OSError, sqlite3.Error) as exc:
        result["errors"].append(f"behavior.board_unavailable: {exc}")
    result["ok"] = (not result["errors"] and result["runtime"]["ok"]
                    and all(row["health"]["ok"] for row in result["selections"] + result["objectives"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("objective_id")
    select = commands.add_parser("select")
    select.add_argument("specification")
    migration = commands.add_parser("migrate")
    migration.add_argument("specification")
    migration.add_argument("--apply", action="store_true")
    check = commands.add_parser("health")
    check.add_argument("--profile", dest="profile_id")
    check.add_argument("--version")
    args = parser.parse_args()
    if args.command == "health":
        if (args.profile_id is None) != (args.version is None):
            parser.error("health requires --profile and --version together")
        result = health(board=args.board, profile_id=args.profile_id, version=args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.command == "show" or (args.command == "migrate" and not args.apply):
        path = kb.kanban_db_path(board=args.board).resolve()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        context = closing(connection)
    else:
        context = kb.connect_closing(board=args.board)
    with context as conn:
        if args.command == "show":
            pin = br.get_pin(conn, args.objective_id)
            result = {"objective": kb.get_grace_objective(conn, args.objective_id),
                      "behavior_pin": pin, "expected_pin_hash": br.digest(pin),
                      "legacy_version": "unresolved" if pin is None else None}
        else:
            spec = json.loads(Path(args.specification).read_text())
            if args.command == "select":
                result = br.set_selection(conn, **spec)
            else:
                if "apply" in spec:
                    raise ValueError("Use --apply explicitly; specification cannot activate a migration")
                result = br.migrate_objective(conn, **spec, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
