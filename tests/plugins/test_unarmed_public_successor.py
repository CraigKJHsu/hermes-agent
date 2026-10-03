import hashlib
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from plugins.openclaw_bridge import clawops_delegate as delegate
from proactive.loop_contract import contract_fingerprint


@pytest.mark.parametrize('fault', [None, 'topic', 'lease', 'armed', 'event', 'rejected', 'evidence', 'body', 'effect', 'budget', 'source_binding', 'report', 'approval', 'terminal', 'new_execution', 'source_lane', 'source_stage', 'source_session_mismatch', 'source_session_rotation', 'crosslinked_review', 'queue', 'queue_invalid_source'])
def test_unarmed_public_successor_requires_exact_accepted_source(tmp_path, fault):
    stage = 'case_public_page_readonly_verification_r1'
    with kb.connect_closing(tmp_path / 'source.db') as conn:
        kb.create_grace_objective(conn, objective_id='go_test', platform='telegram', chat_id='chat', thread_id='2',
            session_key='session', title='Published Page', objective='Existing Page review', original_request_sha256='a' * 64,
            required_stage_keys=['repair', stage, 'finish'], terminal_stage_key='finish', acceptance_criteria=['Verified'], current_stage_key=stage)
        body = 'GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n{"external_effect_budget":0}\n```'
        execution = kb.create_task(conn, title='Source', body=body)
        review = kb.create_task(conn, title='Review', body='review body')
        for task in (execution, review): conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
        execution_run = kb._synthesize_ended_run(conn, execution, outcome='completed', metadata={'external_effects': []})
        source = {'parent_execution_run_id': execution_run, 'parent_execution_task_id': execution,
                  'parent_execution_evidence_sha256': kb.workflow_review_evidence_hash(kb.get_run(conn, execution_run)),
                  'parent_task_body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                  'review_task_body_sha256': hashlib.sha256(b'review body').hexdigest()}
        review_run = kb._synthesize_ended_run(conn, review, outcome='completed', metadata={'review_outcome': 'accepted', 'workflow_review_source': source})
        kb._append_event(conn, review, 'completed', run_id=review_run)
        event = conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
        conn.execute("UPDATE grace_objective_stages SET status='queued',delegation_id='gd_source',execution_task_id=?,review_task_id=? WHERE objective_id='go_test' AND stage_key='repair'", (execution, review))
        conn.execute("UPDATE grace_objective_stages SET status='queued',delegation_id='gd_empty' WHERE objective_id='go_test' AND stage_key=?", (stage,))
        conn.execute("""INSERT INTO grace_delegations
            (delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,session_key,session_id,resolved_route,
             approval_required,state,execution_task_id,review_task_id,objective_id,stage_key,created_at,updated_at)
            VALUES ('gd_source',?,'fixture','telegram','chat','2','session','session','{}',0,'queued',?,?,'go_test','repair',1,1)""", ('f' * 64, execution, review))
        kb.add_grace_loop_callback(conn, review_task_id=review, execution_task_id=execution, platform='telegram', chat_id='chat', thread_id='2',
            session_id='session', contract_fingerprint='f' * 64, completion_mode='intermediate', objective_id='go_test', stage_key='repair')
        conn.execute("UPDATE grace_loop_callbacks SET state='attention',attempt_event_id=?,user_report_event_id=?,user_report_delivered_at=1,user_report_chunk_count=1,user_report_total_chunks=1,user_report_next_chunk=1 WHERE review_task_id=?", (event, event, review))
        contract = {'routing': {'task_type': 'browser_readonly'}, 'external_effect_budget': 0}
        row = {'delegation_id': 'gd_empty', 'state': 'authorized', 'approval_required': 0, 'execution_task_id': None, 'review_task_id': None,
               'build_owner': None, 'build_lease_expires': None, 'contract_snapshot': json.dumps(contract), 'contract_fingerprint': contract_fingerprint(contract),
               'platform': 'telegram', 'chat_id': 'chat', 'thread_id': '2', 'session_id': 'session', 'objective_id': 'go_test', 'stage_key': stage,
               'origin_review_task_id': review, 'origin_event_id': event}
        kwargs = {'review_task_id': review, 'event_id': event, 'platform': 'telegram', 'chat_id': 'chat', 'thread_id': '2', 'session_id': 'session'}
        if fault == 'topic': kwargs['thread_id'] = 'other'
        if fault == 'lease': conn.execute("UPDATE grace_loop_callbacks SET lease_owner='active' WHERE review_task_id=?", (review,))
        if fault == 'armed': row['execution_task_id'] = 'new_task'
        if fault == 'event': kwargs['event_id'] = event + 1
        if fault == 'rejected': conn.execute("UPDATE task_runs SET metadata=json_set(metadata,'$.review_outcome','rejected') WHERE id=?", (review_run,))
        if fault == 'evidence': conn.execute("UPDATE task_runs SET metadata=json_set(metadata,'$.acceptance_evidence',json('{\"tampered\":true}')) WHERE id=?", (execution_run,))
        if fault == 'body': conn.execute("UPDATE tasks SET body='changed' WHERE id=?", (review,))
        if fault == 'effect': conn.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,created_at,updated_at) VALUES (?,'facebook','create','verified',1,1)", (execution,))
        if fault == 'budget': contract['external_effect_budget'] = 1; row['contract_snapshot'] = json.dumps(contract); row['contract_fingerprint'] = contract_fingerprint(contract)
        if fault == 'source_binding': conn.execute("UPDATE grace_delegations SET execution_task_id='other' WHERE delegation_id='gd_source'")
        if fault == 'report': conn.execute('UPDATE grace_loop_callbacks SET user_report_next_chunk=0 WHERE review_task_id=?', (review,))
        if fault == 'approval': row['approval_required'] = 1
        if fault == 'terminal': conn.execute("UPDATE grace_objectives SET terminal_stage_key=? WHERE objective_id='go_test'", (stage,))
        if fault == 'new_execution': kb._synthesize_ended_run(conn, execution, outcome='completed', metadata={'external_effects': []})
        if fault == 'source_lane': conn.execute("UPDATE grace_delegations SET thread_id='other' WHERE delegation_id='gd_source'")
        if fault == 'source_stage': conn.execute("UPDATE grace_delegations SET stage_key='other' WHERE delegation_id='gd_source'")
        if fault == 'source_session_mismatch': conn.execute("UPDATE grace_delegations SET session_id='other' WHERE delegation_id='gd_source'")
        if fault == 'source_session_rotation':
            conn.execute("UPDATE grace_loop_callbacks SET state='delivering',lease_owner='owner',lease_event_id=?,lease_expires=? WHERE review_task_id=?", (event, int(time.time()) + 60, review))
            kb.rebind_active_grace_callback_session(conn, review_task_id=review, event_id=event, platform='telegram', chat_id='chat', thread_id='2', session_id='rotated', lease_owner='owner')
            conn.execute("UPDATE grace_loop_callbacks SET state='attention',lease_owner=NULL,lease_event_id=NULL,lease_expires=NULL WHERE review_task_id=?", (review,))
            row['session_id'] = kwargs['session_id'] = 'rotated'
        if fault == 'crosslinked_review':
            other = kb.create_task(conn, title='Unrelated accepted review', body='review body')
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
            other_run = kb._synthesize_ended_run(conn, other, outcome='completed', metadata={'review_outcome': 'accepted', 'workflow_review_source': source})
            conn.execute('UPDATE task_events SET run_id=? WHERE id=?', (other_run, event))
            conn.execute("UPDATE grace_objective_stages SET review_task_id=? WHERE objective_id='go_test' AND stage_key='repair'", (other,))
            conn.execute("UPDATE grace_delegations SET review_task_id=? WHERE delegation_id='gd_source'", (other,))
        callback = kb.get_grace_loop_callback(conn, review)
        before = dict(callback)
        if fault in ('queue', 'queue_invalid_source'):
            conn.execute("""INSERT INTO grace_delegations
                (delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,session_key,session_id,resolved_route,
                 approval_required,state,objective_id,stage_key,origin_review_task_id,origin_event_id,contract_snapshot,created_at,updated_at)
                VALUES ('gd_empty',?,'queue-fixture','telegram','chat','2','session','session','{}',0,'authorized','go_test',?,?,?,?,1,1)""",
                (row['contract_fingerprint'], stage, review, event, row['contract_snapshot']))
            assert kb.claim_grace_delegation_build(conn, delegation_id='gd_empty', build_owner='builder')
            successor = kb.create_task(conn, title='Readonly successor', body='sealed successor')
            successor_review = kb.create_task(conn, title='Independent review', body='review')
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (successor,))
            if fault == 'queue_invalid_source':
                conn.execute("UPDATE grace_loop_callbacks SET user_report_next_chunk=0 WHERE review_task_id=?", (review,))
                before = dict(kb.get_grace_loop_callback(conn, review))
                with pytest.raises(ValueError):
                    kb.mark_grace_delegation_queued(conn, delegation_id='gd_empty', build_owner='builder', execution_task_id=successor, review_task_id=successor_review)
                assert kb.get_grace_delegation(conn, delegation_id='gd_empty')['state'] == 'building'
                assert kb.get_task(conn, successor).status == 'blocked'
            else:
                queued = kb.mark_grace_delegation_queued(conn, delegation_id='gd_empty', build_owner='builder', execution_task_id=successor, review_task_id=successor_review)
                assert queued['state'] == 'queued'
                assert kb.get_task(conn, successor).status == 'ready'
                assert queued['execution_task_id'] == successor
            assert dict(kb.get_grace_loop_callback(conn, review)) == before
            return
        if fault in (None, 'source_session_rotation'):
            delegate._validate_fresh_reconcile_callback(conn, row=row, callback=callback, callback_kwargs=kwargs, retry_existing=False)
        else:
            with pytest.raises(ValueError):
                delegate._validate_fresh_reconcile_callback(conn, row=row, callback=callback, callback_kwargs=kwargs, retry_existing=False)
        assert dict(kb.get_grace_loop_callback(conn, review)) == before
