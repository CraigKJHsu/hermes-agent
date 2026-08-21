"""Shared helpers for attaching Hermes to a local Chromium-family CDP port."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import threading
import time

from hermes_constants import get_hermes_home


DEFAULT_BROWSER_CDP_PORT = 9222
DEFAULT_BROWSER_CDP_URL = f"http://127.0.0.1:{DEFAULT_BROWSER_CDP_PORT}"
DEFAULT_LOCAL_BROWSER_RECOVERY_TIMEOUT = 12.0


class _LocalBrowserRecoveryAttempt:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = False


_LOCAL_BROWSER_RECOVERY_LOCK = threading.Lock()
_LOCAL_BROWSER_RECOVERY_ACTIVE: _LocalBrowserRecoveryAttempt | None = None

_DARWIN_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

_WINDOWS_BROWSER_GROUPS = (
    (("chrome.exe", "chrome"), (("Google", "Chrome", "Application", "chrome.exe"),)),
    (
        ("chromium.exe", "chromium"),
        (("Chromium", "Application", "chrome.exe"), ("Chromium", "Application", "chromium.exe")),
    ),
    (("brave.exe", "brave"), (("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),)),
    (("msedge.exe", "msedge"), (("Microsoft", "Edge", "Application", "msedge.exe"),)),
)

_WINDOWS_BIN_NAMES = tuple(name for names, _ in _WINDOWS_BROWSER_GROUPS for name in names)
_WINDOWS_INSTALL_PARTS = tuple(parts for _, group in _WINDOWS_BROWSER_GROUPS for parts in group)

_LINUX_BROWSER_GROUPS = (
    (
        ("google-chrome", "google-chrome-stable"),
        ("/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
    ),
    (
        ("chromium-browser", "chromium"),
        ("/usr/bin/chromium-browser", "/usr/bin/chromium"),
    ),
    (
        ("brave-browser", "brave-browser-stable", "brave"),
        (
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/usr/bin/brave",
            "/snap/bin/brave",
            "/opt/brave.com/brave/brave-browser",
            "/opt/brave.com/brave/brave",
            "/opt/brave-bin/brave",
        ),
    ),
    (
        ("microsoft-edge", "microsoft-edge-stable", "msedge"),
        (
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/microsoft-edge",
            "/opt/microsoft/msedge/msedge",
        ),
    ),
)

_LINUX_BIN_NAMES = tuple(name for names, _ in _LINUX_BROWSER_GROUPS for name in names)
_LINUX_INSTALL_PATHS = tuple(path for _, paths in _LINUX_BROWSER_GROUPS for path in paths)


def get_chrome_debug_candidates(system: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized in seen or not os.path.isfile(path):
            return
        candidates.append(path)
        seen.add(normalized)

    def add_windows_install_paths(
        bases: tuple[str | None, ...],
        install_groups: tuple[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], ...],
    ) -> None:
        for _, group in install_groups:
            for base in filter(None, bases):
                for parts in group:
                    add(os.path.join(base, *parts))

    if system == "Darwin":
        for app in _DARWIN_APPS:
            add(app)
        return candidates

    if system == "Windows":
        install_bases = (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        )
        for names, install_parts in _WINDOWS_BROWSER_GROUPS:
            for name in names:
                add(shutil.which(name))
            for base in filter(None, install_bases):
                for parts in install_parts:
                    add(os.path.join(base, *parts))
        return candidates

    for names, paths in _LINUX_BROWSER_GROUPS:
        for name in names:
            add(shutil.which(name))
        for path in paths:
            add(path)
    add_windows_install_paths(("/mnt/c/Program Files", "/mnt/c/Program Files (x86)"), _WINDOWS_BROWSER_GROUPS)
    return candidates


def chrome_debug_data_dir() -> str:
    return str(get_hermes_home() / "chrome-debug")


def _chrome_debug_args(port: int) -> list[str]:
    return [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={chrome_debug_data_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def is_browser_debug_ready(url: str, timeout: float = 1.0) -> bool:
    """Return True when ``url`` exposes a reachable Chrome DevTools endpoint."""
    import socket
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        return False

    if parsed.scheme in {"ws", "wss"} and parsed.path.startswith("/devtools/browser/"):
        if not parsed.hostname:
            return False
        try:
            with socket.create_connection((parsed.hostname, port), timeout=timeout):
                return True
        except OSError:
            return False

    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"http", "https"} or not parsed.netloc:
        return False

    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    for probe in (f"{root}/json/version", f"{root}/json"):
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except Exception:
            continue
    return False


def manual_chrome_debug_command(port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None) -> str | None:
    system = system or platform.system()
    candidates = get_chrome_debug_candidates(system)

    if candidates:
        argv = [candidates[0], *_chrome_debug_args(port)]
        return subprocess.list2cmdline(argv) if system == "Windows" else shlex.join(argv)

    if system == "Darwin":
        data_dir = chrome_debug_data_dir()
        return (
            f'open -a "Google Chrome" --args --remote-debugging-port={port} '
            f'--user-data-dir="{data_dir}" --no-first-run --no-default-browser-check'
        )

    return None


def _detach_kwargs(system: str) -> dict:
    if system != "Windows":
        return {"start_new_session": True}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return {"creationflags": flags} if flags else {}


def try_launch_chrome_debug(
    port: int = DEFAULT_BROWSER_CDP_PORT,
    system: str | None = None,
    *,
    deadline: float | None = None,
) -> bool:
    system = system or platform.system()
    if deadline is not None and time.monotonic() >= deadline:
        return False
    candidates = get_chrome_debug_candidates(system)
    if not candidates:
        return False

    os.makedirs(chrome_debug_data_dir(), exist_ok=True)
    for candidate in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        try:
            subprocess.Popen(
                [candidate, *_chrome_debug_args(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_detach_kwargs(system),
            )
            return True
        except Exception:
            continue
    return False


def is_default_local_browser_debug_url(url: str) -> bool:
    """Return True only for Hermes' discovery-style localhost CDP endpoint."""
    raw = url or ""
    return raw in {
        "http://127.0.0.1:9222",
        "http://127.0.0.1:9222/",
        "http://127.0.0.1:9222/json",
        "http://127.0.0.1:9222/json/version",
        "http://localhost:9222",
        "http://localhost:9222/",
        "http://localhost:9222/json",
        "http://localhost:9222/json/version",
    }


def recover_default_local_browser_debug(
    url: str,
    *,
    timeout: float = 12.0,
    poll_interval: float = 0.5,
) -> bool:
    """Start and health-check Hermes' local CDP browser after a disconnect.

    Recovery is limited to the default localhost discovery endpoint. It never
    launches for remote, custom-port, or concrete WebSocket targets. The
    persistent Hermes Chrome profile is reused so ClawOps can keep the same
    browser login state after recovery.
    """
    global _LOCAL_BROWSER_RECOVERY_ACTIVE
    if not is_default_local_browser_debug_url(url):
        return False
    deadline = time.monotonic() + max(0.0, timeout)
    if time.monotonic() >= deadline:
        return False

    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _LOCAL_BROWSER_RECOVERY_LOCK.acquire(timeout=remaining):
        return False
    new_attempt = False
    try:
        if _LOCAL_BROWSER_RECOVERY_ACTIVE is not None:
            attempt = _LOCAL_BROWSER_RECOVERY_ACTIVE
        else:
            attempt = _LocalBrowserRecoveryAttempt()
            _LOCAL_BROWSER_RECOVERY_ACTIVE = attempt
            recovery_deadline = (
                time.monotonic() + DEFAULT_LOCAL_BROWSER_RECOVERY_TIMEOUT
            )
            recovery_thread = threading.Thread(
                target=_run_default_local_browser_recovery,
                args=(attempt, recovery_deadline, poll_interval),
                daemon=True,
                name="hermes-cdp-recovery",
            )
            new_attempt = True
    finally:
        _LOCAL_BROWSER_RECOVERY_LOCK.release()

    if new_attempt:
        try:
            recovery_thread.start()
        except Exception:
            with _LOCAL_BROWSER_RECOVERY_LOCK:
                attempt.result = False
                if _LOCAL_BROWSER_RECOVERY_ACTIVE is attempt:
                    _LOCAL_BROWSER_RECOVERY_ACTIVE = None
                attempt.done.set()
            return False

    remaining = deadline - time.monotonic()
    if remaining <= 0 or not attempt.done.wait(timeout=remaining):
        return False
    return bool(attempt.result and time.monotonic() <= deadline)


def _run_default_local_browser_recovery(
    attempt: _LocalBrowserRecoveryAttempt,
    deadline: float,
    poll_interval: float,
) -> None:
    """Own one complete recovery attempt; callers only wait on its result."""
    global _LOCAL_BROWSER_RECOVERY_ACTIVE

    def ready() -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        result = is_browser_debug_ready(
            DEFAULT_BROWSER_CDP_URL,
            timeout=min(1.0, remaining),
        )
        return bool(result and time.monotonic() <= deadline)

    result = False
    try:
        if ready():
            result = True
        elif try_launch_chrome_debug(
            DEFAULT_BROWSER_CDP_PORT,
            deadline=deadline,
        ):
            while time.monotonic() < deadline:
                if ready():
                    result = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(max(0.05, poll_interval), remaining))
    finally:
        with _LOCAL_BROWSER_RECOVERY_LOCK:
            attempt.result = result
            if _LOCAL_BROWSER_RECOVERY_ACTIVE is attempt:
                _LOCAL_BROWSER_RECOVERY_ACTIVE = None
            attempt.done.set()
