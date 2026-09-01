import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive import grace_memory_promotion as promotion
from proactive.model_routing import (
    attest_runtime_execution,
    route_grace,
    routing_env,
)
from proactive.policy_registry import create_policy_version


NAMESPACE = "telegram:-1003938559457:2680/dementia_care"
PREFERENCE = "照顧溝通先安定情緒、使用固定短句，保留媽媽真實角色與尊嚴。"


def _review_body() -> str:
    contract = {
        "memory": {
            "namespace": NAMESPACE,
            "working": ["not promoted"],
            "promote_on_acceptance": [PREFERENCE],
        }
    }
    return "\n".join(
        [
            "GRACE_LOOP_CONTRACT_STAGE: grace_review",
            "```json",
            json.dumps(contract, ensure_ascii=False),
            "```",
        ]
    )


def _write_memory_config(home: Path, *, limit: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "\n".join(
            [
                "memory:",
                f"  memory_char_limit: {limit}",
                "  user_char_limit: 1375",
                "  promotion:",
                "    warn_usage_ratio: 0.75",
                "    critical_usage_ratio: 0.90",
                "    critical_merge_chars: 160",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _attest_review_route(conn, review_id: str, home: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "managed-policies"
        / "missioncrew-model-routing-v1.json"
    ).read_text(encoding="utf-8")
    create_policy_version(
        "missioncrew-model-routing-v1",
        "v1",
        source,
        owner_scope="global",
        owner_id="missioncrew",
        activate=True,
        expected_active_version=None,
    )
    route = route_grace("acceptance_review")
    conn.execute(
        "UPDATE tasks SET model_override = ?, routing_decision = ? WHERE id = ?",
        (
            route["requested_model"],
            json.dumps({"selected_backend": "hermes", "model_route": route}),
            review_id,
        ),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", review_id)
    for key, value in routing_env(route, task_id=review_id).items():
        monkeypatch.setenv(key, value)
    attest_runtime_execution(
        model=route["requested_model"],
        reasoning_effort=route["reasoning_effort"],
        api_mode="codex_responses",
    )
    assert os.environ.get("HERMES_MODEL_ROUTING_RECEIPT")


def test_accepted_review_completion_queues_exact_memory_outbox(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        review_id = kb.create_task(
            conn,
            title="Grace review",
            body=_review_body(),
            assignee="default",
        )
        _attest_review_route(conn, review_id, tmp_path / "hermes", monkeypatch)
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={
                "review_outcome": "accepted",
                "memory_promotion": {"state": "done"},
            },
        )
        row = conn.execute(
            "SELECT * FROM grace_memory_promotions WHERE review_task_id = ?",
            (review_id,),
        ).fetchone()
        run = kb.latest_run(conn, review_id)

    assert row is not None
    assert row["namespace"] == NAMESPACE
    assert json.loads(row["entries"]) == [PREFERENCE]
    assert row["state"] == "pending"
    assert run.metadata["memory_promotion"]["state"] == "pending"
    assert run.metadata["memory_promotion"]["promotion_id"] == row["id"]


def test_memory_outbox_claim_and_finish_updates_run_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        review_id = kb.create_task(
            conn,
            title="Grace review",
            body=_review_body(),
            assignee="default",
        )
        _attest_review_route(conn, review_id, tmp_path / "hermes", monkeypatch)
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        claimed = kb.claim_due_grace_memory_promotions(
            conn,
            task_id=review_id,
            lease_owner="test-owner",
        )
        assert len(claimed) == 1
        assert kb.finish_grace_memory_promotion(
            conn,
            claimed[0]["id"],
            lease_owner="test-owner",
            result={
                "complete": True,
                "pending_targets": [],
                "archive_verified": True,
                "mem0_verified": True,
                "prompt_verified": True,
            },
            error=None,
            retry_seconds=1800,
        )
        finished = kb.get_grace_memory_promotion(conn, claimed[0]["id"])
        run = kb.latest_run(conn, review_id)

    assert finished["state"] == "done"
    assert run.metadata["memory_promotion"]["state"] == "done"
    assert run.metadata["memory_promotion"]["archive_verified"] is True


def test_critical_prompt_capacity_merges_same_namespace_and_archives(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_memory_config(home, limit=260)
    memory_dir = home / "memories"
    memory_dir.mkdir(parents=True)
    old = f"{NAMESPACE}：本 Topic 是失智照護；限本 Topic。"
    filler = "其他專案固定規則：" + ("甲" * 157)
    (memory_dir / "MEMORY.md").write_text(
        old + promotion.ENTRY_DELIMITER + filler,
        encoding="utf-8",
    )
    assert promotion.prompt_memory_capacity()["level"] == "critical"

    result = promotion.promote_claimed_record(
        {
            "id": "gmp_test",
            "review_task_id": "t_review",
            "namespace": NAMESPACE,
            "entries": json.dumps([PREFERENCE], ensure_ascii=False),
        }
    )

    assert result["complete"] is True, json.dumps(result, ensure_ascii=False)
    assert result["archive_verified"] is True
    assert result["prompt_verified"] is True
    assert result["mem0_status"] == "not_configured"
    persisted = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert PREFERENCE.rstrip("。") in persisted
    assert "本 Topic 是失智照護" in persisted
    archive = Path(result["archive_path"]).read_text(encoding="utf-8")
    assert NAMESPACE in archive
    assert PREFERENCE in archive


def test_critical_prompt_capacity_collapses_legacy_namespace_entries_atomically(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_memory_config(home, limit=360)
    memory_dir = home / "memories"
    memory_dir.mkdir(parents=True)
    old_one = f"{NAMESPACE}：情緒先行。"
    old_two = f"{NAMESPACE}：固定短句並保留真實角色。"
    unrelated = "其他專案固定規則：" + ("甲" * 210)
    (memory_dir / "MEMORY.md").write_text(
        promotion.ENTRY_DELIMITER.join([old_one, old_two, unrelated]),
        encoding="utf-8",
    )
    assert promotion.prompt_memory_capacity()["level"] == "critical"

    result = promotion.promote_claimed_record(
        {
            "id": "gmp_legacy_merge",
            "review_task_id": "manual_approved_migration",
            "namespace": NAMESPACE,
            "entries": ["本 Topic 照顧溝通：情緒先行、固定短句、保留真實角色與尊嚴。", old_one, old_two],
        }
    )

    assert result["complete"] is True, json.dumps(result, ensure_ascii=False)
    assert result["archive_verified"] is True
    assert result["prompt_verified"] is True
    persisted = promotion.load_on_disk_store().memory_entries
    matching = [item for item in persisted if item.startswith(f"{NAMESPACE}：")]
    assert matching == [result["prompt_entry"]]
    assert unrelated in persisted
    archive = Path(result["archive_path"]).read_text(encoding="utf-8")
    assert old_one in archive
    assert old_two in archive


def test_critical_prompt_capacity_without_namespace_defers_but_archives(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_memory_config(home, limit=220)
    memory_dir = home / "memories"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("甲" * 210, encoding="utf-8")

    result = promotion.promote_claimed_record(
        {
            "id": "gmp_test_deferred",
            "review_task_id": "t_review",
            "namespace": NAMESPACE,
            "entries": [PREFERENCE],
        }
    )

    assert result["complete"] is False
    assert result["archive_verified"] is True
    assert result["prompt_deferred"] is True
    assert result["pending_targets"] == ["prompt_memory"]
    assert PREFERENCE in Path(result["archive_path"]).read_text(encoding="utf-8")
