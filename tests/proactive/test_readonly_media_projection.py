import json
import pytest
from proactive.readonly_media_projection import compact_public_page_readonly_snapshot


def test_public_page_projection_omits_archived_media_but_keeps_source_status():
    snapshot = {
        "stage_key": "fico_ep12_public_page_readonly_verification_r1_r13",
        "objective_id": "go_source", "stages_total": 3,
        "stages": [
            {"stage_key": "prepare_full_package_r6", "outcome_kind": "approval_blocked"},
            {"stage_key": "fico_page_hero_evidence_repair_r2", "status": "done"},
            {"stage_key": "fico_ep12_public_page_readonly_verification_r1_r13", "status": "ready"},
        ],
        "external_effects": [{"external_id": "existing_post", "state": "verified"}],
        "handoff_comments": [{"body": "old /Users/kj/media/ten.png is not new evidence"}],
        "task_runs": [{"summary": "previous blocked review"}],
    }
    compact = compact_public_page_readonly_snapshot(snapshot)
    assert compact["stages"] == snapshot["stages"]
    assert compact["external_effects"] == snapshot["external_effects"]
    assert compact["stages_total"] == 3
    assert "handoff_comments" not in compact
    assert "task_runs" not in compact
    assert "ten.png" not in json.dumps(compact)
    assert snapshot["handoff_comments"]  # source not modified


def test_other_objective_stages_remain_lossless():
    snapshot = {"stage_key": "prepare_full_package", "handoff_comments": ["do not trim"]}
    assert compact_public_page_readonly_snapshot(snapshot) is snapshot


def test_projection_preserves_all_stage_states_and_explicit_requested_evidence():
    snapshot = {
        "stage_key": "case_public_page_readonly_verification_r1",
        "stages": [{"stage_key": "unrelated_prior_stage", "status": "blocked"}],
        "requested_run_evidence": [{"run_id": 17, "acceptance_state": "not_established_by_this_snapshot"}],
        "publication_progress": {"status": "active"},
    }
    compact = compact_public_page_readonly_snapshot(snapshot)
    for key in ("stages", "requested_run_evidence", "publication_progress"):
        assert compact[key] == snapshot[key]


@pytest.mark.parametrize('task_type,budget,public_stage,large_current', [
    ('browser_readonly', 0, True, False), ('devops', 0, True, False), ('browser_readonly', 1, True, False),
    ('browser_readonly', 0, False, False), ('browser_readonly', 0, True, True),
])
def test_public_snapshot_projects_before_archived_history_budget(tmp_path, task_type, budget, public_stage, large_current):
    from hermes_cli import kanban_db as kb
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot
    stage = 'case_public_page_readonly_verification_r1' if public_stage else 'other_stage'
    with kb.connect_closing(tmp_path / 'snapshot.db') as conn:
        kb.create_grace_objective(conn, objective_id='go_public', platform='telegram', chat_id='chat', thread_id='2',
            session_key='session', title='Published Page', objective='Verify existing Page', original_request_sha256='a' * 64,
            required_stage_keys=['prepare', stage], terminal_stage_key=stage, acceptance_criteria=['Verified'], current_stage_key=stage)
        parent = kb.create_task(conn, title='Old execution')
        conn.execute("UPDATE grace_objective_stages SET status='done',execution_task_id=? WHERE objective_id='go_public' AND stage_key='prepare'", (parent,))
        kb.add_comment(conn, parent, 'worker', 'old media /tmp/image.png ' + 'x' * 100000)
        conn.executemany('INSERT INTO task_runs(task_id,status,outcome,summary,error,started_at,ended_at) VALUES(?,?,?,?,?,?,?)',
                         [(parent, 'blocked', 'blocked', 's' * 1200, 'e' * 900, i, i + 1) for i in range(80)])
        conn.execute('INSERT INTO task_external_effects(task_id,platform,effect_key,state,external_id,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                     (parent, 'facebook', 'existing', 'verified', 'post_1', 1, 1, 1))
        if large_current:
            conn.execute("UPDATE grace_objectives SET next_action=? WHERE objective_id='go_public'", ('x' * (256 * 1024 + 1),))
        conn.commit()
        contract = {'objective_ref': {'objective_id': 'go_public', 'stage_key': stage},
                    'identity': {'platform': 'telegram', 'chat_id': 'chat', 'thread_id': '2'},
                    'routing': {'task_type': task_type}, 'external_effect_budget': budget}
        if task_type == 'browser_readonly' and budget == 0 and public_stage and not large_current:
            snapshot = _objective_durable_evidence_snapshot(conn, contract)
            assert snapshot['stages'][0]['execution_task_id'] == parent
            assert snapshot['external_effects'][0]['external_id'] == 'post_1'
            assert snapshot['historical_evidence_omitted']
            assert 'task_runs' not in snapshot and 'handoff_comments' not in snapshot
            assert len(json.dumps(snapshot).encode()) < 256 * 1024
            assert conn.execute('SELECT length(body) FROM task_comments WHERE task_id=?', (parent,)).fetchone()[0] > 100000
        else:
            with pytest.raises(ValueError, match='snapshot exceeds inline byte budget'):
                _objective_durable_evidence_snapshot(conn, contract)
