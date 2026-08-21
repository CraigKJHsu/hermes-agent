"""Persistent CDP supervisor for browser dialog + frame detection.

One ``CDPSupervisor`` runs per Hermes ``task_id`` that has a reachable CDP
endpoint. It holds a single persistent WebSocket to the backend, subscribes
to ``Page`` / ``Runtime`` / ``Target`` events on every attached session
(top-level page and every OOPIF / worker target that auto-attaches), and
surfaces observable state — pending dialogs and frame tree — through a
thread-safe snapshot object that tool handlers consume synchronously.

The supervisor is NOT in the agent's tool schema. Its output reaches the
agent via two channels:

1. ``browser_snapshot`` merges supervisor state into its return payload
   (see ``tools/browser_tool.py``).
2. ``browser_dialog`` tool responds to a pending dialog by calling
   ``respond_to_dialog()`` on the active supervisor.

Design spec: ``website/docs/developer-guide/browser-supervisor.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


# ── Config defaults ───────────────────────────────────────────────────────────

DIALOG_POLICY_MUST_RESPOND = "must_respond"
DIALOG_POLICY_AUTO_DISMISS = "auto_dismiss"
DIALOG_POLICY_AUTO_ACCEPT = "auto_accept"

_VALID_POLICIES = frozenset(
    {DIALOG_POLICY_MUST_RESPOND, DIALOG_POLICY_AUTO_DISMISS, DIALOG_POLICY_AUTO_ACCEPT}
)

DEFAULT_DIALOG_POLICY = DIALOG_POLICY_MUST_RESPOND
DEFAULT_DIALOG_TIMEOUT_S = 300.0

# Snapshot caps for frame_tree — keep payloads bounded on ad-heavy pages.
FRAME_TREE_MAX_ENTRIES = 30
FRAME_TREE_MAX_OOPIF_DEPTH = 2

# Ring buffer of recent console-level events (used later by PR 2 diagnostics).
CONSOLE_HISTORY_MAX = 50

# Keep the last N closed dialogs in ``recent_dialogs`` so agents on backends
# that auto-dismiss server-side (e.g. Browserbase) can still observe that a
# dialog fired, even if they couldn't respond to it in time.
RECENT_DIALOGS_MAX = 20

# Magic host the injected dialog bridge XHRs to.  Intercepted via the CDP
# Fetch domain before any network resolution happens, so the hostname never
# has to exist.  Keep this ASCII + URL-safe; we also gate Fetch patterns on it.
DIALOG_BRIDGE_HOST = "hermes-dialog-bridge.invalid"
DIALOG_BRIDGE_URL_PATTERN = f"http://{DIALOG_BRIDGE_HOST}/*"

FACEBOOK_CROSSPOST_MUTATION_NAME = (
    "MarketplaceForSaleItemCreateXPostsMutation"
)
FACEBOOK_CROSSPOST_MUTATION_DOC_ID = "9628145373942390"

# Script injected into every frame via Page.addScriptToEvaluateOnNewDocument.
# Overrides alert/confirm/prompt to round-trip through a sync XHR that we
# intercept via Fetch.requestPaused. Works on Browserbase (whose CDP proxy
# auto-dismisses REAL native dialogs) because the native dialogs never fire
# in the first place — the overrides take precedence.
_DIALOG_BRIDGE_SCRIPT = r"""
(() => {
  if (window.__hermesDialogBridgeInstalled) return;
  window.__hermesDialogBridgeInstalled = true;
  const ENDPOINT = "http://hermes-dialog-bridge.invalid/";
  function ask(kind, message, defaultPrompt) {
    try {
      const xhr = new XMLHttpRequest();
      // Use GET with query params so we don't need to worry about request
      // body encoding in the Fetch interceptor.
      const params = new URLSearchParams({
        kind: String(kind || ""),
        message: String(message == null ? "" : message),
        default_prompt: String(defaultPrompt == null ? "" : defaultPrompt),
      });
      xhr.open("GET", ENDPOINT + "?" + params.toString(), false);  // sync
      xhr.send(null);
      if (xhr.status !== 200) return null;
      const body = xhr.responseText || "";
      let parsed;
      try { parsed = JSON.parse(body); } catch (e) { return null; }
      if (kind === "alert") return undefined;
      if (kind === "confirm") return Boolean(parsed && parsed.accept);
      if (kind === "prompt") {
        if (!parsed || !parsed.accept) return null;
        return parsed.prompt_text == null ? "" : String(parsed.prompt_text);
      }
      return null;
    } catch (e) {
      // If the bridge is unreachable, fall back to the native call so the
      // page still sees *some* behavior (the backend will auto-dismiss).
      return null;
    }
  }
  const realAlert   = window.alert;
  const realConfirm = window.confirm;
  const realPrompt  = window.prompt;
  window.alert   = function(message) { ask("alert",   message, ""); };
  window.confirm = function(message) {
    const r = ask("confirm", message, "");
    return r === null ? false : Boolean(r);
  };
  window.prompt  = function(message, def) {
    const r = ask("prompt", message, def == null ? "" : def);
    return r === null ? null : String(r);
  };
  // onbeforeunload — we can't really synchronously prompt the user from this
  // event without racing navigation.  Leave native behavior for now; the
  // supervisor's native-dialog fallback path still surfaces them in
  // recent_dialogs.
})();
"""


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class PendingDialog:
    """A JS dialog currently open on some frame's session."""

    id: str
    type: str  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str
    default_prompt: str
    opened_at: float
    cdp_session_id: str  # which attached CDP session the dialog fired in
    frame_id: Optional[str] = None
    # When set, the dialog was captured via the bridge XHR path (Fetch domain).
    # Response must be delivered via Fetch.fulfillRequest, NOT
    # Page.handleJavaScriptDialog — the native dialog never fired.
    bridge_request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "default_prompt": self.default_prompt,
            "opened_at": self.opened_at,
            "frame_id": self.frame_id,
        }


@dataclass
class CrosspostRequestGate:
    """One armed, exact Facebook cross-post GraphQL request boundary."""

    listing_id: str
    group_ids: Tuple[str, ...]
    session_id: str
    graphql_url_pattern: str
    page_identity: str
    armed_at: float
    completed: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    expired: bool = False
    consumed: bool = False


@dataclass
class DialogRecord:
    """A historical record of a dialog that was opened and then handled.

    Retained in ``recent_dialogs`` for a short window so agents on backends
    that auto-dismiss dialogs server-side (Browserbase) can still observe
    that a dialog fired, even though they couldn't respond to it.
    """

    id: str
    type: str
    message: str
    opened_at: float
    closed_at: float
    closed_by: str  # "agent" | "auto_policy" | "remote" | "watchdog"
    frame_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "frame_id": self.frame_id,
        }


@dataclass
class FrameInfo:
    """One frame in the page's frame tree.

    ``is_oopif`` means the frame has its own CDP target (separate process,
    reachable via ``cdp_session_id``). Same-origin / srcdoc iframes share
    the parent process and have ``is_oopif=False`` + ``cdp_session_id=None``.
    """

    frame_id: str
    url: str
    origin: str
    parent_frame_id: Optional[str]
    is_oopif: bool
    cdp_session_id: Optional[str] = None
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "frame_id": self.frame_id,
            "url": self.url,
            "origin": self.origin,
            "is_oopif": self.is_oopif,
        }
        if self.cdp_session_id:
            d["session_id"] = self.cdp_session_id
        if self.parent_frame_id:
            d["parent_frame_id"] = self.parent_frame_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ConsoleEvent:
    """Ring buffer entry for console + exception traffic."""

    ts: float
    level: str  # "log" | "error" | "warning" | "exception"
    text: str
    url: Optional[str] = None


@dataclass(frozen=True)
class SupervisorSnapshot:
    """Read-only snapshot of supervisor state.

    Frozen dataclass so tool handlers can freely dereference without
    worrying about mutation under their feet.
    """

    pending_dialogs: Tuple[PendingDialog, ...]
    recent_dialogs: Tuple[DialogRecord, ...]
    frame_tree: Dict[str, Any]
    console_errors: Tuple[ConsoleEvent, ...]
    active: bool  # False if supervisor is detached/stopped
    cdp_url: str
    task_id: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for inclusion in ``browser_snapshot`` output."""
        out: Dict[str, Any] = {
            "pending_dialogs": [d.to_dict() for d in self.pending_dialogs],
            "frame_tree": self.frame_tree,
        }
        if self.recent_dialogs:
            out["recent_dialogs"] = [d.to_dict() for d in self.recent_dialogs]
        return out


# ── Supervisor core ───────────────────────────────────────────────────────────


class CDPSupervisor:
    """One supervisor per (task_id, cdp_url) pair.

    Lifecycle:
      * ``start()`` — kicked off by ``SupervisorRegistry.get_or_start``; spawns
        a daemon thread running its own asyncio loop, connects the WebSocket,
        attaches to the first page target, enables domains, starts
        auto-attaching to child targets.
      * ``snapshot()`` — sync, thread-safe, called from tool handlers.
      * ``respond_to_dialog(action, ...)`` — sync bridge; schedules a coroutine
        on the supervisor's loop and waits (with timeout) for the CDP ack.
      * ``stop()`` — cancels task, closes WebSocket, joins thread.

    All CDP I/O lives on the supervisor's own loop. External callers never
    touch the loop directly; they go through the sync API above.
    """

    def __init__(
        self,
        task_id: str,
        cdp_url: str,
        *,
        expected_page_url: Optional[str] = None,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
    ) -> None:
        if dialog_policy not in _VALID_POLICIES:
            raise ValueError(
                f"Invalid dialog_policy {dialog_policy!r}; "
                f"must be one of {sorted(_VALID_POLICIES)}"
            )
        self.task_id = task_id
        self.cdp_url = cdp_url
        self.expected_page_url = str(expected_page_url or "").strip() or None
        self.dialog_policy = dialog_policy
        self.dialog_timeout_s = float(dialog_timeout_s)

        # State protected by ``_state_lock`` for cross-thread reads.
        self._state_lock = threading.Lock()
        self._pending_dialogs: Dict[str, PendingDialog] = {}
        self._recent_dialogs: List[DialogRecord] = []
        self._frames: Dict[str, FrameInfo] = {}
        self._console_events: List[ConsoleEvent] = []
        self._active = False

        # Supervisor loop machinery — populated in start().
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._stop_requested = False

        # CDP call tracking (runs on supervisor loop only).
        self._next_call_id = 1
        self._pending_calls: Dict[int, asyncio.Future] = {}
        self._ws: Optional[ClientConnection] = None
        self._page_session_id: Optional[str] = None
        self._page_target_id: Optional[str] = None
        self._child_sessions: Dict[str, Dict[str, Any]] = {}  # session_id -> info

        # Dialog auto-dismiss watchdog handles (per dialog id).
        self._dialog_watchdogs: Dict[str, asyncio.TimerHandle] = {}
        # Monotonic id generator for dialogs (human-readable in snapshots).
        self._dialog_seq = 0
        # Armed only around one contract-bound final cross-post click. The
        # request itself remains paused until its GraphQL variables pass.
        self._crosspost_request_gate: Optional[CrosspostRequestGate] = None
        self._crosspost_graphql_url_patterns: Dict[str, str] = {}
        self._crosspost_tainted_sessions: set[str] = set()

    # ── Public sync API ──────────────────────────────────────────────────────

    def start(self, timeout: float = 15.0) -> None:
        """Launch the background loop and wait until attachment is complete.

        Raises whatever exception attach failed with (connect error, bad
        WebSocket URL, CDP domain enable failure, etc.). On success, the
        supervisor is fully wired up — pending-dialog events will be captured
        as of the moment ``start()`` returns.
        """
        if self._thread and self._thread.is_alive():
            return
        self._ready_event.clear()
        self._start_error = None
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"cdp-supervisor-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=timeout):
            self.stop()
            raise TimeoutError(
                f"CDP supervisor did not attach within {timeout}s "
                f"(cdp_url={self.cdp_url[:80]}...)"
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise err

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel the supervisor task and join the thread."""
        self._stop_requested = True
        loop = self._loop
        if loop is not None and loop.is_running():
            # Close the WebSocket from inside the loop — this makes ``async for
            # raw in self._ws`` return cleanly, ``_run`` hits its ``finally``,
            # pending tasks get cancelled in order, THEN the thread exits.
            async def _close_ws():
                ws = self._ws
                self._ws = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            try:
                from agent.async_utils import safe_schedule_threadsafe
                fut = safe_schedule_threadsafe(_close_ws(), loop)
                if fut is not None:
                    try:
                        fut.result(timeout=2.0)
                    except Exception:
                        pass
            except RuntimeError:
                pass  # loop already shutting down
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._state_lock:
            self._active = False

    def snapshot(self) -> SupervisorSnapshot:
        """Return an immutable snapshot of current state."""
        with self._state_lock:
            dialogs = tuple(self._pending_dialogs.values())
            recent = tuple(self._recent_dialogs[-RECENT_DIALOGS_MAX:])
            frames_tree = self._build_frame_tree_locked()
            console = tuple(self._console_events[-CONSOLE_HISTORY_MAX:])
            active = self._active
        return SupervisorSnapshot(
            pending_dialogs=dialogs,
            recent_dialogs=recent,
            frame_tree=frames_tree,
            console_errors=console,
            active=active,
            cdp_url=self.cdp_url,
            task_id=self.task_id,
        )

    def respond_to_dialog(
        self,
        action: str,
        *,
        prompt_text: Optional[str] = None,
        dialog_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Accept/dismiss a pending dialog. Sync bridge onto the supervisor loop.

        Returns ``{"ok": True, "dialog": {...}}`` on success,
        ``{"ok": False, "error": "..."}`` on a recoverable error (no dialog,
        ambiguous dialog_id, supervisor inactive).
        """
        if action not in {"accept", "dismiss"}:
            return {"ok": False, "error": f"action must be 'accept' or 'dismiss', got {action!r}"}

        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
            pending = list(self._pending_dialogs.values())
            if not pending:
                return {"ok": False, "error": "no dialog is currently open"}
            if dialog_id:
                dialog = self._pending_dialogs.get(dialog_id)
                if dialog is None:
                    return {
                        "ok": False,
                        "error": f"dialog_id {dialog_id!r} not found "
                        f"(known: {sorted(self._pending_dialogs)})",
                    }
            elif len(pending) > 1:
                return {
                    "ok": False,
                    "error": (
                        f"{len(pending)} pending dialogs; specify dialog_id. "
                        f"Candidates: {[d.id for d in pending]}"
                    ),
                }
            else:
                dialog = pending[0]
            snapshot_copy = dialog

        loop = self._loop
        if loop is None:
            return {"ok": False, "error": "supervisor loop is not running"}

        async def _do_respond():
            return await self._handle_dialog_cdp(
                snapshot_copy, accept=(action == "accept"), prompt_text=prompt_text or ""
            )

        try:
            from agent.async_utils import safe_schedule_threadsafe
            fut = safe_schedule_threadsafe(_do_respond(), loop)
            if fut is None:
                return {"ok": False, "error": "Browser supervisor loop unavailable"}
            fut.result(timeout=timeout)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "dialog": snapshot_copy.to_dict()}

    def evaluate_runtime(
        self,
        expression: str,
        *,
        return_by_value: bool = True,
        await_promise: bool = True,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Evaluate ``expression`` in the page's Runtime context over the live WS.

        Reuses the supervisor's already-connected WebSocket — zero subprocess
        startup cost vs the agent-browser CLI ``eval`` command (which does
        fork+exec+Node-startup+CDP-setup on every call).

        Returns a dict shaped like ``{"ok": True, "result": <value>, "result_type": "..."}``
        on success, or ``{"ok": False, "error": "..."}`` on failure.

        ``return_by_value=True`` asks the browser to JSON-serialize the result
        before sending it back, matching DevTools-console semantics for
        primitive / plain-object expressions. For DOM nodes or non-serializable
        objects, the browser returns a description string in ``result_type``.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "supervisor loop is not running"}

        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
            session_id = self._page_session_id

        if not session_id:
            return {"ok": False, "error": "supervisor has no attached page session"}

        async def _do_eval(by_value: bool) -> Dict[str, Any]:
            return await self._cdp(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": by_value,
                    "awaitPromise": await_promise,
                    # userGesture matters for things like clipboard / fullscreen
                    # APIs that require a user-activation context.
                    "userGesture": True,
                },
                session_id=session_id,
                timeout=timeout,
            )

        from agent.async_utils import safe_schedule_threadsafe

        def _run_eval(by_value: bool) -> Dict[str, Any]:
            fut = safe_schedule_threadsafe(_do_eval(by_value), loop)
            if fut is None:
                raise RuntimeError("Browser supervisor loop unavailable")
            return fut.result(timeout=timeout + 1)

        try:
            response = _run_eval(return_by_value)
        except Exception as exc:
            # ``returnByValue=True`` asks Chrome to deep-serialize the result.
            # For live DOM nodes / NodeLists / Window that serialization can
            # blow past CDP's recursion guard and fail the whole call with
            # ``Object reference chain is too long`` (a protocol-level error,
            # not a JS exception).  Retry once with ``returnByValue=False`` so
            # Chrome returns the object's description string instead — the same
            # graceful degradation path used for ``document.querySelector(...)``
            # results — rather than crashing the eval.
            if return_by_value and "reference chain is too long" in str(exc).lower():
                try:
                    response = _run_eval(False)
                except Exception as exc2:
                    return {"ok": False, "error": f"{type(exc2).__name__}: {exc2}"}
            else:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # Runtime.evaluate response shape:
        #   {"id": N, "result": {"result": {"type": "...", "value": ..., ...},
        #                         "exceptionDetails": {...} (only on error)}}
        result_payload = response.get("result", {}) if isinstance(response, dict) else {}
        exception_details = result_payload.get("exceptionDetails")
        if exception_details:
            # Surface the JS-side exception with a clean message.
            exc_text = exception_details.get("text") or "JavaScript exception"
            exc_obj = exception_details.get("exception") or {}
            description = exc_obj.get("description")
            if description:
                exc_text = f"{exc_text}: {description}"
            return {"ok": False, "error": exc_text}

        result_obj = result_payload.get("result", {})
        result_type = result_obj.get("type", "undefined")

        if "value" in result_obj:
            value = result_obj["value"]
        elif result_type == "undefined":
            value = None
        else:
            # Non-serializable (functions, DOM nodes, etc.) — return the
            # browser's string description so the model gets *something*.
            value = result_obj.get("description") or result_obj.get("unserializableValue")

        return {"ok": True, "result": value, "result_type": result_type}

    def call_page_cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Send one CDP command to this supervisor's attached page session."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "supervisor loop is not running"}
        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
            session_id = self._page_session_id
        if not session_id:
            return {"ok": False, "error": "supervisor has no attached page session"}

        async def _do_call() -> Dict[str, Any]:
            return await self._cdp(
                method,
                params or {},
                session_id=session_id,
                timeout=timeout,
            )

        try:
            from agent.async_utils import safe_schedule_threadsafe

            fut = safe_schedule_threadsafe(_do_call(), loop)
            if fut is None:
                return {"ok": False, "error": "Browser supervisor loop unavailable"}
            response = fut.result(timeout=timeout + 1)
            return {"ok": True, "result": response.get("result", {})}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def capture_ax_tree_for_url(
        self,
        expected_url: str,
        *,
        expected_page_identity: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Bind and capture one AX tree from the exact browser page load.

        A browser-level CDP endpoint can expose several ``page`` targets
        (including two tabs at the same URL). When ``expected_page_identity``
        is supplied, URL matches are disambiguated with the selected page's
        ``performance.timeOrigin`` rather than rejected merely because their
        URLs match.

        The agent-browser CLI selects the active web page independently, so
        selection, attachment, configuration, and AX capture run as one
        event-loop operation with one deadline. The returned session id is
        part of the guarded ref capability, so a later snapshot cannot
        redirect its action. Missing, duplicate, or changing page-load
        identities fail closed.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "supervisor loop is not running"}
        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
        deadline = time.monotonic() + timeout

        async def _capture() -> Dict[str, Any]:
            session_id = ""
            adopted = False
            expected_time_origin: Optional[float] = None
            if expected_page_identity is not None:
                identity_url, separator, raw_time_origin = (
                    expected_page_identity.rpartition("|")
                )
                try:
                    expected_time_origin = float(raw_time_origin)
                except (TypeError, ValueError):
                    expected_time_origin = None
                if (
                    not separator
                    or identity_url != expected_url
                    or expected_time_origin is None
                ):
                    return {
                        "ok": False,
                        "error": "expected browser page-load identity was invalid",
                    }

            async def _read_page_identity(
                candidate_session_id: str,
            ) -> tuple[str, Optional[float]]:
                identity_response = await self._cdp(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "({href: location.href, "
                            "timeOrigin: performance.timeOrigin})"
                        ),
                        "returnByValue": True,
                    },
                    session_id=candidate_session_id,
                )
                identity_result = (
                    identity_response.get("result", {})
                    .get("result", {})
                    .get("value")
                )
                if not isinstance(identity_result, dict):
                    return "", None
                href = str(identity_result.get("href") or "")
                raw_origin = identity_result.get("timeOrigin")
                try:
                    time_origin = float(raw_origin)
                except (TypeError, ValueError):
                    time_origin = None
                return href, time_origin

            response = await self._cdp("Target.getTargets")
            targets = response.get("result", {}).get("targetInfos", [])
            matches = [
                target for target in targets
                if target.get("type") == "page"
                and str(target.get("url") or "") == expected_url
            ]
            if not matches:
                return {
                    "ok": False,
                    "error": (
                        "expected at least one browser page target for "
                        f"{expected_url!r}, found 0"
                    ),
                }

            if expected_time_origin is not None:
                identity_matches: list[tuple[Dict[str, Any], str]] = []
                probe_session_ids: list[str] = []
                try:
                    for target in matches:
                        candidate_target_id = str(target.get("targetId") or "")
                        if not candidate_target_id:
                            return {
                                "ok": False,
                                "error": "matched browser page target had no targetId",
                            }
                        attach = await self._cdp(
                            "Target.attachToTarget",
                            {
                                "targetId": candidate_target_id,
                                "flatten": True,
                            },
                        )
                        candidate_session_id = str(
                            attach.get("result", {}).get("sessionId") or ""
                        )
                        if not candidate_session_id:
                            return {
                                "ok": False,
                                "error": (
                                    "browser page identity probe returned no "
                                    "sessionId"
                                ),
                            }
                        probe_session_ids.append(candidate_session_id)
                        href, time_origin = await _read_page_identity(
                            candidate_session_id,
                        )
                        if (
                            href == expected_url
                            and time_origin == expected_time_origin
                        ):
                            identity_matches.append(
                                (target, candidate_session_id)
                            )
                        else:
                            await self._cdp(
                                "Target.detachFromTarget",
                                {"sessionId": candidate_session_id},
                            )
                            probe_session_ids.remove(candidate_session_id)
                    if len(identity_matches) != 1:
                        return {
                            "ok": False,
                            "error": (
                                "expected exactly one browser page-load identity "
                                f"for {expected_url!r}, found "
                                f"{len(identity_matches)}"
                            ),
                        }
                    matches = [identity_matches[0][0]]
                    session_id = identity_matches[0][1]
                    probe_session_ids.remove(session_id)
                finally:
                    for candidate_session_id in probe_session_ids:
                        try:
                            await self._cdp(
                                "Target.detachFromTarget",
                                {"sessionId": candidate_session_id},
                                timeout=2.0,
                            )
                        except Exception as exc:
                            logger.debug(
                                "page identity probe cleanup failed: %s",
                                exc,
                            )
            elif len(matches) != 1:
                return {
                    "ok": False,
                    "error": (
                        "expected exactly one browser page target for "
                        f"{expected_url!r}, found {len(matches)}"
                    ),
                }

            target_id = str(matches[0].get("targetId") or "")
            if not target_id:
                return {
                    "ok": False,
                    "error": "matched browser page target had no targetId",
                }
            if not session_id:
                attach = await self._cdp(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
                session_id = str(attach.get("result", {}).get("sessionId") or "")
                if not session_id:
                    return {
                        "ok": False,
                        "error": "browser page target attach returned no sessionId",
                    }
            try:
                await self._configure_page_session(session_id)
                ax_response = await self._cdp(
                    "Accessibility.getFullAXTree",
                    session_id=session_id,
                )
                if expected_time_origin is not None:
                    final_url, final_time_origin = await _read_page_identity(
                        session_id,
                    )
                    if (
                        final_url != expected_url
                        or final_time_origin != expected_time_origin
                    ):
                        return {
                            "ok": False,
                            "error": (
                                "browser page-load identity changed during "
                                "AX capture"
                            ),
                        }
                with self._state_lock:
                    old_session_id = self._page_session_id
                if old_session_id and old_session_id != session_id:
                    await self._cdp(
                        "Target.detachFromTarget",
                        {"sessionId": old_session_id},
                    )
                # No await occurs between adoption and return, so deadline
                # cancellation cannot report failure and mutate state later.
                with self._state_lock:
                    self._page_target_id = target_id
                    self._page_session_id = session_id
                    self._frames.clear()
                adopted = True
                return {
                    "ok": True,
                    "target_id": target_id,
                    "session_id": session_id,
                    "result": ax_response.get("result", {}),
                }
            finally:
                if session_id and not adopted:
                    try:
                        await self._cdp(
                            "Target.detachFromTarget",
                            {"sessionId": session_id},
                            timeout=2.0,
                        )
                    except Exception as exc:
                        logger.debug(
                            "unadopted page session cleanup failed: %s",
                            exc,
                        )

        async def _capture_with_deadline() -> Dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("AX capture deadline expired before dispatch")
            return await asyncio.wait_for(_capture(), timeout=remaining)

        try:
            from agent.async_utils import safe_schedule_threadsafe

            fut = safe_schedule_threadsafe(_capture_with_deadline(), loop)
            if fut is None:
                return {"ok": False, "error": "Browser supervisor loop unavailable"}
            try:
                return fut.result(timeout=timeout + 1)
            except TimeoutError:
                fut.cancel()
                return {
                    "ok": False,
                    "error": (
                        "browser page AX capture timed out and its queued "
                        "operation was cancelled"
                    ),
                }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def call_session_cdp(
        self,
        session_id: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Send one CDP command to an explicitly captured page session."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "supervisor loop is not running"}
        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
        if not session_id:
            return {"ok": False, "error": "captured page session is unavailable"}
        deadline = time.monotonic() + timeout

        async def _do_call() -> Dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "captured page action deadline expired before dispatch"
                )
            return await self._cdp(
                method,
                params or {},
                session_id=session_id,
                timeout=remaining,
            )

        try:
            from agent.async_utils import safe_schedule_threadsafe

            fut = safe_schedule_threadsafe(_do_call(), loop)
            if fut is None:
                return {"ok": False, "error": "Browser supervisor loop unavailable"}
            try:
                response = fut.result(timeout=timeout + 1)
            except TimeoutError:
                fut.cancel()
                return {
                    "ok": False,
                    "error": (
                        "captured page CDP action timed out; dispatch outcome "
                        "is indeterminate and must not be retried"
                    ),
                    "dispatch_ambiguous": True,
                }
            return {"ok": True, "result": response.get("result", {})}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _arm_crosspost_request_gate(
        self,
        *,
        listing_id: str,
        group_ids: List[str],
        expected_page_identity: str,
        captured_session_id: Optional[str],
        timeout: float,
    ) -> Dict[str, Any]:
        """Pause the one canonical cross-post mutation before it can leave."""
        page_url = expected_page_identity.rsplit("|", 1)[0]
        parsed = urlsplit(page_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                "ok": False,
                "error": "Cross-post request gate has no trusted page origin",
            }
        normalized_groups = tuple(sorted({str(value) for value in group_ids}))
        if (
            not str(listing_id).isdigit()
            or not normalized_groups
            or len(normalized_groups) != len(group_ids)
            or any(not value.isdigit() for value in normalized_groups)
        ):
            return {
                "ok": False,
                "error": "Cross-post request gate received invalid contract ids",
            }
        with self._state_lock:
            session_id = captured_session_id or self._page_session_id or ""
            if not self._active or not session_id:
                return {
                    "ok": False,
                    "error": "Cross-post request gate has no active page session",
                }
            if self._crosspost_request_gate is not None:
                return {
                    "ok": False,
                    "error": "Another cross-post request gate is already armed",
                }
            if session_id in self._crosspost_tainted_sessions:
                return {
                    "ok": False,
                    "error": (
                        "The prior cross-post request timed out; restart the "
                        "controlled browser session before retrying"
                    ),
                }
            gate = CrosspostRequestGate(
                listing_id=str(listing_id),
                group_ids=normalized_groups,
                session_id=session_id,
                graphql_url_pattern=(
                    f"{parsed.scheme}://{parsed.netloc}/api/graphql/*"
                ),
                page_identity=expected_page_identity,
                armed_at=time.monotonic(),
            )
            self._crosspost_request_gate = gate
            previous_pattern = self._crosspost_graphql_url_patterns.get(session_id)
            self._crosspost_graphql_url_patterns[session_id] = (
                gate.graphql_url_pattern
            )

        enabled = self.call_session_cdp(
            session_id,
            "Fetch.enable",
            {
                "patterns": [
                    {
                        "urlPattern": DIALOG_BRIDGE_URL_PATTERN,
                        "requestStage": "Request",
                    },
                    {
                        "urlPattern": gate.graphql_url_pattern,
                        "requestStage": "Request",
                    },
                ],
                "handleAuthRequests": False,
            },
            timeout=min(timeout, 5.0),
        )
        if enabled.get("ok"):
            return {"ok": True, "gate": gate}
        with self._state_lock:
            if self._crosspost_request_gate is gate:
                self._crosspost_request_gate = None
            if previous_pattern is None:
                self._crosspost_graphql_url_patterns.pop(session_id, None)
            else:
                self._crosspost_graphql_url_patterns[session_id] = previous_pattern
        return {
            "ok": False,
            "error": (
                "Cross-post request interception could not be armed: "
                + str(enabled.get("error") or "unknown CDP error")
            ),
        }

    def _cancel_crosspost_request_gate(
        self,
        gate: CrosspostRequestGate,
        *,
        timeout: float,
    ) -> None:
        with self._state_lock:
            if self._crosspost_request_gate is not gate:
                return
            self._crosspost_request_gate = None

    def _taint_and_retire_crosspost_request_gate(
        self,
        gate: CrosspostRequestGate,
    ) -> None:
        """Fail closed after a dispatch whose external outcome is uncertain."""
        with self._state_lock:
            gate.expired = True
            if self._crosspost_request_gate is gate:
                self._crosspost_request_gate = None
            self._crosspost_tainted_sessions.add(gate.session_id)

    def _wait_for_crosspost_request_gate(
        self,
        gate: CrosspostRequestGate,
        *,
        timeout: float,
    ) -> Dict[str, Any]:
        if not gate.completed.wait(timeout=timeout):
            self._taint_and_retire_crosspost_request_gate(gate)
            return {
                "ok": False,
                "dispatch_ambiguous": True,
                "error": (
                    "Facebook cross-post mutation was not observed before "
                    "the guarded request deadline"
                ),
            }
        result = dict(gate.result or {
            "ok": False,
            "dispatch_ambiguous": True,
            "error": "Facebook cross-post request gate ended without a result",
        })
        if result.get("dispatch_ambiguous"):
            self._taint_and_retire_crosspost_request_gate(gate)
        return result

    @staticmethod
    def _parse_crosspost_graphql_request(
        request: Dict[str, Any],
        gate: Optional[CrosspostRequestGate],
        graphql_url_pattern: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Validate the canonical cross-post mutation without exposing its body."""
        url = str(request.get("url") or "")
        parsed_url = urlsplit(url)
        expected_url = urlsplit(
            (graphql_url_pattern or gate.graphql_url_pattern).rstrip("*")
        )
        if (
            parsed_url.scheme != expected_url.scheme
            or parsed_url.netloc != expected_url.netloc
            or parsed_url.path != expected_url.path
        ):
            return None

        post_data = request.get("postData")
        if not isinstance(post_data, str):
            if str(request.get("method") or "").upper() == "POST":
                return {
                    "target": True,
                    "ok": False,
                    "error": (
                        "GraphQL request body could not be inspected while "
                        "the Facebook cross-post gate was armed"
                    ),
                }
            return None
        fields = parse_qs(post_data, keep_blank_values=True)
        names = fields.get("fb_api_req_friendly_name", [])
        doc_ids = fields.get("doc_id", [])
        is_target = (
            FACEBOOK_CROSSPOST_MUTATION_NAME in names
            or FACEBOOK_CROSSPOST_MUTATION_DOC_ID in doc_ids
        )
        if not is_target:
            return None

        def rejected(message: str) -> Dict[str, Any]:
            return {"target": True, "ok": False, "error": message}

        if str(request.get("method") or "").upper() != "POST":
            return rejected("Facebook cross-post mutation did not use POST")
        if names != [FACEBOOK_CROSSPOST_MUTATION_NAME]:
            return rejected(
                "Facebook cross-post mutation name did not match the canonical operation"
            )
        if doc_ids != [FACEBOOK_CROSSPOST_MUTATION_DOC_ID]:
            return rejected(
                "Facebook cross-post mutation document did not match the canonical operation"
            )
        if gate is None:
            return rejected(
                "Facebook cross-post mutation was blocked because no authorized request gate was armed"
            )
        raw_variables = fields.get("variables", [])
        if len(raw_variables) != 1:
            return rejected(
                "Facebook cross-post mutation did not contain one variables payload"
            )
        try:
            variables = json.loads(raw_variables[0])
        except (TypeError, ValueError):
            return rejected("Facebook cross-post mutation variables were invalid JSON")
        input_data = variables.get("input") if isinstance(variables, dict) else None
        if not isinstance(input_data, dict):
            return rejected("Facebook cross-post mutation input was missing")
        item_id = str(input_data.get("item_id") or "")
        if item_id != gate.listing_id:
            return rejected(
                "Facebook cross-post mutation is not bound to the authorized listing"
            )
        raw_targets = input_data.get("additional_target_ids")
        if not isinstance(raw_targets, list):
            return rejected(
                "Facebook cross-post mutation destinations were not a list"
            )
        target_ids = tuple(str(value) for value in raw_targets)
        if (
            not target_ids
            or any(not value.isdigit() for value in target_ids)
            or len(set(target_ids)) != len(target_ids)
            or set(target_ids) != set(gate.group_ids)
        ):
            return rejected(
                "Facebook cross-post mutation destinations did not exactly match the authorized groups"
            )
        return {
            "target": True,
            "ok": True,
            "listing_id": item_id,
            "group_ids": sorted(target_ids),
        }

    async def _handle_crosspost_fetch_paused(
        self,
        params: Dict[str, Any],
        session_id: Optional[str],
        gate: Optional[CrosspostRequestGate],
        graphql_url_pattern: str,
    ) -> None:
        request_id = params.get("requestId")
        if not request_id:
            return
        request = dict(params.get("request") or {})
        if (
            str(request.get("method") or "").upper() == "POST"
            and not isinstance(request.get("postData"), str)
        ):
            try:
                recovered = await self._cdp(
                    "Fetch.getRequestPostData",
                    {"requestId": request_id},
                    session_id=session_id,
                    timeout=3.0,
                )
                recovered_result = recovered.get("result")
                recovered_post_data = (
                    recovered_result.get("postData")
                    if isinstance(recovered_result, dict)
                    else None
                )
                if isinstance(recovered_post_data, str):
                    request["postData"] = recovered_post_data
            except Exception:
                recovered_post_data = None
            if not isinstance(request.get("postData"), str):
                try:
                    await self._cdp(
                        "Fetch.failRequest",
                        {
                            "requestId": request_id,
                            "errorReason": "BlockedByClient",
                        },
                        session_id=session_id,
                        timeout=3.0,
                    )
                except Exception:
                    pass
                return
        validation = self._parse_crosspost_graphql_request(
            request, gate, graphql_url_pattern
        )
        if validation is None:
            try:
                await self._cdp(
                    "Fetch.continueRequest",
                    {"requestId": request_id},
                    session_id=session_id,
                    timeout=3.0,
                )
            except Exception:
                pass
            return

        result = dict(validation)
        if gate is not None:
            with self._state_lock:
                if (
                    self._crosspost_request_gate is not gate
                    or gate.consumed
                    or gate.expired
                ):
                    result = {
                        "target": True,
                        "ok": False,
                        "error": (
                            "Facebook cross-post mutation was blocked because "
                            "the authorized request gate was already consumed"
                        ),
                    }
                    active_gate = False
                else:
                    gate.consumed = True
                    active_gate = True
        else:
            active_gate = False
        try:
            if result.get("ok") and active_gate:
                await self._cdp(
                    "Fetch.continueRequest",
                    {"requestId": request_id},
                    session_id=session_id,
                    timeout=3.0,
                )
                result["request_released"] = True
            else:
                await self._cdp(
                    "Fetch.failRequest",
                    {
                        "requestId": request_id,
                        "errorReason": "BlockedByClient",
                    },
                    session_id=session_id,
                    timeout=3.0,
                )
                result["ok"] = False
                result["request_released"] = False
                if gate is not None and gate.expired:
                    result["dispatch_ambiguous"] = True
                    result["error"] = (
                        "Facebook cross-post mutation arrived after the guarded deadline and was blocked"
                    )
        except Exception as exc:
            result = {
                "ok": False,
                "dispatch_ambiguous": True,
                "error": (
                    "Facebook cross-post request could not be deterministically "
                    f"released or blocked: {type(exc).__name__}: {exc}"
                ),
            }
        finally:
            if gate is not None and active_gate:
                with self._state_lock:
                    if self._crosspost_request_gate is gate:
                        self._crosspost_request_gate = None
                    gate.result = result
                if result.get("dispatch_ambiguous"):
                    self._taint_and_retire_crosspost_request_gate(gate)
                gate.completed.set()

    def guarded_dom_action(
        self,
        *,
        backend_node_id: int,
        expected_page_identity: str,
        action: str,
        expected_role: str,
        expected_name: str,
        required_popup_role: Optional[str] = None,
        expected_popup_semantics_source: Optional[str] = None,
        required_group_id: Optional[str] = None,
        text: Optional[str] = None,
        captured_session_id: Optional[str] = None,
        require_group_composer: bool = False,
        require_readonly_group_navigation: bool = False,
        page_composer_stage: Optional[str] = None,
        page_composer_token: Optional[str] = None,
        required_facebook_page_url: Optional[str] = None,
        required_facebook_page_actor: Optional[str] = None,
        required_marketplace_listing_id: Optional[str] = None,
        required_marketplace_listing_title: Optional[str] = None,
        required_marketplace_source_entity_id: Optional[str] = None,
        required_marketplace_boost_label: Optional[str] = None,
        required_marketplace_for_sale_item_id: Optional[str] = None,
        allowed_crosspost_group_ids: Optional[List[str]] = None,
        allowed_crosspost_group_names: Optional[List[str]] = None,
        selected_crosspost_group_ids: Optional[List[str]] = None,
        selected_crosspost_group_names: Optional[List[str]] = None,
        crosspost_stage: Optional[str] = None,
        crosspost_source_token: Optional[str] = None,
        marketplace_price_stage: Optional[str] = None,
        marketplace_price_token: Optional[str] = None,
        required_marketplace_price_twd: Optional[int] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Mutate one captured AX node after an in-turn page identity check."""
        def guarded_call(
            method: str,
            params: Optional[Dict[str, Any]] = None,
            *,
            timeout: float = timeout,
        ) -> Dict[str, Any]:
            if captured_session_id:
                return self.call_session_cdp(
                    captured_session_id,
                    method,
                    params,
                    timeout=timeout,
                )
            return self.call_page_cdp(method, params, timeout=timeout)

        def normalized_guard_failure(
            raw_error: Any,
            *,
            dispatch_ambiguous: bool = False,
        ) -> Dict[str, Any]:
            canonical_guard_errors = {
                "Cross-post control belongs to a different listing": (
                    "facebook_crosspost_control_different_listing"
                ),
                "Cross-post control is not bound to the authorized listing": (
                    "facebook_crosspost_control_not_bound"
                ),
            }
            page_guard_errors = {
                "facebook_page_switch_required": (
                    "Facebook Page action blocked: the controlled page "
                    "visibly requires Switch into Page."
                ),
                "facebook_page_source_context_unproven": (
                    "Facebook Page action blocked: the exact Page management "
                    "context did not expose one actor."
                ),
                "facebook_page_composer_token_missing": (
                    "Facebook Page action blocked: the composer capability "
                    "token is missing."
                ),
                "facebook_page_composer_not_unique": (
                    "Facebook Page action blocked: the opener did not create "
                    "exactly one new composer."
                ),
                "facebook_page_composer_actor_mismatch": (
                    "Facebook Page action blocked: the opened composer actor "
                    "does not match the Page management actor."
                ),
                "facebook_page_composer_binding_invalid": (
                    "Facebook Page action blocked: the target is not bound to "
                    "the approved Page composer capability."
                ),
            }
            price_guard_errors = {
                "facebook_marketplace_price_fill_guard_rejected": (
                    "Facebook Marketplace price fill blocked: the exact "
                    "listing, Update control, page, or normalized integer value did "
                    "not match the approved price capability."
                ),
                "facebook_marketplace_price_submit_guard_rejected": (
                    "Facebook Marketplace price submit blocked: Update is "
                    "not bound to the exact approved price input and control."
                ),
            }
            first_error_line = str(raw_error).splitlines()[0]
            if first_error_line.startswith("Error: "):
                first_error_line = first_error_line[7:]
            page_guard_diagnostics = None
            page_guard_error_code = None
            price_guard_diagnostics = None
            price_guard_error_code = None
            if first_error_line.startswith("HERMES_PAGE_GUARD|"):
                _, page_guard_error_code, raw_diagnostics = (
                    first_error_line.split("|", 2)
                )
                try:
                    parsed_diagnostics = json.loads(raw_diagnostics)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_diagnostics = None
                if isinstance(parsed_diagnostics, dict):
                    page_guard_diagnostics = parsed_diagnostics
            elif first_error_line.startswith("HERMES_MARKETPLACE_PRICE_GUARD|"):
                _, price_guard_error_code, raw_diagnostics = (
                    first_error_line.split("|", 2)
                )
                try:
                    parsed_diagnostics = json.loads(raw_diagnostics)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_diagnostics = None
                if isinstance(parsed_diagnostics, dict):
                    price_guard_diagnostics = parsed_diagnostics
            canonical_error_line, separator, guard_detail = (
                first_error_line.partition(" | association_debug:")
            )
            error_code = (
                page_guard_error_code
                or price_guard_error_code
                or canonical_guard_errors.get(canonical_error_line)
            )
            if error_code:
                normalized_error = (
                    page_guard_errors.get(str(page_guard_error_code or ""))
                    or price_guard_errors.get(str(price_guard_error_code or ""))
                    or canonical_error_line
                )
            else:
                normalized_error = raw_error
            return {
                "ok": False,
                "dispatch_ambiguous": dispatch_ambiguous,
                "error": normalized_error,
                **({"error_code": error_code} if error_code else {}),
                **(
                    {"guard_diagnostics": page_guard_diagnostics}
                    if page_guard_diagnostics is not None
                    else {}
                ),
                **(
                    {"guard_diagnostics": price_guard_diagnostics}
                    if price_guard_diagnostics is not None
                    else {}
                ),
                **(
                    {"guard_detail": guard_detail}
                    if error_code and separator and guard_detail
                    else {}
                ),
            }

        resolved = guarded_call(
            "DOM.resolveNode",
            {"backendNodeId": int(backend_node_id)},
            timeout=timeout,
        )
        if not resolved.get("ok"):
            return resolved
        object_id = resolved.get("result", {}).get("object", {}).get("objectId")
        if not object_id:
            return {
                "ok": False,
                "error": "captured snapshot node is no longer resolvable",
            }
        guard_token = f"{time.time_ns()}-{threading.get_ident()}"
        armed = guarded_call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": """
                function(
                  token, requireGroupComposer, requireCrosspostDialog,
                  requirePageComposer
                ) {
                  const prior = this.__hermesAtomicGuard;
                  if (prior?.observer) prior.observer.disconnect();
                  if (prior?.listener && prior?.eventTypes) {
                    for (const type of prior.eventTypes) {
                      window.removeEventListener(type, prior.listener, true);
                    }
                  }
                  const state = {token, dirty: false, observer: null};
                  state.observer = new MutationObserver(() => {
                    state.dirty = true;
                  });
                  const targets = [this];
                  const labelledBy = (
                    this.getAttribute("aria-labelledby") || ""
                  ).split(/\\s+/).filter(Boolean);
                  for (const id of labelledBy) {
                    const target = this.ownerDocument.getElementById(id);
                    if (target) targets.push(target);
                  }
                  if (this.id) {
                    for (const label of this.ownerDocument.querySelectorAll(
                      `label[for="${CSS.escape(this.id)}"]`
                    )) targets.push(label);
                  }
                  const wrappingLabel = this.closest?.("label");
                  if (wrappingLabel) targets.push(wrappingLabel);
                  if (requireGroupComposer) {
                    const dialog = this.closest?.('[role="dialog"]');
                    if (dialog) targets.push(dialog);
                  }
                  if (requireCrosspostDialog) {
                    const dialog = this.closest?.('[role="dialog"]');
                    if (dialog) targets.push(dialog);
                  }
                  if (requirePageComposer) {
                    const dialog = this.closest?.('[role="dialog"]');
                    if (dialog) targets.push(dialog);
                  }
                  for (const target of new Set(targets)) {
                    state.observer.observe(target, {
                      subtree: true,
                      childList: true,
                      attributes: true,
                      characterData: true
                    });
                  }
                  Object.defineProperty(this, "__hermesAtomicGuard", {
                    value: state, configurable: true
                  });
                  return true;
                }
                """,
                "arguments": [
                    {"value": guard_token},
                    {"value": bool(require_group_composer)},
                    {
                        "value": crosspost_stage in {
                            "select_group", "submit",
                        }
                    },
                    {
                        "value": page_composer_stage in {
                            "compose", "submit",
                        }
                    },
                ],
                "returnByValue": True,
            },
            timeout=timeout,
        )
        if not armed.get("ok"):
            return armed
        arm_payload = armed.get("result", {})
        if arm_payload.get("exceptionDetails"):
            return {
                "ok": False,
                "error": "failed to arm exact-node mutation guard",
            }

        def _cleanup_guard() -> None:
            guarded_call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """
                    function(token) {
                      const guard = this.__hermesAtomicGuard;
                      if (guard?.token === token) {
                        if (guard.listener && guard.eventTypes) {
                          for (const type of guard.eventTypes) {
                            window.removeEventListener(
                              type, guard.listener, true
                            );
                          }
                        }
                        guard.observer?.disconnect();
                        delete this.__hermesAtomicGuard;
                      }
                    }
                    """,
                    "arguments": [{"value": guard_token}],
                },
                timeout=timeout,
            )

        popup_semantics_source = str(
            expected_popup_semantics_source or ""
        ).strip().casefold()
        live_ax_method = (
            "Accessibility.getFullAXTree"
            if required_popup_role and popup_semantics_source == "ax_full"
            else "Accessibility.getPartialAXTree"
        )
        live_ax_params = (
            {}
            if live_ax_method == "Accessibility.getFullAXTree"
            else {
                "backendNodeId": int(backend_node_id),
                "fetchRelatives": False,
            }
        )
        live_ax = guarded_call(
            live_ax_method,
            live_ax_params,
            timeout=timeout,
        )
        if not live_ax.get("ok"):
            _cleanup_guard()
            return live_ax
        live_nodes = live_ax.get("result", {}).get("nodes", [])
        live_node = next(
            (
                node for node in live_nodes
                if int(node.get("backendDOMNodeId") or 0)
                == int(backend_node_id)
                and not node.get("ignored")
            ),
            None,
        )
        normalize = lambda value: " ".join(str(value or "").split()).casefold()
        live_role = (
            live_node.get("role", {}).get("value") if live_node else None
        )
        live_name = (
            live_node.get("name", {}).get("value") if live_node else None
        )
        if (
            live_node is None
            or normalize(live_role) != normalize(expected_role)
            or normalize(live_name) != normalize(expected_name)
        ):
            _cleanup_guard()
            return {
                "ok": False,
                "error": (
                    "Captured snapshot node semantics changed before "
                    "atomic action"
                ),
            }
        if required_popup_role:
            live_popup_role = ""
            for prop in list(live_node.get("properties") or []):
                if str(prop.get("name") or "").casefold() != "haspopup":
                    continue
                live_popup_role = normalize(
                    (prop.get("value") or {}).get("value")
                )
                break
            if live_popup_role == "true":
                live_popup_role = "menu"
            if popup_semantics_source == "dom_attribute":
                live_popup_role = ""
            if not live_popup_role and popup_semantics_source != "ax_full":
                # Chrome can omit hasPopup from AX for a generic Facebook
                # Options control. Fall back to the exact node's DOM
                # attribute only when AX supplied no popup semantic at all.
                popup_result = guarded_call(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": (
                            "function() { return "
                            "this.getAttribute('aria-haspopup') || ''; }"
                        ),
                        "returnByValue": True,
                    },
                    timeout=timeout,
                )
                if popup_result.get("ok"):
                    live_popup_role = normalize(
                        popup_result.get("result", {})
                        .get("result", {})
                        .get("value")
                    )
                    if live_popup_role == "true":
                        live_popup_role = "menu"
            if live_popup_role != normalize(required_popup_role):
                _cleanup_guard()
                return {
                    "ok": False,
                    "error_code": (
                        "popup_semantics_changed_before_atomic_action"
                    ),
                    "error": (
                        "Captured snapshot popup semantics changed before "
                        "atomic action"
                    ),
                    "expected_popup_role": normalize(required_popup_role),
                    "actual_popup_role": live_popup_role or "missing",
                }
        crosspost_request_gate: Optional[CrosspostRequestGate] = None
        if action == "click" and crosspost_stage == "submit":
            authoritative_for_sale_item_id = str(
                required_marketplace_for_sale_item_id or ""
            )
            if not authoritative_for_sale_item_id:
                binding = guarded_call(
                    "Runtime.evaluate",
                    {
                        "expression": f"""
                        (() => {{
                          if (
                            `${{location.href}}|${{performance.timeOrigin}}`
                            !== {json.dumps(expected_page_identity)}
                          ) return {{ok: false}};
                          try {{
                            const records = require("RelayFBEnvironment")
                              .getStore().getSource().toJSON();
                            const product = records?.[
                              {json.dumps(str(required_marketplace_source_entity_id or ""))}
                            ];
                            const id = product?.for_sale_item?.__ref;
                            if (
                              product?.__typename !== "ProductItem"
                              || String(product.id) !==
                                {json.dumps(str(required_marketplace_source_entity_id or ""))}
                              || !/^[0-9]+$/.test(id || "")
                              || String(id) !==
                                {json.dumps(str(required_marketplace_listing_id or ""))}
                            ) return {{ok: false}};
                            return {{ok: true, id: String(id)}};
                          }} catch {{
                            return {{ok: false}};
                          }}
                        }})()
                        """,
                        "returnByValue": True,
                    },
                    timeout=timeout,
                )
                binding_value = (
                    binding.get("result", {})
                    .get("result", {})
                    .get("value", {})
                    if binding.get("ok")
                    else {}
                )
                if not (
                    isinstance(binding_value, dict)
                    and binding_value.get("ok") is True
                    and str(binding_value.get("id") or "").isdigit()
                ):
                    _cleanup_guard()
                    return {
                        "ok": False,
                        "error": (
                            "Facebook cross-post listing has no authoritative "
                            "for-sale item binding before final Post"
                        ),
                    }
                authoritative_for_sale_item_id = str(binding_value["id"])
            gate_result = self._arm_crosspost_request_gate(
                listing_id=authoritative_for_sale_item_id,
                group_ids=list(selected_crosspost_group_ids or []),
                expected_page_identity=expected_page_identity,
                captured_session_id=captured_session_id,
                timeout=timeout,
            )
            if not gate_result.get("ok"):
                _cleanup_guard()
                return gate_result
            crosspost_request_gate = gate_result["gate"]
            required_marketplace_for_sale_item_id = (
                authoritative_for_sale_item_id
            )
        if action == "click":
            clicked = guarded_call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """
                    async function(
                      expected, token, requiredGroupId, requireGroupComposer,
                      requiredListingId, requiredListingTitle,
                      requiredSourceEntityId, requiredBoostLabel,
                      allowedCrosspostGroupIds,
                      allowedCrosspostGroupNames, selectedCrosspostGroupIds,
                      selectedCrosspostGroupNames, crosspostStage,
                      crosspostSourceToken, pageComposerStage,
                      pageComposerToken, requiredFacebookPageUrl,
                      requiredFacebookPageActor,
                      expectedControlName,
                      expectedPopupRole, expectedPopupSemanticsSource,
                      requiredForSaleItemId, requireReadonlyGroupNavigation,
                      marketplacePriceStage, marketplacePriceToken,
                      requiredMarketplacePriceTwd
                    ) {
                      const guard = this.__hermesAtomicGuard;
                      const fail = message => {
                        guard?.observer?.disconnect();
                        if (guard?.token === token) {
                          delete this.__hermesAtomicGuard;
                        }
                        throw new Error(message);
                      };
                      const failPageGuard = (code, diagnostics) => fail(
                        `HERMES_PAGE_GUARD|${code}|${JSON.stringify(
                          diagnostics || {}
                        )}`
                      );
                      const identity = () =>
                        `${location.href}|${performance.timeOrigin}`;
                      const documentRef = this.ownerDocument;
                      const strictlyVisible = node => {
                        if (!node?.isConnected) return false;
                        const view = node.ownerDocument.defaultView;
                        if (!view) return false;
                        const nodeRect = node.getBoundingClientRect();
                        let visibleLeft = Math.max(0, nodeRect.left);
                        let visibleRight = Math.min(
                          view.innerWidth, nodeRect.right
                        );
                        let visibleTop = Math.max(0, nodeRect.top);
                        let visibleBottom = Math.min(
                          view.innerHeight, nodeRect.bottom
                        );
                        for (
                          let current = node;
                          current;
                          current = current.parentElement
                        ) {
                          const style = view.getComputedStyle(current);
                          const clipPath = String(
                            style.clipPath || style.webkitClipPath || "none"
                          ).trim().toLowerCase();
                          const legacyClip = String(
                            style.clip || "auto"
                          ).trim().toLowerCase();
                          if (
                            current.hasAttribute("hidden")
                            || current.hasAttribute("inert")
                            || String(
                              current.getAttribute("aria-hidden") || ""
                            ).trim().toLowerCase() === "true"
                            || style.display === "none"
                            || style.visibility !== "visible"
                            || Number(style.opacity) === 0
                            || (clipPath && clipPath !== "none")
                            || (legacyClip && legacyClip !== "auto")
                          ) return false;
                          const currentRects = [...current.getClientRects()];
                          if (!currentRects.some(rect => (
                            rect.width > 0 && rect.height > 0
                          ))) return false;
                          if (current !== node) {
                            const clipsX = style.overflowX !== "visible";
                            const clipsY = style.overflowY !== "visible";
                            const containment = String(
                              style.contain || ""
                            ).split(/\\s+/);
                            const clipsPaint = containment.some(value => (
                              value === "paint"
                              || value === "strict"
                              || value === "content"
                            ));
                            if (clipsX || clipsY || clipsPaint) {
                              const borderRect = current.getBoundingClientRect();
                              const clipLeft = borderRect.left + current.clientLeft;
                              const clipTop = borderRect.top + current.clientTop;
                              const clipRight = clipLeft + current.clientWidth;
                              const clipBottom = clipTop + current.clientHeight;
                              if (clipsX || clipsPaint) {
                                visibleLeft = Math.max(
                                  visibleLeft, clipLeft
                                );
                                visibleRight = Math.min(
                                  visibleRight, clipRight
                                );
                              }
                              if (clipsY || clipsPaint) {
                                visibleTop = Math.max(
                                  visibleTop, clipTop
                                );
                                visibleBottom = Math.min(
                                  visibleBottom, clipBottom
                                );
                              }
                            }
                          }
                          if (
                            visibleRight <= visibleLeft
                            || visibleBottom <= visibleTop
                          ) return false;
                        }
                        return true;
                      };
                      const visiblePageComposerShells = () => [...(
                        documentRef.querySelectorAll('[role="dialog"]')
                      )].filter(dialog => {
                        const text = [
                          dialog.getAttribute("aria-label") || "",
                          dialog.innerText || ""
                        ].join(" ").replace(/\\s+/g, " ").toLowerCase();
                        return (
                          strictlyVisible(dialog)
                          && (
                            text.includes("create post")
                            || text.includes("建立貼文")
                          )
                          && !text.includes("anonymous post")
                          && !text.includes("匿名貼文")
                          && !text.includes("share post")
                          && !text.includes("分享貼文")
                        );
                      });
                      // Facebook renders one logical Create Post composer as
                      // nested dialog shells: the outer dialog owns the form
                      // while an inner dialog may contain only the title and
                      // close control. Count the dialog that directly owns a
                      // visible editable surface, not every shell whose
                      // inherited innerText happens to say Create Post.
                      const visiblePageComposers = () => (
                        visiblePageComposerShells().filter(dialog => (
                          [...dialog.querySelectorAll(
                            '[role="textbox"], textarea, '
                            + '[contenteditable="true"]'
                          )].some(editable => (
                            strictlyVisible(editable)
                            && editable.closest('[role="dialog"]') === dialog
                            && !editable.closest('[aria-disabled="true"]')
                            && !editable.closest('[aria-readonly="true"]')
                            && (
                              editable.isContentEditable
                              || (
                                editable.matches("textarea")
                                && !editable.matches(":disabled")
                                && !editable.readOnly
                              )
                            )
                          ))
                        ))
                      );
                      const visibleDialogShellsBefore = new Set(
                        pageComposerStage === "open"
                          ? [...documentRef.querySelectorAll('[role="dialog"]')]
                            .filter(strictlyVisible)
                          : []
                      );
                      const normalizeActor = value => String(value || "")
                        .replace(/\\s+/g, " ").trim();
                      const hasEditableSurface = element => Boolean(
                        element?.isContentEditable
                        || element?.closest(
                          'input,textarea,select,button,[role="textbox"]'
                        )
                        || [...element?.querySelectorAll('*') || []].some(
                          child => (
                            child.isContentEditable
                            || child.matches(
                              'input,textarea,select,button,'
                              + '[role="textbox"]'
                            )
                          )
                        )
                      );
                      const directActorTextVisible = (
                        element, expectedActor, includeDescendants = false
                      ) => {
                        if (!strictlyVisible(element) || !expectedActor) {
                          return false;
                        }
                        const candidateTextNodes = includeDescendants
                          ? (() => {
                            const nodes = [];
                            const walker = element.ownerDocument.createTreeWalker(
                              element, NodeFilter.SHOW_TEXT
                            );
                            while (walker.nextNode()) nodes.push(walker.currentNode);
                            return nodes;
                          })()
                          : [...element.childNodes];
                        const visibleCandidateTextNodes = candidateTextNodes.filter(
                          child => (
                            child.nodeType === 3
                            && strictlyVisible(child.parentElement)
                          )
                        );
                        const textNodes = visibleCandidateTextNodes.filter(
                          child => Boolean(normalizeActor(child.textContent))
                        );
                        if (
                          !textNodes.length
                          || normalizeActor(visibleCandidateTextNodes.map(
                            child => child.textContent
                          ).join("")) !== expectedActor
                        ) return false;
                        const view = element.ownerDocument.defaultView;
                        const elementRect = element.getBoundingClientRect();
                        const colorPainted = value => {
                          const color = String(value || "")
                            .trim().toLowerCase();
                          if (!color || color === "transparent") return false;
                          const slashAlpha = color.match(
                            /\\/\\s*([0-9.]+)(%)?\\s*\\)$/
                          );
                          if (slashAlpha) {
                            const alpha = Number(slashAlpha[1]);
                            return Number.isFinite(alpha)
                              && alpha > 0;
                          }
                          const legacyAlpha = color.match(
                            /^rgba\\([^,]+,[^,]+,[^,]+,\\s*([0-9.]+)\\s*\\)$/
                          );
                          return !legacyAlpha
                            || Number(legacyAlpha[1]) > 0;
                        };
                        const textRectGroups = textNodes.map(textNode => {
                          const range = element.ownerDocument.createRange();
                          range.selectNodeContents(textNode);
                          return [...range.getClientRects()]
                            .filter(rect => rect.width > 0 && rect.height > 0)
                            .map(rect => ({rect, textNode}));
                        });
                        if (textRectGroups.some(rects => !rects.length)) {
                          return false;
                        }
                        const textRects = textRectGroups.flat();
                        return textRects.every(({rect: textRect, textNode}) => {
                          let left = Math.max(
                            0, elementRect.left, textRect.left
                          );
                          let right = Math.min(
                            view.innerWidth, elementRect.right, textRect.right
                          );
                          let top = Math.max(
                            0, elementRect.top, textRect.top
                          );
                          let bottom = Math.min(
                            view.innerHeight,
                            elementRect.bottom,
                            textRect.bottom
                          );
                          for (
                            let current = textNode.parentElement;
                            current;
                            current = current.parentElement
                          ) {
                            const style = view.getComputedStyle(current);
                            if (
                              String(style.filter || "none")
                                .trim().toLowerCase() !== "none"
                            ) return false;
                            if (
                              current === textNode.parentElement
                              && (
                                !colorPainted(style.color)
                                || !colorPainted(
                                  style.webkitTextFillColor || style.color
                                )
                              )
                            ) return false;
                            const clipsX = style.overflowX !== "visible";
                            const clipsY = style.overflowY !== "visible";
                            const containment = String(
                              style.contain || ""
                            ).split(/\\s+/);
                            const clipsPaint = containment.some(value => (
                              value === "paint"
                              || value === "strict"
                              || value === "content"
                            ));
                            if (clipsX || clipsY || clipsPaint) {
                              const borderRect = current.getBoundingClientRect();
                              const clipLeft = borderRect.left + current.clientLeft;
                              const clipTop = borderRect.top + current.clientTop;
                              const clipRight = clipLeft + current.clientWidth;
                              const clipBottom = clipTop + current.clientHeight;
                              if (clipsX || clipsPaint) {
                                left = Math.max(left, clipLeft);
                                right = Math.min(right, clipRight);
                              }
                              if (clipsY || clipsPaint) {
                                top = Math.max(top, clipTop);
                                bottom = Math.min(bottom, clipBottom);
                              }
                            }
                            if (right <= left || bottom <= top) return false;
                          }
                          const epsilon = 0.5;
                          return (
                            left <= textRect.left + epsilon
                            && right >= textRect.right - epsilon
                            && top <= textRect.top + epsilon
                            && bottom >= textRect.bottom - epsilon
                          );
                        });
                      };
                      const canonicalPageUrlOf = value => {
                        try {
                          const parsed = new URL(value, location.href);
                          const hostname = parsed.hostname.toLowerCase();
                          const parts = parsed.pathname
                            .split("/").filter(Boolean);
                          if (
                            parsed.protocol !== "https:"
                            || !["facebook.com", "www.facebook.com"]
                              .includes(hostname)
                            || !["", "443"].includes(parsed.port)
                            || parsed.username || parsed.password
                            || parsed.search || parsed.hash
                            || parts.length !== 1
                          ) return "";
                          return `https://www.facebook.com/${
                            parts[0].toLowerCase()
                          }`;
                        } catch {
                          return "";
                        }
                      };
                      const approvedPageSlug = (() => {
                        if (!requiredFacebookPageUrl) return "";
                        try {
                          const approved = new URL(requiredFacebookPageUrl);
                          const canonicalApproved = canonicalPageUrlOf(
                            requiredFacebookPageUrl
                          );
                          const approvedParts = approved.pathname
                            .split("/").filter(Boolean);
                          if (
                            !canonicalApproved
                            || canonicalApproved !== requiredFacebookPageUrl
                            || canonicalPageUrlOf(location.href)
                              !== requiredFacebookPageUrl
                          ) return "";
                          return approvedParts[0].toLowerCase();
                        } catch {
                          return "";
                        }
                      })();
                      // Opening a composer is a reversible UI transition. At
                      // this stage prove the exact approved Page URL, a visible
                      // Page-management identity, and the absence of an
                      // explicit Switch-into-Page gate. Comment voice and
                      // self-link text are useful diagnostics, but Facebook is
                      // free to render them for a different interaction mode;
                      // they are not posting-authority requirements. After the
                      // atomic click Facebook may hide this navigation behind
                      // the modal; absence then uses the frozen source proof,
                      // while any visible contradictory actor still blocks.
                      const pageOpenContextIn = container => {
                        const canonicalPageAnchors = [
                          ...container.querySelectorAll('a[href]')
                        ]
                          .filter(anchor => {
                            try {
                              return (
                                canonicalPageUrlOf(
                                  anchor.getAttribute("href")
                                ) === requiredFacebookPageUrl
                                && strictlyVisible(anchor)
                              );
                            } catch {
                              return false;
                            }
                          });
                        const commentActorNames = [...container.querySelectorAll(
                          '[role="textbox"], textarea, [contenteditable="true"]'
                        )]
                          .filter(strictlyVisible)
                          .map(node => normalizeActor(
                            node.getAttribute("aria-label")
                            || node.getAttribute("placeholder")
                            || ""
                          ))
                          .map(label => {
                            const lower = label.toLowerCase();
                            if (lower.startsWith("comment as ")) {
                              return normalizeActor(label.slice(11));
                            }
                            const localized = label.match(
                              /^(?:以|使用|用)\\s*(.+?)\\s*(?:的)?(?:身分|身份)留言$/
                            );
                            if (localized) {
                              return normalizeActor(localized[1]);
                            }
                            const labelled = label.match(
                              /^(?:留言身分|留言身份)[：:]\\s*(.+)$/
                            );
                            return labelled
                              ? normalizeActor(labelled[1]) : "";
                          })
                          .filter(Boolean);
                        const managePageLabels = [
                          "manage page", "管理粉絲專頁", "管理页面"
                        ];
                        const pageNavigations = [
                          ...container.querySelectorAll(
                            'nav[aria-label],'
                            + '[role="navigation"][aria-label]'
                          )
                        ].filter(node => (
                          strictlyVisible(node)
                          && [...node.querySelectorAll('h1,h2,[role="heading"]')]
                            .filter(strictlyVisible)
                            .some(heading => managePageLabels.includes(
                              normalizeActor(
                              heading.innerText || heading.textContent
                              ).toLowerCase()
                            ))
                        ));
                        const pageNavigationHeadingNames = (
                          pageNavigations.flatMap(navigation => [
                            ...navigation.querySelectorAll(
                              'h1,h2,[role="heading"]'
                            )
                          ]
                            .filter(strictlyVisible)
                            .map(node => normalizeActor(
                              node.innerText
                              || node.getAttribute("aria-label")
                              || ""
                            ))
                            .filter(name => (
                              Boolean(name)
                              && !managePageLabels.includes(name.toLowerCase())
                            )))
                        );
                        const pageNavigationActorNames = pageNavigations.flatMap(
                          navigation => {
                            const headings = [
                              ...navigation.querySelectorAll(
                                'h1,h2,[role="heading"]'
                              )
                            ].filter(strictlyVisible);
                            const manageHeading = headings.find(node => (
                              managePageLabels.includes(normalizeActor(
                                node.innerText
                                || node.getAttribute("aria-label")
                                || ""
                              ).toLowerCase())
                            ));
                            const identityBoundary = manageHeading
                              ? [...navigation.querySelectorAll(
                                '[role="separator"],hr'
                              )]
                                .filter(strictlyVisible)
                                .find(node => Boolean(
                                  manageHeading.compareDocumentPosition(node)
                                  & Node.DOCUMENT_POSITION_FOLLOWING
                                ))
                              : null;
                            return headings
                              .filter(node => (
                                node !== manageHeading
                                && (
                                  !manageHeading
                                  || Boolean(
                                    manageHeading.compareDocumentPosition(node)
                                    & Node.DOCUMENT_POSITION_FOLLOWING
                                  )
                                )
                                && (
                                  !identityBoundary
                                  || Boolean(
                                    node.compareDocumentPosition(identityBoundary)
                                    & Node.DOCUMENT_POSITION_FOLLOWING
                                  )
                                )
                              ))
                              .map(node => normalizeActor(
                                node.innerText
                                || node.getAttribute("aria-label")
                                || ""
                              ))
                              .filter(Boolean);
                          }
                        );
                        const visibleSwitchGateLabels = [
                          ...container.querySelectorAll(
                            'button,[role="button"],a'
                          )
                        ]
                          .filter(strictlyVisible)
                          .flatMap(node => {
                            const accessible = normalizeActor(
                              node.getAttribute("aria-label")
                              || node.getAttribute("title")
                              || ""
                            );
                            const rendered = normalizeActor(
                              node.innerText || ""
                            );
                            return [accessible, rendered].filter(Boolean);
                          });
                        const distinctCommentActorNames = commentActorNames.filter(
                          (name, index) => (
                            Boolean(name)
                            && commentActorNames.indexOf(name) === index
                          )
                        );
                        const distinctPageNavigationActorNames = (
                          pageNavigationActorNames.filter(
                            (name, index) => (
                              Boolean(name)
                              && pageNavigationActorNames.indexOf(name) === index
                            )
                          )
                        );
                        const anchorActorNames = canonicalPageAnchors
                          .flatMap(anchor => [
                            anchor.getAttribute("aria-label") || "",
                            anchor.getAttribute("title") || "",
                            anchor.innerText || ""
                          ])
                          .map(normalizeActor)
                          .map(label => normalizeActor(label.replace(
                            /[’']s timeline$/i, ""
                          )))
                          .filter(Boolean);
                        const distinctAnchorActorNames = anchorActorNames.filter(
                          (name, index) => anchorActorNames.indexOf(name) === index
                        );
                        const requiredSourceActor = normalizeActor(
                          requiredFacebookPageActor || ""
                        );
                        const sourceShowsSwitchGate = (
                          visibleSwitchGateLabels.some(rawLabel => {
                            const label = rawLabel.toLowerCase()
                              .replace(/[’‘]/g, "'");
                            return (
                              (
                                label.includes("switch into")
                                && label.includes("page")
                              )
                              || (
                                /切[換换]/.test(label)
                                && /(粉絲專頁|粉丝专页|專頁|专页|主頁|主页)/
                                .test(label)
                              )
                            );
                          })
                        );
                        const corroboratedManagementActors = (
                          distinctPageNavigationActorNames.filter(name => (
                            distinctAnchorActorNames.includes(name)
                            || distinctCommentActorNames.includes(name)
                          ))
                        );
                        const actor = (
                          distinctPageNavigationActorNames.length === 1
                            ? distinctPageNavigationActorNames[0]
                            : ""
                        );
                        const diagnostics = {
                          page_url_match: Boolean(approvedPageSlug),
                          manage_page_context_visible: pageNavigations.length > 0,
                          management_actor_names:
                            distinctPageNavigationActorNames,
                          page_navigation_heading_names:
                            pageNavigationHeadingNames,
                          corroborated_management_actor_names:
                            corroboratedManagementActors,
                          source_actor: actor || null,
                          required_source_actor: requiredSourceActor || null,
                          canonical_page_anchor_count:
                            canonicalPageAnchors.length,
                          canonical_page_anchor_actor_names:
                            distinctAnchorActorNames,
                          comment_actor_names: distinctCommentActorNames,
                          switch_into_page_visible: sourceShowsSwitchGate
                        };
                        return {
                          allowed: Boolean(
                            approvedPageSlug
                            && actor
                            && !sourceShowsSwitchGate
                            && (
                              !requiredSourceActor
                              || actor === requiredSourceActor
                            )
                          ),
                          actor,
                          diagnostics
                        };
                      };
                      const composerActorBindingIn = (
                        dialog, actorToken, expectedActor,
                        sourceActorAuthorized = false
                      ) => {
                        const textboxes = [...dialog.querySelectorAll(
                          '[role="textbox"], textarea, [contenteditable="true"]'
                        )].filter(strictlyVisible);
                        if (!textboxes.length) return null;
                        const textboxTop = Math.min(...textboxes.map(
                          node => node.getBoundingClientRect().top
                        ));
                        const dialogTop = dialog.getBoundingClientRect().top;
                        const bindings = [];
                        const actorElements = actorToken
                          ? documentRef.querySelectorAll(
                            '[data-hermes-page-composer-actor-token]'
                          )
                          : dialog.querySelectorAll('a[href]');
                        if (
                          actorToken
                          && (
                            actorElements.length !== 1
                            || actorElements[0].getAttribute(
                              'data-hermes-page-composer-actor-token'
                            ) !== actorToken
                            || !dialog.contains(actorElements[0])
                          )
                        ) {
                          return null;
                        }
                        for (const actorElement of actorElements) {
                          const rect = actorElement.getBoundingClientRect();
                          const actorUrl = actorElement.matches('a[href]')
                            ? canonicalPageUrlOf(
                              actorElement.getAttribute("href")
                            )
                            : requiredFacebookPageUrl;
                          const actorName = normalizeActor(
                            actorElement.innerText
                            || actorElement.textContent
                          );
                          if (
                            !actorUrl || !actorName
                            || (
                              actorToken
                              && actorElement.getAttribute(
                                "data-hermes-page-composer-actor-token"
                              ) !== actorToken
                            )
                            || !strictlyVisible(actorElement)
                            || textboxes.some(
                              textbox => textbox.contains(actorElement)
                            )
                            || hasEditableSurface(actorElement)
                            || rect.top < dialogTop
                            || rect.bottom > textboxTop
                            || !directActorTextVisible(
                              actorElement, expectedActor,
                              actorElement.matches('a[href]')
                            )
                          ) continue;
                          bindings.push({
                            url: actorUrl,
                            name: actorToken ? expectedActor : actorName,
                            element: actorElement
                          });
                        }
                        // Current Facebook Page composers can expose the
                        // actor as non-link StaticText. The source Page URL,
                        // source actor, clicked control, and newly opened
                        // dialog are already atomically bound above. When no
                        // Page anchor exists in that dialog, accept only one
                        // exact, visible, leaf actor label above its textbox.
                        if (
                          !actorToken
                          && !bindings.length
                          && expectedActor
                          && sourceActorAuthorized
                        ) {
                          const semanticActors = [
                            ...dialog.querySelectorAll('*')
                          ].filter(element => {
                            if (
                              element.matches(
                                'a,button,input,textarea,[role="textbox"],'
                                + '[contenteditable="true"]'
                              )
                              || !strictlyVisible(element)
                              || textboxes.some(
                                textbox => textbox.contains(element)
                              )
                              || hasEditableSurface(element)
                              || normalizeActor(
                                element.innerText || element.textContent
                              ) !== expectedActor
                            ) return false;
                            const rect = element.getBoundingClientRect();
                            if (
                              rect.top < dialogTop
                              || rect.bottom > textboxTop
                            ) return false;
                            return directActorTextVisible(
                              element, expectedActor, true
                            );
                          });
                          // Facebook frequently splits one accessible actor
                          // label across nested spans. Keep only the innermost
                          // exact visible label so wrapper duplication cannot
                          // create a false ambiguity.
                          const distinctSemanticActors = semanticActors.filter(
                            element => !semanticActors.some(candidate => (
                              candidate !== element
                              && element.contains(candidate)
                            ))
                          );
                          if (distinctSemanticActors.length === 1) {
                            bindings.push({
                              url: requiredFacebookPageUrl,
                              name: expectedActor,
                              element: distinctSemanticActors[0]
                            });
                          }
                        }
                        const distinctBindings = bindings.filter(
                          (binding, index) => bindings.findIndex(
                            candidate => (
                              candidate.url === binding.url
                              && candidate.name === binding.name
                            )
                          ) === index
                        );
                        if (actorToken) {
                          return bindings.length === 1 ? bindings[0] : null;
                        }
                        return distinctBindings.length === 1
                          ? distinctBindings[0] : null;
                      };
                      const composerActorContradictionsIn = (
                        dialog, expectedActor
                      ) => {
                        const textboxes = [...dialog.querySelectorAll(
                          '[role="textbox"], textarea, [contenteditable="true"]'
                        )].filter(strictlyVisible);
                        if (!textboxes.length) return [];
                        const textboxTop = Math.min(...textboxes.map(
                          node => node.getBoundingClientRect().top
                        ));
                        const dialogTop = dialog.getBoundingClientRect().top;
                        const contradictions = [];
                        for (const element of [
                          dialog,
                          ...dialog.querySelectorAll('[aria-label],[title]')
                        ]) {
                          if (
                            !strictlyVisible(element)
                            || textboxes.some(
                              textbox => textbox.contains(element)
                            )
                          ) continue;
                          const rect = element.getBoundingClientRect();
                          if (
                            element !== dialog
                            && (
                              rect.top < dialogTop
                              || rect.bottom > textboxTop
                            )
                          ) continue;
                          const labels = [
                            element.getAttribute("aria-label"),
                            element.getAttribute("title")
                          ].map(normalizeActor).filter(Boolean);
                          for (const label of labels) {
                            const english = label.match(
                              /^(?:post|posting|publish) as (.+)$/i
                            );
                            const localized = label.match(
                              /^(?:以|使用|用)\\s*(.+?)\\s*(?:身分|身份)(?:發佈|发布|發文|发文|貼文|贴文)$/
                            );
                            const labelled = label.match(
                              /^(?:發佈|发布|發文|发文|貼文|贴文)(?:身分|身份)[：:]\\s*(.+)$/
                            );
                            const declaredActor = normalizeActor(
                              english?.[1]
                              || localized?.[1]
                              || labelled?.[1]
                            );
                            if (
                              declaredActor
                              && declaredActor !== normalizeActor(
                                expectedActor
                              )
                            ) contradictions.push(declaredActor);
                          }
                        }
                        return contradictions.filter(
                          (actor, index) => (
                            contradictions.indexOf(actor) === index
                          )
                        );
                      };
                      let approvedPageActor = null;
                      let approvedPageActorAuthorized = false;
                      let pageGuardDiagnostics = null;
                      if (pageComposerStage === "open") {
                        const pageContext = pageOpenContextIn(documentRef);
                        pageGuardDiagnostics = pageContext.diagnostics;
                        if (pageGuardDiagnostics.switch_into_page_visible) {
                          failPageGuard(
                            "facebook_page_switch_required",
                            {
                              ...pageGuardDiagnostics,
                              failed_predicate: "switch_into_page_visible"
                            }
                          );
                        }
                        if (!pageContext.allowed) {
                          const failedPredicate = (
                            !pageGuardDiagnostics.page_url_match
                              ? "page_url_match"
                              : !pageGuardDiagnostics.manage_page_context_visible
                                ? "manage_page_context_visible"
                                : !pageGuardDiagnostics.source_actor
                                  ? "management_actor_unique"
                                  : "source_actor_match"
                          );
                          failPageGuard(
                            "facebook_page_source_context_unproven",
                            {
                              ...pageGuardDiagnostics,
                              failed_predicate: failedPredicate
                            }
                          );
                        }
                        approvedPageActor = pageContext.actor;
                        approvedPageActorAuthorized = true;
                      }
                      const targetAt = (x, y) => {
                        const hit = this.ownerDocument.elementFromPoint(x, y);
                        return hit === this || (hit && this.contains(hit));
                      };
                      const findTargetPoint = rect => {
                        const left = Math.max(0, rect.left);
                        const right = Math.min(innerWidth, rect.right);
                        const top = Math.max(0, rect.top);
                        const bottom = Math.min(innerHeight, rect.bottom);
                        if (right <= left || bottom <= top) return null;
                        // A sticky modal heading or an unrelated floating
                        // chat can cover the geometric centre of a valid
                        // Facebook checkbox row. Prove a point is owned by
                        // the already-authorized row (or one of its children)
                        // before dispatch; never click by coordinates alone.
                        for (const yFraction of [0.5, 0.25, 0.75]) {
                          for (const xFraction of [0.5, 0.25, 0.75, 0.1, 0.9]) {
                            const candidateX = left
                              + (right - left) * xFraction;
                            const candidateY = top
                              + (bottom - top) * yFraction;
                            if (targetAt(candidateX, candidateY)) {
                              return {x: candidateX, y: candidateY};
                            }
                          }
                        }
                        return null;
                      };
                      if (
                        !guard
                        || guard.token !== token
                        || guard.dirty
                        || guard.observer.takeRecords().length
                      ) fail("Captured snapshot node changed after validation");
                      if (identity() !== expected || !this.isConnected) {
                        fail("Protected page load changed before atomic click");
                      }
                      if (marketplacePriceStage === "submit") {
                        const expectedPrice = String(
                          requiredMarketplacePriceTwd ?? ""
                        );
                        const canonicalIntegerPrice = value => {
                          const raw = String(value ?? "").trim();
                          const match = raw.match(
                            /^(?:NT[$])?((?:0|[1-9][0-9]*)|(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+))$/
                          );
                          return match ? match[1].replace(/,/g, "") : null;
                        };
                        const flow = documentRef.__hermesMarketplacePriceFlow;
                        const candidates = [...documentRef.querySelectorAll(
                          "input, textarea"
                        )].filter(node => (
                          node.__hermesMarketplacePriceFlow?.token
                            === marketplacePriceToken
                        ));
                        const priceInput = candidates[0];
                        const submitCandidates = [...documentRef.querySelectorAll(
                          "button, [role=button]"
                        )].filter(node => (
                          node.__hermesMarketplacePriceSubmitFlow?.token
                            === marketplacePriceToken
                        ));
                        const markedSubmit = submitCandidates[0];
                        const submitDiagnostics = {
                          token_present: Boolean(marketplacePriceToken),
                          contract_price_valid: /^[0-9]+$/.test(expectedPrice),
                          flow_present: Boolean(flow),
                          flow_token_match: flow?.token === marketplacePriceToken,
                          listing_match: String(flow?.listingId || "")
                            === String(requiredListingId),
                          flow_price_match: String(flow?.priceTwd ?? "")
                            === expectedPrice,
                          flow_page_identity_match: flow?.pageIdentity === expected,
                          marked_input_count: candidates.length,
                          input_connected: Boolean(priceInput?.isConnected),
                          live_value: String(priceInput?.value ?? ""),
                          normalized_live_value: canonicalIntegerPrice(
                            priceInput?.value
                          ),
                          input_page_identity_match: (
                            priceInput?.__hermesMarketplacePriceFlow?.pageIdentity
                              === expected
                          ),
                          marked_submit_count: submitCandidates.length,
                          submit_control_match: markedSubmit === this,
                          submit_control_connected: Boolean(
                            markedSubmit?.isConnected
                          )
                        };
                        const failedSubmitPredicate = (
                          !submitDiagnostics.token_present ? "token_present"
                            : !submitDiagnostics.contract_price_valid
                              ? "contract_price_valid"
                            : !submitDiagnostics.flow_present ? "flow_present"
                            : !submitDiagnostics.flow_token_match
                              ? "flow_token_match"
                            : !submitDiagnostics.listing_match ? "listing_match"
                            : !submitDiagnostics.flow_price_match
                              ? "flow_price_match"
                            : !submitDiagnostics.flow_page_identity_match
                              ? "flow_page_identity_match"
                            : submitDiagnostics.marked_input_count !== 1
                              ? "marked_input_count"
                            : !submitDiagnostics.input_connected
                              ? "input_connected"
                            : submitDiagnostics.normalized_live_value
                                !== expectedPrice
                              ? "normalized_live_value"
                            : !submitDiagnostics.input_page_identity_match
                              ? "input_page_identity_match"
                            : submitDiagnostics.marked_submit_count !== 1
                              ? "marked_submit_count"
                            : !submitDiagnostics.submit_control_match
                              ? "submit_control_match"
                            : !submitDiagnostics.submit_control_connected
                              ? "submit_control_connected" : null
                        );
                        if (failedSubmitPredicate) {
                          fail(
                            `HERMES_MARKETPLACE_PRICE_GUARD|facebook_marketplace_price_submit_guard_rejected|${JSON.stringify({
                              ...submitDiagnostics,
                              failed_predicate: failedSubmitPredicate
                            })}`
                          );
                        }
                      }
                      // Popup semantics were re-read from the live AX node
                      // immediately above. The armed MutationObserver keeps
                      // the exact element and its attributes stable between
                      // that check and this renderer-task click. Do not switch
                      // semantic sources here: Facebook may expose hasPopup
                      // through AX while omitting aria-haspopup on this DOM
                      // wrapper.
                      if (requireGroupComposer) {
                        const dialog = this.closest?.('[role="dialog"]');
                        const dialogText = [
                          dialog?.getAttribute("aria-label") || "",
                          dialog?.innerText || ""
                        ].join(" ").replace(/\\s+/g, " ").toLowerCase();
                        const createPost = (
                          dialogText.includes("create post")
                          || dialogText.includes("建立貼文")
                        );
                        const forbiddenMode = (
                          dialogText.includes("anonymous post")
                          || dialogText.includes("匿名貼文")
                          || dialogText.includes("share post")
                          || dialogText.includes("分享貼文")
                        );
                        if (!dialog || !createPost || forbiddenMode) {
                          fail(
                            "Target is not the authorized group post composer"
                          );
                        }
                      }
                      if (["compose", "submit"].includes(pageComposerStage)) {
                        const dialog = this.closest?.('[role="dialog"]');
                        const currentPageContext = pageOpenContextIn(
                          documentRef
                        );
                        const currentSourceActorNames = (
                          currentPageContext.diagnostics
                            ?.management_actor_names || []
                        );
                        const currentSourceActorObservable = (
                          currentSourceActorNames.length > 0
                        );
                        const currentSourceActorMatch = (
                          currentSourceActorNames.length === 1
                          && currentSourceActorNames[0]
                            === requiredFacebookPageActor
                        );
                        const currentSourceActorContradiction = (
                          currentSourceActorObservable
                          && !currentSourceActorMatch
                        );
                        const pageComposerFlow = (
                          documentRef.__hermesPageComposerFlow
                        );
                        const currentComposerActorContradictions = dialog
                          ? composerActorContradictionsIn(
                            dialog, requiredFacebookPageActor
                          )
                          : [];
                        const composerActorProofRequired = Boolean(
                          pageComposerFlow?.composerActorProofRequired
                        );
                        const composerActorProofMarkerMatch = Boolean(
                          composerActorProofRequired
                            ? dialog?.getAttribute(
                              "data-hermes-page-composer-actor-proof"
                            ) === "visible"
                            : !dialog?.hasAttribute(
                              "data-hermes-page-composer-actor-proof"
                            )
                        );
                        const currentActor = (
                          dialog && composerActorProofRequired
                        )
                          ? composerActorBindingIn(
                            dialog,
                            pageComposerToken,
                            requiredFacebookPageActor
                          )
                          : null;
                        const tokenDialogs = [
                          ...documentRef.querySelectorAll(
                            '[data-hermes-page-composer-token]'
                          )
                        ];
                        const currentVisiblePageComposers = (
                          visiblePageComposers()
                        );
                        pageGuardDiagnostics = {
                          page_url_match: (
                            canonicalPageUrlOf(location.href)
                            === requiredFacebookPageUrl
                          ),
                          composer_token_present: Boolean(pageComposerToken),
                          expected_actor_present: Boolean(
                            requiredFacebookPageActor
                          ),
                          target_inside_dialog: Boolean(dialog),
                          token_dialog_count: tokenDialogs.length,
                          composer_visible: Boolean(
                            dialog
                            && currentVisiblePageComposers.includes(dialog)
                          ),
                          visible_composer_count:
                            currentVisiblePageComposers.length,
                          source_actor_observable:
                            currentSourceActorObservable,
                          source_actor_match: currentSourceActorMatch
                            ? true
                            : currentSourceActorObservable
                              ? false
                              : null,
                          source_actor_contradiction:
                            currentSourceActorContradiction,
                          switch_into_page_visible: Boolean(
                            currentPageContext.diagnostics
                              .switch_into_page_visible
                          ),
                          source_capability_match: Boolean(
                            pageComposerFlow?.token === pageComposerToken
                            && pageComposerFlow?.pageUrl
                              === requiredFacebookPageUrl
                            && pageComposerFlow?.actor
                              === requiredFacebookPageActor
                            && pageComposerFlow?.pageIdentity === expected
                          ),
                          contradictory_composer_actors:
                            currentComposerActorContradictions,
                          composer_actor_proof_required:
                            composerActorProofRequired,
                          composer_actor_proof_marker_match:
                            composerActorProofMarkerMatch,
                          composer_actor_match: composerActorProofRequired
                            ? Boolean(
                              currentActor?.url === requiredFacebookPageUrl
                              && currentActor?.name
                                === requiredFacebookPageActor
                            )
                            : null
                        };
                        if (
                          !pageComposerToken
                          || !requiredFacebookPageActor
                          || !dialog
                          || tokenDialogs.length !== 1
                          || tokenDialogs[0] !== dialog
                          || tokenDialogs[0].getAttribute(
                            'data-hermes-page-composer-token'
                          ) !== pageComposerToken
                          || dialog.getAttribute(
                            "data-hermes-page-composer-token"
                          ) !== pageComposerToken
                          || currentVisiblePageComposers.length !== 1
                          || currentVisiblePageComposers[0] !== dialog
                          || !pageGuardDiagnostics.page_url_match
                          || pageGuardDiagnostics.switch_into_page_visible
                          || currentSourceActorContradiction
                          || !pageGuardDiagnostics.source_capability_match
                          || currentComposerActorContradictions.length > 0
                          || !composerActorProofMarkerMatch
                          || (
                            composerActorProofRequired
                            && (
                              currentActor?.url !== requiredFacebookPageUrl
                              || currentActor?.name
                                !== requiredFacebookPageActor
                            )
                          )
                        ) {
                          failPageGuard(
                            "facebook_page_composer_binding_invalid",
                            {
                              ...pageGuardDiagnostics,
                              failed_predicate: (
                                !pageGuardDiagnostics.page_url_match
                                  ? "page_url_match"
                                  : !pageGuardDiagnostics.composer_token_present
                                    ? "composer_token_present"
                                    : !pageGuardDiagnostics.expected_actor_present
                                      ? "expected_actor_present"
                                      : !pageGuardDiagnostics.target_inside_dialog
                                        ? "target_inside_dialog"
                                        : pageGuardDiagnostics.token_dialog_count !== 1
                                          ? "token_dialog_count"
                                          : !pageGuardDiagnostics.composer_visible
                                            ? "composer_visible"
                                            : pageGuardDiagnostics.visible_composer_count !== 1
                                              ? "visible_composer_count"
                                            : pageGuardDiagnostics.switch_into_page_visible
                                              ? "switch_into_page_visible"
                                              : pageGuardDiagnostics.source_actor_contradiction
                                                ? "source_actor_contradiction"
                                              : !pageGuardDiagnostics.source_capability_match
                                                ? "source_capability_match"
                                                : pageGuardDiagnostics.contradictory_composer_actors.length > 0
                                                  ? "composer_actor_contradiction"
                                                : !pageGuardDiagnostics.composer_actor_proof_marker_match
                                                  ? "composer_actor_proof_marker_match"
                                              : "composer_actor_match"
                              )
                            }
                          );
                        }
                      }
                      let crosspostGroupId = null;
                      let crosspostGroupName = null;
                      let crosspostPreselectedGroupIds = [];
                      let crosspostPreselectedGroupNamesById = {};
                      let crosspostForSaleItemId = null;
                      let sourceControl = null;
                      // A Marketplace price update also carries the exact
                      // listing id, but it is not a cross-post operation.
                      // Enter the cross-post guard only when its own stage is
                      // explicitly active; price edit/fill/submit has a
                      // separate fail-closed flow below.
                      if (crosspostStage) {
                        const listingMatch = location.pathname.match(
                          /^\\/marketplace\\/item\\/([0-9]+)\\/?$/
                        );
                        const sellingRoute = (
                          /^\\/marketplace\\/you\\/selling\\/?$/
                            .test(location.pathname)
                        );
                        if (
                          (!listingMatch && !sellingRoute)
                          || (
                            listingMatch
                            && listingMatch[1] !== String(requiredListingId)
                          )
                        ) {
                          fail(
                            "Current route is not an authorized Marketplace listing source"
                          );
                        }
                        const allowedIds = new Set(
                          (allowedCrosspostGroupIds || []).map(String)
                        );
                        const allowedNames = new Set(
                          (allowedCrosspostGroupNames || []).map(String)
                        );
                        const expectedSelectedIds = new Set(
                          (selectedCrosspostGroupIds || []).map(String)
                        );
                        const expectedSelectedNames = new Set(
                          (selectedCrosspostGroupNames || []).map(String)
                        );
                        const readOnlyInspection = (
                          !allowedIds.size && !allowedNames.size
                        );
                        const strictNumericSourceProof = allowedIds.size > 0;
                        if (
                          !readOnlyInspection
                          && Boolean(allowedIds.size) === Boolean(allowedNames.size)
                        ) {
                          fail("Marketplace cross-post authority must use ids or exact names");
                        }
                        if (
                          ![
                            "open_menu", "open_dialog_from_menu",
                            "open_dialog_direct",
                            "select_group", "submit"
                          ].includes(crosspostStage)
                        ) fail("Unsupported Marketplace cross-post stage");
                        if (!crosspostSourceToken) {
                          fail("Marketplace source capability token is missing");
                        }
                        const listingIdsIn = container => {
                          const ids = new Set();
                          for (const candidate of [container, ...(
                            container.querySelectorAll?.(
                              '[data-listing-id], '
                              + '[data-marketplace-listing-id], '
                              + 'a[href*="/marketplace/item/"]'
                            ) || []
                          )]) {
                            for (const attribute of [
                              "data-listing-id",
                              "data-marketplace-listing-id"
                            ]) {
                              const value = candidate.getAttribute?.(attribute);
                              if (/^[0-9]+$/.test(value || "")) ids.add(value);
                            }
                            const href = candidate.getAttribute?.("href") || "";
                            const match = href.match(
                              /\\/marketplace\\/item\\/([0-9]+)(?:\\/|[?#]|$)/
                            );
                            if (match) ids.add(match[1]);
                          }
                          return ids;
                        };
                        const flow = this.ownerDocument.__hermesCrosspostFlow;
                        const bindSourceControl = () => {
                          const listingScopeSelector = [
                            "[data-listing-id]",
                            "[data-marketplace-listing-id]",
                            'a[href*="/marketplace/item/"]',
                            "article", "li", '[role="listitem"]'
                          ].join(", ");
                          const pageScopeSelector = [
                            "html", "body", "main", '[role="main"]',
                            '[role="feed"]', '[role="list"]'
                          ].join(", ");
                          const normalizeLabel = value => String(value || "")
                            .replace(/\\s+/g, " ").trim().toLowerCase();
                          if (listingMatch) {
                            const controlName = normalizeLabel(
                              expectedControlName
                            );
                            const sourceIsDirect = (
                              crosspostStage === "open_dialog_direct"
                            );
                            const supportedNames = sourceIsDirect
                              ? [
                                  "list in more places",
                                  "list your item in more places",
                                  "刊登到更多地方",
                                  "在更多地方刊登"
                                ]
                              : [
                                  "options", "more options",
                                  "選項", "更多選項"
                                ];
                            if (!supportedNames.includes(controlName)) {
                              fail(
                                "Item-page source control label is not supported"
                              );
                            }
                            const candidates = [...(
                              this.ownerDocument.querySelectorAll(
                                'button, a, [role="button"], [role="link"]'
                              )
                            )].filter(candidate => {
                              const candidateName = normalizeLabel(
                                candidate.getAttribute("aria-label")
                                || candidate.getAttribute("title")
                                || candidate.innerText
                              );
                              return (
                                candidateName === controlName
                                && candidate.isConnected
                                && candidate.getClientRects().length > 0
                                && candidate.getAttribute("aria-hidden")
                                  !== "true"
                                && !candidate.matches?.(":disabled")
                              );
                            });
                            if (
                              candidates.length !== 1
                              || candidates[0] !== this
                            ) {
                              fail(
                                "Item-page source menu is not uniquely bound to the authorized listing"
                              );
                            }
                            return;
                          }
                          let sellingAssociationDebug = "not_evaluated";
                          const hasSellingActionAssociation = candidate => {
                            const controlName = normalizeLabel(
                              expectedControlName
                            );
                            const controlPrefixes = [
                              "more options for ",
                              "更多選項：",
                              "更多選項:"
                            ];
                            const controlPrefix = controlPrefixes.find(
                              prefix => controlName.startsWith(prefix)
                            );
                            if (!controlPrefix) {
                              sellingAssociationDebug = "control_prefix";
                              return false;
                            }
                            const listingName = controlName.slice(
                              controlPrefix.length
                            ).trim();
                            const provenListingName = normalizeLabel(
                              requiredListingTitle
                            );
                            if (
                              !listingName
                              || (
                                strictNumericSourceProof
                                && (
                                  !provenListingName
                                  || listingName !== provenListingName
                                )
                              )
                            ) {
                              sellingAssociationDebug = "title_mismatch";
                              return false;
                            }
                            const documentSourceControls = [...(
                              this.ownerDocument.querySelectorAll(
                                'button, a, [role="button"], [role="link"]'
                              )
                            )].filter(control => (
                              normalizeLabel(
                                control.getAttribute("aria-label")
                                || control.getAttribute("title")
                                || control.innerText
                              ) === controlName
                              && control.isConnected
                              && control.getClientRects().length > 0
                              && !control.closest('[aria-hidden="true"]')
                              && !control.closest('[aria-disabled="true"]')
                              && !control.matches?.(":disabled")
                            ));
                            const sameAtomicControlChain = (
                              controls, anchor
                            ) => Boolean(anchor)
                              && controls.length > 0
                              && controls.every(control => (
                                control === anchor
                                || control.contains(anchor)
                                || anchor.contains(control)
                              ))
                              && controls.every((control, index) => (
                                controls.slice(index + 1).every(other => (
                                  control === other
                                  || control.contains(other)
                                  || other.contains(control)
                                ))
                              ));
                            if (
                              !sameAtomicControlChain(
                                documentSourceControls,
                                this
                              )
                            ) {
                              sellingAssociationDebug = "source_not_unique";
                              return false;
                            }
                            const sharePrefixes = [
                              "share ", "分享 "
                            ];
                            const expectedShareNames = new Set([
                              `share ${listingName}`,
                              `分享 ${listingName}`
                            ]);
                            const expectedBoostNames = new Set([
                              `boost listing for ${listingName}. boost to reach more potential buyers`,
                              `boost listings for ${listingName}. boost to reach more potential buyers`,
                              `推廣 ${listingName} 的刊登`,
                              `推廣 ${listingName} 的刊登內容`,
                              `為 ${listingName} 推廣刊登`
                            ]);
                            const requiredBoostName = normalizeLabel(
                              requiredBoostLabel
                            );
                            if (
                              strictNumericSourceProof
                              && !expectedBoostNames.has(requiredBoostName)
                            ) {
                              sellingAssociationDebug = "boost_label_proof";
                              return false;
                            }
                            const exactBoostControls = container => [...(
                              container.querySelectorAll('a[href*="target_id="]')
                            )].filter(control => {
                              try {
                                const parsed = new URL(
                                  control.getAttribute("href") || "",
                                  window.location.href
                                );
                                return parsed.origin === window.location.origin
                                  && parsed.pathname
                                    === "/ad_center/create/listingad/";
                              } catch (_err) {
                                return false;
                              }
                            });
                            let firstCompleteRow = this;
                            while (
                              firstCompleteRow
                              && !firstCompleteRow.matches?.(pageScopeSelector)
                            ) {
                              const actionControls = [...(
                                firstCompleteRow.querySelectorAll(
                                  'button, a, [role="button"], [role="link"]'
                                )
                              )];
                              const listingOptionControls = actionControls.filter(
                                control => controlPrefixes.some(prefix => (
                                  normalizeLabel(
                                    control.getAttribute("aria-label")
                                    || control.getAttribute("title")
                                    || control.innerText
                                  ).startsWith(prefix)
                                ))
                              );
                              const listingShareControls = actionControls.filter(
                                control => sharePrefixes.some(prefix => (
                                  normalizeLabel(
                                    control.getAttribute("aria-label")
                                    || control.getAttribute("title")
                                    || control.innerText
                                  ).startsWith(prefix)
                                ))
                              );
                              const boostControls = exactBoostControls(
                                firstCompleteRow
                              );
                              if (
                                listingOptionControls.length
                                && listingShareControls.length
                                && boostControls.length
                              ) break;
                              firstCompleteRow = firstCompleteRow.parentElement;
                            }
                            if (candidate !== firstCompleteRow) return false;
                            const actionControls = [...candidate.querySelectorAll(
                              'button, a, [role="button"], [role="link"]'
                            )];
                            const listingOptionControls = actionControls.filter(
                              control => controlPrefixes.some(prefix => (
                                normalizeLabel(
                                  control.getAttribute("aria-label")
                                  || control.getAttribute("title")
                                  || control.innerText
                                ).startsWith(prefix)
                              ))
                            );
                            const listingShareControls = actionControls.filter(
                              control => sharePrefixes.some(prefix => (
                                normalizeLabel(
                                  control.getAttribute("aria-label")
                                  || control.getAttribute("title")
                                  || control.innerText
                                ).startsWith(prefix)
                              ))
                            );
                            const boostControls = exactBoostControls(candidate);
                            const allBoostControls = actionControls.filter(
                              control => expectedBoostNames.has(normalizeLabel(
                                control.getAttribute("aria-label")
                                || control.getAttribute("title")
                                || control.innerText
                              ))
                            );
                            const shareAnchor = listingShareControls[0] || null;
                            const optionsAreOneAction = sameAtomicControlChain(
                              listingOptionControls,
                              this
                            ) && listingOptionControls.every(control => (
                              normalizeLabel(
                                control.getAttribute("aria-label")
                                || control.getAttribute("title")
                                || control.innerText
                              ) === controlName
                            ));
                            const sharesAreOneAction = sameAtomicControlChain(
                              listingShareControls,
                              shareAnchor
                            );
                            const optionChainIsOptionsOnly = (
                              listingOptionControls.every(optionControl => (
                                !listingShareControls.some(control => (
                                  control !== optionControl
                                  && optionControl.contains(control)
                                ))
                                && !allBoostControls.some(control => (
                                  control !== optionControl
                                  && optionControl.contains(control)
                                ))
                              ))
                            );
                            sellingAssociationDebug = [
                              "fingerprint_counts",
                              listingOptionControls.length,
                              listingShareControls.length,
                              boostControls.length,
                            ].join(":");
                            if (
                              !optionsAreOneAction
                              || !sharesAreOneAction
                              || !optionChainIsOptionsOnly
                              || !listingShareControls.every(control => (
                                expectedShareNames.has(normalizeLabel(
                                  control.getAttribute("aria-label")
                                  || control.getAttribute("title")
                                  || control.innerText
                                ))
                              ))
                              || boostControls.length !== 1
                            ) return false;
                            let boostTarget = null;
                            try {
                              const parsedBoost = new URL(
                                boostControls[0].getAttribute("href") || "",
                                window.location.href
                              );
                              const targets = parsedBoost.searchParams.getAll(
                                "target_id"
                              );
                              if (
                                parsedBoost.origin === window.location.origin
                                && parsedBoost.pathname
                                  === "/ad_center/create/listingad/"
                                && targets.length === 1
                                && /^[0-9]+$/.test(targets[0])
                              ) boostTarget = targets[0];
                            } catch (_err) {
                              boostTarget = null;
                            }
                            const boostName = normalizeLabel(
                              boostControls[0].getAttribute("aria-label")
                              || boostControls[0].getAttribute("title")
                              || boostControls[0].innerText
                            );
                            const boostNameMatches = (
                              strictNumericSourceProof
                                ? boostName === requiredBoostName
                                : expectedBoostNames.has(boostName)
                            );
                            // The title was proven by a fresh controlled AX
                            // snapshot of the canonical item URL. Facebook's
                            // Boost target_id is only a structural signal that
                            // this is the matching Selling action row; it is
                            // never alternate listing authority.
                            const matched = (
                              Boolean(boostTarget)
                              && boostNameMatches
                              && (
                                !strictNumericSourceProof
                                || boostTarget
                                  === String(requiredSourceEntityId || "")
                              )
                            );
                            sellingAssociationDebug = matched
                              ? "matched"
                              : `boost:${Boolean(boostTarget)}:${boostNameMatches}`;
                            return matched;
                          };
                          let sourceBound = false;
                          let sawAuthorizedListing = false;
                          let canonicalRowBound = false;
                          let associatedListingNode = null;
                          let listingNode = this;
                          while (listingNode) {
                            if (listingNode.matches?.(pageScopeSelector)) {
                              if (
                                sellingRoute
                                  ? associatedListingNode
                                  : (associatedListingNode || canonicalRowBound)
                              ) {
                                // The exact-title association is only accepted
                                // after every bounded ancestor between the
                                // control and page scope has been checked for a
                                // conflicting canonical Marketplace id.
                                sourceBound = true;
                                break;
                              }
                              fail(
                                sawAuthorizedListing
                                  ? "Cross-post control lacks a bounded listing row"
                                  : (
                                      "Cross-post control is not bound to the authorized listing"
                                      + ` | association_debug:${sellingAssociationDebug}`
                                    )
                              );
                            }
                            // Facebook's Selling rows are deeply nested generic
                            // divs. A row is proven by a canonical item/data id
                            // or by one same-row Boost entity plus a unique exact
                            // full-title action set. Shared wrappers fail closed.
                            const sourceIds = listingIdsIn(listingNode);
                            const associated = hasSellingActionAssociation(
                              listingNode
                            );
                            if (associated && !associatedListingNode) {
                              associatedListingNode = listingNode;
                            }
                            if (sourceIds.size > 1) {
                              fail(
                                "Cross-post control belongs to a shared listing container"
                              );
                            }
                            if (sourceIds.size === 1) {
                              if (!sourceIds.has(String(requiredListingId))) {
                                fail(
                                  "Cross-post control belongs to a different listing"
                                );
                              }
                              sawAuthorizedListing = true;
                            }
                            if (sourceIds.has(String(requiredListingId))) {
                              if (
                                listingNode.matches?.(listingScopeSelector)
                                || associatedListingNode
                              ) {
                                canonicalRowBound = true;
                              }
                            }
                            listingNode = listingNode.parentElement;
                          }
                          if (!sourceBound) {
                            fail(
                              sawAuthorizedListing
                                ? "Cross-post control lacks a bounded listing row"
                                : "Cross-post control is not bound to the authorized listing"
                            );
                          }
                        };
                        if (
                          crosspostStage === "open_menu"
                          || crosspostStage === "open_dialog_direct"
                        ) {
                          bindSourceControl();
                          sourceControl = this;
                        } else {
                          if (
                            !flow
                            || flow.token !== crosspostSourceToken
                            || flow.listingId !== String(requiredListingId)
                          ) {
                            fail(
                              "Marketplace cross-post source flow is not bound"
                            );
                          }
                          const markedSources = [...this.ownerDocument.querySelectorAll(
                            'button, a, [role="button"], [role="link"], '
                            + '[role="menuitem"]'
                          )].filter(candidate =>
                            candidate.__hermesCrosspostSource?.token
                              === crosspostSourceToken
                            && candidate.__hermesCrosspostSource?.listingId
                              === String(requiredListingId)
                          );
                          if (markedSources.length !== 1) {
                            fail(
                              "Marketplace source control is no longer unique"
                            );
                          }
                          sourceControl = markedSources[0];
                        }
                        if (crosspostStage === "open_dialog_from_menu") {
                          if (flow.stage !== "menu_open") {
                            fail("Marketplace source menu state changed");
                          }
                          const menu = this.closest?.('[role="menu"]');
                          const controlledId = sourceControl.getAttribute(
                            "aria-controls"
                          );
                          if (controlledId) {
                            if (
                              !menu
                              || this.ownerDocument.getElementById(controlledId)
                                !== menu
                            ) {
                              fail(
                                "List in more places is not in the source menu"
                              );
                            }
                          } else {
                            const visibleMenus = [...(
                              this.ownerDocument.querySelectorAll('[role="menu"]')
                            )].filter(candidate => {
                              const style = getComputedStyle(candidate);
                              const rect = candidate.getBoundingClientRect();
                              return (
                                rect.width > 0 && rect.height > 0
                                && style.display !== "none"
                                && style.visibility !== "hidden"
                              );
                            });
                            if (
                              sourceControl.getAttribute("aria-expanded")
                                !== "true"
                              || !menu
                              || visibleMenus.length !== 1
                              || visibleMenus[0] !== menu
                            ) {
                              fail(
                                "List in more places is not uniquely bound to the source menu"
                              );
                            }
                          }
                        } else if (crosspostStage === "open_dialog_direct") {
                          if (flow) {
                            fail(
                              "Direct List in more places did not start a fresh source flow"
                            );
                          }
                        }
                        if (
                          crosspostStage === "select_group"
                          || crosspostStage === "submit"
                        ) {
                          const normalizeGroupName = value => String(value || "")
                            .normalize("NFC").replace(/\\s+/g, " ").trim();
                          const dialog = this.closest?.('[role="dialog"]');
                          const dialogText = [
                            dialog?.getAttribute("aria-label") || "",
                            dialog?.innerText || ""
                          ].join(" ").replace(/\\s+/g, " ").toLowerCase();
                          const isCrosspostDialog = (
                            dialogText.includes("list in more places")
                            || dialogText.includes("list your item in more places")
                            || dialogText.includes("刊登到更多地方")
                            || dialogText.includes("更多地方")
                          );
                          if (!dialog || !isCrosspostDialog) {
                            fail(
                              "Target is not the authorized List in more places dialog"
                            );
                          }
                          if (
                            crosspostStage === "select_group"
                            && !["dialog_requested", "selecting"].includes(
                              flow.stage
                            )
                          ) {
                            fail("Marketplace source dialog state changed");
                          }
                          if (
                            crosspostStage === "submit"
                            && !["dialog_requested", "selecting"].includes(
                              flow.stage
                            )
                          ) {
                            fail("Marketplace source selection state changed");
                          }
                          let authoritativeCrosspostRows = null;
                          let authoritativeGroupNamesById = null;
                          const bindAuthoritativeCrosspostRows = () => {
                            if (authoritativeCrosspostRows) {
                              return authoritativeCrosspostRows;
                            }
                            let records;
                            try {
                              records = require("RelayFBEnvironment")
                                .getStore().getSource().toJSON();
                            } catch {
                              fail(
                                "Facebook cross-post Relay data is unavailable"
                              );
                            }
                            const productItem = requiredSourceEntityId
                              ? records?.[String(requiredSourceEntityId)]
                              : null;
                            const liveForSaleItemId = productItem
                              ?.for_sale_item?.__ref;
                            let forSaleItemId = String(
                              requiredForSaleItemId || ""
                            );
                            if (forSaleItemId) {
                              if (
                                !/^[0-9]+$/.test(forSaleItemId)
                                || forSaleItemId !== String(requiredListingId)
                              ) {
                                fail(
                                  "Facebook cross-post for-sale item binding changed before dispatch"
                                );
                              }
                              // Facebook may evict the canonical ProductItem
                              // after opening the dialog.  When it remains in
                              // the Relay store it is corroborating evidence
                              // and any conflict still fails closed.
                              if (
                                productItem
                                && (
                                  productItem.__typename !== "ProductItem"
                                  || String(productItem.id) !== String(
                                    requiredSourceEntityId
                                  )
                                  || String(liveForSaleItemId || "")
                                    !== forSaleItemId
                                )
                              ) {
                                fail(
                                  "Facebook cross-post for-sale item binding changed before dispatch"
                                );
                              }
                            } else {
                              if (
                                productItem?.__typename !== "ProductItem"
                                || String(productItem.id) !== String(
                                  requiredSourceEntityId
                                )
                                || !/^[0-9]+$/.test(
                                  liveForSaleItemId || ""
                                )
                                || String(liveForSaleItemId) !== String(
                                  requiredListingId
                                )
                              ) {
                                fail(
                                  "Facebook cross-post listing has no authoritative for-sale item binding"
                                );
                              }
                              forSaleItemId = String(liveForSaleItemId);
                            }
                            crosspostForSaleItemId = String(forSaleItemId);
                            const connectionNeedle = (
                              `for_sale_item_id:\"${forSaleItemId}\"`
                            );
                            const connections = Object.entries(records || {})
                              .filter(([key, record]) => (
                                record?.__typename
                                  === "MarketplaceSuggestedCrosspostTargetsEdgeViewConnection"
                                && key.includes(
                                  "marketplace_suggested_crosspost_targets("
                                )
                                && key.includes(connectionNeedle)
                              ));
                            if (connections.length !== 1) {
                              fail(
                                "Facebook cross-post destination connection is not bound to the active listing"
                              );
                            }
                            const [, connection] = connections[0];
                            const edgeRefs = connection.edges?.__refs;
                            if (
                              !Array.isArray(edgeRefs)
                              || !edgeRefs.length
                            ) {
                              fail(
                                "Facebook cross-post destination connection is empty"
                              );
                            }
                            const groupDescriptors = edgeRefs.map(edgeRef => {
                              const edge = records[edgeRef];
                              const groupId = edge?.node?.__ref;
                              const group = records[groupId];
                              if (
                                !/^[0-9]+$/.test(groupId || "")
                                || group?.__typename !== "Group"
                                || String(group.id) !== groupId
                              ) {
                                fail(
                                  "Facebook cross-post connection contains an invalid group record"
                                );
                              }
                              const picturePaths = new Set();
                              for (const [key, value] of Object.entries(group)) {
                                if (
                                  !key.startsWith("profile_picture(")
                                  || typeof value?.__ref !== "string"
                                ) continue;
                                const raw = records[value.__ref]?.uri || "";
                                try {
                                  const parsed = new URL(raw, location.href);
                                  if (parsed.hostname.endsWith("fbcdn.net")) {
                                    picturePaths.add(parsed.pathname);
                                  }
                                } catch {}
                              }
                              if (!picturePaths.size) {
                                fail(
                                  "Facebook cross-post group record has no profile image identity"
                                );
                              }
                              return {
                                groupId,
                                name: normalizeGroupName(group.name),
                                picturePaths
                              };
                            });
                            if (
                              new Set(groupDescriptors.map(
                                descriptor => descriptor.groupId
                              )).size !== groupDescriptors.length
                            ) {
                              fail(
                                "Facebook cross-post connection repeats a group record"
                              );
                            }
                            const allSelectionControls = [...(
                              dialog.querySelectorAll([
                                '[role="checkbox"]',
                                '[role="menuitemcheckbox"]',
                                '[role="switch"]', '[role="option"]',
                                'input[type="checkbox"]'
                              ].join(", "))
                            )];
                            const optionRoots = allSelectionControls.filter(
                              candidate => !allSelectionControls.some(other =>
                                other !== candidate && other.contains(candidate)
                              )
                            );
                            if (optionRoots.length !== edgeRefs.length) {
                              fail(
                                "Facebook cross-post rows do not match the authoritative destination connection"
                              );
                            }
                            const rowMap = new Map();
                            const nameMap = new Map();
                            const seenGroupIds = new Set();
                            optionRoots.forEach(option => {
                              const rowAssetPaths = new Set();
                              for (const asset of option.querySelectorAll(
                                "img[src], image"
                              )) {
                                const raw = asset.getAttribute("src")
                                  || asset.getAttribute("xlink:href")
                                  || asset.getAttributeNS?.(
                                    "http://www.w3.org/1999/xlink", "href"
                                  )
                                  || asset.getAttribute("href") || "";
                                try {
                                  const parsed = new URL(raw, location.href);
                                  if (parsed.hostname.endsWith("fbcdn.net")) {
                                    rowAssetPaths.add(parsed.pathname);
                                  }
                                } catch {}
                              }
                              const rowElements = [
                                option, ...option.querySelectorAll("span, div")
                              ];
                              const rowNames = new Set(rowElements.map(
                                element => normalizeGroupName(element.innerText)
                              ).filter(Boolean));
                              const matches = groupDescriptors.filter(
                                descriptor => (
                                  rowNames.has(descriptor.name)
                                  && [...descriptor.picturePaths].some(
                                    path => rowAssetPaths.has(path)
                                  )
                                )
                              );
                              if (matches.length !== 1) {
                                fail(
                                  "Facebook cross-post row has no unique authoritative group identity"
                                );
                              }
                              const groupId = matches[0].groupId;
                              if (seenGroupIds.has(groupId)) {
                                fail(
                                  "Facebook cross-post rows repeat an authoritative group"
                                );
                              }
                              seenGroupIds.add(groupId);
                              rowMap.set(option, groupId);
                              nameMap.set(groupId, matches[0].name);
                            });
                            const missingAllowedIds = [...allowedIds].filter(
                              groupId => !seenGroupIds.has(groupId)
                            );
                            if (missingAllowedIds.length) {
                              fail(
                                "Authorized cross-post groups are absent from Facebook's destination connection"
                              );
                            }
                            const descriptorNames = groupDescriptors.map(
                              descriptor => descriptor.name
                            );
                            if (descriptorNames.some(name => !name)) {
                              fail(
                                "Facebook cross-post destination names are missing"
                              );
                            }
                            const invalidAllowedNames = [...allowedNames].filter(
                              name => descriptorNames.filter(
                                descriptorName => descriptorName === name
                              ).length !== 1
                            );
                            if (invalidAllowedNames.length) {
                              fail(
                                "Authorized exact-name groups are absent or ambiguous in Facebook's destination connection"
                              );
                            }
                            authoritativeCrosspostRows = rowMap;
                            authoritativeGroupNamesById = nameMap;
                            return rowMap;
                          };
                          const groupNameForId = groupId => {
                            bindAuthoritativeCrosspostRows();
                            return authoritativeGroupNamesById?.get(groupId) || null;
                          };
                          const discoverGroupId = control => {
                            const containerSelector = [
                              "label", "li", '[role="checkbox"]',
                              '[role="menuitemcheckbox"]', '[role="option"]',
                              '[role="switch"]', "[data-group-id]",
                              "[data-groupid]"
                            ].join(", ");
                            const selectionSelector = [
                              '[role="checkbox"]',
                              '[role="menuitemcheckbox"]', '[role="option"]',
                              '[role="switch"]', 'input[type="checkbox"]'
                            ].join(", ");
                            let node = control;
                            let sawUniqueSelectionRow = false;
                            for (
                              let depth = 0;
                              node && node !== dialog && depth < 8;
                              depth += 1, node = node.parentElement
                            ) {
                              const selectionControls = [
                                ...(node.matches?.(selectionSelector)
                                  ? [node] : []),
                                ...node.querySelectorAll(selectionSelector)
                              ];
                              const roots = selectionControls.filter(candidate =>
                                !selectionControls.some(other =>
                                  other !== candidate
                                  && other.contains(candidate)
                                )
                              );
                              // Snapshot references often resolve to the
                              // visible text or image inside Facebook's
                              // checkbox row. Keep walking until an ancestor
                              // actually contains the selection control; an
                              // empty descendant is not ambiguous.
                              if (!roots.length) continue;
                              if (roots.length > 1 && sawUniqueSelectionRow) {
                                return null;
                              }
                              if (
                                roots.length > 1
                                || !(
                                  roots[0] === control
                                  || roots[0].contains(control)
                                  || control.contains?.(roots[0])
                                )
                              ) {
                                fail(
                                  "Cross-post option is not a unique selection row"
                                );
                              }
                              sawUniqueSelectionRow = true;
                              const identityNode = node.matches?.(
                                containerSelector
                              ) ? node : roots[0].matches?.(containerSelector)
                                ? roots[0] : null;
                              if (!identityNode) continue;
                              const ids = new Set();
                              for (const link of identityNode.querySelectorAll(
                                'a[href*="/groups/"]'
                              )) {
                                const href = link.getAttribute("href") || "";
                                const match = href.match(
                                  /\\/groups\\/([0-9]+)(?:\\/|[?#]|$)/
                                );
                                if (match) ids.add(match[1]);
                              }
                              for (const candidate of [identityNode, ...(
                                identityNode.querySelectorAll(
                                  "[data-group-id], [data-groupid]"
                                )
                              )]) {
                                for (const attribute of [
                                  "data-group-id", "data-groupid"
                                ]) {
                                  const value = candidate.getAttribute?.(
                                    attribute
                                  );
                                  if (/^[0-9]+$/.test(value || "")) {
                                    ids.add(value);
                                  }
                                }
                              }
                              if (ids.size > 1) {
                                fail(
                                  "Cross-post option maps to multiple group ids"
                                );
                              }
                              if (!ids.size) {
                                const authoritativeId = (
                                  bindAuthoritativeCrosspostRows().get(roots[0])
                                );
                                if (authoritativeId) ids.add(authoritativeId);
                              }
                              if (ids.size > 1) {
                                fail(
                                  "Cross-post option maps to multiple authoritative groups"
                                );
                              }
                              if (ids.size === 1) return [...ids][0];
                            }
                            return null;
                          };
                          const selectedControls = [...dialog.querySelectorAll(
                            '[role="checkbox"][aria-checked="true"], '
                            + '[role="menuitemcheckbox"][aria-checked="true"], '
                            + '[role="switch"][aria-checked="true"], '
                            + '[role="option"][aria-selected="true"], '
                            + 'input[type="checkbox"]:checked'
                          )];
                          const controlIsSelected = control => (
                            control.matches?.('input[type="checkbox"]')
                              ? Boolean(control.checked)
                              : control.getAttribute?.("aria-checked") === "true"
                                || control.getAttribute?.("aria-selected") === "true"
                          );
                          const actualSelectedIds = new Set();
                          const actualSelectedNames = new Set();
                          for (const selectedControl of selectedControls) {
                            const selectedId = discoverGroupId(selectedControl);
                            const selectedName = selectedId
                              ? groupNameForId(selectedId) : null;
                            if (
                              !selectedId
                              || (
                                allowedIds.size
                                  ? !allowedIds.has(selectedId)
                                  : !selectedName || !allowedNames.has(selectedName)
                              )
                            ) {
                              fail(
                                "Selected cross-post option is not bound to an authorized destination"
                              );
                            }
                            actualSelectedIds.add(selectedId);
                            if (selectedName) actualSelectedNames.add(selectedName);
                          }
                          if (crosspostStage === "submit") {
                            const allSelectionControls = [...(
                              dialog.querySelectorAll([
                                '[role="checkbox"]',
                                '[role="menuitemcheckbox"]',
                                '[role="switch"]', '[role="option"]',
                                'input[type="checkbox"]'
                              ].join(", "))
                            )];
                            const optionRoots = allSelectionControls.filter(
                              candidate => !allSelectionControls.some(other =>
                                other !== candidate && other.contains(candidate)
                              )
                            );
                            if (!optionRoots.length) {
                              fail("Cross-post destination list is empty");
                            }
                            const unknownStateControls = [...(
                              dialog.querySelectorAll(
                                "[aria-checked], [aria-selected]"
                              )
                            )].filter(candidate =>
                              !candidate.matches([
                                '[role="checkbox"]',
                                '[role="menuitemcheckbox"]',
                                '[role="switch"]', '[role="option"]'
                              ].join(", "))
                            );
                            if (unknownStateControls.length) {
                              fail(
                                "Cross-post dialog has an unknown selection control"
                              );
                            }
                            const enumeratedGroupIds = new Set();
                            for (const optionRoot of optionRoots) {
                              const optionId = discoverGroupId(optionRoot);
                              if (!optionId && controlIsSelected(optionRoot)) {
                                fail(
                                  "Selected cross-post destination has no authorized identity"
                                );
                              }
                              if (optionId && enumeratedGroupIds.has(optionId)) {
                                fail(
                                  "Cross-post destination rows are not uniquely enumerable"
                                );
                              }
                              if (optionId) enumeratedGroupIds.add(optionId);
                            }
                            bindAuthoritativeCrosspostRows();
                          }
                          if (crosspostStage === "select_group") {
                            const discovered = discoverGroupId(this);
                            const discoveredName = discovered
                              ? groupNameForId(discovered) : null;
                            if (
                              !discovered
                              || (
                                allowedIds.size
                                  ? !allowedIds.has(discovered)
                                  : !discoveredName
                                    || !allowedNames.has(discoveredName)
                              )
                            ) {
                              fail(
                                "Cross-post option is not bound to an authorized destination"
                              );
                            }
                            crosspostGroupId = discovered;
                            if (allowedNames.size) {
                              crosspostGroupName = discoveredName;
                            }
                            crosspostPreselectedGroupIds = [
                              ...actualSelectedIds
                            ];
                            if (allowedNames.size) {
                              for (const selectedId of actualSelectedIds) {
                                crosspostPreselectedGroupNamesById[selectedId] =
                                  groupNameForId(selectedId);
                              }
                            }
                          } else if (crosspostStage === "submit") {
                            const selectionChanged = allowedIds.size
                              ? (
                                  actualSelectedIds.size
                                    !== expectedSelectedIds.size
                                  || [...expectedSelectedIds].some(
                                    id => !actualSelectedIds.has(id)
                                  )
                                )
                              : (
                                  actualSelectedNames.size
                                    !== expectedSelectedNames.size
                                  || [...expectedSelectedNames].some(
                                    name => !actualSelectedNames.has(name)
                                  )
                                );
                            if (selectionChanged) {
                              fail(
                                "Selected cross-post groups changed before final Post"
                              );
                            }
                          }
                        }
                      }
                      if (marketplacePriceStage === "submit") {
                        const markedInput = [...documentRef.querySelectorAll(
                          "input, textarea"
                        )].find(node => (
                          node.__hermesMarketplacePriceFlow?.token
                            === marketplacePriceToken
                        ));
                        if (markedInput) {
                          delete markedInput.__hermesMarketplacePriceFlow;
                        }
                        const markedSubmit = [...documentRef.querySelectorAll(
                          "button, [role=button]"
                        )].find(node => (
                          node.__hermesMarketplacePriceSubmitFlow?.token
                            === marketplacePriceToken
                        ));
                        if (markedSubmit) {
                          delete markedSubmit.__hermesMarketplacePriceSubmitFlow;
                        }
                        delete documentRef.__hermesMarketplacePriceFlow;
                      }
                      let rect = this.getBoundingClientRect();
                      let x = rect.left + rect.width / 2;
                      let y = rect.top + rect.height / 2;
                      let provenTargetPoint = targetAt(x, y)
                        ? {x, y} : null;
                      if (
                        crosspostStage === "select_group"
                        && (
                          x < 0 || y < 0
                          || x > innerWidth || y > innerHeight
                          || !provenTargetPoint
                        )
                      ) {
                        // The Facebook destination list scrolls inside its
                        // modal rather than in the page viewport. Once the
                        // exact row has been atomically bound to an approved
                        // destination above, bring only that row into view
                        // before the hit test. This is a guarded local UI
                        // transition; it neither selects a group nor permits
                        // arbitrary CDP/DOM mutation.
                        this.scrollIntoView({
                          behavior: "auto",
                          block: "center",
                          inline: "nearest"
                        });
                        rect = this.getBoundingClientRect();
                        x = rect.left + rect.width / 2;
                        y = rect.top + rect.height / 2;
                        provenTargetPoint = findTargetPoint(rect);
                      }
                      const style = getComputedStyle(this);
                      if (
                        rect.width <= 0 || rect.height <= 0
                        || x < 0 || y < 0
                        || x > innerWidth || y > innerHeight
                        || style.display === "none"
                        || style.visibility === "hidden"
                        || style.pointerEvents === "none"
                        || Number(style.opacity) === 0
                        || this.disabled
                        || this.getAttribute("aria-disabled") === "true"
                        || !(provenTargetPoint || targetAt(x, y))
                      ) fail("Captured snapshot node is not interactable");
                      if (requiredGroupId) {
                        const match = location.pathname.match(
                          /^\\/groups\\/([0-9]+)(?:\\/|$)/
                        );
                        if (
                          !match
                          || match[1] !== String(requiredGroupId)
                        ) {
                          fail(
                            "Current route is not the authorized group"
                          );
                        }
                      }
                      let readonlyCanonicalGroupUrl = null;
                      if (requireReadonlyGroupNavigation) {
                        const anchor = this.matches?.("a[href]")
                          ? this : this.closest?.("a[href]");
                        if (!anchor) {
                          fail("Read-only group evidence target is not a link");
                        }
                        let target;
                        try {
                          target = new URL(
                            anchor.getAttribute("href") || "",
                            location.href
                          );
                        } catch (_err) {
                          fail("Read-only group evidence link URL is invalid");
                        }
                        if (target.origin !== location.origin) {
                          fail("Read-only group evidence link leaves Facebook");
                        }
                        const direct = target.pathname.match(new RegExp(
                          `^/groups/${String(requiredGroupId)}/(?:posts|permalink)/([0-9]+)/?$`
                        ));
                        const photoSet = target.searchParams.get("set") || "";
                        const photo = (
                          target.pathname === "/photo"
                          || target.pathname === "/photo/"
                          || target.pathname === "/photo.php"
                        ) && photoSet.match(/^(?:gm|pcb)[.]([0-9]+)$/);
                        const article = anchor.closest?.('[role="article"]');
                        const articlePostIds = article ? new Set(
                          [...article.querySelectorAll('a[href]')]
                            .map(candidate => {
                              try {
                                const parsed = new URL(
                                  candidate.getAttribute("href") || "",
                                  location.href
                                );
                                if (parsed.origin !== location.origin) return "";
                                const match = parsed.pathname.match(new RegExp(
                                  `^/groups/${String(requiredGroupId)}/(?:posts|permalink)/([0-9]+)/?$`
                                ));
                                return match?.[1] || "";
                              } catch (_err) {
                                return "";
                              }
                            })
                            .filter(Boolean)
                        ) : new Set();
                        const photoPostId = photo?.[1] || "";
                        // Facebook group-search results do not consistently
                        // expose role=article or a visible permalink.  In
                        // that layout, bind the photo to the smallest live
                        // post unit instead: exactly one post-actions control,
                        // its matching visible author link, one unique
                        // same-group permalink, and one matching pcb/gm
                        // parent-post id across all photos in the unit.  The
                        // permalink remains the authoritative group binding;
                        // the structural unit only replaces role=article.
                        let structuralPhotoPostId = "";
                        if (photoPostId && !articlePostIds.has(photoPostId)) {
                          const normalizePostLabel = value => String(value || "")
                            .normalize("NFC").replace(/\\s+/g, " ").trim();
                          let candidate = anchor.parentElement;
                          while (candidate && candidate !== document.body) {
                            const actionControls = [...candidate.querySelectorAll(
                              'button[aria-label], [role="button"][aria-label]'
                            )].filter(control => normalizePostLabel(
                              control.getAttribute("aria-label")
                            ).match(/^Actions for this post by (.+)$/i));
                            if (actionControls.length === 1) {
                              const actionMatch = normalizePostLabel(
                                actionControls[0].getAttribute("aria-label")
                              ).match(/^Actions for this post by (.+)$/i);
                              const authorName = normalizePostLabel(
                                actionMatch?.[1] || ""
                              );
                              const matchingAuthors = [...(
                                candidate.querySelectorAll('a[href]')
                              )].filter(author => {
                                try {
                                  const parsed = new URL(
                                    author.getAttribute("href") || "",
                                    location.href
                                  );
                                  const label = normalizePostLabel(
                                    author.innerText
                                    || author.getAttribute("aria-label")
                                    || author.getAttribute("title")
                                  );
                                  return parsed.origin === location.origin
                                    && label === authorName
                                    && author.isConnected
                                    && author.getClientRects().length > 0;
                                } catch (_err) {
                                  return false;
                                }
                              });
                              const unitPhotoPostIds = new Set(
                                [...candidate.querySelectorAll('a[href]')]
                                  .map(photoAnchor => {
                                    try {
                                      const parsed = new URL(
                                        photoAnchor.getAttribute("href") || "",
                                        location.href
                                      );
                                      if (parsed.origin !== location.origin) {
                                        return "";
                                      }
                                      const set = parsed.searchParams.get("set")
                                        || "";
                                      return set.match(
                                        /^(?:gm|pcb)[.]([0-9]+)$/
                                      )?.[1] || "";
                                    } catch (_err) {
                                      return "";
                                    }
                                  })
                                  .filter(Boolean)
                              );
                              const unitGroupPostIds = new Set(
                                [...candidate.querySelectorAll('a[href]')]
                                  .map(postAnchor => {
                                    try {
                                      const parsed = new URL(
                                        postAnchor.getAttribute("href") || "",
                                        location.href
                                      );
                                      if (parsed.origin !== location.origin) {
                                        return "";
                                      }
                                      return parsed.pathname.match(new RegExp(
                                        `^/groups/${String(requiredGroupId)}/(?:posts|permalink)/([0-9]+)/?$`
                                      ))?.[1] || "";
                                    } catch (_err) {
                                      return "";
                                    }
                                  })
                                  .filter(Boolean)
                              );
                              if (
                                matchingAuthors.length >= 1
                                && unitGroupPostIds.size === 1
                                && unitGroupPostIds.has(photoPostId)
                                && unitPhotoPostIds.size === 1
                                && unitPhotoPostIds.has(photoPostId)
                              ) {
                                structuralPhotoPostId = photoPostId;
                                break;
                              }
                            }
                            candidate = candidate.parentElement;
                          }
                        }
                        if (
                          photoPostId
                          && (
                            (
                              articlePostIds.size !== 1
                              || !articlePostIds.has(photoPostId)
                            )
                            && structuralPhotoPostId !== photoPostId
                          )
                        ) {
                          fail(
                            "Read-only photo link is not atomically bound to "
                            + "one same-group post unit"
                          );
                        }
                        const postId = direct?.[1] || photoPostId;
                        if (!postId) {
                          fail(
                            "Read-only group evidence link is not a canonical "
                            + "same-group post/permalink or group-post photo"
                          );
                        }
                        readonlyCanonicalGroupUrl = [
                          location.origin,
                          "groups",
                          String(requiredGroupId),
                          "posts",
                          postId
                        ].join("/");
                      }
                      guard.observer.disconnect();
                      delete this.__hermesAtomicGuard;
                      if (crosspostStage) {
                        if (
                          crosspostStage === "open_menu"
                          || crosspostStage === "open_dialog_direct"
                        ) {
                          Object.defineProperty(
                            this, "__hermesCrosspostSource", {
                              value: {
                                token: crosspostSourceToken,
                                listingId: String(requiredListingId)
                              },
                              configurable: true
                            }
                          );
                          Object.defineProperty(
                            this.ownerDocument, "__hermesCrosspostFlow", {
                              value: {
                                token: crosspostSourceToken,
                                listingId: String(requiredListingId),
                                stage: (
                                  crosspostStage === "open_menu"
                                    ? "menu_open" : "dialog_requested"
                                )
                              },
                              configurable: true,
                              writable: true
                            }
                          );
                        } else if (
                          crosspostStage === "open_dialog_from_menu"
                        ) {
                          this.ownerDocument.__hermesCrosspostFlow.stage =
                            "dialog_requested";
                        } else if (crosspostStage === "select_group") {
                          this.ownerDocument.__hermesCrosspostFlow.stage =
                            "selecting";
                        } else if (crosspostStage === "submit") {
                          delete sourceControl.__hermesCrosspostSource;
                          delete this.ownerDocument.__hermesCrosspostFlow;
                        }
                      }
                      // The hit test, page identity check, group binding, and
                      // dispatch run in one renderer task. This cannot click a
                      // replacement document between separate CDP calls.
                      Object.defineProperty(
                        this, "__hermesAtomicDispatchToken", {
                          value: token, configurable: true
                        }
                      );
                      this.click();
                      if (this.__hermesAtomicDispatchToken === token) {
                        delete this.__hermesAtomicDispatchToken;
                      }
                      let boundPageComposerToken = null;
                      if (pageComposerStage === "open") {
                        if (!pageComposerToken) {
                          failPageGuard(
                            "facebook_page_composer_token_missing",
                            pageGuardDiagnostics
                          );
                        }
                        let candidates = [];
                        for (let attempt = 0; attempt < 100; attempt += 1) {
                          await new Promise(resolve => setTimeout(resolve, 50));
                          candidates = visiblePageComposers().filter(
                            dialog => !visibleDialogShellsBefore.has(dialog)
                          );
                        }
                        const openedComposerShells = (
                          visiblePageComposerShells().filter(
                            dialog => !visibleDialogShellsBefore.has(dialog)
                          )
                        );
                        if (candidates.length !== 1) {
                          failPageGuard(
                            "facebook_page_composer_not_unique",
                            {
                              ...pageGuardDiagnostics,
                              opened_composer_shell_count:
                                openedComposerShells.length,
                              opened_composer_count: candidates.length,
                              failed_predicate: "opened_composer_count"
                            }
                          );
                        }
                        // Keep the five-second uniqueness observation cheap.
                        // Actor proof scans a large Facebook DOM and only the
                        // final settled singleton can be accepted, so compute
                        // it once after the observation window.
                        const composerActor = composerActorBindingIn(
                          candidates[0], null, approvedPageActor,
                          approvedPageActorAuthorized
                        );
                        const settledPageContext = pageOpenContextIn(
                          documentRef
                        );
                        const settledSourceActorNames = (
                          settledPageContext.diagnostics
                            ?.management_actor_names || []
                        );
                        const settledSourceActorObservable = (
                          settledSourceActorNames.length > 0
                        );
                        const settledSourceActorMatch = (
                          settledSourceActorNames.length === 1
                          && settledSourceActorNames[0]
                            === approvedPageActor
                        );
                        const settledSourceActorContradiction = (
                          settledSourceActorObservable
                          && !settledSourceActorMatch
                        );
                        const composerActorContradictions = (
                          composerActorContradictionsIn(
                            candidates[0], approvedPageActor
                          )
                        );
                        if (
                          !approvedPageActor
                          || !settledPageContext.diagnostics.page_url_match
                          || settledPageContext.diagnostics
                            .switch_into_page_visible
                          || settledSourceActorContradiction
                          || composerActorContradictions.length > 0
                        ) {
                          failPageGuard(
                            "facebook_page_composer_actor_mismatch",
                            {
                              ...pageGuardDiagnostics,
                              opened_composer_shell_count:
                                openedComposerShells.length,
                              opened_composer_count: candidates.length,
                              expected_actor: approvedPageActor,
                              observed_actor: composerActor?.name || null,
                              observed_actor_url: composerActor?.url || null,
                              settled_source_actor:
                                settledPageContext.actor || null,
                              settled_source_actor_observable:
                                settledSourceActorObservable,
                              settled_source_actor_match:
                                settledSourceActorMatch
                                  ? true
                                  : settledSourceActorObservable
                                    ? false
                                    : null,
                              settled_source_actor_contradiction:
                                settledSourceActorContradiction,
                              contradictory_composer_actors:
                                composerActorContradictions,
                              failed_predicate: (
                                !settledPageContext.diagnostics.page_url_match
                              )
                                ? "page_url_match"
                                : settledPageContext.diagnostics
                                  .switch_into_page_visible
                                  ? "switch_into_page_visible"
                                  : settledSourceActorContradiction
                                    ? "source_actor_contradiction"
                                    : composerActorContradictions.length > 0
                                      ? "composer_actor_contradiction"
                                      : "composer_actor_match"
                            }
                          );
                        }
                        candidates[0].setAttribute(
                          "data-hermes-page-composer-token",
                          pageComposerToken
                        );
                        candidates[0].removeAttribute(
                          "data-hermes-page-composer-actor-proof"
                        );
                        for (const staleActor of candidates[0].querySelectorAll(
                          '[data-hermes-page-composer-actor-token]'
                        )) staleActor.removeAttribute(
                          "data-hermes-page-composer-actor-token"
                        );
                        Object.defineProperty(
                          documentRef, "__hermesPageComposerFlow", {
                            value: Object.freeze({
                              token: pageComposerToken,
                              pageUrl: requiredFacebookPageUrl,
                              actor: approvedPageActor,
                              pageIdentity: expected,
                              composerActorProofRequired: Boolean(
                                composerActor
                              )
                            }),
                            configurable: true
                          }
                        );
                        if (composerActor) {
                          composerActor.element.setAttribute(
                            "data-hermes-page-composer-actor-token",
                            pageComposerToken
                          );
                          candidates[0].setAttribute(
                            "data-hermes-page-composer-actor-proof",
                            "visible"
                          );
                        }
                        boundPageComposerToken = pageComposerToken;
                        pageGuardDiagnostics = {
                          ...pageGuardDiagnostics,
                          opened_composer_shell_count:
                            openedComposerShells.length,
                          opened_composer_count: candidates.length,
                          settled_source_actor:
                            settledPageContext.actor || null,
                          settled_source_actor_observable:
                            settledSourceActorObservable,
                          settled_source_actor_match:
                            settledSourceActorMatch
                              ? true
                              : settledSourceActorObservable
                                ? false
                                : null,
                          settled_source_actor_contradiction:
                            settledSourceActorContradiction,
                          composer_actor_match: composerActor ? true : null,
                          actor_binding_source: composerActor
                            ? "composer_visible_actor"
                            : "source_page_management_context",
                          bound_actor: approvedPageActor
                        };
                      } else if (pageComposerStage === "submit") {
                        const dialog = this.closest?.('[role="dialog"]');
                        dialog?.removeAttribute(
                          "data-hermes-page-composer-token"
                        );
                        dialog?.removeAttribute(
                          "data-hermes-page-composer-actor-proof"
                        );
                        for (const actor of dialog?.querySelectorAll(
                          '[data-hermes-page-composer-actor-token]'
                        ) || []) actor.removeAttribute(
                          "data-hermes-page-composer-actor-token"
                        );
                        delete documentRef.__hermesPageComposerFlow;
                      }
                      return {
                        ok: true, action: "click", pageIdentity: expected,
                        crosspostGroupId, crosspostGroupName,
                        crosspostPreselectedGroupIds,
                        crosspostPreselectedGroupNamesById,
                        crosspostForSaleItemId, boundPageComposerToken,
                        boundPageActor: approvedPageActor,
                        pageGuardDiagnostics,
                        readonlyCanonicalGroupUrl
                      };
                    }
                    """,
                    "arguments": [
                        {"value": expected_page_identity},
                        {"value": guard_token},
                        {"value": required_group_id},
                        {"value": bool(require_group_composer)},
                        {"value": required_marketplace_listing_id},
                        {"value": required_marketplace_listing_title},
                        {"value": required_marketplace_source_entity_id},
                        {"value": required_marketplace_boost_label},
                        {"value": list(allowed_crosspost_group_ids or [])},
                        {"value": list(allowed_crosspost_group_names or [])},
                        {"value": list(selected_crosspost_group_ids or [])},
                        {"value": list(selected_crosspost_group_names or [])},
                        {"value": crosspost_stage},
                        {"value": crosspost_source_token},
                        {"value": page_composer_stage},
                        {"value": page_composer_token},
                        {"value": required_facebook_page_url},
                        {"value": required_facebook_page_actor},
                        {"value": expected_name},
                        {"value": required_popup_role},
                        {"value": popup_semantics_source or None},
                        {"value": required_marketplace_for_sale_item_id},
                        {"value": bool(require_readonly_group_navigation)},
                        {"value": marketplace_price_stage},
                        {"value": marketplace_price_token},
                        {"value": required_marketplace_price_twd},
                    ],
                    "returnByValue": True,
                    "awaitPromise": True,
                    "userGesture": True,
                },
                timeout=timeout,
            )
            if not clicked.get("ok"):
                _cleanup_guard()
                if crosspost_request_gate is not None:
                    self._taint_and_retire_crosspost_request_gate(
                        crosspost_request_gate
                    )
                clicked["dispatch_ambiguous"] = True
                return clicked
            payload = clicked.get("result", {})
            exception = payload.get("exceptionDetails")
            if exception:
                dispatch_started = False
                dispatch_check_reliable = True
                if crosspost_request_gate is not None:
                    dispatch_check = guarded_call(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": object_id,
                            "functionDeclaration": """
                            function(token) {
                              const started = (
                                this.__hermesAtomicDispatchToken === token
                              );
                              if (started) {
                                delete this.__hermesAtomicDispatchToken;
                              }
                              return started;
                            }
                            """,
                            "arguments": [{"value": guard_token}],
                            "returnByValue": True,
                        },
                        timeout=timeout,
                    )
                    dispatch_started = (
                        dispatch_check.get("ok")
                        and not dispatch_check.get("result", {}).get(
                            "exceptionDetails"
                        )
                        and dispatch_check.get("result", {})
                        .get("result", {})
                        .get("value") is True
                    )
                    dispatch_check_reliable = (
                        dispatch_check.get("ok")
                        and not dispatch_check.get("result", {}).get(
                            "exceptionDetails"
                        )
                    )
                    if dispatch_started or not dispatch_check_reliable:
                        self._taint_and_retire_crosspost_request_gate(
                            crosspost_request_gate
                        )
                    else:
                        self._cancel_crosspost_request_gate(
                            crosspost_request_gate,
                            timeout=timeout,
                        )
                raw_error = (
                    exception.get("exception", {}).get("description")
                    or exception.get("text")
                    or "guarded atomic click failed"
                )
                return normalized_guard_failure(
                    raw_error,
                    dispatch_ambiguous=bool(
                        crosspost_request_gate is not None
                        and (
                            dispatch_started
                            or not dispatch_check_reliable
                        )
                    ),
                )
            if crosspost_request_gate is not None:
                request_result = self._wait_for_crosspost_request_gate(
                    crosspost_request_gate,
                    timeout=timeout,
                )
                if not request_result.get("ok"):
                    return request_result
                return {
                    "ok": True,
                    "result": payload.get("result", {}).get("value"),
                    "crosspost_request": request_result,
                }
            return {
                "ok": True,
                "result": payload.get("result", {}).get("value"),
            }
        function_declaration = """
        async function(
          expected, action, text, token, requireGroupComposer,
          pageComposerStage, pageComposerToken, requiredFacebookPageUrl,
          requiredFacebookPageActor, marketplacePriceStage,
          marketplacePriceToken, requiredMarketplaceListingId,
          requiredMarketplacePriceTwd
        ) {
          const guard = this.__hermesAtomicGuard;
          const fail = message => {
            guard?.observer?.disconnect();
            if (guard?.token === token) delete this.__hermesAtomicGuard;
            throw new Error(message);
          };
          const failPageGuard = (code, diagnostics) => fail(
            `HERMES_PAGE_GUARD|${code}|${JSON.stringify(
              diagnostics || {}
            )}`
          );
          const failPriceGuard = (code, diagnostics) => fail(
            `HERMES_MARKETPLACE_PRICE_GUARD|${code}|${JSON.stringify(
              diagnostics || {}
            )}`
          );
          const actual = `${location.href}|${performance.timeOrigin}`;
          if (actual !== expected) {
            fail("Protected page load changed before atomic action");
          }
          if (!this.isConnected) {
            fail("Captured snapshot node is detached");
          }
          if (requireGroupComposer) {
            const dialog = this.closest?.('[role="dialog"]');
            const dialogText = [
              dialog?.getAttribute("aria-label") || "",
              dialog?.innerText || ""
            ].join(" ").replace(/\\s+/g, " ").toLowerCase();
            const createPost = (
              dialogText.includes("create post")
              || dialogText.includes("建立貼文")
            );
            const forbiddenMode = (
              dialogText.includes("anonymous post")
              || dialogText.includes("匿名貼文")
              || dialogText.includes("share post")
              || dialogText.includes("分享貼文")
            );
            if (!dialog || !createPost || forbiddenMode) {
              fail("Target is not the authorized group post composer");
            }
          }
          if (pageComposerStage === "compose") {
            const dialog = this.closest?.('[role="dialog"]');
            const dialogText = [
              dialog?.getAttribute("aria-label") || "",
              dialog?.innerText || ""
            ].join(" ").replace(/\\s+/g, " ").toLowerCase();
            const normalizeActor = value => String(value || "")
              .replace(/\\s+/g, " ").trim();
            const strictlyVisible = node => {
              if (!node?.isConnected) return false;
              const view = node.ownerDocument.defaultView;
              if (!view) return false;
              const nodeRect = node.getBoundingClientRect();
              let visibleLeft = Math.max(0, nodeRect.left);
              let visibleRight = Math.min(
                view.innerWidth, nodeRect.right
              );
              let visibleTop = Math.max(0, nodeRect.top);
              let visibleBottom = Math.min(
                view.innerHeight, nodeRect.bottom
              );
              for (
                let current = node;
                current;
                current = current.parentElement
              ) {
                const style = view.getComputedStyle(current);
                  const clipPath = String(
                    style.clipPath || style.webkitClipPath || "none"
                  ).trim().toLowerCase();
                  const legacyClip = String(
                    style.clip || "auto"
                  ).trim().toLowerCase();
                  if (
                    current.hasAttribute("hidden")
                    || current.hasAttribute("inert")
                    || String(
                      current.getAttribute("aria-hidden") || ""
                    ).trim().toLowerCase() === "true"
                    || style.display === "none"
                    || style.visibility !== "visible"
                    || Number(style.opacity) === 0
                    || (clipPath && clipPath !== "none")
                    || (legacyClip && legacyClip !== "auto")
                  ) return false;
                  const currentRects = [...current.getClientRects()];
                  if (!currentRects.some(rect => (
                    rect.width > 0 && rect.height > 0
                  ))) return false;
                  if (current !== node) {
                    const clipsX = style.overflowX !== "visible";
                    const clipsY = style.overflowY !== "visible";
                    const containment = String(
                      style.contain || ""
                    ).split(/\\s+/);
                    const clipsPaint = containment.some(value => (
                      value === "paint"
                      || value === "strict"
                      || value === "content"
                    ));
                    if (clipsX || clipsY || clipsPaint) {
                      const borderRect = current.getBoundingClientRect();
                      const clipLeft = borderRect.left + current.clientLeft;
                      const clipTop = borderRect.top + current.clientTop;
                      const clipRight = clipLeft + current.clientWidth;
                      const clipBottom = clipTop + current.clientHeight;
                      if (clipsX || clipsPaint) {
                        visibleLeft = Math.max(visibleLeft, clipLeft);
                        visibleRight = Math.min(visibleRight, clipRight);
                      }
                      if (clipsY || clipsPaint) {
                        visibleTop = Math.max(visibleTop, clipTop);
                        visibleBottom = Math.min(visibleBottom, clipBottom);
                      }
                    }
                  }
                  if (
                    visibleRight <= visibleLeft
                    || visibleBottom <= visibleTop
                  ) return false;
              }
              return true;
            };
            const visiblePageComposers = () => [...(
              dialog?.ownerDocument?.querySelectorAll('[role="dialog"]')
              || []
            )].filter(candidateDialog => {
              const candidateText = [
                candidateDialog.getAttribute("aria-label") || "",
                candidateDialog.innerText || ""
              ].join(" ").replace(/\\s+/g, " ").toLowerCase();
              return (
                strictlyVisible(candidateDialog)
                && (
                  candidateText.includes("create post")
                  || candidateText.includes("建立貼文")
                )
                && !candidateText.includes("anonymous post")
                && !candidateText.includes("匿名貼文")
                && !candidateText.includes("share post")
                && !candidateText.includes("分享貼文")
                && [...candidateDialog.querySelectorAll(
                  '[role="textbox"], textarea, [contenteditable="true"]'
                )].some(editable => (
                  strictlyVisible(editable)
                  && editable.closest('[role="dialog"]')
                    === candidateDialog
                  && !editable.closest('[aria-disabled="true"]')
                  && !editable.closest('[aria-readonly="true"]')
                  && (
                    editable.isContentEditable
                    || (
                      editable.matches("textarea")
                      && !editable.matches(":disabled")
                      && !editable.readOnly
                    )
                  )
                ))
              );
            });
            const directActorTextVisible = (
              element, expectedActor, includeDescendants = false
            ) => {
              if (!strictlyVisible(element) || !expectedActor) return false;
              const candidateTextNodes = includeDescendants
                ? (() => {
                  const nodes = [];
                  const walker = element.ownerDocument.createTreeWalker(
                    element, NodeFilter.SHOW_TEXT
                  );
                  while (walker.nextNode()) nodes.push(walker.currentNode);
                  return nodes;
                })()
                : [...element.childNodes];
              const visibleCandidateTextNodes = candidateTextNodes.filter(
                child => (
                  child.nodeType === 3
                  && strictlyVisible(child.parentElement)
                )
              );
              const textNodes = visibleCandidateTextNodes.filter(child => (
                child.nodeType === 3
                && Boolean(normalizeActor(child.textContent))
              ));
              if (
                !textNodes.length
                || normalizeActor(visibleCandidateTextNodes.map(
                  child => child.textContent
                ).join("")) !== expectedActor
              ) return false;
              const view = element.ownerDocument.defaultView;
              const elementRect = element.getBoundingClientRect();
              const colorPainted = value => {
                const color = String(value || "").trim().toLowerCase();
                if (!color || color === "transparent") return false;
                const slashAlpha = color.match(
                  /\\/\\s*([0-9.]+)(%)?\\s*\\)$/
                );
                if (slashAlpha) {
                  const alpha = Number(slashAlpha[1]);
                  return Number.isFinite(alpha) && alpha > 0;
                }
                const legacyAlpha = color.match(
                  /^rgba\\([^,]+,[^,]+,[^,]+,\\s*([0-9.]+)\\s*\\)$/
                );
                return !legacyAlpha || Number(legacyAlpha[1]) > 0;
              };
              const textRectGroups = textNodes.map(textNode => {
                const range = element.ownerDocument.createRange();
                range.selectNodeContents(textNode);
                return [...range.getClientRects()]
                  .filter(rect => rect.width > 0 && rect.height > 0)
                  .map(rect => ({rect, textNode}));
              });
              if (textRectGroups.some(rects => !rects.length)) return false;
              const textRects = textRectGroups.flat();
              return textRects.every(({rect: textRect, textNode}) => {
                let left = Math.max(0, elementRect.left, textRect.left);
                let right = Math.min(
                  view.innerWidth, elementRect.right, textRect.right
                );
                let top = Math.max(0, elementRect.top, textRect.top);
                let bottom = Math.min(
                  view.innerHeight, elementRect.bottom, textRect.bottom
                );
                for (
                  let current = textNode.parentElement;
                  current;
                  current = current.parentElement
                ) {
                  const style = view.getComputedStyle(current);
                  if (
                    String(style.filter || "none")
                      .trim().toLowerCase() !== "none"
                  ) return false;
                  if (
                    current === textNode.parentElement
                    && (
                      !colorPainted(style.color)
                      || !colorPainted(
                        style.webkitTextFillColor || style.color
                      )
                    )
                  ) return false;
                  const clipsX = style.overflowX !== "visible";
                  const clipsY = style.overflowY !== "visible";
                  const containment = String(
                    style.contain || ""
                  ).split(/\\s+/);
                  const clipsPaint = containment.some(value => (
                    value === "paint"
                    || value === "strict"
                    || value === "content"
                  ));
                  if (clipsX || clipsY || clipsPaint) {
                    const borderRect = current.getBoundingClientRect();
                    const clipLeft = borderRect.left + current.clientLeft;
                    const clipTop = borderRect.top + current.clientTop;
                    const clipRight = clipLeft + current.clientWidth;
                    const clipBottom = clipTop + current.clientHeight;
                    if (clipsX || clipsPaint) {
                      left = Math.max(left, clipLeft);
                      right = Math.min(right, clipRight);
                    }
                    if (clipsY || clipsPaint) {
                      top = Math.max(top, clipTop);
                      bottom = Math.min(bottom, clipBottom);
                    }
                  }
                  if (right <= left || bottom <= top) return false;
                }
                const epsilon = 0.5;
                return (
                  left <= textRect.left + epsilon
                  && right >= textRect.right - epsilon
                  && top <= textRect.top + epsilon
                  && bottom >= textRect.bottom - epsilon
                );
              });
            };
            const hasEditableSurface = element => Boolean(
              element?.isContentEditable
              || element?.closest(
                'input,textarea,select,button,[role="textbox"]'
              )
              || [...element?.querySelectorAll('*') || []].some(
                child => (
                  child.isContentEditable
                  || child.matches(
                    'input,textarea,select,button,[role="textbox"]'
                  )
                )
              )
            );
            const canonicalPageUrlOf = value => {
              try {
                const parsed = new URL(value, location.href);
                const parts = parsed.pathname.split("/").filter(Boolean);
                if (
                  parsed.protocol !== "https:"
                  || !["facebook.com", "www.facebook.com"]
                    .includes(parsed.hostname.toLowerCase())
                  || !["", "443"].includes(parsed.port)
                  || parsed.username || parsed.password
                  || parsed.search || parsed.hash
                  || parts.length !== 1
                ) return "";
                return `https://www.facebook.com/${parts[0].toLowerCase()}`;
              } catch {
                return "";
              }
            };
            const currentSourceActorState = (() => {
              const managePageLabels = new Set([
                "manage page", "管理粉絲專頁", "管理页面"
              ]);
              const navigations = [
                ...dialog?.ownerDocument?.querySelectorAll(
                  'nav[aria-label],[role="navigation"][aria-label]'
                ) || []
              ].filter(navigation => (
                strictlyVisible(navigation)
                && [...navigation.querySelectorAll(
                  'h1,h2,[role="heading"]'
                )].filter(strictlyVisible).some(heading => (
                  managePageLabels.has(normalizeActor(
                    heading.innerText
                    || heading.getAttribute("aria-label")
                  ).toLowerCase())
                ))
              ));
              const actorNames = navigations.flatMap(navigation => {
                const headings = [...navigation.querySelectorAll(
                  'h1,h2,[role="heading"]'
                )].filter(strictlyVisible);
                const manageHeading = headings.find(heading => (
                  managePageLabels.has(normalizeActor(
                    heading.innerText
                    || heading.getAttribute("aria-label")
                  ).toLowerCase())
                ));
                const identityBoundary = manageHeading
                  ? [...navigation.querySelectorAll(
                    '[role="separator"],hr'
                  )].filter(strictlyVisible).find(node => Boolean(
                    manageHeading.compareDocumentPosition(node)
                    & Node.DOCUMENT_POSITION_FOLLOWING
                  ))
                  : null;
                return headings.filter(heading => (
                  heading !== manageHeading
                  && Boolean(
                    manageHeading?.compareDocumentPosition(heading)
                    & Node.DOCUMENT_POSITION_FOLLOWING
                  )
                  && (
                    !identityBoundary
                    || Boolean(
                      heading.compareDocumentPosition(identityBoundary)
                      & Node.DOCUMENT_POSITION_FOLLOWING
                    )
                  )
                )).map(heading => normalizeActor(
                  heading.innerText
                  || heading.getAttribute("aria-label")
                )).filter(Boolean);
              }).filter((actor, index, actors) => (
                actors.indexOf(actor) === index
              ));
              const switchGateVisible = [
                ...dialog?.ownerDocument?.querySelectorAll(
                  'button,[role="button"],a'
                ) || []
              ].filter(strictlyVisible).flatMap(element => [
                element.getAttribute("aria-label"),
                element.getAttribute("title"),
                element.innerText
              ]).map(normalizeActor).filter(Boolean).some(rawLabel => {
                const label = rawLabel.toLowerCase()
                  .replace(/[’‘]/g, "'");
                return (
                  label.includes("switch into")
                  && label.includes("page")
                ) || (
                  /切[換换]/.test(label)
                  && /(粉絲專頁|粉丝专页|專頁|专页|主頁|主页)/
                    .test(label)
                );
              });
              const actorMatch = (
                actorNames.length === 1
                && actorNames[0] === normalizeActor(
                  requiredFacebookPageActor
                )
              );
              return {
                pageUrlMatch: canonicalPageUrlOf(location.href)
                  === requiredFacebookPageUrl,
                actorNames,
                actorObservable: actorNames.length > 0,
                actorMatch: actorMatch
                  ? true
                  : actorNames.length > 0
                    ? false
                    : null,
                actorContradiction: (
                  actorNames.length > 0 && !actorMatch
                ),
                switchGateVisible
              };
            })();
            const composerActorContradictions = (() => {
              if (!dialog) return [];
              const textboxes = [...dialog.querySelectorAll(
                '[role="textbox"], textarea, [contenteditable="true"]'
              )].filter(strictlyVisible);
              if (!textboxes.length) return [];
              const textboxTop = Math.min(...textboxes.map(
                node => node.getBoundingClientRect().top
              ));
              const dialogTop = dialog.getBoundingClientRect().top;
              const contradictions = [];
              for (const element of [
                dialog,
                ...dialog.querySelectorAll('[aria-label],[title]')
              ]) {
                if (
                  !strictlyVisible(element)
                  || textboxes.some(textbox => textbox.contains(element))
                ) continue;
                const rect = element.getBoundingClientRect();
                if (
                  element !== dialog
                  && (
                    rect.top < dialogTop || rect.bottom > textboxTop
                  )
                ) continue;
                const labels = [
                  element.getAttribute("aria-label"),
                  element.getAttribute("title")
                ].map(normalizeActor).filter(Boolean);
                for (const label of labels) {
                  const english = label.match(
                    /^(?:post|posting|publish) as (.+)$/i
                  );
                  const localized = label.match(
                    /^(?:以|使用|用)\\s*(.+?)\\s*(?:身分|身份)(?:發佈|发布|發文|发文|貼文|贴文)$/
                  );
                  const labelled = label.match(
                    /^(?:發佈|发布|發文|发文|貼文|贴文)(?:身分|身份)[：:]\\s*(.+)$/
                  );
                  const declaredActor = normalizeActor(
                    english?.[1]
                    || localized?.[1]
                    || labelled?.[1]
                  );
                  if (
                    declaredActor
                    && declaredActor !== normalizeActor(
                      requiredFacebookPageActor
                    )
                  ) contradictions.push(declaredActor);
                }
              }
              return contradictions.filter(
                (actor, index) => contradictions.indexOf(actor) === index
              );
            })();
            const pageComposerFlow = (
              dialog?.ownerDocument?.__hermesPageComposerFlow
            );
            const composerActorProofRequired = Boolean(
              pageComposerFlow?.composerActorProofRequired
            );
            const composerActorProofMarkerMatch = Boolean(
              composerActorProofRequired
                ? dialog?.getAttribute(
                  "data-hermes-page-composer-actor-proof"
                ) === "visible"
                : !dialog?.hasAttribute(
                  "data-hermes-page-composer-actor-proof"
                )
            );
            const composerActorBinding = composerActorProofRequired
              ? (() => {
              if (
                !strictlyVisible(dialog)
                || canonicalPageUrlOf(requiredFacebookPageUrl)
                  !== requiredFacebookPageUrl
                || canonicalPageUrlOf(location.href)
                  !== requiredFacebookPageUrl
              ) return null;
              const textboxes = [...dialog.querySelectorAll(
                '[role="textbox"], textarea, [contenteditable="true"]'
              )].filter(strictlyVisible);
              if (
                !strictlyVisible(this)
                || !textboxes.includes(this)
              ) return null;
              const textboxTop = Math.min(...textboxes.map(
                node => node.getBoundingClientRect().top
              ));
              const dialogTop = dialog.getBoundingClientRect().top;
              const bindings = [];
              const actorMarkers = [...dialog.ownerDocument.querySelectorAll(
                '[data-hermes-page-composer-actor-token]'
              )];
              if (
                actorMarkers.length !== 1
                || actorMarkers[0].getAttribute(
                  'data-hermes-page-composer-actor-token'
                ) !== pageComposerToken
                || !dialog.contains(actorMarkers[0])
              ) return null;
              for (const actorElement of actorMarkers) {
                const rect = actorElement.getBoundingClientRect();
                const actorUrl = actorElement.matches('a[href]')
                  ? canonicalPageUrlOf(
                    actorElement.getAttribute("href")
                  )
                  : requiredFacebookPageUrl;
                const actorName = normalizeActor(
                  actorElement.innerText || actorElement.textContent
                );
                if (
                  !actorUrl || !actorName
                  || actorElement.getAttribute(
                    "data-hermes-page-composer-actor-token"
                  ) !== pageComposerToken
                  || !strictlyVisible(actorElement)
                  || textboxes.some(
                    textbox => textbox.contains(actorElement)
                  )
                  || hasEditableSurface(actorElement)
                  || rect.top < dialogTop
                  || rect.bottom > textboxTop
                  || !directActorTextVisible(
                    actorElement, requiredFacebookPageActor,
                    true
                  )
                ) continue;
                bindings.push({
                  url: actorUrl, name: requiredFacebookPageActor
                });
              }
              return bindings.length === 1 ? bindings[0] : null;
              })()
              : null;
            const tokenDialogs = [
                ...dialog?.ownerDocument?.querySelectorAll(
                  '[data-hermes-page-composer-token]'
                ) || []
              ];
            const currentVisiblePageComposers = visiblePageComposers();
            const pageGuardDiagnostics = {
              page_url_match: (
                canonicalPageUrlOf(location.href)
                === requiredFacebookPageUrl
              ),
              composer_token_present: Boolean(pageComposerToken),
              expected_actor_present: Boolean(requiredFacebookPageActor),
              target_inside_dialog: Boolean(dialog),
              token_dialog_count: tokenDialogs.length,
              composer_visible: Boolean(
                dialog && currentVisiblePageComposers.includes(dialog)
              ),
              visible_composer_count:
                currentVisiblePageComposers.length,
              source_capability_match: Boolean(
                pageComposerFlow?.token === pageComposerToken
                && pageComposerFlow?.pageUrl === requiredFacebookPageUrl
                && pageComposerFlow?.actor === requiredFacebookPageActor
                && pageComposerFlow?.pageIdentity === expected
              ),
              source_actor_names: currentSourceActorState.actorNames,
              source_actor_observable:
                currentSourceActorState.actorObservable,
              source_actor_match: currentSourceActorState.actorMatch,
              source_actor_contradiction:
                currentSourceActorState.actorContradiction,
              switch_into_page_visible:
                currentSourceActorState.switchGateVisible,
              contradictory_composer_actors:
                composerActorContradictions,
              composer_actor_proof_required: composerActorProofRequired,
              composer_actor_proof_marker_match:
                composerActorProofMarkerMatch,
              composer_actor_match: composerActorProofRequired
                ? Boolean(
                  composerActorBinding?.url === requiredFacebookPageUrl
                  && composerActorBinding?.name
                    === requiredFacebookPageActor
                )
                : null
            };
            if (
              tokenDialogs.length !== 1
              ||
              !pageComposerToken
              || !requiredFacebookPageActor
              || !dialog
              || dialog.getAttribute("role") !== "dialog"
              || dialog.getAttribute(
                "data-hermes-page-composer-token"
              ) !== pageComposerToken
              || currentVisiblePageComposers.length !== 1
              || currentVisiblePageComposers[0] !== dialog
              || !pageGuardDiagnostics.source_capability_match
              || !currentSourceActorState.pageUrlMatch
              || currentSourceActorState.switchGateVisible
              || currentSourceActorState.actorContradiction
              || composerActorContradictions.length > 0
              || !composerActorProofMarkerMatch
              || (
                composerActorProofRequired
                && (
                  composerActorBinding?.url !== requiredFacebookPageUrl
                  || composerActorBinding?.name
                    !== requiredFacebookPageActor
                )
              )
              || !(
                dialogText.includes("create post")
                || dialogText.includes("建立貼文")
              )
              || dialogText.includes("anonymous post")
              || dialogText.includes("匿名貼文")
              || dialogText.includes("share post")
              || dialogText.includes("分享貼文")
            ) {
              failPageGuard(
                "facebook_page_composer_binding_invalid",
                {
                  ...pageGuardDiagnostics,
                  failed_predicate: (
                    !pageGuardDiagnostics.page_url_match
                      ? "page_url_match"
                      : !pageGuardDiagnostics.composer_token_present
                        ? "composer_token_present"
                        : !pageGuardDiagnostics.expected_actor_present
                          ? "expected_actor_present"
                          : !pageGuardDiagnostics.target_inside_dialog
                            ? "target_inside_dialog"
                            : pageGuardDiagnostics.token_dialog_count !== 1
                              ? "token_dialog_count"
                              : !pageGuardDiagnostics.composer_visible
                                ? "composer_visible"
                                : pageGuardDiagnostics.visible_composer_count !== 1
                                  ? "visible_composer_count"
                              : !pageGuardDiagnostics.source_capability_match
                                ? "source_capability_match"
                                : pageGuardDiagnostics.switch_into_page_visible
                                  ? "switch_into_page_visible"
                                  : pageGuardDiagnostics.source_actor_contradiction
                                    ? "source_actor_contradiction"
                                : pageGuardDiagnostics.contradictory_composer_actors.length > 0
                                  ? "composer_actor_contradiction"
                                : !pageGuardDiagnostics.composer_actor_proof_marker_match
                                  ? "composer_actor_proof_marker_match"
                              : "composer_actor_match"
                  )
                }
              );
            }
          }
          if (
            !guard
            || guard.token !== token
            || guard.dirty
            || guard.observer.takeRecords().length
          ) {
            fail("Captured snapshot node changed after semantic validation");
          }
          if (action === "fill") {
            const isNativeSelect = this instanceof HTMLSelectElement;
            const proto = isNativeSelect
              ? HTMLSelectElement.prototype
              : this instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : this instanceof HTMLInputElement
                  ? HTMLInputElement.prototype
                  : null;
            const valueSetter = proto
              && Object.getOwnPropertyDescriptor(proto, "value")?.set;
            const selectedIndexSetter = isNativeSelect
              && Object.getOwnPropertyDescriptor(
                HTMLSelectElement.prototype, "selectedIndex"
              )?.set;
            this.focus();
            if (
              `${location.href}|${performance.timeOrigin}` !== expected
              || !this.isConnected
              || guard.dirty
              || guard.observer.takeRecords().length
            ) {
              fail("Fill target changed while receiving focus");
            }
            guard.observer.disconnect();
            delete this.__hermesAtomicGuard;
            if (isNativeSelect) {
              const normalize = value => String(value ?? "")
                .replace(/\\s+/g, " ").trim().toLowerCase();
              const requested = normalize(text);
              const matches = [...this.options].filter(option => (
                normalize(option.label || option.textContent) === requested
                || String(option.value) === String(text ?? "")
              ));
              if (!requested || matches.length !== 1) {
                throw new Error(
                  "Guarded select requires one exact enabled option label or value"
                );
              }
              const option = matches[0];
              if (
                this.multiple
                || this.matches(":disabled")
                || option.matches(":disabled")
                || !selectedIndexSetter
              ) {
                throw new Error("Guarded select target or option is disabled");
              }
              const previousIndex = this.selectedIndex;
              selectedIndexSetter.call(this, option.index);
              if (this.value !== option.value || this.selectedIndex !== option.index) {
                selectedIndexSetter.call(this, previousIndex);
                throw new Error("Guarded select did not retain the exact requested option");
              }
              this.dispatchEvent(new Event("input", {bubbles: true}));
              this.dispatchEvent(new Event("change", {bubbles: true}));
              await Promise.resolve();
              if (this.value !== option.value || this.selectedIndex !== option.index) {
                selectedIndexSetter.call(this, previousIndex);
                throw new Error(
                  "Guarded select did not retain the exact requested option after events"
                );
              }
            } else if (this.isContentEditable) {
              this.textContent = String(text ?? "");
              this.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                inputType: "insertText",
                data: String(text ?? "")
              }));
              this.dispatchEvent(new Event("change", {bubbles: true}));
            } else if (!valueSetter) {
              throw new Error("Guarded fill target has no native value setter");
            } else {
              valueSetter.call(this, String(text ?? ""));
              this.dispatchEvent(new InputEvent("input", {
                bubbles: true, inputType: "insertText", data: String(text ?? "")
              }));
              this.dispatchEvent(new Event("change", {bubbles: true}));
            }
            if (marketplacePriceStage === "fill") {
              const exactPrice = String(requiredMarketplacePriceTwd ?? "");
              const canonicalIntegerPrice = value => {
                const raw = String(value ?? "").trim();
                const match = raw.match(
                  /^(?:NT[$])?((?:0|[1-9][0-9]*)|(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+))$/
                );
                return match ? match[1].replace(/,/g, "") : null;
              };
              await Promise.resolve();
              const normalizeControlName = node => String(
                node?.getAttribute?.("aria-label")
                || node?.getAttribute?.("title")
                || node?.innerText
                || ""
              ).replace(/[ ]+/g, " ").trim().toLowerCase();
              const submitControls = [...this.ownerDocument.querySelectorAll(
                "button, [role=button]"
              )].filter(node => (
                ["save", "update", "儲存", "更新"].includes(
                  normalizeControlName(node)
                )
                && node.isConnected
                && node.getClientRects().length > 0
                && node.getAttribute("aria-hidden") !== "true"
                && !node.matches?.(":disabled")
                && !node.closest?.('[aria-disabled="true"]')
              ));
              const submitControl = submitControls[0];
              const fillDiagnostics = {
                token_present: Boolean(marketplacePriceToken),
                contract_price_valid: /^[0-9]+$/.test(exactPrice),
                requested_text_match: String(text ?? "") === exactPrice,
                target_is_input: this instanceof HTMLInputElement,
                input_connected: Boolean(this.isConnected),
                live_value: String(this.value ?? ""),
                normalized_live_value: canonicalIntegerPrice(this.value),
                submit_control_count: submitControls.length,
                page_identity_match: (
                  `${location.href}|${performance.timeOrigin}` === expected
                )
              };
              const failedFillPredicate = (
                !fillDiagnostics.token_present ? "token_present"
                  : !fillDiagnostics.contract_price_valid
                    ? "contract_price_valid"
                  : !fillDiagnostics.requested_text_match
                    ? "requested_text_match"
                  : !fillDiagnostics.target_is_input ? "target_is_input"
                  : !fillDiagnostics.input_connected ? "input_connected"
                  : fillDiagnostics.normalized_live_value !== exactPrice
                    ? "normalized_live_value"
                  : fillDiagnostics.submit_control_count !== 1
                    ? "submit_control_count"
                  : !fillDiagnostics.page_identity_match
                    ? "page_identity_match" : null
              );
              if (failedFillPredicate) {
                failPriceGuard(
                  "facebook_marketplace_price_fill_guard_rejected",
                  {
                    ...fillDiagnostics,
                    failed_predicate: failedFillPredicate
                  }
                );
              }
              const priceFlow = Object.freeze({
                token: marketplacePriceToken,
                listingId: String(requiredMarketplaceListingId || ""),
                priceTwd: Number(requiredMarketplacePriceTwd),
                pageIdentity: expected
              });
              Object.defineProperty(this, "__hermesMarketplacePriceFlow", {
                value: priceFlow, configurable: true
              });
              Object.defineProperty(
                this.ownerDocument, "__hermesMarketplacePriceFlow", {
                  value: priceFlow, configurable: true
                }
              );
              Object.defineProperty(
                submitControl, "__hermesMarketplacePriceSubmitFlow", {
                  value: priceFlow, configurable: true
                }
              );
            }
          } else {
            fail(`Unsupported guarded action ${action}`);
          }
          return {ok: true, action, pageIdentity: actual};
        }
        """
        called = guarded_call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": function_declaration,
                "arguments": [
                    {"value": expected_page_identity},
                    {"value": action},
                    {"value": text},
                    {"value": guard_token},
                    {"value": bool(require_group_composer)},
                    {"value": page_composer_stage},
                    {"value": page_composer_token},
                    {"value": required_facebook_page_url},
                    {"value": required_facebook_page_actor},
                    {"value": marketplace_price_stage},
                    {"value": marketplace_price_token},
                    {"value": required_marketplace_listing_id},
                    {"value": required_marketplace_price_twd},
                ],
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if not called.get("ok"):
            _cleanup_guard()
            return called
        payload = called.get("result", {})
        exception = payload.get("exceptionDetails")
        if exception:
            raw_error = (
                exception.get("exception", {}).get("description")
                or exception.get("text")
                or "guarded DOM action failed"
            )
            return normalized_guard_failure(raw_error)
        return {
            "ok": True,
            "result": payload.get("result", {}).get("value"),
        }

    # ── Supervisor loop internals ────────────────────────────────────────────

    def _thread_main(self) -> None:
        """Entry point for the supervisor's dedicated thread."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run())
        except BaseException as e:  # noqa: BLE001 — propagate via _start_error
            if not self._ready_event.is_set():
                self._start_error = e
                self._ready_event.set()
            else:
                logger.warning(
                    "CDP supervisor %s crashed: %s: %r",
                    self.task_id,
                    type(e).__name__,
                    e,
                )
        finally:
            # Flush any remaining tasks before closing the loop so we don't
            # emit "Task was destroyed but it is pending" warnings.
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            with self._state_lock:
                self._active = False

    async def _run(self) -> None:
        """Top-level supervisor coroutine.

        Holds a reconnecting loop so we survive the remote closing the
        WebSocket — Browserbase in particular tears down the CDP socket
        every time a short-lived client (e.g. agent-browser's per-command
        CDP client) disconnects.  We drop our state snapshot keys that
        depend on specific CDP session ids, re-attach, and keep going.
        """
        attempt = 0
        last_success_at = 0.0
        backoff = 0.5
        while not self._stop_requested:
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self.cdp_url, max_size=50 * 1024 * 1024),
                    timeout=10.0,
                )
            except Exception as e:
                attempt += 1
                if not self._ready_event.is_set():
                    # Never connected once — fatal for start().
                    self._start_error = e
                    self._ready_event.set()
                    return
                logger.warning(
                    "CDP supervisor %s: connect failed (attempt %s): %s",
                    self.task_id, attempt, e,
                )
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 10.0)
                continue

            reader_task = asyncio.create_task(self._read_loop(), name="cdp-reader")
            try:
                # Reset per-connection session state so stale ids don't hang
                # around after a reconnect.
                self._page_session_id = None
                self._page_target_id = None
                self._child_sessions.clear()
                # We deliberately keep `_pending_dialogs` and `_frames` —
                # they're reconciled as the supervisor resubscribes and
                # receives fresh events.  Worst case: an agent sees a stale
                # dialog entry that the new session's handleJavaScriptDialog
                # call rejects with "no dialog is showing" (logged, not
                # surfaced).
                await self._attach_initial_page()
                with self._state_lock:
                    self._active = True
                last_success_at = time.time()
                backoff = 0.5  # reset after a successful attach
                if not self._ready_event.is_set():
                    self._ready_event.set()
                # Run until the reader returns.
                await reader_task
            except BaseException as e:
                if not self._ready_event.is_set():
                    # Never got to ready — propagate to start().
                    self._start_error = e
                    self._ready_event.set()
                    raise
                logger.warning(
                    "CDP supervisor %s: session dropped after %.1fs: %s: %r",
                    self.task_id,
                    time.time() - last_success_at,
                    type(e).__name__,
                    e,
                )
            finally:
                with self._state_lock:
                    self._active = False
                if not reader_task.done():
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for handle in list(self._dialog_watchdogs.values()):
                    handle.cancel()
                self._dialog_watchdogs.clear()
                ws = self._ws
                self._ws = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            if self._stop_requested:
                return

            # Reconnect: brief backoff, then reattach.
            logger.debug(
                "CDP supervisor %s: reconnecting in %.1fs...", self.task_id, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def _attach_initial_page(self) -> None:
        """Find a page target, attach flattened session, enable domains, install dialog bridge."""
        resp = await self._cdp("Target.getTargets")
        targets = resp.get("result", {}).get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        if self.expected_page_url:
            matches = [
                target
                for target in pages
                if str(target.get("url") or "") == self.expected_page_url
            ]
            if not matches:
                raise RuntimeError(
                    "CDP exact page binding requires a target for "
                    f"{self.expected_page_url!r}; found 0"
                )
            if len(matches) > 1:
                logger.info(
                    "CDP supervisor %s found %d targets for %s; initial "
                    "attachment is provisional until page-load identity capture",
                    self.task_id,
                    len(matches),
                    self.expected_page_url,
                )
            page_target = matches[0]
        else:
            page_target = next(
                (
                    target for target in pages
                    if str(target.get("url") or "").startswith(
                        ("http://", "https://")
                    )
                ),
                pages[0] if pages else None,
            )
        if page_target is None:
            created = await self._cdp("Target.createTarget", {"url": "about:blank"})
            target_id = created["result"]["targetId"]
        else:
            target_id = page_target["targetId"]

        attach = await self._cdp(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attach["result"]["sessionId"]
        self._page_target_id = target_id
        self._page_session_id = session_id
        await self._configure_page_session(session_id)

    async def _configure_page_session(self, session_id: str) -> None:
        """Enable supervisor domains and dialog handling on one page session."""
        await self._cdp("Page.enable", session_id=session_id)
        await self._cdp("Runtime.enable", session_id=session_id)
        await self._cdp(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            session_id=session_id,
        )
        # Install the dialog bridge — overrides native alert/confirm/prompt with
        # a synchronous XHR we intercept via Fetch domain. This is how we make
        # dialog response work on Browserbase (whose CDP proxy auto-dismisses
        # real native dialogs before we can call handleJavaScriptDialog).
        await self._install_dialog_bridge(session_id)

    async def _install_dialog_bridge(self, session_id: str) -> None:
        """Install the dialog-bridge init script + Fetch interceptor on a session.

        Two CDP calls:
          1. ``Page.addScriptToEvaluateOnNewDocument`` — the JS override runs
             in every frame before any page script. Replaces alert/confirm/
             prompt with a sync XHR to our bridge URL.
          2. ``Fetch.enable`` scoped to the bridge URL — we catch those XHRs,
             surface them as pending dialogs, then fulfill once the agent
             responds.

        Idempotent at the CDP level: Chromium de-duplicates identical
        add-script calls by source, and Fetch.enable replaces prior patterns.
        """
        try:
            await self._cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _DIALOG_BRIDGE_SCRIPT, "runImmediately": True},
                session_id=session_id,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(
                "dialog bridge: addScriptToEvaluateOnNewDocument failed on sid=%s: %s",
                (session_id or "")[:16], e,
            )
        try:
            patterns = [
                {
                    "urlPattern": DIALOG_BRIDGE_URL_PATTERN,
                    "requestStage": "Request",
                }
            ]
            with self._state_lock:
                graphql_url_pattern = self._crosspost_graphql_url_patterns.get(
                    session_id
                )
                if graphql_url_pattern:
                    patterns.append({
                        "urlPattern": graphql_url_pattern,
                        "requestStage": "Request",
                    })
            await self._cdp(
                "Fetch.enable",
                {
                    "patterns": patterns,
                    "handleAuthRequests": False,
                },
                session_id=session_id,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(
                "dialog bridge: Fetch.enable failed on sid=%s: %s",
                (session_id or "")[:16], e,
            )
        # Also try to inject into the already-loaded document so existing
        # pages pick up the override on reconnect. Best-effort.
        try:
            await self._cdp(
                "Runtime.evaluate",
                {"expression": _DIALOG_BRIDGE_SCRIPT, "returnByValue": True},
                session_id=session_id,
                timeout=3.0,
            )
        except Exception:
            pass

    async def _cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Send a CDP command and await its response."""
        if self._ws is None:
            raise RuntimeError("supervisor WebSocket is not connected")
        call_id = self._next_call_id
        self._next_call_id += 1
        payload: Dict[str, Any] = {"id": call_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_calls[call_id] = fut
        await self._ws.send(json.dumps(payload))
        try:
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"CDP method {method} timed out after {timeout:.1f}s "
                    f"(task={self.task_id}, session={session_id or 'browser'})"
                ) from exc
        finally:
            self._pending_calls.pop(call_id, None)

    async def _read_loop(self) -> None:
        """Continuously dispatch incoming CDP frames."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._stop_requested:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.debug("CDP supervisor: non-JSON frame dropped")
                    continue
                if "id" in msg:
                    fut = self._pending_calls.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(f"CDP error on id={msg['id']}: {msg['error']}")
                            )
                        else:
                            fut.set_result(msg)
                elif "method" in msg:
                    await self._on_event(msg["method"], msg.get("params", {}), msg.get("sessionId"))
        except Exception as e:
            logger.debug("CDP read loop exited: %s", e)

    # ── Event dispatch ──────────────────────────────────────────────────────

    async def _on_event(
        self, method: str, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        if method == "Page.javascriptDialogOpening":
            await self._on_dialog_opening(params, session_id)
        elif method == "Page.javascriptDialogClosed":
            await self._on_dialog_closed(params, session_id)
        elif method == "Fetch.requestPaused":
            await self._on_fetch_paused(params, session_id)
        elif method == "Page.frameAttached":
            self._on_frame_attached(params, session_id)
        elif method == "Page.frameNavigated":
            self._on_frame_navigated(params, session_id)
        elif method == "Page.frameDetached":
            self._on_frame_detached(params, session_id)
        elif method == "Target.attachedToTarget":
            await self._on_target_attached(params)
        elif method == "Target.detachedFromTarget":
            self._on_target_detached(params)
        elif method == "Runtime.consoleAPICalled":
            self._on_console(params, level_from="api")
        elif method == "Runtime.exceptionThrown":
            self._on_console(params, level_from="exception")

    async def _on_dialog_opening(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        self._dialog_seq += 1
        dialog = PendingDialog(
            id=f"d-{self._dialog_seq}",
            type=str(params.get("type") or ""),
            message=str(params.get("message") or ""),
            default_prompt=str(params.get("defaultPrompt") or ""),
            opened_at=time.time(),
            cdp_session_id=session_id or self._page_session_id or "",
            frame_id=params.get("frameId"),
        )

        if self.dialog_policy == DIALOG_POLICY_AUTO_DISMISS:
            # Archive immediately with the policy tag so the ``closed`` event
            # arriving right after our handleJavaScriptDialog call doesn't
            # re-archive it as "remote".
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._auto_handle_dialog(dialog, accept=False, prompt_text="")
            )
        elif self.dialog_policy == DIALOG_POLICY_AUTO_ACCEPT:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._auto_handle_dialog(
                    dialog, accept=True, prompt_text=dialog.default_prompt
                )
            )
        else:
            # must_respond → add to pending and arm watchdog.
            with self._state_lock:
                self._pending_dialogs[dialog.id] = dialog
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                self.dialog_timeout_s,
                lambda: asyncio.create_task(self._dialog_timeout_expired(dialog.id)),
            )
            self._dialog_watchdogs[dialog.id] = handle

    async def _auto_handle_dialog(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """Send handleJavaScriptDialog for auto_dismiss/auto_accept.

        Dialog has already been archived by the caller (``_on_dialog_opening``);
        this just fires the CDP call so the page unblocks.
        """
        params: Dict[str, Any] = {"accept": accept}
        if dialog.type == "prompt":
            params["promptText"] = prompt_text
        try:
            await self._cdp(
                "Page.handleJavaScriptDialog",
                params,
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("auto-handle CDP call failed for %s: %s", dialog.id, e)

    async def _dialog_timeout_expired(self, dialog_id: str) -> None:
        with self._state_lock:
            dialog = self._pending_dialogs.get(dialog_id)
        if dialog is None:
            return
        logger.warning(
            "CDP supervisor %s: dialog %s (%s) auto-dismissed after %ss timeout",
            self.task_id,
            dialog_id,
            dialog.type,
            self.dialog_timeout_s,
        )
        try:
            # Archive with watchdog tag BEFORE fulfilling / dismissing.
            with self._state_lock:
                if dialog_id in self._pending_dialogs:
                    self._pending_dialogs.pop(dialog_id, None)
                    self._archive_dialog_locked(dialog, "watchdog")
            # Unblock the page — via bridge Fetch fulfill for bridge dialogs,
            # else native Page.handleJavaScriptDialog for real dialogs.
            if dialog.bridge_request_id:
                await self._fulfill_bridge_request(dialog, accept=False, prompt_text="")
            else:
                await self._cdp(
                    "Page.handleJavaScriptDialog",
                    {"accept": False},
                    session_id=dialog.cdp_session_id or None,
                    timeout=5.0,
                )
        except Exception as e:
            logger.debug("auto-dismiss failed for %s: %s", dialog_id, e)

    def _archive_dialog_locked(self, dialog: PendingDialog, closed_by: str) -> None:
        """Move a pending dialog to the recent_dialogs ring buffer. Must hold state_lock."""
        record = DialogRecord(
            id=dialog.id,
            type=dialog.type,
            message=dialog.message,
            opened_at=dialog.opened_at,
            closed_at=time.time(),
            closed_by=closed_by,
            frame_id=dialog.frame_id,
        )
        self._recent_dialogs.append(record)
        if len(self._recent_dialogs) > RECENT_DIALOGS_MAX * 2:
            self._recent_dialogs = self._recent_dialogs[-RECENT_DIALOGS_MAX:]

    async def _handle_dialog_cdp(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """Send the Page.handleJavaScriptDialog CDP command (agent path only).

        Routes to the bridge-fulfill path when the dialog was captured via
        the injected XHR override (see ``_on_fetch_paused``).
        """
        if dialog.bridge_request_id:
            try:
                await self._fulfill_bridge_request(
                    dialog, accept=accept, prompt_text=prompt_text
                )
            finally:
                with self._state_lock:
                    if dialog.id in self._pending_dialogs:
                        self._pending_dialogs.pop(dialog.id, None)
                        self._archive_dialog_locked(dialog, "agent")
                handle = self._dialog_watchdogs.pop(dialog.id, None)
                if handle is not None:
                    handle.cancel()
            return

        params: Dict[str, Any] = {"accept": accept}
        if dialog.type == "prompt":
            params["promptText"] = prompt_text
        try:
            await self._cdp(
                "Page.handleJavaScriptDialog",
                params,
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        finally:
            # Clear regardless — the CDP error path usually means the dialog
            # already closed (browser auto-dismissed after navigation, etc.).
            with self._state_lock:
                if dialog.id in self._pending_dialogs:
                    self._pending_dialogs.pop(dialog.id, None)
                    self._archive_dialog_locked(dialog, "agent")
            handle = self._dialog_watchdogs.pop(dialog.id, None)
            if handle is not None:
                handle.cancel()

    async def _on_dialog_closed(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        # ``Page.javascriptDialogClosed`` spec has only ``result`` (bool) and
        # ``userInput`` (string), not the original ``message``.  Match by
        # session id and clear the oldest dialog on that session — if Chrome
        # closed one on us (e.g. our disconnect auto-dismissed it, or the
        # browser navigated, or Browserbase's CDP proxy auto-dismissed), there
        # shouldn't be more than one in flight per session anyway because the
        # JS thread is blocked while a dialog is up.
        with self._state_lock:
            candidate_ids = [
                d.id
                for d in self._pending_dialogs.values()
                if d.cdp_session_id == session_id
                # Bridge-captured dialogs aren't cleared by native close events;
                # they're resolved via Fetch.fulfillRequest instead. Only the
                # real-native-dialog path uses Page.javascriptDialogClosed.
                and d.bridge_request_id is None
            ]
            if candidate_ids:
                did = candidate_ids[0]
                dialog = self._pending_dialogs.pop(did, None)
                if dialog is not None:
                    self._archive_dialog_locked(dialog, "remote")
                handle = self._dialog_watchdogs.pop(did, None)
                if handle is not None:
                    handle.cancel()

    async def _on_fetch_paused(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        """Bridge XHR captured mid-flight — materialize as a pending dialog.

        The injected script (``_DIALOG_BRIDGE_SCRIPT``) fires a synchronous
        XHR to ``DIALOG_BRIDGE_HOST`` whenever page code calls alert/confirm/
        prompt. We catch it via Fetch.enable pattern; the page's JS thread
        is blocked on the XHR's response until we call Fetch.fulfillRequest
        (which happens from ``respond_to_dialog``) or until the watchdog
        fires (at which point we fulfill with a cancel response).
        """
        url = str(params.get("request", {}).get("url") or "")
        request_id = params.get("requestId")
        if not request_id:
            return
        with self._state_lock:
            gate = self._crosspost_request_gate
            graphql_url_pattern = self._crosspost_graphql_url_patterns.get(
                session_id or ""
            )
        if (
            graphql_url_pattern
            and url.startswith(graphql_url_pattern.rstrip("*"))
        ):
            asyncio.create_task(
                self._handle_crosspost_fetch_paused(
                    params,
                    session_id,
                    gate if gate and gate.session_id == session_id else None,
                    graphql_url_pattern,
                )
            )
            return
        # Only care about our bridge URLs. Fetch can still deliver other
        # intercepted requests if patterns were ever broadened.
        if DIALOG_BRIDGE_HOST not in url:
            # Not ours — forward unchanged so the page sees its own request.
            async def continue_unrelated() -> None:
                try:
                    await self._cdp(
                        "Fetch.continueRequest", {"requestId": request_id},
                        session_id=session_id, timeout=3.0,
                    )
                except Exception:
                    pass

            asyncio.create_task(continue_unrelated())
            return

        # Parse query string for dialog metadata. Use urllib to be robust.
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)

        def _q(name: str) -> str:
            v = q.get(name, [""])
            return v[0] if v else ""

        kind = _q("kind") or "alert"
        message = _q("message")
        default_prompt = _q("default_prompt")

        self._dialog_seq += 1
        dialog = PendingDialog(
            id=f"d-{self._dialog_seq}",
            type=kind,
            message=message,
            default_prompt=default_prompt,
            opened_at=time.time(),
            cdp_session_id=session_id or self._page_session_id or "",
            frame_id=params.get("frameId"),
            bridge_request_id=str(request_id),
        )

        # Apply policy exactly as for native dialogs.
        if self.dialog_policy == DIALOG_POLICY_AUTO_DISMISS:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._fulfill_bridge_request(dialog, accept=False, prompt_text="")
            )
        elif self.dialog_policy == DIALOG_POLICY_AUTO_ACCEPT:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._fulfill_bridge_request(
                    dialog, accept=True, prompt_text=default_prompt
                )
            )
        else:
            # must_respond — add to pending + arm watchdog.
            with self._state_lock:
                self._pending_dialogs[dialog.id] = dialog
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                self.dialog_timeout_s,
                lambda: asyncio.create_task(self._dialog_timeout_expired(dialog.id)),
            )
            self._dialog_watchdogs[dialog.id] = handle

    async def _fulfill_bridge_request(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """Resolve a bridge XHR via Fetch.fulfillRequest so the page unblocks."""
        if not dialog.bridge_request_id:
            return
        payload = {
            "accept": bool(accept),
            "prompt_text": prompt_text if dialog.type == "prompt" else "",
            "dialog_id": dialog.id,
        }
        body = json.dumps(payload).encode()
        try:
            import base64 as _b64
            await self._cdp(
                "Fetch.fulfillRequest",
                {
                    "requestId": dialog.bridge_request_id,
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Access-Control-Allow-Origin", "value": "*"},
                    ],
                    "body": _b64.b64encode(body).decode(),
                },
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("bridge fulfill failed for %s: %s", dialog.id, e)

    # ── Frame / target tracking ─────────────────────────────────────────────

    def _on_frame_attached(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        frame_id = params.get("frameId")
        if not frame_id:
            return
        with self._state_lock:
            self._frames[frame_id] = FrameInfo(
                frame_id=frame_id,
                url="",
                origin="",
                parent_frame_id=params.get("parentFrameId"),
                is_oopif=False,
                cdp_session_id=session_id,
            )

    def _on_frame_navigated(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        frame = params.get("frame") or {}
        frame_id = frame.get("id")
        if not frame_id:
            return
        with self._state_lock:
            existing = self._frames.get(frame_id)
            info = FrameInfo(
                frame_id=frame_id,
                url=str(frame.get("url") or ""),
                origin=str(frame.get("securityOrigin") or frame.get("origin") or ""),
                parent_frame_id=frame.get("parentId") or (existing.parent_frame_id if existing else None),
                is_oopif=bool(existing.is_oopif if existing else False),
                cdp_session_id=existing.cdp_session_id if existing else session_id,
                name=str(frame.get("name") or (existing.name if existing else "")),
            )
            self._frames[frame_id] = info

    def _on_frame_detached(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        """Remove a frame from our state only when it's truly gone.

        CDP emits ``Page.frameDetached`` with a ``reason`` of either
        ``"remove"`` (the frame is actually gone from the DOM) or ``"swap"``
        (the frame is migrating to a new process — typical when a
        same-process iframe becomes an OOPIF, or when history navigates).
        Dropping on ``swap`` would hide OOPIFs from the agent the moment
        Chromium promotes them to their own process, so treat swap as a
        no-op.

        Even with ``reason=remove``, the parent page's perspective is
        "the child frame left MY process tree" — which is what happens
        when a same-origin iframe gets promoted to an OOPIF. If we
        already have a live child CDP session attached for that frame_id,
        the frame is still very much alive; only drop it when we have
        no session record.
        """
        frame_id = params.get("frameId")
        if not frame_id:
            return
        reason = str(params.get("reason") or "remove").lower()
        if reason == "swap":
            return
        with self._state_lock:
            existing = self._frames.get(frame_id)
            # Keep OOPIF records even when the parent says the frame was
            # "removed" — the iframe is still visible, just in a different
            # process. If the frame truly goes away later, Target.detached
            # + the next Page.frameDetached without a live session will
            # clear it.
            if existing and existing.is_oopif and existing.cdp_session_id:
                return
            self._frames.pop(frame_id, None)

    async def _on_target_attached(self, params: Dict[str, Any]) -> None:
        info = params.get("targetInfo") or {}
        sid = params.get("sessionId")
        target_type = info.get("type")
        if not sid or target_type not in {"iframe", "worker"}:
            return
        self._child_sessions[sid] = {"info": info, "type": target_type}

        # Record the frame with its OOPIF session id for interaction routing.
        if target_type == "iframe":
            target_id = info.get("targetId")
            with self._state_lock:
                existing = self._frames.get(target_id)
                self._frames[target_id] = FrameInfo(
                    frame_id=target_id,
                    url=str(info.get("url") or ""),
                    origin="",  # filled by frameNavigated on the child session
                    parent_frame_id=(existing.parent_frame_id if existing else None),
                    is_oopif=True,
                    cdp_session_id=sid,
                    name=str(info.get("title") or (existing.name if existing else "")),
                )

        # Enable domains on the child off-loop so the reader keeps pumping.
        # Awaiting the CDP replies here would deadlock because only the
        # reader can resolve those replies' Futures.
        asyncio.create_task(self._enable_child_domains(sid))

    async def _enable_child_domains(self, sid: str) -> None:
        """Enable Page+Runtime (+nested setAutoAttach) on a child CDP session.

        Also installs the dialog bridge so iframe-scoped alert/confirm/prompt
        calls round-trip through Fetch too.
        """
        try:
            await self._cdp("Page.enable", session_id=sid, timeout=3.0)
            await self._cdp("Runtime.enable", session_id=sid, timeout=3.0)
            await self._cdp(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                session_id=sid,
                timeout=3.0,
            )
        except Exception as e:
            logger.debug("child session %s setup failed: %s", sid[:16], e)
        # Install the dialog bridge on the child so iframe dialogs are captured.
        await self._install_dialog_bridge(sid)

    def _on_target_detached(self, params: Dict[str, Any]) -> None:
        """Handle a child CDP session detaching.

        We deliberately DO NOT drop frames from ``_frames`` here — Browserbase
        fires transient detach events during page transitions even while the
        iframe is still visible to the user, and dropping the record hides
        OOPIFs from the agent between the detach and the next
        ``Target.attachedToTarget``. Instead, we just clear the session
        binding so stale ``cdp_session_id`` values aren't used for routing.
        If the iframe truly goes away, ``Page.frameDetached`` will clean up.
        """
        sid = params.get("sessionId")
        if not sid:
            return
        self._child_sessions.pop(sid, None)
        with self._state_lock:
            for fid, frame in list(self._frames.items()):
                if frame.cdp_session_id == sid:
                    # Replace with a copy that has cdp_session_id cleared so
                    # routing falls back to top-level page session if retried.
                    self._frames[fid] = FrameInfo(
                        frame_id=frame.frame_id,
                        url=frame.url,
                        origin=frame.origin,
                        parent_frame_id=frame.parent_frame_id,
                        is_oopif=frame.is_oopif,
                        cdp_session_id=None,
                        name=frame.name,
                    )

    # ── Console / exception ring buffer ─────────────────────────────────────

    def _on_console(self, params: Dict[str, Any], *, level_from: str) -> None:
        if level_from == "exception":
            details = params.get("exceptionDetails") or {}
            text = str(details.get("text") or "")
            url = details.get("url")
            event = ConsoleEvent(ts=time.time(), level="exception", text=text, url=url)
        else:
            raw_level = str(params.get("type") or "log")
            level = "error" if raw_level in {"error", "assert"} else (
                "warning" if raw_level == "warning" else "log"
            )
            args = params.get("args") or []
            parts: List[str] = []
            for a in args[:4]:
                if isinstance(a, dict):
                    parts.append(str(a.get("value") or a.get("description") or ""))
            event = ConsoleEvent(ts=time.time(), level=level, text=" ".join(parts))
        with self._state_lock:
            self._console_events.append(event)
            if len(self._console_events) > CONSOLE_HISTORY_MAX * 2:
                # Keep last CONSOLE_HISTORY_MAX; allow 2x slack to reduce churn.
                self._console_events = self._console_events[-CONSOLE_HISTORY_MAX:]

    # ── Frame tree building (bounded) ───────────────────────────────────────

    def _build_frame_tree_locked(self) -> Dict[str, Any]:
        """Build the capped frame_tree payload. Must be called under state lock."""
        frames = self._frames
        if not frames:
            return {"top": None, "children": [], "truncated": False}

        # Identify a top frame — one with no parent, preferring oopif=False.
        tops = [f for f in frames.values() if not f.parent_frame_id]
        top = next((f for f in tops if not f.is_oopif), tops[0] if tops else None)

        # BFS from top, capped by FRAME_TREE_MAX_ENTRIES and
        # FRAME_TREE_MAX_OOPIF_DEPTH for OOPIF branches.
        children: List[Dict[str, Any]] = []
        truncated = False
        if top is None:
            return {"top": None, "children": [], "truncated": False}

        queue: List[Tuple[FrameInfo, int]] = [
            (f, 1) for f in frames.values() if f.parent_frame_id == top.frame_id
        ]
        visited: set[str] = {top.frame_id}
        while queue and len(children) < FRAME_TREE_MAX_ENTRIES:
            frame, depth = queue.pop(0)
            if frame.frame_id in visited:
                continue
            visited.add(frame.frame_id)
            if frame.is_oopif and depth > FRAME_TREE_MAX_OOPIF_DEPTH:
                truncated = True
                continue
            children.append(frame.to_dict())
            for f in frames.values():
                if f.parent_frame_id == frame.frame_id and f.frame_id not in visited:
                    queue.append((f, depth + 1))
        if queue:
            truncated = True

        return {
            "top": top.to_dict(),
            "children": children,
            "truncated": truncated,
        }


# ── Registry ─────────────────────────────────────────────────────────────────


class _SupervisorRegistry:
    """Process-global (task_id → supervisor) map with idempotent start/stop.

    One instance, exposed as ``SUPERVISOR_REGISTRY``. Safe to call from any
    thread — mutations go through ``_lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_task: Dict[str, CDPSupervisor] = {}

    def get(self, task_id: str) -> Optional[CDPSupervisor]:
        """Return the supervisor for ``task_id`` if running, else ``None``."""
        with self._lock:
            return self._by_task.get(task_id)

    def get_or_start(
        self,
        task_id: str,
        cdp_url: str,
        *,
        expected_page_url: Optional[str] = None,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
        start_timeout: float = 15.0,
    ) -> CDPSupervisor:
        """Idempotently ensure a supervisor is running for ``(task_id, cdp_url)``.

        If a supervisor exists for this task but was bound to a different
        ``cdp_url``, the old one is stopped and a fresh one is started.
        """
        with self._lock:
            existing = self._by_task.get(task_id)
            if existing is not None:
                if (
                    existing.cdp_url == cdp_url
                    and getattr(existing, "expected_page_url", None)
                    == (str(expected_page_url or "").strip() or None)
                ):
                    thread_ok = existing._thread is not None and existing._thread.is_alive()
                    loop_ok = existing._loop is not None and existing._loop.is_running()
                    if thread_ok and loop_ok:
                        return existing
                    # Unhealthy — tear down and recreate.
                # URL changed or unhealthy — tear down, fall through to re-create.
                self._by_task.pop(task_id, None)
        if existing is not None:
            existing.stop()

        supervisor_kwargs: Dict[str, Any] = {
            "task_id": task_id,
            "cdp_url": cdp_url,
            "dialog_policy": dialog_policy,
            "dialog_timeout_s": dialog_timeout_s,
        }
        if expected_page_url:
            supervisor_kwargs["expected_page_url"] = expected_page_url
        supervisor = CDPSupervisor(
            **supervisor_kwargs,
        )
        supervisor.start(timeout=start_timeout)
        with self._lock:
            # Guard against a concurrent get_or_start from another thread.
            already = self._by_task.get(task_id)
            if (
                already is not None
                and already.cdp_url == cdp_url
                and getattr(already, "expected_page_url", None)
                == (str(expected_page_url or "").strip() or None)
            ):
                supervisor.stop()
                return already
            self._by_task[task_id] = supervisor
        return supervisor

    def stop(self, task_id: str) -> None:
        """Stop and discard the supervisor for ``task_id`` if it exists."""
        with self._lock:
            supervisor = self._by_task.pop(task_id, None)
        if supervisor is not None:
            supervisor.stop()

    def stop_all(self) -> None:
        """Stop every running supervisor. For shutdown / test teardown."""
        with self._lock:
            items = list(self._by_task.items())
            self._by_task.clear()
        for _, supervisor in items:
            supervisor.stop()


SUPERVISOR_REGISTRY = _SupervisorRegistry()


__all__ = [
    "CDPSupervisor",
    "ConsoleEvent",
    "DEFAULT_DIALOG_POLICY",
    "DEFAULT_DIALOG_TIMEOUT_S",
    "DIALOG_POLICY_AUTO_ACCEPT",
    "DIALOG_POLICY_AUTO_DISMISS",
    "DIALOG_POLICY_MUST_RESPOND",
    "DialogRecord",
    "FrameInfo",
    "PendingDialog",
    "SUPERVISOR_REGISTRY",
    "SupervisorSnapshot",
    "_SupervisorRegistry",
]
