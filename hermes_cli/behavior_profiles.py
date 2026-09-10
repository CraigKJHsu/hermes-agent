"""Operator-only profile inspection, new-Objective canary and explicit migration.

python -m hermes_cli.behavior_profiles show OBJECTIVE_ID
python -m hermes_cli.behavior_profiles select selection.json
python -m hermes_cli.behavior_profiles migrate migration.json [--apply]
"""
import argparse
from contextlib import closing
import sqlite3
import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from proactive.behavior_profiles import registry as br


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
    args = parser.parse_args()
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
