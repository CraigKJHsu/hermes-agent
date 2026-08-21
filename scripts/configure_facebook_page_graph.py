#!/usr/bin/env python3
"""Configure one Facebook Page Graph API identity without leaking secrets."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import re
import tempfile


AUTHORIZED_PAGE_URL = "https://www.facebook.com/solobizai"


ENV_KEYS = (
    "FACEBOOK_GRAPH_API_VERSION",
    "FACEBOOK_PAGE_ID",
    "FACEBOOK_PAGE_NAME",
    "FACEBOOK_PAGE_URL",
    "FACEBOOK_PAGE_ACCESS_TOKEN",
    "FACEBOOK_APP_SECRET",
)


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_env(env_path: Path, values: dict[str, str]) -> None:
    existing = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    output: list[str] = []
    updated: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            output.append(f"{key}={_dotenv_value(values[key])}")
            updated.add(key)
        else:
            output.append(line)
    for key in ENV_KEYS:
        if key in values and key not in updated:
            output.append(f"{key}={_dotenv_value(values[key])}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.facebook.",
        dir=env_path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        # mkstemp creates the file as 0600 before any credential bytes are
        # written, independent of the caller's umask.
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, env_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    env_path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely save one Facebook Page token in ~/.hermes/.env",
    )
    parser.add_argument("--page-id", required=True)
    parser.add_argument(
        "--page-name",
        default="AI BizWeek｜SoloBiz AI 一人公司商業誌",
    )
    parser.add_argument(
        "--page-url",
        default="https://www.facebook.com/solobizai",
    )
    parser.add_argument("--api-version", default="v26.0")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / ".hermes" / ".env",
    )
    args = parser.parse_args()

    if re.fullmatch(r"[1-9][0-9]*", args.page_id) is None:
        parser.error("--page-id must be numeric")
    if re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", args.api_version) is None:
        parser.error("--api-version must look like v26.0")
    if args.page_url != AUTHORIZED_PAGE_URL:
        parser.error("--page-url must be the canonical authorized Page URL")

    page_token = getpass("Facebook Page access token (hidden): ").strip()
    if not page_token:
        parser.error("a Page access token is required")
    app_secret = getpass(
        "Facebook App Secret (hidden, optional; Enter to skip): "
    ).strip()
    values = {
        "FACEBOOK_GRAPH_API_VERSION": args.api_version,
        "FACEBOOK_PAGE_ID": args.page_id,
        "FACEBOOK_PAGE_NAME": args.page_name.strip(),
        "FACEBOOK_PAGE_URL": AUTHORIZED_PAGE_URL,
        "FACEBOOK_PAGE_ACCESS_TOKEN": page_token,
    }
    if app_secret:
        values["FACEBOOK_APP_SECRET"] = app_secret
    _write_env(args.env_file.expanduser(), values)
    print(f"Saved Facebook Page Graph API configuration to {args.env_file}")
    print("Secrets were entered without shell-history exposure and were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
