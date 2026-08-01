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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Bind and capture one AX tree from the unique page at ``expected_url``.

        A browser-level CDP endpoint can expose several ``page`` targets
        (for example the user's Facebook tab plus Chrome's New Tab page).
        The agent-browser CLI selects the active web page independently, so
        selection, attachment, configuration, and AX capture run as one
        event-loop operation with one deadline. The returned session id is
        part of the guarded ref capability, so a later snapshot cannot
        redirect its action. Missing or duplicate targets fail closed.
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
            response = await self._cdp("Target.getTargets")
            targets = response.get("result", {}).get("targetInfos", [])
            matches = [
                target for target in targets
                if target.get("type") == "page"
                and str(target.get("url") or "") == expected_url
            ]
            if len(matches) != 1:
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

    def guarded_dom_action(
        self,
        *,
        backend_node_id: int,
        expected_page_identity: str,
        action: str,
        expected_role: str,
        expected_name: str,
        required_group_id: Optional[str] = None,
        text: Optional[str] = None,
        captured_session_id: Optional[str] = None,
        require_group_composer: bool = False,
        required_marketplace_listing_id: Optional[str] = None,
        allowed_crosspost_group_ids: Optional[List[str]] = None,
        selected_crosspost_group_ids: Optional[List[str]] = None,
        crosspost_stage: Optional[str] = None,
        crosspost_source_token: Optional[str] = None,
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
                function(token, requireGroupComposer, requireCrosspostDialog) {
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

        live_ax = guarded_call(
            "Accessibility.getPartialAXTree",
            {
                "backendNodeId": int(backend_node_id),
                "fetchRelatives": False,
            },
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
        if action == "click":
            clicked = guarded_call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """
                    function(
                      expected, token, requiredGroupId, requireGroupComposer,
                      requiredListingId, allowedCrosspostGroupIds,
                      selectedCrosspostGroupIds, crosspostStage,
                      crosspostSourceToken, expectedControlName
                    ) {
                      const guard = this.__hermesAtomicGuard;
                      const fail = message => {
                        guard?.observer?.disconnect();
                        if (guard?.token === token) {
                          delete this.__hermesAtomicGuard;
                        }
                        throw new Error(message);
                      };
                      const identity = () =>
                        `${location.href}|${performance.timeOrigin}`;
                      const targetAt = (x, y) => {
                        const hit = this.ownerDocument.elementFromPoint(x, y);
                        return hit === this || (hit && this.contains(hit));
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
                      let crosspostGroupId = null;
                      let crosspostPreselectedGroupIds = [];
                      if (requiredListingId) {
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
                        const expectedSelectedIds = new Set(
                          (selectedCrosspostGroupIds || []).map(String)
                        );
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
                              + 'a[href*="/marketplace/item/"], '
                              + 'a[href*="/ad_center/create/listingad/"]'
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
                            if (
                              /\\/ad_center\\/create\\/listingad\\/(?:[?#]|$)/
                                .test(href)
                            ) {
                              const targetMatch = href.match(
                                /[?&]target_id=([0-9]+)(?:[&#]|$)/
                              );
                              if (targetMatch) ids.add(targetMatch[1]);
                            }
                          }
                          return ids;
                        };
                        const flow = this.ownerDocument.__hermesCrosspostFlow;
                        let sourceControl = null;
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
                          const hasSellingActionAssociation = candidate => {
                            const controlName = normalizeLabel(
                              expectedControlName
                            );
                            const controlPrefix = "more options for ";
                            if (!controlName.startsWith(controlPrefix)) {
                              return false;
                            }
                            const listingName = controlName.slice(
                              controlPrefix.length
                            ).trim();
                            if (!listingName) return false;
                            for (const link of candidate.querySelectorAll(
                              'a[href*="/ad_center/create/listingad/"]'
                              + '[href*="target_id="]'
                            )) {
                              const href = link.getAttribute("href") || "";
                              if (
                                !/\\/ad_center\\/create\\/listingad\\/(?:[?#]|$)/
                                  .test(href)
                              ) continue;
                              const targetMatch = href.match(
                                /[?&]target_id=([0-9]+)(?:[&#]|$)/
                              );
                              if (
                                !targetMatch
                                || targetMatch[1]
                                  !== String(requiredListingId)
                              ) continue;
                              for (const value of [
                                link.getAttribute("aria-label"),
                                link.getAttribute("title"), link.innerText
                              ]) {
                                const actionName = normalizeLabel(value);
                                const expectedPrefix = (
                                  `boost listing for ${listingName}`
                                );
                                if (
                                  actionName === expectedPrefix
                                  || actionName.startsWith(`${expectedPrefix}.`)
                                ) return true;
                              }
                            }
                            return false;
                          };
                          let sourceBound = false;
                          let sawAuthorizedListing = false;
                          let listingNode = this;
                          while (listingNode) {
                            // Facebook's Selling rows are deeply nested generic
                            // divs.  A row is proven by a numeric item/data id or
                            // by the same-row Boost target id plus matching full
                            // action labels. Shared listing wrappers fail closed.
                            const sourceIds = listingIdsIn(listingNode);
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
                              if (listingNode.matches?.(pageScopeSelector)) {
                                fail(
                                  "Cross-post control lacks a bounded listing row"
                                );
                              }
                              if (
                                listingNode.matches?.(listingScopeSelector)
                                || hasSellingActionAssociation(listingNode)
                              ) {
                                sourceBound = true;
                                break;
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
                              if (
                                roots.length !== 1
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
                              if (!node.matches?.(containerSelector)) continue;
                              const ids = new Set();
                              for (const link of node.querySelectorAll(
                                'a[href*="/groups/"]'
                              )) {
                                const href = link.getAttribute("href") || "";
                                const match = href.match(
                                  /\\/groups\\/([0-9]+)(?:\\/|[?#]|$)/
                                );
                                if (match) ids.add(match[1]);
                              }
                              for (const candidate of [node, ...(
                                node.querySelectorAll(
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
                          const actualSelectedIds = new Set();
                          for (const selectedControl of selectedControls) {
                            const selectedId = discoverGroupId(selectedControl);
                            if (!selectedId || !allowedIds.has(selectedId)) {
                              fail(
                                "Selected cross-post option is not bound to an authorized group id"
                              );
                            }
                            actualSelectedIds.add(selectedId);
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
                              if (
                                !optionId || enumeratedGroupIds.has(optionId)
                              ) {
                                fail(
                                  "Cross-post destination rows are not uniquely enumerable"
                                );
                              }
                              enumeratedGroupIds.add(optionId);
                            }
                            const scrollContainers = new Set();
                            for (const optionRoot of optionRoots) {
                              let ancestor = optionRoot.parentElement;
                              while (ancestor && ancestor !== dialog) {
                                const style = getComputedStyle(ancestor);
                                if (
                                  /(auto|scroll)/.test(style.overflowY)
                                  && ancestor.scrollHeight
                                    > ancestor.clientHeight + 1
                                ) scrollContainers.add(ancestor);
                                ancestor = ancestor.parentElement;
                              }
                            }
                            const dialogStyle = getComputedStyle(dialog);
                            if (
                              /(auto|scroll)/.test(dialogStyle.overflowY)
                              && dialog.scrollHeight > dialog.clientHeight + 1
                            ) scrollContainers.add(dialog);
                            if (scrollContainers.size) {
                              const setSizes = optionRoots.map(option => Number(
                                option.getAttribute("aria-setsize")
                                || option.closest?.("[aria-setsize]")
                                  ?.getAttribute("aria-setsize")
                              ));
                              const positions = optionRoots.map(option => Number(
                                option.getAttribute("aria-posinset")
                              ));
                              const declaredSize = setSizes[0];
                              const completeAriaSet = (
                                Number.isInteger(declaredSize)
                                && declaredSize === optionRoots.length
                                && setSizes.every(size => size === declaredSize)
                                && new Set(positions).size === declaredSize
                                && positions.every(position =>
                                  Number.isInteger(position)
                                  && position >= 1
                                  && position <= declaredSize
                                )
                              );
                              if (!completeAriaSet) {
                                fail(
                                  "Cross-post destination list may be virtualized"
                                );
                              }
                            }
                          }
                          if (crosspostStage === "select_group") {
                            const discovered = discoverGroupId(this);
                            if (!discovered || !allowedIds.has(discovered)) {
                              fail(
                                "Cross-post option is not bound to an authorized group id"
                              );
                            }
                            crosspostGroupId = discovered;
                            crosspostPreselectedGroupIds = [
                              ...actualSelectedIds
                            ];
                          } else if (crosspostStage === "submit") {
                            if (
                              actualSelectedIds.size !== expectedSelectedIds.size
                              || [...expectedSelectedIds].some(
                                id => !actualSelectedIds.has(id)
                              )
                            ) {
                              fail(
                                "Selected cross-post groups changed before final Post"
                              );
                            }
                          }
                        }
                      }
                      const rect = this.getBoundingClientRect();
                      const x = rect.left + rect.width / 2;
                      const y = rect.top + rect.height / 2;
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
                        || !targetAt(x, y)
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
                      guard.observer.disconnect();
                      delete this.__hermesAtomicGuard;
                      if (requiredListingId) {
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
                      this.click();
                      return {
                        ok: true, action: "click", pageIdentity: expected,
                        crosspostGroupId, crosspostPreselectedGroupIds
                      };
                    }
                    """,
                    "arguments": [
                        {"value": expected_page_identity},
                        {"value": guard_token},
                        {"value": required_group_id},
                        {"value": bool(require_group_composer)},
                        {"value": required_marketplace_listing_id},
                        {"value": list(allowed_crosspost_group_ids or [])},
                        {"value": list(selected_crosspost_group_ids or [])},
                        {"value": crosspost_stage},
                        {"value": crosspost_source_token},
                        {"value": expected_name},
                    ],
                    "returnByValue": True,
                    "userGesture": True,
                },
                timeout=timeout,
            )
            if not clicked.get("ok"):
                _cleanup_guard()
                clicked["dispatch_ambiguous"] = True
                return clicked
            payload = clicked.get("result", {})
            exception = payload.get("exceptionDetails")
            if exception:
                return {
                    "ok": False,
                    "error": (
                        exception.get("exception", {}).get("description")
                        or exception.get("text")
                        or "guarded atomic click failed"
                    ),
                }
            return {
                "ok": True,
                "result": payload.get("result", {}).get("value"),
            }
        function_declaration = """
        function(expected, action, text, token, requireGroupComposer) {
          const guard = this.__hermesAtomicGuard;
          const fail = message => {
            guard?.observer?.disconnect();
            if (guard?.token === token) delete this.__hermesAtomicGuard;
            throw new Error(message);
          };
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
          if (
            !guard
            || guard.token !== token
            || guard.dirty
            || guard.observer.takeRecords().length
          ) {
            fail("Captured snapshot node changed after semantic validation");
          }
          if (action === "fill") {
            const proto = this instanceof HTMLTextAreaElement
              ? HTMLTextAreaElement.prototype
              : this instanceof HTMLInputElement
                ? HTMLInputElement.prototype
                : null;
            const setter = proto
              && Object.getOwnPropertyDescriptor(proto, "value")?.set;
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
            if (this.isContentEditable) {
              this.textContent = String(text ?? "");
              this.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                inputType: "insertText",
                data: String(text ?? "")
              }));
              this.dispatchEvent(new Event("change", {bubbles: true}));
            } else if (!setter) {
              throw new Error("Guarded fill target has no native value setter");
            } else {
              setter.call(this, String(text ?? ""));
              this.dispatchEvent(new InputEvent("input", {
                bubbles: true, inputType: "insertText", data: String(text ?? "")
              }));
              this.dispatchEvent(new Event("change", {bubbles: true}));
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
            return {
                "ok": False,
                "error": (
                    exception.get("exception", {}).get("description")
                    or exception.get("text")
                    or "guarded DOM action failed"
                ),
            }
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
                logger.warning("CDP supervisor %s crashed: %s", self.task_id, e)
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
                    "CDP supervisor %s: session dropped after %.1fs: %s",
                    self.task_id,
                    time.time() - last_success_at,
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
        page_target = next(
            (
                target for target in pages
                if str(target.get("url") or "").startswith(("http://", "https://"))
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
            await self._cdp(
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": DIALOG_BRIDGE_URL_PATTERN,
                            "requestStage": "Request",
                        }
                    ],
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
            return await asyncio.wait_for(fut, timeout=timeout)
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
        # Only care about our bridge URLs. Fetch can still deliver other
        # intercepted requests if patterns were ever broadened.
        if DIALOG_BRIDGE_HOST not in url:
            # Not ours — forward unchanged so the page sees its own request.
            try:
                await self._cdp(
                    "Fetch.continueRequest", {"requestId": request_id},
                    session_id=session_id, timeout=3.0,
                )
            except Exception:
                pass
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
                if existing.cdp_url == cdp_url:
                    thread_ok = existing._thread is not None and existing._thread.is_alive()
                    loop_ok = existing._loop is not None and existing._loop.is_running()
                    if thread_ok and loop_ok:
                        return existing
                    # Unhealthy — tear down and recreate.
                # URL changed or unhealthy — tear down, fall through to re-create.
                self._by_task.pop(task_id, None)
        if existing is not None:
            existing.stop()

        supervisor = CDPSupervisor(
            task_id=task_id,
            cdp_url=cdp_url,
            dialog_policy=dialog_policy,
            dialog_timeout_s=dialog_timeout_s,
        )
        supervisor.start(timeout=start_timeout)
        with self._lock:
            # Guard against a concurrent get_or_start from another thread.
            already = self._by_task.get(task_id)
            if already is not None and already.cdp_url == cdp_url:
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
