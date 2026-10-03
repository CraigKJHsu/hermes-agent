from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import struct
import pytest

from tools import facebook_page_graph_tool as tool
from hermes_cli import kanban_db as kb


@pytest.fixture
def accepted_page_package(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    image = tmp_path / "hero.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                      + struct.pack(">II", 1664, 936) + b"\x08\x06\x00\x00\x00")
    message = "  已驗收正文\r\n\r\n保留 Page → Group CTA。\n\n#案例\n"
    contract = {"identity": {"platform": "telegram", "chat_id": "chat-1",
                             "thread_id": "2", "project": "case-project"}}
    with kb.connect_closing() as conn:
        source_id = kb.create_task(conn, title="accepted package")
        assert kb.claim_task(conn, source_id)
        assert kb.complete_task(conn, source_id, metadata={
            "loop_contract": contract,
            "acceptance_evidence": {"inline_content_package": {"facebook_page_post": message}},
        })
        review_id = kb.create_task(conn, title="accepted review", parents=(source_id,))
        assert kb.claim_task(conn, review_id)
        assert kb.complete_task(conn, review_id, metadata={
            "review_outcome": "accepted", "accepted": True,
            "asset_review": [{"asset_family": "page_hero", "accepted": True,
                              "path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                              "width": 1664, "height": 936}],
        })
        conn.execute(
            "INSERT INTO grace_delegations (delegation_id, contract_fingerprint, request_instance_id, "
            "platform, chat_id, thread_id, session_key, session_id, resolved_route, approval_required, "
            "state, execution_task_id, review_task_id, created_at, updated_at) "
            "VALUES ('gd-source', ?, 'request-source', 'telegram', 'chat-1', '2', 'session-key', "
            "'session', '{}', 0, 'queued', ?, ?, 1, 1)",
            ("a" * 64, source_id, review_id),
        )
    contract["scope"] = {"allowed": [f"Use accepted Facebook Page package: execution_task_id={source_id}; review_task_id={review_id}"]}
    contract["facebook_page_preflight_source"] = tool.bind_accepted_page_preflight_source(contract)
    return contract, message, source_id, review_id


@pytest.mark.parametrize("fault", [None, "wrong_topic", "wrong_project", "rejected", "new_execution",
                                       "wrong_review", "ambiguous", "unselected"])
def test_accepted_page_source_requires_exact_review_and_topic(accepted_page_package, fault):
    contract, message, source_id, review_id = accepted_page_package
    if fault == "wrong_topic":
        contract["identity"]["thread_id"] = "3"
    elif fault == "wrong_project":
        contract["identity"]["project"] = "other-case"
    elif fault == "rejected":
        with kb.connect_closing() as conn:
            row = kb.latest_run(conn, review_id)
            conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"review_outcome":"rejected"}', row.id))
    elif fault == "new_execution":
        with kb.connect_closing() as conn:
            conn.execute("UPDATE task_runs SET ended_at=ended_at+100 WHERE task_id=?", (source_id,))
    elif fault == "wrong_review":
        contract["scope"]["allowed"][0] = contract["scope"]["allowed"][0].replace(review_id, "t_ffffffff")
    elif fault == "ambiguous":
        contract["scope"]["allowed"] *= 2
    elif fault == "unselected":
        contract["scope"]["allowed"] = []
        assert tool.bind_accepted_page_preflight_source(contract) is None
        return
    if fault:
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"] == message
        assert resolved["message_utf8_bytes"] == len(message.encode("utf-8"))
        assert resolved["message_sha256"] == hashlib.sha256(message.encode("utf-8")).hexdigest()


def test_accepted_page_source_accepts_canonical_page_hero_review(accepted_page_package):
    contract, message, _, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        run = kb.latest_run(conn, review_id)
        metadata = run.metadata
        page_hero = metadata.pop("asset_review")[0]
        page_hero.pop("accepted")
        metadata["page_hero"] = page_hero
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), run.id),
        )

    resolved = tool.bind_accepted_page_preflight_source(contract)

    assert resolved["message"] == message
    assert resolved["image_path"] == page_hero["path"]
    assert resolved["image_sha256"] == page_hero["sha256"]


def test_accepted_page_source_accepts_schema_repair_canonical_projection(
    accepted_page_package,
):
    contract, message, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        source_metadata = source_run.metadata
        review_run = kb.latest_run(conn, review_id)
        reviewed = review_run.metadata.pop("asset_review")[0]
        source_metadata.pop("acceptance_evidence")
        source_metadata["canonical_package"] = {
            "facebook_page_post": {
                "text": message,
                "utf8_byte_count": len(message.encode("utf-8")),
                "utf8_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            },
            "page_hero": {
                "asset_family": "page_hero",
                "filename": Path(reviewed["path"]).name,
                "path": reviewed["path"],
                "raw_byte_sha256": reviewed["sha256"],
                "dimensions": {"width": reviewed["width"], "height": reviewed["height"]},
                "exact_16_9": True,
            },
        }
        review_run.metadata["verified"] = {
            "page_hero": {
                "asset_family": "page_hero",
                "actual_image_opened": True,
                "exact_16_9": True,
                "path": reviewed["path"],
                "raw_byte_sha256": reviewed["sha256"],
                "width": reviewed["width"],
                "height": reviewed["height"],
            },
        }
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(source_metadata), source_run.id),
        )
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_run.metadata), review_run.id),
        )

    resolved = tool.bind_accepted_page_preflight_source(contract)

    assert resolved["source_field"] == "canonical_package.facebook_page_post.text"
    assert resolved["message"] == message
    assert resolved["image_path"] == reviewed["path"]
    assert resolved["image_sha256"] == reviewed["sha256"]


@pytest.mark.parametrize("fault", [None, "duplicate", "wrong_lineage"])
def test_schema_repair_resolves_missing_hero_from_accepted_lineage_review_attachment(
    accepted_page_package,
    tmp_path,
    fault,
):
    contract, message, source_id, review_id = accepted_page_package
    durable_image = tmp_path / "durable" / "hero.png"
    durable_image.parent.mkdir()
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        source_metadata = source_run.metadata
        review_run = kb.latest_run(conn, review_id)
        reviewed = review_run.metadata.pop("asset_review")[0]
        original_image = Path(reviewed["path"])
        durable_image.write_bytes(original_image.read_bytes())
        source_metadata.pop("acceptance_evidence")
        source_metadata["source_lineage"] = {
            "execution_task_id": source_id,
            "review_task_id": (
                "t_ffffffff" if fault == "wrong_lineage" else review_id
            ),
        }
        source_metadata["canonical_package"] = {
            "facebook_page_post": {
                "text": message,
                "utf8_byte_count": len(message.encode("utf-8")),
                "utf8_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            },
            "page_hero": {
                "asset_family": "page_hero",
                "filename": original_image.name,
                "path": str(original_image),
                "raw_byte_sha256": reviewed["sha256"],
                "dimensions": {"width": reviewed["width"], "height": reviewed["height"]},
                "exact_16_9": True,
            },
        }
        review_run.metadata["verified"] = {
            "page_hero": {
                "asset_family": "page_hero",
                "actual_image_opened": True,
                "exact_16_9": True,
                "path": str(original_image),
                "raw_byte_sha256": reviewed["sha256"],
                "width": reviewed["width"],
                "height": reviewed["height"],
            },
        }
        kb.add_attachment(
            conn,
            review_id,
            filename=original_image.name,
            stored_path=str(durable_image),
            content_type="image/png",
            size=durable_image.stat().st_size,
            uploaded_by="kanban_complete",
        )
        if fault == "duplicate":
            duplicate = durable_image.with_name("duplicate.png")
            duplicate.write_bytes(durable_image.read_bytes())
            kb.add_attachment(
                conn,
                review_id,
                filename=original_image.name,
                stored_path=str(duplicate),
                content_type="image/png",
                size=duplicate.stat().st_size,
                uploaded_by="kanban_complete",
            )
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(source_metadata), source_run.id),
        )
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_run.metadata), review_run.id),
        )
    original_image.unlink()

    if fault:
        with pytest.raises(ValueError, match="verified Page Hero file is unavailable"):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["image_path"] == str(durable_image)
        assert resolved["image_sha256"] == reviewed["sha256"]
        assert resolved["image_bytes"] == durable_image.stat().st_size
        assert resolved["image_resolution"] == {
            "source": "accepted_lineage_review_attachment",
            "candidate_count": 1,
            "lineage_execution_task_id": source_id,
            "lineage_review_task_id": review_id,
            "attachment_id": resolved["image_resolution"]["attachment_id"],
            "attachment_filename": original_image.name,
            "attachment_size": durable_image.stat().st_size,
        }


@pytest.mark.parametrize(
    "fault",
    ["package_not_object", "post_not_object", "post_missing_text",
     "hero_not_object", "hero_missing_hash"],
)
def test_accepted_page_source_rejects_malformed_canonical_projection(
    accepted_page_package,
    fault,
):
    contract, message, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        review_run = kb.latest_run(conn, review_id)
        reviewed = review_run.metadata["asset_review"][0]
        canonical = {
            "facebook_page_post": {
                "text": message,
                "utf8_byte_count": len(message.encode("utf-8")),
                "utf8_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            },
            "page_hero": {
                "asset_family": "page_hero",
                "filename": Path(reviewed["path"]).name,
                "path": reviewed["path"],
                "raw_byte_sha256": reviewed["sha256"],
                "dimensions": {"width": reviewed["width"], "height": reviewed["height"]},
                "exact_16_9": True,
            },
        }
        if fault == "package_not_object":
            source_run.metadata["canonical_package"] = []
        else:
            if fault == "post_not_object":
                canonical["facebook_page_post"] = message
            elif fault == "post_missing_text":
                canonical["facebook_page_post"].pop("text")
            elif fault == "hero_not_object":
                canonical["page_hero"] = []
            elif fault == "hero_missing_hash":
                canonical["page_hero"].pop("raw_byte_sha256")
            source_run.metadata["canonical_package"] = canonical
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(source_run.metadata), source_run.id),
        )

    with pytest.raises(ValueError, match="malformed canonical"):
        tool.bind_accepted_page_preflight_source(contract)


@pytest.mark.parametrize(
    "fault",
    [None, "changed_hash", "wrong_dimensions", "missing_byte_receipt", "ambiguous",
     "malformed_visual", "malformed_hashes", "missing_visual_safety", "occluded",
     "obstructive_disclosure", "visual_defect", "intermediate_package",
     "incomplete_terminal_package"],
)
def test_accepted_page_source_accepts_controller_actual_pixel_review(
    accepted_page_package, fault,
):
    """Historical Topic 4641 schema sample; behavior must remain project-agnostic."""
    contract, message, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        review_run = kb.latest_run(conn, review_id)
        image_path = Path(review_run.metadata["asset_review"][0]["path"])
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        kb.add_attachment(
            conn,
            source_id,
            filename="hero.png",
            stored_path=str(image_path),
            content_type="image/png",
            size=image_path.stat().st_size,
            uploaded_by="kanban_complete",
        )
        if fault == "ambiguous":
            kb.add_attachment(
                conn,
                source_id,
                filename="duplicate.png",
                stored_path=str(image_path),
                content_type="image/png",
                size=image_path.stat().st_size,
                uploaded_by="kanban_complete",
            )
        review_metadata = {
            "review_outcome": "accepted",
            "asset_family": "page_hero",
            "asset_declarations": {
                "page_hero": {"dimensions": "1664x936"},
            },
            "visual_review": {
                "all_required_text_readable": True,
                "text_occlusion_free": True,
                "disclosure_non_obstructive": True,
                "defects_found": [],
            },
            "workflow_review_source": {
                "parent_execution_task_id": source_id,
                "parent_execution_run_id": source_run.id,
                "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
                    source_run
                ),
            },
            "evidence": {
                "page_hero_actual_pixel_analysis": {
                    "asset_family": "page_hero",
                    "status": "passed",
                    "dimensions": "1664x936"
                    if fault != "wrong_dimensions"
                    else "1600x900",
                    "traditional_chinese_readable": True,
                    "ai_disclosure_visible": True,
                },
                "hash_verification": {
                    "page_hero_sha256": image_hash
                    if fault != "changed_hash"
                    else "f" * 64,
                    "exact_attachment_bytes_verified": fault != "missing_byte_receipt",
                },
                "controller_content_package_readback": {
                    "package_complete": fault not in {
                        "intermediate_package", "incomplete_terminal_package",
                    },
                    "task_attachment_row_count": 2 if fault == "ambiguous" else 1,
                },
            },
        }
        if fault == "malformed_visual":
            review_metadata["evidence"]["page_hero_actual_pixel_analysis"] = ["invalid"]
        elif fault == "malformed_hashes":
            review_metadata["evidence"]["hash_verification"] = "invalid"
        elif fault == "missing_visual_safety":
            review_metadata.pop("visual_review")
        elif fault == "occluded":
            review_metadata["visual_review"]["text_occlusion_free"] = False
        elif fault == "obstructive_disclosure":
            review_metadata["visual_review"]["disclosure_non_obstructive"] = False
        elif fault == "visual_defect":
            review_metadata["visual_review"]["defects_found"] = ["overlay obstructs text"]
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_metadata), review_run.id),
        )
        if fault in {"intermediate_package", "incomplete_terminal_package"}:
            sealed_contract = {
                "identity": dict(contract["identity"]),
                "completion_mode": (
                    "intermediate"
                    if fault == "intermediate_package"
                    else "terminal"
                ),
            }
            snapshot = json.dumps(sealed_contract, ensure_ascii=False)
            conn.execute(
                "UPDATE grace_delegations SET contract_snapshot=?, "
                "contract_fingerprint=? WHERE execution_task_id=?",
                (
                    snapshot,
                    hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                    source_id,
                ),
            )

    if fault and fault != "intermediate_package":
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"] == message
        assert resolved["execution_run_id"] == source_run.id
        assert resolved["review_run_id"] == review_run.id
        assert resolved["image_path"] == str(image_path)
        assert resolved["image_sha256"] == image_hash


@pytest.mark.parametrize(
    "fault",
    [None, "normalized_representation", "unsafe_visual", "wrong_dimensions", "wrong_hash", "wrong_attachment", "wrong_message", "conflict", "incomplete_current_with_legacy", "uninspected_current_with_legacy"],
)
def test_accepted_page_source_accepts_current_controller_review(
    accepted_page_package, fault,
):
    contract, message, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        review_run = kb.latest_run(conn, review_id)
        legacy = review_run.metadata["asset_review"][0]
        image_path = Path(legacy["path"])
        image_bytes = image_path.read_bytes()
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        attachment_id = kb.add_attachment(
            conn,
            source_id,
            filename="page-hero.png",
            stored_path=str(image_path),
            content_type="image/png",
            size=len(image_bytes),
            uploaded_by="kanban_complete",
        )
        declaration = {
            "asset_family": "page_hero",
            "filename": "page-hero.png",
            "dimensions": "1664x936",
            "width": 1664,
            "height": 936,
            "ratio": "16:9",
            "sha256": image_hash,
            "bytes": len(image_bytes),
        }
        evidence = {
            "actual_controller_attachment_inspected": True,
            "asset_family": "page_hero",
            "asset_declarations": {"page_hero": declaration},
            "controller_attachment": {
                "attachment_id": attachment_id,
                "filename": "page-hero.png",
                "stored_path": str(image_path),
                "bytes": len(image_bytes),
                "sha256": image_hash,
                "width": 1664,
                "height": 936,
                "byte_for_byte_equal": True,
            },
            "facebook_page_post_readback": {
                "utf8_bytes": len(message.encode("utf-8")),
                "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "byte_preserving": True,
            },
            "visual_review": {
                "all_required_text_readable": True,
                "text_occlusion_free": True,
                "disclosure_non_obstructive": True,
                "defects_found": [],
            },
        }
        if fault == "unsafe_visual":
            evidence["visual_review"]["text_occlusion_free"] = False
        elif fault == "normalized_representation":
            declaration.pop("width")
            declaration.pop("height")
            declaration["sha256"] = image_hash.upper()
            evidence["controller_attachment"]["sha256"] = image_hash.upper()
            evidence["facebook_page_post_readback"]["sha256"] = (
                evidence["facebook_page_post_readback"]["sha256"].upper()
            )
        elif fault == "wrong_dimensions":
            declaration["width"] = 1600
            declaration["height"] = 900
        elif fault == "wrong_hash":
            evidence["controller_attachment"]["sha256"] = "f" * 64
        elif fault == "wrong_attachment":
            evidence["controller_attachment"]["attachment_id"] += 1
        elif fault == "wrong_message":
            evidence["facebook_page_post_readback"]["sha256"] = "f" * 64
        elif fault in {
            "incomplete_current_with_legacy",
            "uninspected_current_with_legacy",
        }:
            evidence.pop("controller_attachment")
            evidence.pop("facebook_page_post_readback")
            if fault == "incomplete_current_with_legacy":
                evidence.pop("actual_controller_attachment_inspected")
            else:
                evidence["actual_controller_attachment_inspected"] = False
        review_metadata = {
            "review_outcome": "accepted",
            "evidence": evidence,
        }
        if fault in {
            "incomplete_current_with_legacy",
            "uninspected_current_with_legacy",
        }:
            review_metadata["asset_review"] = [legacy]
        if fault == "conflict":
            review_metadata["asset_declarations"] = {
                "page_hero": {**declaration, "sha256": "f" * 64},
            }
            review_metadata["visual_review"] = dict(evidence["visual_review"])
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_metadata), review_run.id),
        )

    if fault and fault != "normalized_representation":
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"] == message
        assert resolved["image_path"] == str(image_path)
        assert resolved["image_sha256"] == image_hash


@pytest.mark.parametrize("source_filename", ["hero.png", "accepted-page-source.json"])
def test_historical_accepted_page_is_controller_materialized_for_fresh_relay(
    accepted_page_package, tmp_path, source_filename,
):
    contract, message, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        review_run = kb.latest_run(conn, review_id)
        legacy = review_run.metadata["asset_review"][0]
        image_path = Path(legacy["path"])
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        kb.add_attachment(
            conn,
            source_id,
            filename=source_filename,
            stored_path=str(image_path),
            content_type="image/png",
            size=image_path.stat().st_size,
            uploaded_by="kanban_complete",
        )
        review_metadata = {
            "review_outcome": "accepted",
            "workflow_review_source": {
                "parent_execution_task_id": source_id,
                "parent_execution_run_id": source_run.id,
                "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
                    source_run
                ),
            },
            "evidence": {
                "page_hero_actual_pixel_analysis": {
                    "asset_family": "page_hero",
                    "status": "passed",
                    "dimensions": "1664x936",
                    "traditional_chinese_readable": True,
                    "ai_disclosure_visible": True,
                },
                "hash_verification": {
                    "page_hero_sha256": image_hash,
                    "exact_attachment_bytes_verified": True,
                },
                "controller_content_package_readback": {
                    "package_complete": True,
                    "task_attachment_row_count": 1,
                },
            },
        }
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_metadata), review_run.id),
        )

    # Historical evidence is a source for a new review, never direct preflight.
    with pytest.raises(ValueError, match="current structured Page Hero safety"):
        tool.bind_accepted_page_preflight_source(contract)

    contract.update(
        completion_handoff={"metadata_source": "workspace_file"},
        routing={"resolved": {"task_type": "devops"}},
        user_facing_delivery={
            "required": True,
            "kind": "content_package",
            "delivery": "inline_with_attachment",
            "body_field": "metadata.facebook_page_post.text",
            "asset_filenames": ["page-hero.png"],
        },
    )
    workspace = tmp_path / "relay-workspace"
    workspace.mkdir()
    receipt = tool.materialize_accepted_page_relay_handoff(
        contract, str(workspace)
    )
    bundle_path = Path(receipt["bundle_path"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["facebook_page_post"]["text"].encode("utf-8") == message.encode(
        "utf-8"
    )
    assert bundle["facebook_page_post"]["sha256"] == hashlib.sha256(
        message.encode("utf-8")
    ).hexdigest()
    assert bundle["facebook_page_post"]["utf8_bytes"] == len(
        message.encode("utf-8")
    )
    copied_image = Path(bundle["page_hero"]["path"])
    assert copied_image.is_relative_to(workspace)
    assert copied_image.name == "page-hero.png"
    assert bundle["page_hero"]["filename"] == source_filename
    assert copied_image != bundle_path
    assert copied_image.read_bytes() == image_path.read_bytes()
    assert bundle["external_effects"] == []
    readback = tool.read_materialized_accepted_page_relay_handoff(receipt)
    assert readback["message"].encode("utf-8") == message.encode("utf-8")
    assert readback["image_path"] == str(copied_image)
    assert readback["image_data"] == copied_image.read_bytes()

    copied_image.unlink()
    os.mkfifo(copied_image)
    with pytest.raises(ValueError, match="file is invalid"):
        tool.read_materialized_accepted_page_relay_handoff(receipt)

    bundle_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match accepted bytes"):
        tool.materialize_accepted_page_relay_handoff(contract, str(workspace))


def test_accepted_page_relay_rejects_wrong_delivery_contract(
    accepted_page_package, tmp_path,
):
    contract, _, _, _ = accepted_page_package
    contract.update(
        completion_handoff={"metadata_source": "workspace_file"},
        routing={"resolved": {"task_type": "devops"}},
        user_facing_delivery={
            "required": True,
            "kind": "content_package",
            "delivery": "inline_only",
            "body_field": "domain_inventory_report",
        },
    )
    workspace = tmp_path / "wrong-delivery"
    workspace.mkdir()

    with pytest.raises(ValueError, match="inline_with_attachment"):
        tool.materialize_accepted_page_relay_handoff(contract, str(workspace))


def test_controller_relay_handoff_ignores_ordinary_workspace_tasks(tmp_path):
    workspace = tmp_path / "ordinary"
    workspace.mkdir()
    assert tool.materialize_accepted_page_relay_handoff(
        {
            "completion_handoff": {"metadata_source": "inline"},
            "routing": {"resolved": {"task_type": "devops"}},
        },
        str(workspace),
    ) is None
    assert list(workspace.iterdir()) == []


def test_accepted_page_source_remains_valid_in_another_topic(
    accepted_page_package,
):
    contract, message, source_id, _ = accepted_page_package
    contract["identity"].update(thread_id="other-topic", project="other-project")
    with kb.connect_closing() as conn:
        source_run = kb.latest_run(conn, source_id)
        metadata = source_run.metadata
        metadata["loop_contract"]["identity"].update(
            thread_id="other-topic", project="other-project"
        )
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), source_run.id),
        )
        conn.execute(
            "UPDATE grace_delegations SET thread_id='other-topic' "
            "WHERE execution_task_id=?",
            (source_id,),
        )

    resolved = tool.bind_accepted_page_preflight_source(contract)

    assert resolved["message"] == message


def test_accepted_page_source_rejects_explicitly_rejected_canonical_page_hero(
    accepted_page_package,
):
    contract, _, _, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        run = kb.latest_run(conn, review_id)
        metadata = run.metadata
        page_hero = metadata.pop("asset_review")[0]
        page_hero["accepted"] = False
        metadata["page_hero"] = page_hero
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), run.id),
        )

    with pytest.raises(ValueError, match="one reviewed Page Hero"):
        tool.bind_accepted_page_preflight_source(contract)


@pytest.mark.parametrize("fault", [None, "changed_review_hash", "duplicate_hero", "changed_page_text"])
def test_full_package_projects_only_reviewed_page_hero(
    accepted_page_package, tmp_path, fault,
):
    contract, message, source_id, review_id = accepted_page_package
    message = message.strip()
    hero_path = tmp_path / "hero.png"
    audio_path = tmp_path / "audio.png"
    audio_path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", 1254, 1254) + b"\x08\x06\x00\x00\x00"
    )
    hero_hash = hashlib.sha256(hero_path.read_bytes()).hexdigest()
    audio_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    hero = {"asset_family": "page_hero", "filename": "hero.png", "path": str(hero_path),
            "sha256": hero_hash, "width": 1664, "height": 936}
    audio = {"asset_family": "audio_brief", "filename": "audio.png", "path": str(audio_path),
             "sha256": audio_hash, "width": 1254, "height": 1254}
    body = (f"## 1｜Facebook Page 貼文\n\n{message}\n\n"
            "## 2｜Facebook Group 手動轉貼附文\n\nGroup\n\n"
            "## 3｜Gemini Notebook 音訊生成提示\n\nPrompt\n\n"
            "## 4｜Podcast 單集標題\n\nTitle\n\n"
            "## 5｜Podcast 單集描述\n\nDescription\n\n"
            "## 6｜Accepted Source 原文\n\nSource")
    if fault == "changed_page_text":
        message = "審查後遭改寫的正文"
    with kb.connect_closing() as conn:
        hero_attachment = kb.add_attachment(conn, source_id, filename="hero.png",
                                            stored_path=str(hero_path), content_type="image/png",
                                            size=hero_path.stat().st_size, uploaded_by="kanban_complete")
        kb.add_attachment(conn, source_id, filename="audio.png", stored_path=str(audio_path),
                          content_type="image/png", size=audio_path.stat().st_size,
                          uploaded_by="kanban_complete")
        source_run = kb.latest_run(conn, source_id)
        source = source_run.metadata
        source.update({
            "external_effect_budget": 0, "external_effects": [],
            "facebook_page_post": {"text": message},
            "acceptance_evidence": {"sections": {"facebook_page_post": message},
                                    "assets": [hero, audio]},
            "user_facing_report": {"package_kind": "full_publication_package",
                                   "complete": True, "body": body,
                                   "sections": {"facebook_page_post": message},
                                   "assets": [hero, audio], "external_effects": []},
            "attachment_manifest": kb.task_attachment_manifest(conn, source_id),
        })
        review_run = kb.latest_run(conn, review_id)
        review = review_run.metadata
        reviewed = {"asset_family": "page_hero", "filename": "hero.png",
                    "sha256": "0" * 64 if fault == "changed_review_hash" else hero_hash,
                    "width": 1664, "height": 936, "aspect_ratio": "16:9",
                    "byte_for_byte_equal": True, "visual_review": "pass",
                    "ai_disclosure_visible": True, "attachment_id": hero_attachment}
        review["verification"] = {
            "package_kind": "full_publication_package", "package_complete": True,
            "facebook_page_post_text_equals_section": True,
            "controller_zero_effect_check": True, "external_effect_count": 0,
            "external_effects": [],
            "controller_digests": {
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "body_utf8_byte_count": len(body.encode("utf-8")),
            },
            "assets": [reviewed, {"asset_family": "audio_brief", "filename": "audio.png"}]
            + ([reviewed] if fault == "duplicate_hero" else []),
        }
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     (json.dumps(source), source_run.id))
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     (json.dumps(review), review_run.id))
    if fault:
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"] == message
        assert resolved["image_sha256"] == hero_hash
        assert resolved["image_path"] == str(hero_path)


@pytest.mark.parametrize("fault", [None, "changed_pin", "changed_message", "wrong_image", "inactive"])
def test_preflight_reads_accepted_bytes_through_active_capability(
    accepted_page_package, monkeypatch, fault,
):
    contract, message, _, _ = accepted_page_package
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="preflight")
        task = kb.claim_task(conn, task_id)
        run_id = task.current_run_id
        scope = tool.OpenClawCapabilityScope(task_id=task_id, run_id=run_id,
            delegation_id="gd-preflight", contract_fingerprint="b" * 64, approval_grant_id="",
            backend_agent_id="missioncrew-facebook-page-operator", task_type="facebook_page_publish_preflight")
        if fault == "changed_pin":
            contract["facebook_page_preflight_source"]["message"] = "different case"
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({
            "delegation_id": scope.delegation_id, "contract_fingerprint": scope.contract_fingerprint,
            "approval_grant_id": "", "backend_agent_id": scope.backend_agent_id,
            "allowed_tools": ["facebook_page_publish_preflight"], "external_effect_budget": 0,
            "task_type": scope.task_type, "credential_refs": ["missioncrew-facebook-page"],
            "loop_contract": contract,
        }), run_id))
        if fault == "inactive":
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    calls = []
    def status(_args):
        calls.append("GET")
        return json.dumps({"identity_verified": True, "configured": True, "page_id": "123",
                           "page_name": "Test", "page_url": "https://www.facebook.com/test", "api_version": "v26.0"})
    monkeypatch.setattr(tool, "_handle_status", status)
    result = json.loads(tool._handle_publish_preflight({
        "final_message": "changed" if fault == "changed_message" else "",
        "image_path": "/unbound/private.txt" if fault == "wrong_image" else "",
    }, capability_scope=scope))
    assert result["success"] is (fault is None)
    assert result["published"] is False
    assert calls == ([] if fault else ["GET"])
    if not fault:
        assert result["final_message"] == message
        assert result["manifest"]["message_utf8_bytes"] == len(message.encode("utf-8"))
        assert result["external_effects"] == []


def _config() -> tool.FacebookPageConfig:
    return tool.FacebookPageConfig(
        api_version="v26.0",
        page_id="123",
        page_name="Test Page",
        page_url="https://www.facebook.com/testpage",
        access_token="secret-token",
        app_secret="secret-app",
    )


def test_status_returns_verified_identity_without_token(monkeypatch):
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: {
            "id": "123",
            "name": "Test Page",
            "link": "https://www.facebook.com/testpage",
        },
    )

    result = json.loads(tool._handle_status({}))

    assert result["success"] is True
    assert result["identity_verified"] is True
    assert "secret-token" not in json.dumps(result)


def test_status_verifies_configured_vanity_url_from_graph_username(monkeypatch):
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: {
            "id": "123",
            "name": "Test Page",
            "link": "https://www.facebook.com/123",
            "username": "TestPage",
        },
    )

    result = json.loads(tool._handle_status({}))

    assert result["success"] is True
    assert result["identity_verified"] is True
    assert result["canonical_url_verified"] is True
    assert result["page_url"] == "https://www.facebook.com/testpage"
    assert result["graph_page_url"] == "https://www.facebook.com/123"
    assert result["page_username"] == "TestPage"


def test_status_rejects_identity_when_graph_omits_page_link(monkeypatch):
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: {"id": "123", "name": "Test Page"},
    )

    result = json.loads(tool._handle_status({}))

    assert result["success"] is False
    assert result["identity_verified"] is False
    assert result["page_url"] == ""


def test_legacy_sealed_page_publish_scope_compiles_exact_manifest():
    message_hash = "a" * 64
    image_hash = "b" * 64
    contract = {
        "external_targets": ["https://www.facebook.com/testpage"],
        "scope": {
            "allowed": [
                "唯一目標：Facebook Page https://www.facebook.com/testpage（Page ID 123）",
                f"僅使用已驗證的精確正文，SHA-256={message_hash}",
                f"僅使用 /tmp/hero.png，SHA-256={image_hash}",
            ]
        },
    }

    assert kb._facebook_page_post_manifest(contract) == {
        "action": "create_post",
        "transport": "graph_api",
        "page_url": "https://www.facebook.com/testpage",
        "message_sha256": message_hash,
        "image_sha256": image_hash,
        "page_id": "123",
    }


def test_legacy_sealed_page_publish_scope_rejects_ambiguous_hashes():
    contract = {
        "external_targets": ["https://www.facebook.com/testpage"],
        "scope": {
            "allowed": [
                "唯一目標：Facebook Page https://www.facebook.com/testpage（Page ID 123）",
                f"僅使用已驗證的精確正文，SHA-256={'a' * 64}",
                f"僅使用已驗證的精確正文，SHA-256={'c' * 64}",
                f"僅使用 /tmp/hero.png，SHA-256={'b' * 64}",
            ]
        },
    }

    assert kb._facebook_page_post_manifest(contract) is None


def test_preflight_verifies_source_diff_png_hash_and_page_identity(monkeypatch, tmp_path):
    source = (
        "案例正文\n\n今日可做：建立報價計算器。\n\n"
        "💬 Group 討論題：\n討論內容\n\n"
        "Page → Group 導流：\n導流內容"
    )
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    image_path = tmp_path / "hero.png"
    image_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 1600, 900)
        + b"\x08\x06\x00\x00\x00"
    )
    image_path.write_bytes(image_bytes)
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    contract = {
        "original_request": (
            f"sha256={source_hash}\nBEGIN_FACEBOOK_PAGE_SOURCE_TEXT\n"
            f"{source}\nEND_FACEBOOK_PAGE_SOURCE_TEXT"
        ),
        "scope": {
            "allowed": [f"{image_path} PNG 1600×900 SHA-256 {image_hash}"]
        },
    }
    scope = tool.OpenClawCapabilityScope(
        task_id="t_preflight",
        run_id=1,
        delegation_id="delegation",
        contract_fingerprint="f" * 64,
        approval_grant_id="",
        backend_agent_id="missioncrew-facebook-page-operator",
        task_type="facebook_page_publish_preflight",
    )
    monkeypatch.setattr(
        tool, "_authorized_openclaw_contract", lambda _scope: (contract, "")
    )
    monkeypatch.setattr(
        tool,
        "_handle_status",
        lambda _args: json.dumps({
            "identity_verified": True,
            "page_id": "123",
            "page_name": "Test Page",
            "page_url": "https://www.facebook.com/testpage",
        }),
    )

    final_message = "案例正文\n\n今日可做：建立報價計算器。\n\n#一人公司 #報價系統"
    result = json.loads(tool._handle_publish_preflight(
        {"final_message": final_message, "image_path": str(image_path)},
        capability_scope=scope,
    ))

    assert result["success"] is True
    assert result["published"] is False
    assert result["external_actions_performed"] is False
    assert result["manifest"]["image_ratio"] == "16:9"
    assert result["manifest"]["message_sha256"] == hashlib.sha256(
        final_message.encode("utf-8")
    ).hexdigest()
    assert result["evidence"]["hashtags_are_final_paragraph"] is True


def test_openclaw_status_rejects_unbound_capability_before_graph(monkeypatch):
    scope = tool.OpenClawCapabilityScope(
        task_id="t_missing",
        run_id=1,
        delegation_id="delegation",
        contract_fingerprint="f" * 64,
        approval_grant_id="approval",
        backend_agent_id="missioncrew-facebook-page-operator",
    )
    monkeypatch.setattr(
        tool,
        "_authorized_openclaw_contract",
        lambda _scope: (None, "no approved contract"),
    )
    monkeypatch.setattr(
        tool,
        "_handle_status",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("Graph status must not run for an unbound capability")
        ),
    )

    result = json.loads(
        tool.execute_openclaw_facebook_page_capability("status", {}, scope)
    )

    assert result == {
        "success": False,
        "published": False,
        "error": "no approved contract",
    }


def test_page_url_parser_rejects_lookalike_hosts_and_extra_routes():
    assert tool._canonical_page_url(
        "https://www.facebook.com/testpage/"
    ) == "https://www.facebook.com/testpage"
    assert tool._canonical_page_url(
        "https://www.facebook.com.evil.example/testpage"
    ) == ""
    assert tool._canonical_page_url(
        "https://www.facebook.com/testpage/posts/1"
    ) == ""
    assert tool._canonical_page_url(
        "https://www.facebook.com:bad/testpage"
    ) == ""


def test_publish_rejects_hash_mismatch_before_reservation(monkeypatch, tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"approved-image")
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": "0" * 64,
            "image_sha256": hashlib.sha256(b"approved-image").hexdigest(),
        }, ""),
    )
    graph_called = False

    def fail_graph(*_args, **_kwargs):
        nonlocal graph_called
        graph_called = True
        raise AssertionError("Graph API must not run after a hash mismatch")

    monkeypatch.setattr(tool, "_graph_request", fail_graph)

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": "wrong message",
        "image_path": str(image_path),
    }))

    assert result["success"] is False
    assert result["published"] is False
    assert graph_called is False


def test_publish_uses_unique_grace_accepted_preflight_body(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    approved_message = "Exact approved message"
    supplied_message = "Wrong source body"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(approved_message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(
        tool,
        "_accepted_preflight_message",
        lambda *_args, **_kwargs: approved_message,
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    posted_messages = []

    def graph(_cfg, method, _path, **kwargs):
        if method == "POST":
            posted_messages.append(kwargs["data"]["message"])
            return {"id": "photo-1", "post_id": "123_post-1"}
        return {
            "id": "123_post-1",
            "message": approved_message,
            "permalink_url": "https://www.facebook.com/test/posts/post-1",
            "attachments": {"data": [{
                "media_type": "photo",
                "target": {"id": "photo-1"},
                "url": "https://www.facebook.com/photo.php?id=photo-1",
            }]},
        }

    monkeypatch.setattr(tool, "_graph_request", graph)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(kb, "reserve_facebook_page_create", lambda *_a, **_k: None)
    monkeypatch.setattr(kb, "record_external_effect", lambda *_a, **_k: {})

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": supplied_message,
        "image_path": str(image_path),
    }))

    assert result["success"] is True
    assert result["verified"] is True
    assert posted_messages == [approved_message]
    assert result["message_sha256"] == hashlib.sha256(
        approved_message.encode()
    ).hexdigest()


def test_publish_reserves_records_and_verifies_readback(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    message = "Exact approved message"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    responses = iter([
        {"id": "photo-1", "post_id": "123_post-1"},
        {
            "id": "123_post-1",
            "message": message,
            "permalink_url": "https://www.facebook.com/test/posts/post-1",
            "created_time": "2026-08-26T00:00:00+0000",
            "attachments": {"data": [{
                "media_type": "photo",
                "target": {"id": "photo-1"},
                "url": "https://www.facebook.com/photo.php?id=photo-1",
            }]},
        },
    ])
    monkeypatch.setattr(
        tool, "_graph_request", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    calls = []

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(
        kb,
        "reserve_facebook_page_create",
        lambda *_args, **kwargs: calls.append(("reserve", kwargs)) or None,
    )
    monkeypatch.setattr(
        kb,
        "record_external_effect",
        lambda *_args, **kwargs: calls.append(("record", kwargs)) or {},
    )

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": message,
        "image_path": str(image_path),
    }))

    assert result["success"] is True
    assert result["published"] is True
    assert result["verified"] is True
    assert result["post_id"] == "123_post-1"
    assert [name for name, _ in calls] == ["reserve", "record", "record"]
    assert calls[-1][1]["state"] == "verified"
    details = calls[-1][1]["details"]
    assert details["readback_message"] == message
    assert details["readback_message_length"] == len(message)
    assert details["readback_message_sha256"] == hashlib.sha256(
        message.encode("utf-8")
    ).hexdigest()
    assert details["readback_message_final_paragraph"] == message
    assert result["readback_message_sha256"] == details["readback_message_sha256"]


def test_publish_preserves_reservation_after_transport_error(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    message = "Exact approved message"
    image = b"approved-image"
    image_path = tmp_path / "image.png"
    image_path.write_bytes(image)
    monkeypatch.setattr(
        tool,
        "_authorized_execution_contract",
        lambda: ({
            "page_url": "https://www.facebook.com/testpage",
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
        }, ""),
    )
    monkeypatch.setattr(tool, "_load_page_config", _config)
    monkeypatch.setattr(
        tool,
        "_fetch_page_status",
        lambda _cfg: {"success": True, "identity_verified": True},
    )
    monkeypatch.setattr(
        tool,
        "_graph_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tool.FacebookGraphError("ReadTimeout")
        ),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_publish")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    calls = []

    @contextmanager
    def fake_connect(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(kb, "connect_closing", fake_connect)
    monkeypatch.setattr(
        kb,
        "reserve_facebook_page_create",
        lambda *_args, **kwargs: calls.append(("reserve", kwargs)) or None,
    )
    monkeypatch.setattr(
        kb,
        "record_external_effect",
        lambda *_args, **kwargs: calls.append(("record", kwargs)) or {},
    )

    result = json.loads(tool._handle_publish({
        "page_url": "https://www.facebook.com/testpage",
        "message": message,
        "image_path": str(image_path),
    }))

    assert result["success"] is False
    assert result["published"] is None
    assert result["retry_permitted"] is False
    assert [name for name, _ in calls] == ["reserve"]


@pytest.mark.parametrize("source_format", ["acceptance_evidence", "evidence"])
@pytest.mark.parametrize("topic", ["4641", "2"])  # Historical failure plus an unrelated Topic.
@pytest.mark.parametrize("fault", [None, "fingerprint", "project", "metadata_identity", "text_conflict",
                                    "report_only", "uninspected", "dimensions", "rejected", "hash_alias_conflict",
                                    "inspection_alias_conflict", "inspection_alias_nonboolean",
                                    "inspection_canonical_integer", "inspection_canonical_float"])
def test_native_hermes_accepted_package_preserves_sealed_source(accepted_page_package, topic, fault, source_format):
    contract, message, source_id, review_id = accepted_page_package
    contract["identity"]["thread_id"] = topic
    sealed = {"identity": dict(contract["identity"])}
    if fault == "project":
        sealed["identity"]["project"] = "another-project"
    snapshot = json.dumps(sealed)
    fingerprint = hashlib.sha256(snapshot.encode()).hexdigest()
    if fault == "fingerprint":
        fingerprint = "0" * 64
    with kb.connect_closing() as conn:
        conn.execute("UPDATE grace_delegations SET thread_id=?, contract_snapshot=?, contract_fingerprint=?",
                     (topic, snapshot, fingerprint))
        run = kb.latest_run(conn, source_id)
        metadata = {"facebook_page_post": {"text": message}}
        if fault == "metadata_identity":
            metadata["loop_contract"] = {"identity": {"project": "another-project"}}
        if fault == "text_conflict":
            metadata["acceptance_evidence"] = {"inline_content_package": {"facebook_page_post": message.strip()}}
        if fault == "report_only":
            metadata = {"user_facing_report": {"body": message}}
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id))
        review_run = kb.latest_run(conn, review_id)
        review_metadata = review_run.metadata
        hero = review_metadata.pop("asset_review")[0]
        hero.pop("accepted")  # Canonical native verdict is on the independent review root.
        hero["pixel_dimensions"] = [hero.pop("width"), hero.pop("height")]
        hero["actual_image_inspected"] = fault != "uninspected"
        if fault == "dimensions":
            hero["pixel_dimensions"] = [True, 936]
        if fault == "rejected":
            hero["accepted"] = False
        expected_image_hash = hero["sha256"]
        if source_format == "evidence":
            hero["raw_file_sha256"] = hero.pop("sha256")
            hero["actual_image_inspected_by_review"] = hero.pop("actual_image_inspected")
        if fault == "hash_alias_conflict":
            hero.update(sha256=expected_image_hash, raw_file_sha256="0" * 64)
        if fault == "inspection_alias_conflict":
            hero.update(actual_image_inspected=False, actual_image_inspected_by_review=True)
        if fault == "inspection_alias_nonboolean":
            hero["actual_image_inspected_by_review"] = 1
        if fault in ("inspection_canonical_integer", "inspection_canonical_float"):
            hero.update(actual_image_inspected=1 if fault.endswith("integer") else 1.0,
                        actual_image_inspected_by_review=True)
        review_metadata[source_format] = {"page_hero": hero}
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run.id))
    if fault:
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        resolved = tool.bind_accepted_page_preflight_source(contract)
        assert resolved["message"].encode() == message.encode()
        assert resolved["source_field"] == "facebook_page_post.text"
        assert resolved["message_sha256"] == hashlib.sha256(message.encode()).hexdigest()
        assert resolved["image_sha256"] == expected_image_hash
        assert resolved["dimensions"] == "1664×936"


def test_empty_sealed_snapshot_cannot_fall_back_to_legacy_identity(accepted_page_package):
    contract, _, _, _ = accepted_page_package
    with kb.connect_closing() as conn:
        conn.execute("UPDATE grace_delegations SET contract_snapshot=''")
    with pytest.raises(ValueError, match="sealed contract fingerprint"):
        tool.bind_accepted_page_preflight_source(contract)


@pytest.mark.parametrize("fault", [None, "rejected", "uninspected", "different_image"])
def test_legacy_image_cannot_shadow_native_review(accepted_page_package, fault):
    contract, _, _, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        run = kb.latest_run(conn, review_id)
        metadata = run.metadata
        hero = dict(metadata["asset_review"][0])
        hero["pixel_dimensions"] = [hero.pop("width"), hero.pop("height")]
        hero["actual_image_inspected"] = fault != "uninspected"
        if fault == "rejected":
            hero["accepted"] = False
        if fault == "different_image":
            hero["sha256"] = "f" * 64
        metadata["acceptance_evidence"] = {"page_hero": hero}
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id))
    if fault:
        with pytest.raises(ValueError):
            tool.bind_accepted_page_preflight_source(contract)
    else:
        assert tool.bind_accepted_page_preflight_source(contract)["image_sha256"] == hero["sha256"]


def test_preflight_rejects_superseded_run(accepted_page_package):
    contract, _, source_id, review_id = accepted_page_package
    with kb.connect_closing() as conn:
        source = kb.latest_run(conn, source_id)
        review = kb.latest_run(conn, review_id)
        metadata = dict(review.metadata)
        metadata["workflow_review_source"] = {
            "parent_execution_task_id": source_id,
            "parent_execution_run_id": source.id,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(source),
        }
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), review.id))
        conn.execute("INSERT INTO task_runs(task_id, started_at, status) VALUES (?, ?, 'running')",
                     (source_id, source.ended_at + 100))
    with pytest.raises(ValueError, match="latest execution"):
        tool.bind_accepted_page_preflight_source(contract)
