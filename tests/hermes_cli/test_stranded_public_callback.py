import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb
from proactive.loop_contract import contract_fingerprint


@pytest.mark.parametrize('fault', [None, 'approval', 'effect', 'body', 'new_run', 'armed', 'topic', 'lease', 'reported', 'terminal', 'unrelated', 'budget_bool', 'fingerprint', 'native_phase', 'native_receipt', 'native_card', 'native_snapshot', 'session_key'])
def test_exact_stranded_public_capability_callback_recovery(tmp_path, fault):
    stage = 'case_public_page_readonly_verification_r1_r13'
    target = 'case_public_page_readonly_verification_r1_r14'
    error = 'Grace callback delivery failed: ValueError: Objective current stage changed without an exact callback successor'
    with kb.connect_closing(tmp_path / 'source.db') as conn:
        kb.create_grace_objective(conn, objective_id='go_test', platform='telegram', chat_id='chat', thread_id='2',
            session_key='session', title='Published Page', objective='Readonly verify', original_request_sha256='a' * 64,
            required_stage_keys=[stage, target, 'finish'], terminal_stage_key='finish', acceptance_criteria=['Verified'], current_stage_key=target)
        body = 'GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n{"external_effect_budget":0,"routing":{"task_type":"browser_readonly"},"identity":{"project":"fixture"}}\n```'
        execution = kb.create_task(conn, title='Failed readonly', body=body, initial_status='blocked')
        review = kb.create_task(conn, title='Independent rejected review', body='review', initial_status='blocked')
        conn.execute("UPDATE tasks SET block_kind='capability' WHERE id IN (?,?)", (execution, review))
        contract = kb._grace_compiled_contract(body)
        fingerprint = contract_fingerprint(contract)
        execution_run = kb._synthesize_ended_run(conn, execution, outcome='blocked', metadata={
            'external_effect_budget': 0, 'external_effects': [], 'contract_fingerprint': fingerprint,
            'execution_card_fingerprint': fingerprint, 'delegation_id': 'gd_source'})
        lineage = {'parent_execution_run_id': execution_run, 'parent_execution_task_id': execution,
                   'parent_execution_evidence_sha256': kb.workflow_review_evidence_hash(kb.get_run(conn, execution_run)),
                   'parent_task_body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                   'review_task_body_sha256': hashlib.sha256(b'review').hexdigest()}
        review_run = kb._synthesize_ended_run(conn, review, outcome='blocked', metadata={'review_outcome': 'rejected', 'workflow_review_source': lineage})
        kb._append_event(conn, review, 'blocked', {'kind': 'capability'}, run_id=review_run)
        event = conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
        conn.execute("UPDATE grace_objective_stages SET status='queued',delegation_id='gd_source',execution_task_id=?,review_task_id=? WHERE objective_id='go_test' AND stage_key=?", (execution, review, stage))
        conn.execute("""INSERT INTO grace_delegations
            (delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,session_key,session_id,resolved_route,
             approval_required,state,execution_task_id,review_task_id,objective_id,stage_key,created_at,updated_at)
            VALUES ('gd_source',?,'fixture','telegram','chat','2','session','session','{}',0,'queued',?,?,'go_test',?,1,1)""", (fingerprint, execution, review, stage))
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='gd_source'", (json.dumps(contract),))
        kb.add_grace_loop_callback(conn, review_task_id=review, execution_task_id=execution, platform='telegram', chat_id='chat', thread_id='2',
            session_key='session', session_id='session', contract_fingerprint=fingerprint, completion_mode='intermediate', objective_id='go_test', stage_key=stage)
        conn.execute("UPDATE grace_loop_callbacks SET state='attention',attempts=3,attempt_event_id=?,last_error=? WHERE review_task_id=?", (event, error, review))
        if fault in ('native_phase', 'native_receipt', 'native_card', 'native_snapshot'):
            snapshot = {'external_effect_budget': 0, 'routing': {'task_type': 'browser_readonly'}, 'normalization': 'native'}
            conn.execute("UPDATE grace_delegations SET contract_snapshot=?,contract_fingerprint=? WHERE delegation_id='gd_source'",
                         (json.dumps(snapshot), contract_fingerprint(snapshot)))
            metadata = kb.get_run(conn, execution_run).metadata | {
                'contract_fingerprint': fingerprint, 'delegation_id': 'gd_source',
                'execution_card_fingerprint': contract_fingerprint(kb._grace_compiled_contract(body))}
            if fault == 'native_receipt': metadata['contract_fingerprint'] = 'bad'
            if fault == 'native_card': metadata['execution_card_fingerprint'] = 'bad'
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), execution_run))
            if fault == 'native_snapshot': conn.execute("UPDATE grace_delegations SET contract_snapshot='{}' WHERE delegation_id='gd_source'")
        if fault == 'session_key': conn.execute("UPDATE grace_delegations SET session_key='other' WHERE delegation_id='gd_source'")
        if fault == 'fingerprint': conn.execute("UPDATE grace_loop_callbacks SET contract_fingerprint=? WHERE review_task_id=?", ('b' * 64, review))
        if fault == 'approval': conn.execute("UPDATE grace_delegations SET approval_required=1 WHERE delegation_id='gd_source'")
        if fault == 'effect': conn.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,created_at,updated_at) VALUES (?,'facebook','create','verified',1,1)", (execution,))
        if fault == 'body': conn.execute("UPDATE tasks SET body='tampered' WHERE id=?", (review,))
        if fault == 'new_run': kb._synthesize_ended_run(conn, execution, outcome='blocked', metadata={'external_effect_budget': 0, 'external_effects': []})
        if fault == 'armed': conn.execute("UPDATE grace_objective_stages SET delegation_id='another' WHERE objective_id='go_test' AND stage_key=?", (target,))
        if fault == 'topic': conn.execute("UPDATE grace_loop_callbacks SET thread_id='other' WHERE review_task_id=?", (review,))
        if fault == 'lease': conn.execute("UPDATE grace_loop_callbacks SET lease_owner='active' WHERE review_task_id=?", (review,))
        if fault == 'reported': conn.execute("UPDATE grace_loop_callbacks SET user_report_event_id=? WHERE review_task_id=?", (event, review))
        if fault == 'terminal': conn.execute("UPDATE grace_objectives SET terminal_stage_key=? WHERE objective_id='go_test'", (target,))
        if fault == 'unrelated': conn.execute("UPDATE grace_objective_stages SET stage_key='another_family_r14' WHERE objective_id='go_test' AND stage_key=?", (target,)); conn.execute("UPDATE grace_objectives SET current_stage_key='another_family_r14' WHERE objective_id='go_test'")
        if fault == 'budget_bool':
            conn.execute("UPDATE task_runs SET metadata=json_set(metadata,'$.external_effect_budget',json('false')) WHERE id=?", (execution_run,))
            changed = kb.workflow_review_evidence_hash(kb.get_run(conn, execution_run))
            conn.execute("UPDATE task_runs SET metadata=json_set(metadata,'$.workflow_review_source.parent_execution_evidence_sha256',?) WHERE id=?", (changed, review_run))
        before = kb.get_grace_loop_callback(conn, review)
        stage_before = [tuple(r) for r in conn.execute("SELECT * FROM grace_objective_stages")]
        run_before = [tuple(r) for r in conn.execute("SELECT * FROM task_runs")]
        if fault in (None, 'native_phase'):
            assert kb._stranded_public_capability_callback_is_idle(conn, before)
            assert kb.retry_delivered_grace_loop_callback_after_control_plane_repair(conn, review_task_id=review, event_id=event, expected_error=error)
            after = kb.get_grace_loop_callback(conn, review)
            assert after['state'] == 'pending' and after['attempts'] == 0 and after['last_error'] is None
            assert {k:v for k,v in before.items() if k not in ('state','attempts','last_error')} == {k:v for k,v in after.items() if k not in ('state','attempts','last_error')}
        else:
            assert not kb._stranded_public_capability_callback_is_idle(conn, before)
            with pytest.raises(ValueError): kb.retry_delivered_grace_loop_callback_after_control_plane_repair(conn, review_task_id=review, event_id=event, expected_error=error)
            assert kb.get_grace_loop_callback(conn, review) == before
        assert [tuple(r) for r in conn.execute('SELECT * FROM grace_objective_stages')] == stage_before
        assert [tuple(r) for r in conn.execute('SELECT * FROM task_runs')] == run_before


from tests.proactive.test_behavior_profiles import runtime_review_profile
from proactive.behavior_profiles import registry as br


def test_stranded_callback_migration_preserves_rejected_review(tmp_path, monkeypatch, runtime_review_profile):
    retry = kb.retry_delivered_grace_loop_callback_after_control_plane_repair

    def migrate_then_retry(conn, **kwargs):
        previous = br._make_pin('ai_bizweek', 'v62', {}, project_namespace='fixture')
        conn.execute("INSERT INTO grace_objective_behavior_pins(objective_id,pin,policy) VALUES('go_test',?,'{}')", (json.dumps(previous),))
        before = {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] for table in
                  ('tasks', 'task_runs', 'grace_delegations', 'grace_loop_callbacks', 'grace_objective_stages')}
        args = dict(objective_id='go_test', platform='telegram', chat_id='chat', thread_id='2',
                    profile_id='ai_bizweek', version='v86', expected_revision=1, expected_pin_hash=br.digest(previous),
                    reason='Exact stranded callback repair', kernel_repair_review_task_id=kwargs['review_task_id'])
        proposed = br.migrate_objective(conn, **args, apply=False)
        assert not proposed['applied'] and br.get_pin(conn, 'go_test') == previous
        result = br.migrate_objective(conn, **args, apply=True)
        assert result['next_revision'] == 2 and result['next_pin']['generation'] == previous['generation'] + 1
        for table, rows in before.items():
            assert [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] == rows
        receipt = json.loads(conn.execute("SELECT reason FROM grace_behavior_migrations WHERE objective_id='go_test'").fetchone()[0])
        assert receipt['review_repin_applied'] is False
        assert receipt['review_task_id'] == kwargs['review_task_id']
        return retry(conn, **kwargs)

    monkeypatch.setattr(kb, 'retry_delivered_grace_loop_callback_after_control_plane_repair', migrate_then_retry)
    test_exact_stranded_public_capability_callback_recovery(tmp_path, None)
