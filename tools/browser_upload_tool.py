#!/usr/bin/env python3
"""Upload local files into a browser ``<input type=file>`` via Playwright/CDP.

CDP alone cannot set a local file chooser payload reliably because browsers
intentionally protect file inputs.  Playwright exposes the supported
``setInputFiles`` path while still connecting to the same user-authenticated
Chrome instance via CDP.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

DEFAULT_SELECTOR = 'input[type="file"]'
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_cdp_endpoint() -> str:
    """Return a CDP endpoint suitable for Playwright ``connectOverCDP``."""
    raw_env = (os.environ.get("BROWSER_CDP_URL") or "").strip()
    if raw_env:
        return raw_env
    try:
        from tools.browser_tool import _get_cdp_override  # type: ignore[import-not-found]

        return (_get_cdp_override() or "").strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("browser_upload_files: failed to resolve CDP endpoint: %s", exc)
        return ""


def _node_command() -> Optional[str]:
    try:
        from hermes_constants import find_node_executable

        return find_node_executable("node")
    except Exception:
        return shutil.which("node")


def _normalise_files(files: Iterable[str]) -> List[str]:
    out: List[str] = []
    for item in files:
        path = Path(str(item)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Upload file does not exist or is not a file: {path}")
        out.append(str(path.resolve()))
    return out


def _playwright_node_script() -> str:
    return r"""
const payload = JSON.parse(process.argv[1]);

async function main() {
  let playwright;
  try {
    playwright = require("playwright-core");
  } catch (error) {
    throw new Error(
      "Node package 'playwright-core' is not available. Install/sync it locally. " +
      error.message
    );
  }

  const browser = await playwright.chromium.connectOverCDP(payload.cdpEndpoint);
  try {
    const contexts = browser.contexts();
    const pages = contexts.flatMap((context) => context.pages());
    const matchingPages = [];
    for (const candidate of pages) {
      const url = candidate.url();
      const title = await candidate.title().catch(() => "");
      if (
        (!payload.targetUrlContains || url.includes(payload.targetUrlContains)) &&
        (!payload.targetTitleContains || title.includes(payload.targetTitleContains))
      ) {
        matchingPages.push(candidate);
      }
    }
    if (matchingPages.length < 1) {
      throw new Error("No matching page found for requested target filters");
    }
    if (matchingPages.length > 1 && payload.pageIndex === null) {
      throw new Error(
        `Multiple pages (${matchingPages.length}) matched the target filters; ` +
        "provide page_index to select one deterministically"
      );
    }
    const pageIndex = payload.pageIndex === null ? 0 : payload.pageIndex;
    if (pageIndex < 0 || pageIndex >= matchingPages.length) {
      throw new Error(
        `page_index=${pageIndex} is outside the ${matchingPages.length} matching pages`
      );
    }
    const page = matchingPages[pageIndex];

    const count = await page.locator(payload.selector).count();
    if (count < 1) {
      throw new Error(`No file input matched selector: ${payload.selector}`);
    }
    if (count > 1 && payload.inputIndex === null) {
      throw new Error(
        `Multiple file inputs (${count}) matched selector ${payload.selector}; ` +
        "provide input_index or a narrower selector"
      );
    }
    const inputIndex = payload.inputIndex === null ? 0 : payload.inputIndex;
    if (inputIndex < 0 || inputIndex >= count) {
      throw new Error(`input_index=${inputIndex} is outside the ${count} matching inputs`);
    }
    const locator = page.locator(payload.selector).nth(inputIndex);
    const elementHandle = await locator.elementHandle();
    if (!elementHandle) {
      throw new Error("Selected file input disappeared before safety inspection");
    }
    const pageIdentity = await elementHandle.evaluate((element) => {
      const view = element.ownerDocument.defaultView;
      if (!view) {
        throw new Error("Selected file input is detached from a live document");
      }
      return `${view.location.href}|${view.performance.timeOrigin}`;
    });

    if (payload.inspectOnly) {
      return {
        success: true,
        inspectOnly: true,
        selector: payload.selector,
        matchingPages: matchingPages.length,
        pageIndex,
        matchingInputs: count,
        inputIndex,
        targetUrl: page.url(),
        pageIdentity,
        targetTitle: await page.title().catch(() => ""),
      };
    }
    if (payload.guardedTargetUrl && page.url() !== payload.guardedTargetUrl) {
      throw new Error(
        `Selected page changed after safety inspection: ` +
        `${JSON.stringify(payload.guardedTargetUrl)} -> ${JSON.stringify(page.url())}`
      );
    }
    const currentPageIdentity = await elementHandle.evaluate((element) => {
      const view = element.ownerDocument.defaultView;
      if (!view) {
        throw new Error("Selected file input is detached from a live document");
      }
      return `${view.location.href}|${view.performance.timeOrigin}`;
    });
    if (
      payload.guardedPageIdentity &&
      currentPageIdentity !== payload.guardedPageIdentity
    ) {
      throw new Error(
        `Selected page load changed after safety inspection: ` +
        `${JSON.stringify(payload.guardedPageIdentity)} -> ` +
        `${JSON.stringify(currentPageIdentity)}`
      );
    }
    if (payload.guardedComposerToken) {
      const composerBound = await elementHandle.evaluate(
        (element, binding) => {
          const composer = element.closest('[role="dialog"]');
          const normalizeActor = (value) => String(value || "")
            .replace(/\s+/g, " ").trim();
          const strictlyVisible = (node) => {
            if (!node?.isConnected) return false;
            const view = node.ownerDocument.defaultView;
            if (!view) return false;
            const nodeRect = node.getBoundingClientRect();
            let visibleLeft = Math.max(0, nodeRect.left);
            let visibleRight = Math.min(view.innerWidth, nodeRect.right);
            let visibleTop = Math.max(0, nodeRect.top);
            let visibleBottom = Math.min(view.innerHeight, nodeRect.bottom);
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
              if (!currentRects.some((rect) => (
                rect.width > 0 && rect.height > 0
              ))) return false;
              if (current !== node) {
                const clipsX = style.overflowX !== "visible";
                const clipsY = style.overflowY !== "visible";
                const containment = String(
                  style.contain || ""
                ).split(/\s+/);
                const clipsPaint = containment.some((value) => (
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
                /\/\s*([0-9.]+)(%)?\s*\)$/
              );
              if (slashAlpha) {
                const alpha = Number(slashAlpha[1]);
                return Number.isFinite(alpha) && alpha > 0;
              }
              const legacyAlpha = color.match(
                /^rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\s*\)$/
              );
              return !legacyAlpha || Number(legacyAlpha[1]) > 0;
            };
            const textRectGroups = textNodes.map((textNode) => {
              const range = element.ownerDocument.createRange();
              range.selectNodeContents(textNode);
              return [...range.getClientRects()]
                .filter((rect) => rect.width > 0 && rect.height > 0)
                .map((rect) => ({rect, textNode}));
            });
            if (textRectGroups.some((rects) => !rects.length)) return false;
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
                ).split(/\s+/);
                const clipsPaint = containment.some((value) => (
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
          const canonicalPageUrlOf = (value) => {
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
          const actorBinding = (() => {
            if (
              !strictlyVisible(composer)
              || canonicalPageUrlOf(binding.pageUrl) !== binding.pageUrl
              || canonicalPageUrlOf(location.href) !== binding.pageUrl
            ) return null;
            const textboxes = [...composer.querySelectorAll(
              '[role="textbox"], textarea, [contenteditable="true"]'
            )].filter(strictlyVisible);
            if (!textboxes.length) return null;
            const textboxTop = Math.min(...textboxes.map(
              node => node.getBoundingClientRect().top
            ));
            const dialogTop = composer.getBoundingClientRect().top;
            const bindings = [];
            const pageComposerFlow = (
              element.ownerDocument.__hermesPageComposerFlow
            );
            const actorProofRequired = Boolean(
              pageComposerFlow?.composerActorProofRequired
            );
            if (
              pageComposerFlow?.token !== binding.token
              || pageComposerFlow?.pageUrl !== binding.pageUrl
              || normalizeActor(pageComposerFlow?.actor) !== binding.actor
            ) return null;
            const actorMarkers = [...element.ownerDocument.querySelectorAll(
              '[data-hermes-page-composer-actor-token]'
            )];
            if (!actorProofRequired) {
              return (
                actorMarkers.length === 0
                && !composer.hasAttribute(
                  "data-hermes-page-composer-actor-proof"
                )
              ) ? {url: binding.pageUrl, name: binding.actor} : null;
            }
            if (
              actorMarkers.length !== 1
              || actorMarkers[0].getAttribute(
                'data-hermes-page-composer-actor-token'
              ) !== binding.token
              || !composer.contains(actorMarkers[0])
            ) return null;
            for (const actorElement of actorMarkers) {
              const rect = actorElement.getBoundingClientRect();
              const actorUrl = actorElement.matches('a[href]')
                ? canonicalPageUrlOf(
                  actorElement.getAttribute("href")
                )
                : binding.pageUrl;
              const actorName = normalizeActor(
                actorElement.innerText || actorElement.textContent
              );
              if (
                !actorUrl || !actorName
                || actorElement.getAttribute(
                  "data-hermes-page-composer-actor-token"
                ) !== binding.token
                || !strictlyVisible(actorElement)
                || textboxes.some(
                  textbox => textbox.contains(actorElement)
                )
                || hasEditableSurface(actorElement)
                || rect.top < dialogTop
                || rect.bottom > textboxTop
                || textboxTop - rect.bottom > 240
                || !directActorTextVisible(
                  actorElement, binding.actor,
                  true
                )
              ) continue;
              bindings.push({
                url: actorUrl, name: binding.actor
              });
            }
            return bindings.length === 1 ? bindings[0] : null;
          })();
          const fileInputBound = Boolean(
            element instanceof HTMLInputElement
            && element.type === "file"
            && !element.matches(":disabled")
            && element.isConnected
            && element.closest('[role="dialog"]') === composer
            // Facebook normally CSS-hides its file input. Validate every
            // wrapper up through the bound composer without requiring the
            // input's own box to be visible.
            && strictlyVisible(element.parentElement || composer)
          );
          const tokenDialogs = [
            ...element.ownerDocument.querySelectorAll(
              '[data-hermes-page-composer-token]'
            )
          ];
          return Boolean(
            fileInputBound
            && strictlyVisible(composer)
            && tokenDialogs.length === 1
            && tokenDialogs[0] === composer
            && tokenDialogs[0].getAttribute(
              'data-hermes-page-composer-token'
            ) === binding.token
            && tokenDialogs[0].getAttribute("role") === "dialog"
            && composer.getAttribute(
              "data-hermes-page-composer-token"
            ) === binding.token
            && binding.actor
            && actorBinding?.url === binding.pageUrl
            && actorBinding?.name === binding.actor
          );
        },
        {
          token: payload.guardedComposerToken,
          pageUrl: payload.guardedComposerPageUrl,
          actor: payload.guardedComposerPageActor,
        }
      );
      if (!composerBound) {
        throw new Error(
          "Selected file input is not inside the atomically opened Page composer"
        );
      }
    }
    if (payload.verifyTextContains) {
      const markerAlreadyPresent = await page.evaluate(
        (expected) => (document.body ? document.body.innerText : "").includes(expected),
        payload.verifyTextContains
      );
      if (markerAlreadyPresent) {
        throw new Error(
          `Upload verification marker was already present before upload: ` +
          `${JSON.stringify(payload.verifyTextContains)}`
        );
      }
    }
    await elementHandle.setInputFiles(payload.files);
    await page.waitForTimeout(payload.settleMs);
    if (payload.verifyTextContains) {
      await page.waitForFunction(
        (expected) => (document.body ? document.body.innerText : "").includes(expected),
        payload.verifyTextContains,
        { timeout: payload.verifyTimeoutMs }
      ).catch(() => {
        throw new Error(
          `Upload postcondition was not observed: page text did not contain ` +
          `${JSON.stringify(payload.verifyTextContains)}`
        );
      });
    }

    const state = await locator.evaluate((selectedInput, inputIndex) => {
      const norm = (value) => (value || "").replace(/\s+/g, " ").trim();
      const fileInputs = Array.from(document.querySelectorAll("input[type=file]")).map(
        (el, index) => ({
          index,
          selected: el === selectedInput,
          accept: el.getAttribute("accept") || "",
          multiple: !!el.multiple,
          disabled: !!el.disabled,
          files: el.files ? el.files.length : null,
        })
      );
      const buttons = Array.from(document.querySelectorAll('div[role="button"], button, a[role="button"]'))
        .map((el) => ({
          text: norm(el.innerText || el.getAttribute("aria-label")),
          aria: norm(el.getAttribute("aria-label")),
          ariaDisabled: el.getAttribute("aria-disabled") || "",
          disabled: !!el.disabled,
          visible: (() => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          })(),
        }))
        .filter((item) => /^(Next|Post|Publish|Submit|Done|下一步|刊登|發布|送出)$/.test(item.text || item.aria));
      return {
        href: location.href,
        title: document.title,
        selectedInput: selectedInput ? {
          selectorMatchIndex: inputIndex,
          accept: selectedInput.getAttribute("accept") || "",
          multiple: !!selectedInput.multiple,
          disabled: !!selectedInput.disabled,
          files: selectedInput.files ? selectedInput.files.length : null,
          fileNames: selectedInput.files
            ? Array.from(selectedInput.files).map((file) => file.name)
            : [],
        } : null,
        fileInputs,
        actionButtons: buttons,
        textTail: norm(document.body ? document.body.innerText : "").slice(-1200),
      };
    }, inputIndex);

    return {
      success: true,
      uploadedFiles: payload.files.length,
      selector: payload.selector,
      matchingPages: matchingPages.length,
      pageIndex,
      matchingInputs: count,
      inputIndex,
      postconditionVerified: !!payload.verifyTextContains,
      verifyTextContains: payload.verifyTextContains,
      targetUrl: page.url(),
      targetTitle: await page.title().catch(() => ""),
      state,
    };
  } finally {
    await browser.close().catch(() => {});
  }
}

main()
  .then((result) => process.stdout.write(JSON.stringify(result)))
  .catch((error) => {
    process.stdout.write(JSON.stringify({ success: false, error: String(error && error.message || error) }));
    process.exitCode = 1;
  });
"""


def _run_playwright_upload(payload: Dict[str, Any], timeout: float) -> subprocess.CompletedProcess[str]:
    node = _node_command()
    if not node:
        raise RuntimeError("Node.js is required for browser_upload_files but was not found on PATH")

    script = _playwright_node_script()
    command = [node, "-e", script, json.dumps(payload, ensure_ascii=False)]
    env = os.environ.copy()

    # Prefer explicit local dependencies.  The old npx fallback is deliberately
    # not used because npm/npx do not expose package imports reliably to
    # ``node -e`` across npm versions.
    configured_node_paths = [
        p.strip()
        for p in (env.get("HERMES_BROWSER_UPLOAD_NODE_PATHS") or "").split(os.pathsep)
        if p.strip()
    ]
    node_paths = configured_node_paths or [str(PROJECT_ROOT / "node_modules")]
    if node_paths:
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = os.pathsep.join(node_paths + ([existing] if existing else []))

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(1.0, timeout),
        env=env,
    )


def browser_upload_files(
    files: List[str],
    selector: str = DEFAULT_SELECTOR,
    target_url_contains: Optional[str] = None,
    target_title_contains: Optional[str] = None,
    page_index: Optional[int] = None,
    input_index: Optional[int] = None,
    verify_text_contains: Optional[str] = None,
    verify_timeout_ms: int = 30000,
    timeout: float = 60.0,
    settle_ms: int = 5000,
) -> str:
    """Set local files on a file input in a CDP-connected browser page.

    This tool does not click buttons or submit forms.  It only populates a
    file input and reports page state after the browser processes the upload.
    """
    if not isinstance(files, list) or not files:
        return tool_error("'files' must be a non-empty list of local file paths")
    if not isinstance(selector, str) or not selector.strip():
        return tool_error("'selector' must be a non-empty CSS selector")
    try:
        upload_files = _normalise_files(files)
    except FileNotFoundError as exc:
        return tool_error(str(exc))

    endpoint = _resolve_cdp_endpoint()
    if not endpoint:
        return tool_error(
            "No CDP endpoint is available. Set BROWSER_CDP_URL or run /browser connect first."
        )

    try:
        safe_timeout = float(timeout) if timeout else 60.0
    except (TypeError, ValueError):
        safe_timeout = 60.0
    safe_timeout = max(1.0, min(safe_timeout, 300.0))

    payload = {
        "cdpEndpoint": endpoint,
        "files": upload_files,
        "selector": selector.strip(),
        "targetUrlContains": target_url_contains or "",
        "targetTitleContains": target_title_contains or "",
        "pageIndex": int(page_index) if page_index is not None else None,
        "inputIndex": int(input_index) if input_index is not None else None,
        "verifyTextContains": str(verify_text_contains or ""),
        "verifyTimeoutMs": max(1000, min(int(verify_timeout_ms or 0), 120000)),
        "settleMs": max(0, min(int(settle_ms or 0), 30000)),
        "inspectOnly": False,
        "guardedTargetUrl": "",
        "guardedPageIdentity": "",
        "guardedComposerToken": "",
        "guardedComposerPageUrl": "",
        "guardedComposerPageActor": "",
    }

    kanban_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    grace_execution_task = False
    if kanban_task_id:
        try:
            from hermes_cli import kanban_db as _kanban_db

            with _kanban_db.connect_closing() as _scope_conn:
                grace_execution_task = _kanban_db.is_grace_execution_task(
                    _scope_conn,
                    kanban_task_id,
                )
        except Exception as exc:
            return tool_error(
                "Upload safety scope check failed closed: "
                f"{type(exc).__name__}: {exc}"
            )
    proc = None
    if grace_execution_task:
        inspection_payload = dict(payload)
        inspection_payload["files"] = []
        inspection_payload["verifyTextContains"] = ""
        inspection_payload["inspectOnly"] = True
        try:
            inspection_proc = _run_playwright_upload(
                inspection_payload, safe_timeout,
            )
            inspection = (
                json.loads((inspection_proc.stdout or "").strip())
                if inspection_proc.stdout else {}
            )
        except subprocess.TimeoutExpired:
            return tool_error(
                f"browser_upload_files safety inspection timed out after "
                f"{safe_timeout}s"
            )
        except Exception as exc:
            return tool_error(
                "External create safety inspection failed closed: "
                f"{type(exc).__name__}: {exc}"
            )
        if inspection_proc.returncode != 0 or not inspection.get("success"):
            return tool_error(
                inspection.get("error")
                or (inspection_proc.stderr or "").strip()
                or "Upload target safety inspection failed"
            )
        actual_target_url = str(inspection.get("targetUrl") or "")
        actual_page_identity = str(inspection.get("pageIdentity") or "")
        if not actual_target_url or not actual_page_identity:
            return tool_error(
                "External create safety inspection failed closed: "
                "selected page identity was unavailable"
            )
        payload["guardedTargetUrl"] = actual_target_url
        payload["guardedPageIdentity"] = actual_page_identity
        try:
            raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
            expected_run_id = int(raw_run_id) if raw_run_id else None
            from tools import browser_tool as _browser_tool

            with _kanban_db.connect_closing() as _page_scope_conn:
                page_post_permission = (
                    _kanban_db.grace_task_facebook_page_post_permission(
                        _page_scope_conn,
                        kanban_task_id,
                    )
                )
            if page_post_permission is not None:
                composer_binding, page_upload_error = (
                    _browser_tool.facebook_page_post_upload_guard(
                        actual_target_url,
                        actual_page_identity,
                        kanban_task_id=kanban_task_id,
                        expected_run_id=expected_run_id,
                    )
                )
                if page_upload_error:
                    return tool_error(page_upload_error)
                payload["guardedComposerToken"] = str(
                    (composer_binding or {}).get("composer_token") or ""
                )
                payload["guardedComposerPageUrl"] = str(
                    (composer_binding or {}).get("page_url") or ""
                )
                payload["guardedComposerPageActor"] = str(
                    (composer_binding or {}).get("page_actor") or ""
                )
            with _kanban_db.connect_closing() as _guard_conn:
                with _kanban_db.write_txn(_guard_conn):
                    guard_error = _kanban_db.external_create_mutation_guard(
                        _guard_conn,
                        kanban_task_id,
                        actual_target_url,
                        expected_run_id=expected_run_id,
                        page_identity=actual_page_identity,
                    )
                    if guard_error:
                        return tool_error(guard_error)
                    proc = _run_playwright_upload(payload, safe_timeout)
        except subprocess.TimeoutExpired:
            return tool_error(
                f"browser_upload_files timed out after {safe_timeout}s"
            )
        except Exception as exc:
            return tool_error(
                "External create safety check failed closed: "
                f"{type(exc).__name__}: {exc}"
            )

    if proc is None:
        try:
            proc = _run_playwright_upload(payload, safe_timeout)
        except subprocess.TimeoutExpired:
            return tool_error(
                f"browser_upload_files timed out after {safe_timeout}s"
            )
        except Exception as exc:
            return tool_error(f"browser_upload_files failed before upload: {exc}")

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"success": False, "error": "Upload helper returned non-JSON output", "stdout": stdout}

    if proc.returncode != 0 or not data.get("success"):
        return tool_error(
            data.get("error") or stderr or f"Upload helper exited with rc={proc.returncode}",
            stderr=stderr[-1000:] if stderr else "",
        )

    if stderr:
        data["stderr_tail"] = stderr[-1000:]
    return json.dumps(data, ensure_ascii=False)


BROWSER_UPLOAD_FILES_SCHEMA: Dict[str, Any] = {
    "name": "browser_upload_files",
    "description": (
        "Upload one or more local files into a browser file input using "
        "Playwright connected to the current CDP browser session. This only "
        "sets files on an input[type=file] and reports resulting page state; "
        "it does not click Next/Post/Publish or submit any form. Use this when "
        "a web form requires photos or attachments and browser_cdp cannot set "
        "the local file chooser payload."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute or user-relative local file paths to upload.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for the file input. Defaults to input[type=\"file\"].",
                "default": DEFAULT_SELECTOR,
            },
            "target_url_contains": {
                "type": "string",
                "description": "Optional substring used to select the browser tab by URL.",
            },
            "target_title_contains": {
                "type": "string",
                "description": "Optional substring used to select the browser tab by title.",
            },
            "page_index": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based index among pages matching the target filters. Required when more than one page matches.",
            },
            "input_index": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based index among inputs matching selector. Required when more than one input matches.",
            },
            "verify_text_contains": {
                "type": "string",
                "description": "Optional visible page text that must appear after upload before success is returned.",
            },
            "verify_timeout_ms": {
                "type": "integer",
                "description": "Milliseconds to wait for verify_text_contains (default 30000, max 120000).",
                "default": 30000,
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 60, max 300).",
                "default": 60,
            },
            "settle_ms": {
                "type": "integer",
                "description": "Milliseconds to wait after setInputFiles before reading page state.",
                "default": 5000,
            },
        },
        "required": ["files"],
    },
}


def _browser_upload_files_check() -> bool:
    # Tool schemas are fixed when an agent is initialized, while both the CDP
    # endpoint and the effective Node PATH can settle later in the worker
    # startup sequence. Hiding this tool on a transient check failure makes it
    # permanently unavailable for the rest of that turn. Keep the schema
    # visible and let browser_upload_files report a concrete call-time error
    # for a missing endpoint or Node executable.
    return True


registry.register(
    name="browser_upload_files",
    toolset="browser-cdp",
    schema=BROWSER_UPLOAD_FILES_SCHEMA,
    handler=lambda args, **kw: browser_upload_files(
        files=args.get("files", []),
        selector=args.get("selector", DEFAULT_SELECTOR),
        target_url_contains=args.get("target_url_contains"),
        target_title_contains=args.get("target_title_contains"),
        page_index=args.get("page_index"),
        input_index=args.get("input_index"),
        verify_text_contains=args.get("verify_text_contains"),
        verify_timeout_ms=args.get("verify_timeout_ms", 30000),
        timeout=args.get("timeout", 60.0),
        settle_ms=args.get("settle_ms", 5000),
    ),
    check_fn=_browser_upload_files_check,
    emoji="📎",
)
