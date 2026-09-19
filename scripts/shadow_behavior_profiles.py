#!/usr/bin/env python3
"""Offline, zero-effect legacy/selected-profile replay with full outputs and semantic diffs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


SEMANTIC_FIELDS = ("decision", "review_outcome", "objective", "stages", "callback", "package",
                   "task_states", "external_effect_count", "successor_task_count", "approval_states")


def deny_external_effects(event, _args):
    # This guards CPython's audited socket/process APIs, including UDP and
    # direct forks. It is not an OS sandbox for hostile native extensions.
    if event.startswith("socket.") or event in {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        "os.fork", "os.forkpty", "os.spawn", "os.startfile", "os.startfile/2",
    }:
        raise RuntimeError("Shadow replay prohibits network and process execution: " + event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--version",
        required=True,
        help="Exact installed candidate behavior profile version to verify",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    task_path = os.environ.get("PATH", "")
    os.environ.clear()
    os.environ.update(PATH=task_path, LANG="C.UTF-8", TZ="UTC", HOME=str(output),
                      HERMES_HOME=str(output / "hermes-home"), HERMES_KANBAN_DB=str(output / "unused.db"))
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    sys.addaudithook(deny_external_effects)
    from proactive.behavior_observation import capture
    from scripts.replay_behavior_observation import load_cases, replay_case
    fixture = load_cases()
    report = {"network_and_subprocess_blocked": True, "model_called": False,
              "isolation": "CPython audit hook; not an OS sandbox for native code",
              "source_root": str(root), "fixture_sha256": hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest(),
              "cases": {}}
    with (output / "observations.jsonl").open("w") as observations:
        with capture(lambda record: observations.write(json.dumps(record, ensure_ascii=False) + "\n")):
            for case in fixture["cases"]:
                results = {}
                for version in (None, args.version):
                    label = version or "legacy"
                    os.environ["HERMES_HOME"] = str(output / (case["id"] + "-" + label + "-home"))
                    result = replay_case(fixture, case, output / (case["id"] + "-" + label + ".db"), behavior_version=version)
                    (output / (case["id"] + "-" + label + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2))
                    results[label] = result
                differences = {key: {label: result.get(key) for label, result in results.items()}
                               for key in SEMANTIC_FIELDS if results["legacy"].get(key) != results[args.version].get(key)}
                report["cases"][case["id"]] = {"semantic_differences": differences,
                    "compiler_bytes_changed": results["legacy"].get("execution_body") != results[args.version].get("execution_body"),
                    "decision": results[args.version]["decision"], "review_outcome": results[args.version]["review_outcome"]}
    report["passed"] = not any(case["semantic_differences"] for case in report["cases"].values())
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
