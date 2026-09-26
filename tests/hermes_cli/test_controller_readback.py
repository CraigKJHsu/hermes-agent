import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.controller_readback import task_controller_readback


@pytest.fixture
def control_db(tmp_path):
    with kb.connect_closing(tmp_path / 'control.db') as conn:
        execution = kb.create_task(conn, title='execution', assignee='clawops-ops')
        review = kb.create_task(conn, title='review', parents=(execution,), executor_profile='grace-policy-review')
        kb.create_grace_objective(conn, objective_id='objective', platform='telegram', chat_id='chat',
            session_key='session', title='check', objective='check connection',
            original_request_sha256='a' * 64, required_stage_keys=['prepare', 'execute'],
            terminal_stage_key='execute', acceptance_criteria=['verified'])
        with kb.write_txn(conn):
            conn.execute('''INSERT INTO grace_delegations
                (delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,
                 session_key,session_id,resolved_route,objective_id,stage_key,state,
                 execution_task_id,review_task_id,created_at,updated_at)
                VALUES ('delegation',?,'request','telegram','chat','session','session','ops',
                        'objective','prepare','queued',?,?,1,1)''', ('b' * 64, execution, review))
            conn.execute('''UPDATE grace_objective_stages SET delegation_id='delegation',
                execution_task_id=?,review_task_id=? WHERE objective_id='objective' AND stage_key='prepare' ''',
                (execution, review))
        yield conn, execution, review


def test_controller_binding_spine_and_history(control_db):
    conn, execution, review = control_db
    evidence = task_controller_readback(conn, execution)
    assert evidence['binding_verified'] is True
    assert evidence['task']['assignee'] == 'clawops-ops'
    assert [s['stage_key'] for s in evidence['stages']] == ['prepare', 'execute']
    assert evidence['delegation']['review_task_id'] == review
    assert task_controller_readback(conn, execution)['history']['sha256'] == evidence['history']['sha256']
    with kb.write_txn(conn):
        kb._append_event(conn, execution, 'diagnostic_observed')
    changed = task_controller_readback(conn, execution)
    assert changed['history']['sha256'] != evidence['history']['sha256']
    assert changed['external_effect_count'] == 0
    assert 'controller_zero_effect_check' not in changed


def test_mismatched_stage_never_attests_binding(control_db):
    conn, execution, review = control_db
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_objective_stages SET review_task_id='wrong' WHERE objective_id='objective'")
    assert task_controller_readback(conn, execution)['binding_verified'] is False


def test_show_controller_view(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from tools import kanban_tools
    with kb.connect() as conn:
        task = kb.create_task(conn, title='legacy', assignee='clawops-ops')
    result = json.loads(kanban_tools._handle_show({'task_id': task, 'view': 'controller'}))
    assert result['source'] == 'controller_database'
    assert result['binding_verified'] is False
    assert result['delegation_count'] == 0


@pytest.mark.parametrize('status', ['ready', 'running', 'done'])
def test_callback_waits_for_in_place_correction(control_db, status):
    conn, execution, review = control_db
    kb.add_grace_loop_callback(conn, execution_task_id=execution, review_task_id=review,
        platform='telegram', chat_id='chat', contract_fingerprint='b' * 64)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, execution))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (review,))
        kb._append_event(conn, review, 'dependency_wait')
    event = conn.execute('SELECT max(id) FROM task_events WHERE task_id=?', (review,)).fetchone()[0]
    assert kb._grace_execution_callback_review_pending(conn, review_task_id=review, event_id=event)
    assert not kb.claim_grace_loop_callback(conn, review_task_id=review, event_id=event, lease_owner='callback')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution,))
    assert not kb._grace_execution_callback_review_pending(conn, review_task_id=review, event_id=event)


def test_readback_preserves_existing_transaction(control_db):
    conn, execution, _ = control_db
    conn.execute('BEGIN')
    conn.execute("UPDATE tasks SET title='uncommitted' WHERE id=?", (execution,))
    task_controller_readback(conn, execution)
    assert conn.in_transaction
    assert conn.execute('SELECT title FROM tasks WHERE id=?', (execution,)).fetchone()[0] == 'uncommitted'
    conn.rollback()
    task_controller_readback(conn, execution)
    assert not conn.in_transaction


def test_history_digest_covers_content_and_bounds_tail(control_db):
    conn, execution, _ = control_db
    before = task_controller_readback(conn, execution)['history']['sha256']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body='changed content' WHERE id=?", (execution,))
        for _ in range(105):
            kb._append_event(conn, execution, 'observed')
    result = task_controller_readback(conn, execution)
    assert before != result['history']['sha256']
    assert result['history']['event_count'] > 100
    assert len(result['history']['events']) == 100
    assert result['history']['truncated']


def test_named_history_readbacks_are_same_lane_and_non_recursive(control_db):
    conn, execution, review = control_db
    foreign = kb.create_task(conn, title='other Topic')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET idempotency_key='sealed-key' WHERE id=?", (execution,))
        conn.execute('UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id=?',
                     (json.dumps({'verification': [review, foreign, 't_00000000']}), 'delegation'))
        conn.execute('''INSERT INTO grace_delegations
          (delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,
           session_key,session_id,resolved_route,state,execution_task_id,created_at,updated_at)
          VALUES ('foreign',?,'foreign','telegram','chat','another-topic','session','session','ops',
                  'queued',?,1,1)''', ('c'*64, foreign))
        conn.execute('''INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at,metadata)
          VALUES (?,'default','done','completed',1,2,?)''',
          (review,json.dumps({'review_outcome':'accepted','verification':{'baseline':'recorded'}})))
    observed = task_controller_readback(conn, execution)
    assert observed['task']['idempotency_key'] == 'sealed-key'
    assert [r['task']['id'] for r in observed['referenced_task_readbacks']] == [review]
    historical = observed['referenced_task_readbacks'][0]
    assert historical['history_sha256'] == task_controller_readback(conn, review)['history']['sha256']
    assert historical['recorded_review']['verification'] == {'baseline': 'recorded'}
    assert historical['recorded_review']['source'] == 'completed_review_run_metadata'
    assert 'referenced_task_readbacks' not in historical


def test_native_baseline_captures_then_independently_detects_history_changes(control_db):
    from hermes_cli.controller_readback import native_history_baseline, compare_native_history_baseline
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot
    conn, execution, review = control_db
    with kb.write_txn(conn):
        conn.execute('UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id=?',
                     (json.dumps({'verification': [review]}), 'delegation'))
    claimed = kb.claim_task(conn, execution)
    assert claimed.current_run_id
    contract = {'objective_ref': {'objective_id':'objective','stage_key':'prepare'},
        'identity': {'platform':'telegram','chat_id':'chat','thread_id':''},
        'control_plane_receipt': {'execution_task_id': execution,'delegation_id':'delegation',
                                 'grace_review_task_id':review}}
    baseline = _objective_durable_evidence_snapshot(conn, contract)['controller_history_baseline']
    assert baseline['tasks'][0]['task_id'] == review
    altered_projection = {**contract, 'goal': {'objective': 't_00000000'},
        'referenced_task_ids': ['t_00000000'], 'contract_fingerprint': 'forged'}
    controller_only = native_history_baseline(conn, altered_projection)
    assert controller_only['tasks'] == baseline['tasks']
    assert controller_only['delegation_contract_fingerprint'] == 'b' * 64
    assert 'contract_fingerprint' not in controller_only

    comparison = compare_native_history_baseline(task_controller_readback(conn, execution),
        baseline, execution_run_id=claimed.current_run_id)
    assert comparison['all_referenced_tasks_unchanged']
    advanced = task_controller_readback(conn, execution)
    advanced['task']['current_run_id'] = claimed.current_run_id + 1
    assert not compare_native_history_baseline(advanced, baseline,
        execution_run_id=claimed.current_run_id)['baseline_binding_verified']
    assert not compare_native_history_baseline(task_controller_readback(conn, execution),
        baseline, execution_run_id=claimed.current_run_id+1)['baseline_binding_verified']
    before_hash = baseline['tasks'][0]['history_sha256']
    kb.add_comment(conn, review, 'reviewer', 'changed after baseline')
    comparison = compare_native_history_baseline(task_controller_readback(conn, execution),
        baseline, execution_run_id=claimed.current_run_id)
    assert comparison['baseline_binding_verified']
    assert not comparison['all_referenced_tasks_unchanged']
    assert comparison['comparisons'][0]['history_sha256'] == before_hash
    assert kb.complete_task(conn, execution, result='complete', expected_run_id=claimed.current_run_id)
    terminal = task_controller_readback(conn, execution)
    assert terminal['task']['current_run_id'] is None
    assert compare_native_history_baseline(terminal, baseline,
        execution_run_id=claimed.current_run_id)['baseline_binding_verified']
    contract['control_plane_receipt']['delegation_id'] = 'foreign'
    with pytest.raises(ValueError, match='controller binding'):
        native_history_baseline(conn, contract)


def test_native_history_comparison_never_invents_missing_baseline(control_db):
    from hermes_cli.controller_readback import compare_native_history_baseline
    conn, execution, _ = control_db
    assert not compare_native_history_baseline(task_controller_readback(conn, execution),
        None, execution_run_id=1)['all_referenced_tasks_unchanged']


def test_execution_metadata_cannot_impersonate_review(control_db):
    conn, execution, review = control_db
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",
                     (json.dumps({"verification": [execution]}),))
        conn.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at,metadata) VALUES (?,'default','done','completed',1,2,?)",
                     (execution, json.dumps({"review_outcome": "accepted", "verification": {"forged": True}})))
    result = task_controller_readback(conn, review)
    assert result["referenced_task_readbacks"][0]["task"]["id"] == execution
    assert result["referenced_task_readbacks"][0]["recorded_review"] is None
