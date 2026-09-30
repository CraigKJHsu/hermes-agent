import json
import hashlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.controller_readback import task_controller_readback


def test_controller_readback_exposes_verified_attachment_hash(control_db, tmp_path, monkeypatch):
    conn, execution, _ = control_db
    root = tmp_path / 'attachments'
    path = root / execution / 'probe.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"status":"ok"}')
    monkeypatch.setattr(kb, 'attachments_root', lambda board=None: root)
    attachment_id = kb.add_attachment(
        conn, execution, filename='probe.json', stored_path=str(path),
        content_type='application/json', size=path.stat().st_size,
    )
    observed = task_controller_readback(conn, execution)
    assert observed['attachment_files'] == [{
        'id': attachment_id, 'filename': 'probe.json', 'stored_path': str(path),
        'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }]
    path.write_text('changed')
    changed = task_controller_readback(conn, execution)['attachment_files'][0]
    assert observed['attachment_files'][0]['sha256'] != changed['sha256']
    assert changed['size'] == len(b'changed')
    for index in range(32):
        extra = path.parent / f'extra-{index}.json'
        extra.write_text('{}')
        kb.add_attachment(
            conn, execution, filename=extra.name, stored_path=str(extra),
            content_type='application/json', size=2,
        )
    bounded = task_controller_readback(conn, execution)
    assert len(bounded['attachment_files']) == 32
    assert bounded['attachment_files_truncated'] is True
    assert bounded['attachments_available'] is False


def test_controller_readback_extracts_gateway_model_from_verified_attachment(control_db, tmp_path, monkeypatch):
    conn, execution, _ = control_db
    root = tmp_path / 'attachments'
    path = root / execution / 'gateway.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'status': 'ok', 'result': {'meta': {'agentMeta': {'model': 'gpt-6-sol'}}}}))
    monkeypatch.setattr(kb, 'attachments_root', lambda board=None: root)
    kb.add_attachment(conn, execution, filename=path.name, stored_path=str(path),
                      content_type='application/json', size=path.stat().st_size)
    item = task_controller_readback(conn, execution)['attachment_files'][0]
    assert item['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert item['attachment_json_claims'] == {
        'status': 'ok', 'model': 'gpt-6-sol',
        'model_source': 'result.meta.agentMeta.model',
        'trust_scope': 'untrusted_attachment_content',
    }
    path.write_text('[' * 300 + '0' + ']' * 300)
    assert 'attachment_json_claims' not in task_controller_readback(conn, execution)['attachment_files'][0]
    path.write_text(' ' * 65537)
    assert task_controller_readback(conn, execution)['attachment_files'][0]['json_claims_unavailable'] == 'attachment_exceeds_65536_bytes'


def test_native_claim_persists_controller_baseline_and_detects_attachment_changes(control_db, tmp_path):
    from hermes_cli.controller_readback import execution_history_baseline, compare_native_history_baseline
    conn, execution, review = control_db
    with kb.write_txn(conn):
        conn.execute('UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id=?',
                     (json.dumps({'verification': [review]}), 'delegation'))
    claimed = kb.claim_task(conn, execution)
    baseline = execution_history_baseline(conn,execution,claimed.current_run_id)
    assert baseline['execution_run_id'] == claimed.current_run_id
    from hermes_cli.controller_readback import capture_execution_history
    capture_execution_history(conn,execution,claimed.current_run_id)
    assert execution_history_baseline(conn,execution,claimed.current_run_id) == baseline
    assert not (kb.latest_run(conn,execution).metadata or {}).get('loop_contract')
    observed = compare_native_history_baseline(task_controller_readback(conn,execution),baseline,
                                              execution_run_id=claimed.current_run_id)
    assert observed['all_referenced_tasks_unchanged']
    assert observed['all_referenced_task_attachments_unchanged']
    # A worker-authored metadata field cannot replace the controller event.
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                     (json.dumps({'controller_history_baseline':{'forged':True}}),claimed.current_run_id))
    assert execution_history_baseline(conn,execution,claimed.current_run_id) == baseline
    kb.add_attachment(conn,review,filename='new.json',stored_path=str(tmp_path/'new.json'),content_type='application/json',size=2)
    changed = compare_native_history_baseline(task_controller_readback(conn,execution),baseline,
                                              execution_run_id=claimed.current_run_id)
    assert not changed['all_referenced_tasks_unchanged']
    assert not changed['all_referenced_task_attachments_unchanged']
    assert execution_history_baseline(conn,execution,claimed.current_run_id+1) is None


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
    assert json.loads(evidence['objective']['acceptance_criteria']) == ['verified']
    assert evidence['objective']['acceptance_criteria_sha256'] == hashlib.sha256(
        json.dumps(['verified'], ensure_ascii=False).encode()
    ).hexdigest()
    assert task_controller_readback(conn, execution)['history']['sha256'] == evidence['history']['sha256']
    with kb.write_txn(conn):
        kb._append_event(conn, execution, 'diagnostic_observed')
    changed = task_controller_readback(conn, execution)
    assert changed['history']['sha256'] != evidence['history']['sha256']
    assert changed['external_effect_count'] == 0
    assert 'controller_zero_effect_check' not in changed


def test_controller_readback_marks_nontext_criteria_invalid(control_db):
    conn, execution, _ = control_db
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_objectives SET acceptance_criteria=? WHERE objective_id='objective'", (b'not-json',))
    objective = task_controller_readback(conn, execution)['objective']
    assert objective['acceptance_criteria'] is None
    assert objective['acceptance_criteria_error'] == 'invalid_controller_text'


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


def test_native_baseline_detects_overwritten_attachment_bytes(control_db, tmp_path):
    from hermes_cli.controller_readback import execution_history_baseline, compare_native_history_baseline
    conn,execution,review=control_db
    path=tmp_path/'source.txt';path.write_text('original')
    kb.add_attachment(conn,review,filename='source.txt',stored_path=str(path),content_type='text/plain',size=8)
    with kb.write_txn(conn):
        conn.execute('UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id=?',(json.dumps({'verification':[review]}),'delegation'))
    claimed=kb.claim_task(conn,execution);baseline=execution_history_baseline(conn,execution,claimed.current_run_id)
    before=[dict(r) for r in conn.execute('SELECT * FROM task_attachments')]
    path.write_text('modified')
    assert [dict(r) for r in conn.execute('SELECT * FROM task_attachments')]==before
    comparison=compare_native_history_baseline(task_controller_readback(conn,execution),baseline,execution_run_id=claimed.current_run_id)
    assert not comparison['all_referenced_task_attachments_unchanged']
    assert not comparison['all_referenced_tasks_unchanged']


def test_attachment_read_race_reports_unavailable_evidence(control_db, tmp_path, monkeypatch):
    from pathlib import Path
    conn,execution,review=control_db
    path=tmp_path/'disappearing.txt';path.write_text('old bytes')
    kb.add_attachment(conn,review,filename='disappearing.txt',stored_path=str(path),content_type='text/plain',size=9)
    before=task_controller_readback(conn,review)['attachments_sha256']
    import os
    real_open=os.open
    def unavailable(target,*args,**kwargs):
        if str(target)==path.name:raise FileNotFoundError('disappeared during read')
        return real_open(target,*args,**kwargs)
    monkeypatch.setattr(os,'open',unavailable)
    unavailable = task_controller_readback(conn,review)
    assert unavailable['attachments_sha256']!=before
    assert unavailable['attachments_available'] is False


@pytest.mark.parametrize('kind', ['fifo', 'device', 'oversized'])
def test_attachment_special_or_oversized_file_is_unavailable(control_db, tmp_path, kind):
    import os
    from hermes_cli.controller_readback import MAX_EVIDENCE_FILE_BYTES
    conn, execution, _ = control_db
    path=tmp_path/'evidence'
    if kind=='fifo': os.mkfifo(path)
    elif kind=='device': path=__import__('pathlib').Path('/dev/zero')
    else:
        with path.open('wb') as stream: stream.truncate(MAX_EVIDENCE_FILE_BYTES+1)
    kb.add_attachment(conn,execution,filename='evidence',stored_path=str(path),content_type='application/octet-stream',size=1)
    assert not task_controller_readback(conn,execution)['attachments_available']



@pytest.mark.parametrize('kind', ['fifo', 'device', 'oversized'])
def test_candidate_revalidation_uses_bounded_file_reader(control_db, tmp_path, kind):
    import os, hashlib
    from hermes_cli.controller_readback import compare_native_history_baseline, execution_history_baseline, MAX_EVIDENCE_FILE_BYTES
    conn,execution,_=control_db
    path=tmp_path/'evidence';path.write_bytes(b'original candidate')
    claimed=kb.claim_task(conn,execution)
    baseline=execution_history_baseline(conn,execution,claimed.current_run_id)
    baseline['candidate_assets']=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
    current=task_controller_readback(conn,execution)
    assert compare_native_history_baseline(current,baseline,execution_run_id=claimed.current_run_id)['candidate_assets_unchanged']
    if kind=='fifo':
        path.unlink();os.mkfifo(path)
    elif kind=='device':baseline['candidate_assets'][0]['path']='/dev/zero'
    else:
        with path.open('wb') as stream:stream.truncate(MAX_EVIDENCE_FILE_BYTES+1)
    comparison=compare_native_history_baseline(current,baseline,execution_run_id=claimed.current_run_id)
    assert comparison['baseline_binding_verified']
    assert not comparison['candidate_assets_unchanged']
    assert comparison['candidate_asset_comparisons'][0]['current_sha256'] is None


@pytest.mark.parametrize('change', ['replace_at_open', 'modify_during_read'])
def test_evidence_reader_rejects_file_identity_races(tmp_path, monkeypatch, change):
    import os
    from hermes_cli import controller_readback as reader
    path=tmp_path/'source';path.write_bytes(b'original')
    if change=='replace_at_open':
        original_open=os.open
        def racing_open(value, flags, **kwargs):
            descriptor=original_open(value,flags,**kwargs)
            if value == path.name:
                path.replace(tmp_path/'old-source');path.write_bytes(b'modified')
            return descriptor
        monkeypatch.setattr(os,'open',racing_open)
    else:
        original_fdopen=os.fdopen
        class RacingStream:
            def __init__(self,stream):self.stream=stream;self.changed=False
            def __enter__(self):return self
            def __exit__(self,*exc):return self.stream.__exit__(*exc)
            def fileno(self):return self.stream.fileno()
            def read(self,count):
                chunk=self.stream.read(count)
                if chunk and not self.changed:
                    self.changed=True;path.write_bytes(b'modified')
                return chunk
        monkeypatch.setattr(os,'fdopen',lambda *a,**k:RacingStream(original_fdopen(*a,**k)))
    assert reader.evidence_file_sha256(str(path)) is None


@pytest.mark.parametrize('budget', ['count','bytes'])
def test_attachment_aggregate_budget_reports_unavailable(control_db,tmp_path,monkeypatch,budget):
    from hermes_cli import controller_readback as reader
    conn,execution,_=control_db
    for number in range(3):
        path=tmp_path/str(number);path.write_bytes(b'123')
        kb.add_attachment(conn,execution,filename=str(number),stored_path=str(path),content_type='text/plain',size=3)
    if budget=='count':monkeypatch.setattr(reader,'MAX_ATTACHMENT_FILES',2)
    else:monkeypatch.setattr(reader,'MAX_ATTACHMENT_BYTES',4)
    actual=reader._evidence_file;reads=[]
    def observed(value,max_bytes=reader.MAX_EVIDENCE_FILE_BYTES,**kwargs):
        result=actual(value,max_bytes,**kwargs);reads.append(result[1]);return result
    monkeypatch.setattr(reader,'_evidence_file',observed)
    assert not reader.task_controller_readback(conn,execution)['attachments_available']
    if budget=='count':assert len([n for n in reads if n])==2
    else:assert sum(reads)<=4


def test_review_detects_historical_delegation_mutation_after_claim(control_db):
    from hermes_cli.controller_readback import compare_native_history_baseline, execution_history_baseline
    conn,execution,_=control_db
    old_execution=kb.create_task(conn,title='historical execution');old_review=kb.create_task(conn,title='historical review')
    with kb.write_txn(conn):
        conn.execute("""INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,session_key,session_id,resolved_route,state,execution_task_id,review_task_id,contract_snapshot,created_at,updated_at)
        VALUES ('old-source',?,'old-instance','telegram','chat','session','session','ops','queued',?,?,'{}',1,1)""",('c'*64,old_execution,old_review))
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[old_execution,old_review]}),))
    claimed=kb.claim_task(conn,execution);baseline=execution_history_baseline(conn,execution,claimed.current_run_id)
    before=task_controller_readback(conn,old_execution)['history']['sha256']
    assert compare_native_history_baseline(task_controller_readback(conn,execution),baseline,execution_run_id=claimed.current_run_id)['all_referenced_tasks_unchanged']
    with kb.write_txn(conn):conn.execute("UPDATE grace_delegations SET contract_snapshot='changed source' WHERE delegation_id='old-source'")
    assert task_controller_readback(conn,old_execution)['history']['sha256']==before
    comparison=compare_native_history_baseline(task_controller_readback(conn,execution),baseline,execution_run_id=claimed.current_run_id)
    assert not comparison['all_referenced_tasks_unchanged']
    assert all(not row['delegation_unchanged'] for row in comparison['comparisons'])


@pytest.fixture(autouse=True)
def approved_test_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_ATTACHMENTS_ROOT', str(tmp_path))


@pytest.mark.parametrize('kind',['outside','symlink'])
def test_evidence_reader_never_opens_unapproved_path(tmp_path,monkeypatch,kind):
    import os
    from hermes_cli.controller_readback import evidence_file_sha256
    outside=tmp_path.parent/(tmp_path.name+'-outside');outside.write_bytes(b'not an artifact')
    path=outside
    if kind=='symlink':path=tmp_path/'link';path.symlink_to(outside)
    original_open=os.open
    def directories_only(value,flags,**kwargs):
        if not flags & os.O_DIRECTORY:pytest.fail('Unapproved controller file read')
        return original_open(value,flags,**kwargs)
    monkeypatch.setattr(os,'open',directories_only)
    assert evidence_file_sha256(str(path)) is None


def test_attachment_budget_is_shared_with_referenced_tasks(control_db,tmp_path,monkeypatch):
    from hermes_cli import controller_readback as reader
    conn,execution,review=control_db
    with kb.write_txn(conn):conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[review]}),))
    for task in [execution,review]:
        path=tmp_path/task;path.write_bytes(b'123')
        kb.add_attachment(conn,task,filename='source',stored_path=str(path),content_type='text/plain',size=3)
    monkeypatch.setattr(reader,'MAX_ATTACHMENT_FILES',1)
    result=reader.task_controller_readback(conn,execution)
    assert result['attachments_available']
    assert len(result['referenced_task_readbacks'])==1
    assert not result['referenced_task_readbacks'][0]['attachments_available']


@pytest.mark.parametrize('moment',['before_parent_open','after_parent_open'])
def test_directory_replacement_cannot_redirect_controller_file_reads(tmp_path,monkeypatch,moment):
    import os
    from hermes_cli.controller_readback import evidence_file_sha256
    parent=tmp_path/'candidate-dir';parent.mkdir();path=parent/'source';path.write_bytes(b'authorized artifact')
    outside=tmp_path.parent/(tmp_path.name+'-private-dir');outside.mkdir();(outside/'source').write_bytes(b'outside storage')
    forbidden_inode=(outside/'source').stat().st_ino
    original_open=os.open;changed=False;file_inodes=[]
    def replace_parent():
        parent.rename(tmp_path/'preserved-dir');parent.symlink_to(outside)
    def racing_open(value,flags,**kwargs):
        nonlocal changed
        is_parent=value=='candidate-dir' and flags & os.O_DIRECTORY
        if is_parent and not changed and moment=='before_parent_open':
            changed=True;replace_parent()
        descriptor=original_open(value,flags,**kwargs)
        if is_parent and not changed:
            changed=True;replace_parent()
        if not flags & os.O_DIRECTORY:file_inodes.append(os.fstat(descriptor).st_ino)
        return descriptor
    monkeypatch.setattr(os,'open',racing_open)
    assert evidence_file_sha256(str(path)) is None
    assert changed and forbidden_inode not in file_inodes


def test_capture_cannot_rebase_attachment_after_verified_snapshot(control_db,tmp_path,monkeypatch):
    from hermes_cli import objective_recovery as recovery
    conn,execution,review=control_db
    path=tmp_path/'source';path.write_bytes(b'authorized bytes')
    kb.add_attachment(conn,review,filename='source',stored_path=str(path),content_type='text/plain',size=16)
    with kb.write_txn(conn):conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[review]}),))
    observed=task_controller_readback(conn,review)
    snapshot={'historical_baseline':{review:observed['history']['sha256']},
              'historical_attachment_baseline':{review:observed['attachments_sha256']},
              'historical_delegation_sha256':observed['delegation_sha256'],
              'source_selectors':{'candidate_assets':[]}}
    def verified_then_replaced(*_):
        path.write_bytes(b'replacement bytes');return snapshot
    monkeypatch.setattr(recovery,'verify_recovery_execution',verified_then_replaced)
    with pytest.raises(ValueError,match='baseline differs from authorized'):
        kb.claim_task(conn,execution)
    assert kb.get_task(conn,execution).status=='ready'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='controller_execution_history_baseline'",(execution,)).fetchone()[0]==0


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


def test_ambiguous_execution_review_membership_is_not_evidence(control_db):
    import hashlib
    conn,execution,review=control_db
    another=kb.create_task(conn,title='Other execution')
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[execution]}),))
        conn.execute("""INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,session_key,session_id,resolved_route,state,execution_task_id,review_task_id,contract_snapshot,created_at,updated_at)
          SELECT 'ambiguous-role',?,'other-role',platform,chat_id,thread_id,session_key,session_id,resolved_route,state,?,?,'{}',created_at,updated_at FROM grace_delegations WHERE delegation_id='delegation'""",(hashlib.sha256(b'ambiguous-role').hexdigest(),another,execution))
        conn.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'done','completed',1,2,?)",(execution,json.dumps({'review_outcome':'accepted','verification':{'forged':True}})))
    assert task_controller_readback(conn,review)['referenced_task_readbacks']==[]


@pytest.mark.parametrize('binding',['unbound','stale-stage'])
def test_accepted_review_requires_verified_referenced_binding(control_db,binding):
    import hashlib
    conn,execution,review=control_db
    old_execution=kb.create_task(conn,title='Referenced execution')
    old_review=kb.create_task(conn,title='Referenced review')
    row=dict(conn.execute("SELECT * FROM grace_delegations WHERE delegation_id='delegation'").fetchone())
    row.update(delegation_id='unverified-reference',contract_fingerprint=hashlib.sha256(b'unverified-reference').hexdigest(),
        request_instance_id='unverified-reference',execution_task_id=old_execution,review_task_id=old_review,
        objective_id=None if binding=='unbound' else row['objective_id'],stage_key='not-the-bound-stage')
    with kb.write_txn(conn):
        names=list(row)
        conn.execute('INSERT INTO grace_delegations ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',tuple(row[n] for n in names))
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[old_review]}),))
        conn.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'done','completed',1,2,?)",(old_review,json.dumps({'review_outcome':'accepted'})))
    observed=task_controller_readback(conn,execution)['referenced_task_readbacks'][0]
    assert observed['task']['id']==old_review and not observed['binding_verified']
    assert observed['recorded_review'] is None


@pytest.mark.parametrize('registered',[True,False])
def test_claim_capture_cannot_rebase_verified_content_asset(control_db,tmp_path,monkeypatch,registered):
    import hashlib
    from hermes_cli import content_revision as revision
    conn,execution,review=control_db
    path=tmp_path/'accepted-source';path.write_bytes(b'accepted original')
    attachment_id=kb.add_attachment(conn,review,filename='accepted-source',stored_path=str(path),content_type='text/plain',size=17) if registered else None
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_delegations SET contract_snapshot=? WHERE delegation_id='delegation'",(json.dumps({'verification':[review]}),))
    accepted_sha=hashlib.sha256(path.read_bytes()).hexdigest()
    def verified_then_replaced(*_):
        path.write_bytes(b'replacement during capture')
        return {'execution_task_id':review,'verified_attachments':{'assets':[{'attachment_id':attachment_id,'stored_path':str(path),'sha256':accepted_sha}]}}
    monkeypatch.setattr(revision,'verify_content_revision_execution',verified_then_replaced)
    with pytest.raises(ValueError,match='changed during claim baseline capture|lacks a controller attachment binding'):
        kb.claim_task(conn,execution)
    assert kb.get_task(conn,execution).status=='ready'
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='controller_execution_history_baseline'",(execution,)).fetchone()[0]==0


def test_dual_role_task_cannot_supply_accepted_review(control_db):
    import hashlib
    conn,execution,review=control_db
    current=kb.create_task(conn,title='Current execution')
    current_review=kb.create_task(conn,title='Current review')
    row=dict(conn.execute("SELECT * FROM grace_delegations WHERE delegation_id='delegation'").fetchone())
    objective_id=row['objective_id'];old_stage=row['stage_key']
    row.update(delegation_id='current-source-reader',contract_fingerprint=hashlib.sha256(b'current-source-reader').hexdigest(),
        request_instance_id='current-source-reader',execution_task_id=current,review_task_id=current_review,stage_key='current-reader',contract_snapshot=json.dumps({'verification':[execution]}))
    with kb.write_txn(conn):
        conn.execute("UPDATE grace_delegations SET review_task_id=? WHERE delegation_id='delegation'",(execution,))
        conn.execute('UPDATE grace_objective_stages SET review_task_id=? WHERE objective_id=? AND stage_key=?',(execution,objective_id,old_stage))
        names=list(row)
        conn.execute('INSERT INTO grace_delegations ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',tuple(row[n] for n in names))
        conn.execute("INSERT INTO grace_objective_stages(objective_id,stage_key,position,status,delegation_id,execution_task_id,review_task_id,created_at,updated_at) VALUES (?,'current-reader',1,'queued','current-source-reader',?,?,1,1)",(objective_id,current,current_review))
        conn.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'done','completed',1,2,?)",(execution,json.dumps({'review_outcome':'accepted'})))
    observed=task_controller_readback(conn,current)['referenced_task_readbacks'][0]
    assert observed['task']['id']==execution and observed['binding_verified']
    assert observed['recorded_review'] is None
