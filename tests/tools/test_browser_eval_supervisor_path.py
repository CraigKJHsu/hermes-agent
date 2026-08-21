"""Unit tests for the supervisor-WS fast path in browser_console / _browser_eval.

These exercise the dispatch logic in ``tools.browser_tool._browser_eval`` and
the response shaping in ``CDPSupervisor.evaluate_runtime`` using mocks — no
real browser, no real WebSocket.  Real-CDP coverage lives in
``tests/tools/test_browser_supervisor.py`` (gated on Chrome being installed).
"""
from __future__ import annotations

import json
import inspect
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fast-path dispatch: tools.browser_tool._browser_eval
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_camofox(monkeypatch):
    """Force the non-camofox path so our supervisor branch is reached."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "test-task")


def _patch_supervisor(monkeypatch, supervisor):
    """Wire SUPERVISOR_REGISTRY.get to return ``supervisor`` for any task_id."""
    import tools.browser_supervisor as bs

    registry = MagicMock()
    registry.get.return_value = supervisor
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    return registry


def test_item_page_atomic_guard_accepts_all_supported_more_options_labels():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)

    for label in ('"options"', '"more options"', '"選項"', '"更多選項"'):
        assert label in source
    for label in (
        '"list in more places"',
        '"list your item in more places"',
        '"刊登到更多地方"',
        '"在更多地方刊登"',
    ):
        assert label in source
    assert 'crosspostStage === "open_dialog_direct"' in source


def test_selling_page_atomic_guard_supports_traditional_chinese_row_labels():
    from tools.browser_supervisor import CDPSupervisor
    from tools import browser_tool

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    caller_source = inspect.getsource(browser_tool._run_atomic_ref_action)

    assert '"更多選項："' in source
    assert 'normalized_name.startswith("更多選項:")' in caller_source
    assert '`推廣 ${listingName} 的刊登`' in source
    assert '`為 ${listingName} 推廣刊登`' in source
    assert '`boost listings for ${listingName}`' in source


def test_marketplace_price_guard_binds_live_value_page_and_update_control():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)

    assert 'marketplacePriceStage === "fill"' in source
    assert 'canonicalIntegerPrice(this.value)' in source
    assert 'match[1].replace(/,/g, "")' in source
    assert '^(?:NT[$])?' in source
    assert 'failed_predicate: failedFillPredicate' in source
    assert 'pageIdentity: expected' in source
    assert 'marketplacePriceStage === "submit"' in source
    assert 'normalized_live_value: canonicalIntegerPrice(' in source
    assert 'failed_predicate: failedSubmitPredicate' in source
    assert '__hermesMarketplacePriceSubmitFlow' in source
    assert 'submit_control_count: submitControls.length' in source
    assert 'submit_control_match: markedSubmit === this' in source
    assert 'form_present' not in source
    assert 'same_form' not in source
    assert 'facebook_marketplace_price_fill_guard_rejected' in source
    assert 'facebook_marketplace_price_submit_guard_rejected' in source


def test_marketplace_price_listing_id_does_not_enter_crosspost_guard():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    crosspost_guard = source.split(
        "let crosspostGroupId = null;", 1
    )[1].split('if (marketplacePriceStage === "submit")', 1)[0]
    crosspost_state_update = source.split(
        "delete this.__hermesAtomicGuard;", 1
    )[1].split("// The hit test, page identity check", 1)[0]

    assert "if (crosspostStage) {" in crosspost_guard
    assert "if (requiredListingId) {" not in crosspost_guard
    assert "if (crosspostStage) {" in crosspost_state_update
    assert "if (requiredListingId) {" not in crosspost_state_update


def test_page_composer_guard_accepts_canonical_facebook_page_redirects():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    page_binding = source.split(
        "const approvedPageSlug = (() => {", 1
    )[1].split("const pageOpenContextIn = container =>", 1)[0]

    assert (
        "canonicalPageUrlOf(location.href)\n"
        "                              !== requiredFacebookPageUrl"
    ) in page_binding
    assert source.count("canonicalPageUrlOf(location.href)") == 5
    assert "location.href !== requiredFacebookPageUrl" not in source


def test_page_composer_open_uses_management_actor_and_diagnostic_signals():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    page_binding = source.split(
        "const pageOpenContextIn = container => {", 1
    )[1].split("const composerActorBindingIn =", 1)[0]

    assert "const canonicalPageAnchors = [" in page_binding
    assert "const commentActorNames =" in page_binding
    assert 'lower.startsWith("comment as ")' in page_binding
    assert "return normalizeActor(label.slice(11))" in page_binding
    assert "(?:身分|身份)留言$/" in page_binding
    assert "(?:留言身分|留言身份)" in page_binding
    assert "const distinctCommentActorNames = commentActorNames.filter(" in page_binding
    assert "const distinctPageNavigationActorNames = (" in page_binding
    assert "const pageNavigationHeadingNames = (" in page_binding
    assert "const identityBoundary = manageHeading" in page_binding
    assert "'[role=\"separator\"],hr'" in page_binding
    assert "Boolean(name)" in page_binding
    assert "commentActorNames.indexOf(name) === index" in page_binding
    assert "pageNavigationActorNames.indexOf(name) === index" in page_binding
    assert "comment_actor_names: distinctCommentActorNames" in page_binding
    assert "canonical_page_anchor_count:" in page_binding
    assert "manage_page_context_visible: pageNavigations.length > 0" in page_binding
    assert "page_navigation_heading_names:" in page_binding
    assert "distinctPageNavigationActorNames.length === 1" in page_binding
    assert "corroboratedManagementActors.length === 1" not in page_binding
    assert "distinctCommentActorNames.length !== 1" not in page_binding
    assert "const sourcePageAnchor = (" not in page_binding
    assert "const requiredSourceActor = normalizeActor(" in page_binding
    assert "!requiredSourceActor" in page_binding
    assert "actor === requiredSourceActor" in page_binding
    assert "switch_into_page_visible: sourceShowsSwitchGate" in page_binding
    assert '"facebook_page_switch_required"' in source
    assert '"facebook_page_source_context_unproven"' in source
    assert "const visiblePageComposerShells = () =>" in source
    assert "editable.closest('[role=\"dialog\"]') === dialog" in source
    assert "const visibleDialogShellsBefore = new Set(" in source
    assert "[...documentRef.querySelectorAll('[role=\"dialog\"]')]" in source
    assert "pageComposersBefore" not in source
    assert "!editable.closest('[aria-disabled=\"true\"]')" in source
    assert "!editable.closest('[aria-readonly=\"true\"]')" in source
    assert "editable.isContentEditable" in source
    assert '!editable.matches(":disabled")' in source
    assert "!editable.readOnly" in source
    assert "opened_composer_shell_count:" in source
    assert "attempt < 100" in source
    settling_loop = source.split(
        "for (let attempt = 0; attempt < 100; attempt += 1)", 1
    )[1].split("const openedComposerShells =", 1)[0]
    final_binding = source.split(
        "const openedComposerShells =", 1
    )[1].split('"facebook_page_composer_actor_mismatch"', 1)[0]
    assert "composerActorBindingIn(" not in settling_loop
    assert "const composerActor = composerActorBindingIn(" in final_binding
    assert "const settledPageContext = pageOpenContextIn(" in final_binding
    assert '"source_page_management_context"' in source
    assert "composer_mentions_expected_actor:" not in source
    assert "composerActorContradictionsIn" in source
    assert "for (const element of [" in source
    assert "dialog," in source
    assert '"composer_actor_contradiction"' in source
    assert source.count("contradictory_composer_actors:") >= 2
    assert source.count("composerActorContradictions.length > 0") >= 2
    assert "const currentSourceActorState = (() =>" in source
    assert "source_actor_match:" in source
    assert "source_actor_contradiction:" in source
    assert "settled_source_actor_observable:" in source
    assert source.count("visible_composer_count:") == 2
    assert source.count('"visible_composer_count"') == 2


def test_page_composer_guard_supports_exact_static_text_actor_binding():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    composer_binding = source.split(
        "const composerActorBindingIn = (", 1
    )[1].split("let approvedPageActor = null", 1)[0]

    assert "When no" in composer_binding
    assert "Page anchor exists in that dialog" in composer_binding
    assert "normalizeActor(" in composer_binding
    assert ") !== expectedActor" in composer_binding
    assert "distinctSemanticActors.length === 1" in composer_binding
    assert "url: requiredFacebookPageUrl" in composer_binding
    assert "name: expectedActor" in composer_binding
    assert "candidateTextNodes" in source
    assert "actorElements.length !== 1" in composer_binding
    assert "directActorTextVisible" in composer_binding
    assert "createRange()" in source
    assert "range.getClientRects()" in source
    assert "textRects.every(({rect: textRect, textNode}) =>" in source
    assert "const colorPainted = value =>" in source
    assert 'style.filter || "none"' in source
    assert "right >= textRect.right - epsilon" in source
    assert "dialog.contains(actorElements[0])" in composer_binding
    assert "hasEditableSurface(element)" in composer_binding
    assert "element?.isContentEditable" in source
    assert "child.isContentEditable" in source
    assert "textbox => textbox.contains(element)" in composer_binding
    assert "rect.bottom > textboxTop" in composer_binding
    assert "rect.bottom > textboxTop + 24" not in composer_binding
    assert "return bindings.length === 1 ? bindings[0] : null" in source


def test_page_composer_actor_proof_excludes_hidden_and_offscreen_nodes():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)

    assert source.count("const strictlyVisible = node => {") == 2
    assert source.count("current = current.parentElement") >= 4
    assert source.count('current.hasAttribute("hidden")') == 2
    assert source.count('current.hasAttribute("inert")') == 2
    assert source.count(
        'current.getAttribute("aria-hidden") || ""'
    ) == 2
    assert source.count('style.visibility !== "visible"') == 2
    assert source.count("Number(style.opacity) === 0") >= 2
    assert source.count('style.overflowX !== "visible"') >= 4
    assert source.count('style.overflowY !== "visible"') >= 4
    assert source.count('clipPath && clipPath !== "none"') == 2
    assert source.count('legacyClip && legacyClip !== "auto"') == 2
    assert source.count("const directActorTextVisible = (") == 2
    assert source.count('value === "paint"') >= 4
    assert source.count('value === "strict"') >= 4
    assert source.count('value === "content"') >= 4
    assert source.count("current.clientLeft") >= 4
    assert source.count("current.clientTop") >= 4
    assert source.count("current.clientWidth") >= 4
    assert source.count("current.clientHeight") >= 4
    assert source.count("visibleRight <= visibleLeft") == 2
    assert source.count("visibleBottom <= visibleTop") == 2
    assert "&& strictlyVisible(anchor)" in source
    assert ".filter(strictlyVisible)" in source
    assert "|| !strictlyVisible(actorElement)" in source
    assert source.count("candidate.url === binding.url") >= 1
    assert source.count("candidate.name === binding.name") >= 1
    assert source.count("return distinctBindings.length === 1") >= 1
    assert "const pageNavigationActorNames = pageNavigations.flatMap(" in source
    assert "management_actor_names:" in source
    assert "const pageNavigations = [" in source
    assert "sourceShowsSwitchGate" in source
    assert "const visibleSwitchGateLabels = [" in source
    assert "visibleSwitchGateLabels.some(rawLabel =>" in source
    assert 'node.getAttribute("aria-label")' in source
    assert '.replace(/[’‘]/g, "\'")' in source
    assert "/切[換换]/" in source
    assert "manage page" in source
    assert "const managePageLabels = [" in source
    assert "&& sourceActorAuthorized" in source
    assert "approvedPageActorAuthorized = true" in source
    assert "approvedPageActorAuthorized" in source
    assert "includeDescendants = false" in source
    assert source.count("NodeFilter.SHOW_TEXT") >= 2
    assert "actorElement, requiredFacebookPageActor," in source
    assert "actorElement.matches('a[href]')" in source
    assert "textRectGroups.some(rects => !rects.length)" in source
    assert "url: actorUrl, name: requiredFacebookPageActor" in source
    assert "name: actorToken ? expectedActor : actorName" in source
    assert source.count("visibleCandidateTextNodes") >= 4
    assert ').join("")) !== expectedActor' in source
    assert "!strictlyVisible(this)" in source
    assert "!textboxes.includes(this)" in source
    assert "textboxTop - rect.bottom > 240" not in source
    assert "element.contains(candidate)" in source
    assert "pageGuardDiagnostics" in source


def test_selling_row_binding_does_not_treat_boost_entity_as_listing_id():
    from tools.browser_supervisor import CDPSupervisor
    from tools import browser_tool

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    listing_id_parser = source.split(
        "const listingIdsIn = container =>", 1
    )[1].split("const flow =", 1)[0]
    association = source.split(
        "const hasSellingActionAssociation = candidate =>", 1
    )[1].split("let sourceBound = false", 1)[0]

    assert "/ad_center/create/listingad/" not in listing_id_parser
    assert "let firstCompleteRow = this" in association
    assert "candidate !== firstCompleteRow" in association
    assert "boostControls.length !== 1" in association
    assert "provenListingName = normalizeLabel" in association
    assert "listingName !== provenListingName" in association
    assert 'parsed.origin === window.location.origin' in association
    assert '=== "/ad_center/create/listingad/"' in association
    assert 'listingad/preview' not in association
    assert "documentSourceControls" in association
    assert "sameAtomicControlChain" in association
    assert "optionsAreOneAction" in association
    assert "sharesAreOneAction" in association
    assert "expectedShareNames.has" in association
    assert "control.getClientRects().length > 0" in association
    assert '!control.closest(\'[aria-hidden="true"]\')' in association
    assert '!control.closest(\'[aria-disabled="true"]\')' in association
    assert "targetMatch[1] !== String(requiredListingId)" not in association
    assert "let associatedListingNode = null" in source
    assert "if (listingNode.matches?.(pageScopeSelector))" in source
    assert "sellingRoute\n                                  ? associatedListingNode" in source
    assert "associatedListingNode || canonicalRowBound" in source
    assert "sourceIds.has(String(requiredListingId))" in source
    assert "requiredSourceEntityIds" not in source
    assert '"guard_detail"' in inspect.getsource(browser_tool.browser_click)


def test_canonical_item_title_proof_requires_exact_url_share_and_boost():
    from tools.browser_tool import (
        _canonical_marketplace_boost_label_from_refs,
        _canonical_marketplace_boost_target_from_refs,
        _canonical_marketplace_item_title_from_refs,
        _is_approved_marketplace_item_url,
        _remember_snapshot_refs,
    )

    listing_id = "915975414881937"
    title = "Kolin KD-291M06"
    refs = {
        "e1": {"role": "button", "name": f"Share {title}"},
        "e2": {
            "role": "link",
            "name": (
                f"Boost listing for {title}. "
                "Boost to reach more potential buyers"
            ),
        },
    }
    assert _is_approved_marketplace_item_url(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        listing_id,
    )
    assert not _is_approved_marketplace_item_url(
        "https://www.facebook.com/marketplace/you/selling",
        listing_id,
    )
    remember_source = inspect.getsource(_remember_snapshot_refs)
    assert "_facebook_crosspost_source_proofs.pop" in remember_source
    assert _canonical_marketplace_item_title_from_refs(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        refs,
        listing_id,
    ) == title
    plural_refs = dict(refs)
    plural_refs["e2"] = {
        "role": "link",
        "name": (
            f"Boost listings for {title}. "
            "Boost to reach more potential buyers"
        ),
    }
    assert _canonical_marketplace_item_title_from_refs(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        plural_refs,
        listing_id,
    ) == title
    assert _canonical_marketplace_item_title_from_refs(
        "https://www.facebook.com/marketplace/you/selling",
        refs,
        listing_id,
    ) is None

    class FakeSupervisor:
        def call_session_cdp(self, _session_id, method, _params):
            if method == "DOM.resolveNode":
                return {
                    "ok": True,
                    "result": {"object": {"objectId": "boost-object"}},
                }
            return {
                "ok": True,
                "result": {"result": {"value": "37276725125275496"}},
            }

    import tools.browser_supervisor as browser_supervisor

    original_get = browser_supervisor.SUPERVISOR_REGISTRY.get
    browser_supervisor.SUPERVISOR_REGISTRY.get = lambda _task: FakeSupervisor()
    try:
        boost_refs = dict(refs)
        boost_refs["e2"] = {
            **boost_refs["e2"],
            "backend_node_id": 99,
            "captured_session_id": "session-1",
        }
        boost_label = _canonical_marketplace_boost_label_from_refs(
            boost_refs,
            title,
        )
        assert boost_label == boost_refs["e2"]["name"]
        assert _canonical_marketplace_boost_target_from_refs(
            "browser-1",
            boost_refs,
            boost_label,
            "https://www.facebook.com",
        ) == "37276725125275496"
    finally:
        browser_supervisor.SUPERVISOR_REGISTRY.get = original_get
    assert _canonical_marketplace_item_title_from_refs(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        {**refs, "e3": {"role": "button", "name": f"Share {title}"}},
        listing_id,
    ) == title
    assert _canonical_marketplace_item_title_from_refs(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        {"e1": refs["e1"]},
        listing_id,
    ) is None
    assert _canonical_marketplace_item_title_from_refs(
        f"https://www.facebook.com/marketplace/item/{listing_id}",
        {
            "e1": {"role": "button", "name": "Share Bike"},
            "e2": {
                "role": "link",
                "name": (
                    "Boost listing for Bike. Deluxe. "
                    "Boost to reach more potential buyers"
                ),
            },
        },
        listing_id,
    ) is None


def test_crosspost_atomic_guard_scrolls_only_authorized_group_rows():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)

    assert 'crosspostStage === "select_group"' in source
    assert "this.scrollIntoView({" in source
    assert 'block: "center"' in source
    assert "const findTargetPoint = rect =>" in source
    assert "this.ownerDocument.elementFromPoint(x, y)" in source
    assert "provenTargetPoint = findTargetPoint(rect)" in source


def test_crosspost_atomic_guard_keeps_prebound_for_sale_id_after_relay_eviction():
    from tools.browser_supervisor import CDPSupervisor

    source = inspect.getsource(CDPSupervisor.guarded_dom_action)
    binding = source.split(
        "const bindAuthoritativeCrosspostRows = () =>", 1
    )[1].split("const groupNameForId = groupId =>", 1)[0]

    assert "requiredForSaleItemId || \"\"" in binding
    assert "forSaleItemId !== String(requiredListingId)" in binding
    assert "productItem\n                                &&" in binding
    assert "Facebook cross-post for-sale item binding changed" in binding
    assert "for_sale_item_id:" in binding


def test_guarded_snapshot_reattaches_supervisor_for_persistent_page(
    monkeypatch,
):
    from tools import browser_supervisor, browser_tool

    expected_url = "https://www.facebook.com/marketplace/you/selling"
    expected_identity = f"{expected_url}|123.0"
    supervisor = MagicMock()
    supervisor.capture_ax_tree_for_url.return_value = {
        "ok": True,
        "session_id": "session-1",
        "result": {
            "nodes": [{
                "backendDOMNodeId": 42,
                "role": {"value": "button"},
                "name": {"value": "Post"},
            }],
        },
    }
    registry = MagicMock()
    registry.get.side_effect = [None, supervisor]
    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", registry)
    attached = []
    monkeypatch.setattr(
        browser_tool,
        "_ensure_cdp_supervisor",
        lambda task_id, expected_page_url=None: attached.append(
            (task_id, expected_page_url)
        ) or None,
    )

    nodes, error = browser_tool._snapshot_ax_nodes(
        "persistent-task",
        expected_url,
        expected_identity,
    )

    assert error is None
    assert nodes[0]["backend_node_id"] == 42
    assert nodes[0]["captured_session_id"] == "session-1"
    assert attached == [("persistent-task", expected_url)]


class TestBrowserEvalSupervisorPath:
    """The supervisor fast path replaces the agent-browser subprocess hop."""

    def test_primitive_result_routes_through_supervisor(self, monkeypatch):
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": 42,
            "result_type": "number",
        }
        _patch_supervisor(monkeypatch, sup)
        # If the subprocess path is hit we want a loud failure.
        monkeypatch.setattr(
            bt, "_run_browser_command",
            lambda *a, **kw: pytest.fail("subprocess path must not run when supervisor is healthy"),
        )

        out = json.loads(bt._browser_eval("1 + 41"))
        assert out["success"] is True
        assert out["result"] == 42
        assert out["method"] == "cdp_supervisor"
        sup.evaluate_runtime.assert_called_once_with("1 + 41")

    def test_json_string_result_is_parsed(self, monkeypatch):
        """Match agent-browser semantics: JSON-string results get parsed."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": '{"a": 1, "b": [2, 3]}',
            "result_type": "string",
        }
        _patch_supervisor(monkeypatch, sup)
        monkeypatch.setattr(
            bt, "_run_browser_command",
            lambda *a, **kw: pytest.fail("subprocess path must not run"),
        )

        out = json.loads(bt._browser_eval('JSON.stringify({a:1,b:[2,3]})'))
        assert out["success"] is True
        assert out["result"] == {"a": 1, "b": [2, 3]}
        # result_type reflects the parsed Python type, not the raw JS type.
        assert out["result_type"] == "dict"

    def test_non_json_string_result_kept_as_string(self, monkeypatch):
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": "hello world",
            "result_type": "string",
        }
        _patch_supervisor(monkeypatch, sup)
        monkeypatch.setattr(bt, "_run_browser_command", lambda *a, **kw: pytest.fail("nope"))

        out = json.loads(bt._browser_eval('"hello world"'))
        assert out["result"] == "hello world"
        assert out["result_type"] == "str"

    def test_js_exception_surfaces_without_subprocess_fallthrough(self, monkeypatch):
        """A JS-side error must NOT trigger a (slow + redundant) subprocess retry."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "Uncaught ReferenceError: foo is not defined",
        }
        _patch_supervisor(monkeypatch, sup)
        called = {"subprocess": False}

        def _fake_subprocess(*a, **kw):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "should-not-be-used"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("foo.bar"))
        assert out["success"] is False
        assert "ReferenceError" in out["error"]
        assert called["subprocess"] is False, \
            "JS exception should be surfaced, not retried via subprocess"

    def test_supervisor_loop_down_falls_through_to_subprocess(self, monkeypatch):
        """When the supervisor itself is unavailable, fall back to the subprocess."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "supervisor loop is not running",
        }
        _patch_supervisor(monkeypatch, sup)

        called = {"subprocess": False}

        def _fake_subprocess(task_id, cmd, args):
            called["subprocess"] = True
            assert cmd == "eval"
            return {"success": True, "data": {"result": "fallback-result"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("anything"))
        assert called["subprocess"] is True
        assert out["success"] is True
        assert out["result"] == "fallback-result"
        # Subprocess path doesn't tag the response with method=cdp_supervisor.
        assert out.get("method") != "cdp_supervisor"

    def test_no_active_supervisor_falls_through_to_subprocess(self, monkeypatch):
        """When SUPERVISOR_REGISTRY.get returns None, subprocess path runs."""
        import tools.browser_tool as bt

        _patch_supervisor(monkeypatch, None)
        called = {"subprocess": False}

        def _fake_subprocess(task_id, cmd, args):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "agent-browser-result"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("1+1"))
        assert called["subprocess"] is True
        assert out["success"] is True
        assert out.get("method") != "cdp_supervisor"

    def test_supervisor_no_session_falls_through(self, monkeypatch):
        """A supervisor without an attached page session must fall through cleanly."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "supervisor has no attached page session",
        }
        _patch_supervisor(monkeypatch, sup)
        called = {"subprocess": False}

        def _fake_subprocess(*a, **kw):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "fallback"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)
        json.loads(bt._browser_eval("1+1"))
        assert called["subprocess"] is True

    def test_subprocess_reference_chain_error_becomes_guidance(self, monkeypatch):
        """The CLI subprocess can't retry with returnByValue=False, so the
        cryptic 'Object reference chain is too long' CDP error must be turned
        into actionable guidance instead of surfaced raw."""
        import tools.browser_tool as bt

        # No supervisor → subprocess path runs.
        _patch_supervisor(monkeypatch, None)

        def _fake_subprocess(task_id, cmd, args):
            assert cmd == "eval"
            return {
                "success": False,
                "error": "Runtime.evaluate failed: Object reference chain is too long",
            }

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("document.body"))
        assert out["success"] is False
        # Raw protocol error must NOT leak through.
        assert "reference chain" not in out["error"].lower()
        # Actionable guidance instead.
        assert "primitive" in out["error"].lower()
        assert "DOM node" in out["error"] or "dom node" in out["error"].lower()


# ---------------------------------------------------------------------------
# Response shaping: CDPSupervisor.evaluate_runtime
# ---------------------------------------------------------------------------


def _make_supervisor_with_cdp(cdp_response):
    """Build a CDPSupervisor instance that mocks ``_cdp`` to return ``cdp_response``.

    Bypasses ``__init__`` entirely so we don't need a real WS connection.  We
    set just the state ``evaluate_runtime`` reads.
    """
    import asyncio
    import threading

    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup._state_lock = threading.Lock()
    sup._active = True
    sup._page_session_id = "test-session-id"
    sup._page_target_id = "test-target-id"
    sup._frames = {}

    # Build a real running event loop on a background thread so
    # asyncio.run_coroutine_threadsafe has somewhere to dispatch.
    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        return cdp_response

    sup._cdp = _fake_cdp  # type: ignore[method-assign]
    sup._loop = loop
    sup._thread = thread
    return sup


def _stop_supervisor(sup):
    sup._loop.call_soon_threadsafe(sup._loop.stop)
    sup._thread.join(timeout=2)


class TestEvaluateRuntimeResponseShaping:
    """CDPSupervisor.evaluate_runtime decodes the Runtime.evaluate response correctly."""

    def test_primitive_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {"result": {"type": "number", "value": 42}},
        })
        try:
            out = sup.evaluate_runtime("1 + 41")
            assert out == {"ok": True, "result": 42, "result_type": "number"}
        finally:
            _stop_supervisor(sup)

    def test_object_value_returned_by_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {
                    "type": "object",
                    "value": {"foo": "bar", "n": 7},
                }
            },
        })
        try:
            out = sup.evaluate_runtime('({foo:"bar", n:7})')
            assert out["ok"] is True
            assert out["result"] == {"foo": "bar", "n": 7}
            assert out["result_type"] == "object"
        finally:
            _stop_supervisor(sup)

    def test_undefined_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {"result": {"type": "undefined"}},
        })
        try:
            out = sup.evaluate_runtime("undefined")
            assert out == {"ok": True, "result": None, "result_type": "undefined"}
        finally:
            _stop_supervisor(sup)

    def test_dom_node_returns_description(self):
        """Non-serializable values (DOM nodes, functions) come back as description strings."""
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {
                    "type": "object",
                    "subtype": "node",
                    "description": "div#main.app",
                    # No 'value' key — returnByValue couldn't serialize it.
                }
            },
        })
        try:
            out = sup.evaluate_runtime("document.querySelector('#main')")
            assert out["ok"] is True
            assert out["result"] == "div#main.app"
            assert out["result_type"] == "object"
        finally:
            _stop_supervisor(sup)

    def test_js_exception_returns_error(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {"type": "undefined"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {
                        "description": "ReferenceError: foo is not defined",
                    },
                },
            },
        })
        try:
            out = sup.evaluate_runtime("foo.bar")
            assert out["ok"] is False
            assert "ReferenceError" in out["error"]
        finally:
            _stop_supervisor(sup)

    def test_inactive_supervisor_returns_error_without_dispatch(self):
        """Inactive supervisor short-circuits before even touching the loop."""
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = object.__new__(CDPSupervisor)
        sup._state_lock = threading.Lock()
        sup._active = False  # ← key
        sup._page_session_id = None
        sup._loop = None

        out = sup.evaluate_runtime("1+1")
        assert out["ok"] is False
        # Either "loop is not running" or "is not active" is acceptable —
        # both are caught by the supervisor-side error branch in _browser_eval.
        assert "supervisor" in out["error"].lower()

    def test_no_session_attached_returns_error(self):
        import asyncio
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = object.__new__(CDPSupervisor)
        sup._state_lock = threading.Lock()
        sup._active = True
        sup._page_session_id = None  # ← attach hasn't happened yet

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
            daemon=True,
        )
        thread.start()
        sup._loop = loop
        try:
            out = sup.evaluate_runtime("1+1")
            assert out["ok"] is False
            assert "session" in out["error"].lower()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)


def _make_supervisor_with_cdp_fn(cdp_fn):
    """Like ``_make_supervisor_with_cdp`` but lets the test supply a coroutine
    function as ``_cdp`` so behaviour can vary by params (e.g. returnByValue).
    """
    import asyncio
    import threading

    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup._state_lock = threading.Lock()
    sup._active = True
    sup._page_session_id = "test-session-id"
    sup._page_target_id = "test-target-id"
    sup._frames = {}

    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    sup._cdp = cdp_fn  # type: ignore[method-assign]
    sup._loop = loop
    sup._thread = thread
    return sup


def test_capture_ax_tree_retargets_unique_exact_page():
    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "new-tab",
                            "url": "chrome://newtab/",
                        },
                        {
                            "type": "page",
                            "targetId": "facebook",
                            "url": "https://www.facebook.com/marketplace/you/selling",
                        },
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "facebook-session"}}
        if method == "Accessibility.getFullAXTree":
            return {"result": {"nodes": [{"backendDOMNodeId": 42}]}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(session_id):
        calls.append(("configure", None, session_id))

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url(
            "https://www.facebook.com/marketplace/you/selling"
        )
        assert result["ok"] is True
        assert result["target_id"] == "facebook"
        assert result["session_id"] == "facebook-session"
        assert result["result"]["nodes"][0]["backendDOMNodeId"] == 42
        assert sup._page_target_id == "facebook"
        assert sup._page_session_id == "facebook-session"
        assert [call[0] for call in calls] == [
            "Target.getTargets",
            "Target.attachToTarget",
            "configure",
            "Accessibility.getFullAXTree",
            "Target.detachFromTarget",
        ]
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_fails_closed_when_target_is_ambiguous():
    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        assert method == "Target.getTargets"
        return {
            "result": {
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "facebook-1",
                        "url": "https://www.facebook.com/groups/123",
                    },
                    {
                        "type": "page",
                        "targetId": "facebook-2",
                        "url": "https://www.facebook.com/groups/123",
                    },
                ]
            }
        }

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    try:
        result = sup.capture_ax_tree_for_url(
            "https://www.facebook.com/groups/123"
        )
        assert result["ok"] is False
        assert "found 2" in result["error"]
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_disambiguates_duplicate_urls_by_page_load_identity():
    url = "https://www.facebook.com/marketplace/you/selling/"
    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {"type": "page", "targetId": "old", "url": url},
                        {"type": "page", "targetId": "current", "url": url},
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {
                "result": {
                    "sessionId": f"{params['targetId']}-session",
                }
            }
        if method == "Runtime.evaluate":
            time_origin = 100.0 if session_id == "old-session" else 200.0
            return {
                "result": {
                    "result": {
                        "value": {"href": url, "timeOrigin": time_origin},
                    }
                }
            }
        if method == "Accessibility.getFullAXTree":
            assert session_id == "current-session"
            return {"result": {"nodes": [{"backendDOMNodeId": 42}]}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(session_id):
        assert session_id == "current-session"

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url(
            url,
            expected_page_identity=f"{url}|200.0",
        )
        assert result["ok"] is True
        assert result["target_id"] == "current"
        assert result["session_id"] == "current-session"
        assert sup._page_target_id == "current"
        assert sup._page_session_id == "current-session"
        detached = [
            call[1]["sessionId"]
            for call in calls
            if call[0] == "Target.detachFromTarget"
        ]
        assert detached == ["old-session", "test-session-id"]
        assert sum(call[0] == "Runtime.evaluate" for call in calls) == 3
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_fails_closed_when_page_load_identity_is_missing():
    url = "https://www.facebook.com/marketplace/you/selling/"
    detached = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {"type": "page", "targetId": "first", "url": url},
                        {"type": "page", "targetId": "second", "url": url},
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {
                "result": {
                    "sessionId": f"{params['targetId']}-session",
                }
            }
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "result": {
                        "value": {"href": url, "timeOrigin": 100.0},
                    }
                }
            }
        if method == "Target.detachFromTarget":
            detached.append(params["sessionId"])
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    try:
        result = sup.capture_ax_tree_for_url(
            url,
            expected_page_identity=f"{url}|999.0",
        )
        assert result["ok"] is False
        assert "page-load identity" in result["error"]
        assert "found 0" in result["error"]
        assert detached == ["first-session", "second-session"]
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_rejects_identity_change_during_capture():
    url = "https://example.com/current"
    identity_reads = 0
    detached = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        nonlocal identity_reads
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {"type": "page", "targetId": "current", "url": url},
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "current-session"}}
        if method == "Runtime.evaluate":
            identity_reads += 1
            return {
                "result": {
                    "result": {
                        "value": {
                            "href": url,
                            "timeOrigin": 100.0 if identity_reads == 1 else 200.0,
                        },
                    }
                }
            }
        if method == "Accessibility.getFullAXTree":
            return {"result": {"nodes": []}}
        if method == "Target.detachFromTarget":
            detached.append(params["sessionId"])
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(_session_id):
        return None

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url(
            url,
            expected_page_identity=f"{url}|100.0",
        )
        assert result["ok"] is False
        assert "changed during AX capture" in result["error"]
        assert detached == ["current-session"]
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_reattaches_even_when_target_id_is_cached():
    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append(method)
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [{
                        "type": "page",
                        "targetId": "test-target-id",
                        "url": "https://example.com/current",
                    }]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "fresh-session"}}
        if method == "Accessibility.getFullAXTree":
            return {"result": {"nodes": []}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(_session_id):
        calls.append("configure")

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url("https://example.com/current")
        assert result["ok"] is True
        assert sup._page_session_id == "fresh-session"
        assert calls == [
            "Target.getTargets",
            "Target.attachToTarget",
            "configure",
            "Accessibility.getFullAXTree",
            "Target.detachFromTarget",
        ]
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_timeout_cleans_up_without_late_adoption():
    import asyncio
    import time

    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [{
                        "type": "page",
                        "targetId": "fresh-target",
                        "url": "https://example.com/current",
                    }]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "fresh-session"}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def slow_configure(_session_id):
        await asyncio.sleep(1)

    sup._configure_page_session = slow_configure
    try:
        result = sup.capture_ax_tree_for_url(
            "https://example.com/current",
            timeout=0.01,
        )
        time.sleep(0.05)
        assert result["ok"] is False
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
        assert (
            "Target.detachFromTarget",
            {"sessionId": "fresh-session"},
        ) in calls
    finally:
        _stop_supervisor(sup)


def test_queued_ax_capture_does_not_dispatch_after_caller_timeout():
    import threading
    import time

    cdp_calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        cdp_calls.append(method)
        return {"result": {"targetInfos": []}}

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    blocker_started = threading.Event()

    def block_loop():
        blocker_started.set()
        time.sleep(1.2)

    try:
        sup._loop.call_soon_threadsafe(block_loop)
        assert blocker_started.wait(timeout=1)
        result = sup.capture_ax_tree_for_url(
            "https://example.com/current",
            timeout=0.01,
        )
        time.sleep(0.3)
        assert result["ok"] is False
        assert cdp_calls == []
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_queued_captured_action_is_ambiguous_and_never_dispatches_late():
    import threading
    import time

    cdp_calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        cdp_calls.append(method)
        return {"result": {}}

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    blocker_started = threading.Event()

    def block_loop():
        blocker_started.set()
        time.sleep(1.2)

    try:
        sup._loop.call_soon_threadsafe(block_loop)
        assert blocker_started.wait(timeout=1)
        result = sup.call_session_cdp(
            "captured-session",
            "Runtime.callFunctionOn",
            {"objectId": "node-1"},
            timeout=0.01,
        )
        time.sleep(0.3)
        assert result["ok"] is False
        assert result["dispatch_ambiguous"] is True
        assert cdp_calls == []
    finally:
        _stop_supervisor(sup)


class TestEvaluateRuntimeDomNodeCrashRetry:
    """returnByValue=True on a DOM node fails CDP serialization with 'Object
    reference chain is too long'.  evaluate_runtime must retry with
    returnByValue=False and return the node's description instead of crashing.
    """

    def test_reference_chain_crash_retries_without_by_value(self):
        calls = []

        async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
            by_value = (params or {}).get("returnByValue")
            calls.append(by_value)
            if by_value:
                # Mirror _read_loop turning a top-level CDP error into a RuntimeError.
                raise RuntimeError(
                    "CDP error on id=7: {'code': -32000, "
                    "'message': 'Object reference chain is too long'}"
                )
            # returnByValue=False: Chrome returns the node's description, no value.
            return {
                "id": 8,
                "result": {
                    "result": {
                        "type": "object",
                        "subtype": "node",
                        "description": "body",
                    }
                },
            }

        sup = _make_supervisor_with_cdp_fn(_fake_cdp)
        try:
            out = sup.evaluate_runtime("document.body")
            assert out["ok"] is True
            assert out["result"] == "body"
            assert out["result_type"] == "object"
            # First call by_value=True (crashed), retried with by_value=False.
            assert calls == [True, False]
        finally:
            _stop_supervisor(sup)

    def test_unrelated_error_does_not_retry(self):
        calls = []

        async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
            calls.append((params or {}).get("returnByValue"))
            raise RuntimeError("CDP error on id=3: {'message': 'Target closed'}")

        sup = _make_supervisor_with_cdp_fn(_fake_cdp)
        try:
            out = sup.evaluate_runtime("document.body")
            assert out["ok"] is False
            assert "Target closed" in out["error"]
            # No retry for unrelated failures — exactly one call.
            assert calls == [True]
        finally:
            _stop_supervisor(sup)
