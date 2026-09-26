"""Real controller DB recovery: preserve roots/history, reject authority changes."""
import hashlib
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import objective_recovery as recovery
from proactive.behavior_profiles import registry as br


@pytest.fixture
def recovery_db(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        execution = kb.create_task(conn, title='Old execution')
        review = kb.create_task(conn, title='Old review', parents=(execution,))
        original = f'Owner asks Grace to recover existing package from {execution} and {review}.'
        objective = kb.create_grace_objective(conn, objective_id='go_user_aaaaaaaaaaaaaaaaaaaaaaaa', platform='telegram',
            chat_id='chat', thread_id='4641', session_key='owner-session', title='Recovery',
            objective=original, original_request_sha256=hashlib.sha256(original.encode()).hexdigest(),
            required_stage_keys=['bind_source','produce_package'], terminal_stage_key='produce_package',
            acceptance_criteria=['Grace delivers'])
        pin = br._make_pin('ai_bizweek','v96',{})
        br.verify_pin(pin)
        conn.execute('INSERT INTO grace_objective_behavior_pins VALUES (?,?,?)',
                     (objective['objective_id'],json.dumps(pin),'{}'))
        contract = json.dumps({'original_request':'Exact original EP source.', 'external_effect_budget':0})
        fingerprint = hashlib.sha256(contract.encode()).hexdigest()
        image = tmp_path/'candidate.png';image.write_bytes(b'original asset')
        metadata = {'external_effects':[], 'loop_contract_blocked_result':{
            'acceptanceEvidence':{'image_assets':[{'asset_family':'page_hero','path':str(image),
                'sha256':hashlib.sha256(image.read_bytes()).hexdigest()}]}}}
        parent_id = conn.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'blocked','blocked',1,2,?)",
                                 (execution,json.dumps(metadata))).lastrowid
        parent = kb.get_run(conn,parent_id)
        source = {'parent_execution_task_id':execution, 'parent_execution_run_id':parent_id,
                  'parent_execution_evidence_sha256':kb.workflow_review_evidence_hash(parent)}
        conn.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'blocked','blocked',3,4,?)",
                                  (review,json.dumps({'review_outcome':'rejected','workflow_review_source':source})))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution,))
        conn.execute("UPDATE tasks SET status='triage',block_recurrences=2 WHERE id=?", (review,))
        conn.execute("""INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,
          platform,chat_id,thread_id,session_key,session_id,resolved_route,state,execution_task_id,
          review_task_id,contract_snapshot,created_at,updated_at)
          VALUES ('old',?,'old-request','telegram','chat','4641','owner-session','session','{}','queued',?,?,?,1,1)""",
          (fingerprint,execution,review,contract))
        conn.commit()
        args = {'objective_ref':{'objective_id':objective['objective_id'],'stage_key':'bind_source'},
                'original_request':original,'external_effect_budget':0,'approved':False,
                'verification':{'evidence_required':[execution,review]}}
        row = recovery._schedule_owner_stage(conn, objective['objective_id'])
        args['verification']['evidence_required'].append(recovery.source_evidence_requirement(json.loads(row['snapshot'])))
        conn.execute('DELETE FROM grace_objective_stage_wakeups WHERE request_id=?', (row['request_id'],));conn.commit()
        yield conn, {'objective_id':objective['objective_id']}, args, execution, review, image


def validate(conn, request, args, **overrides):
    values = dict(platform='telegram',chat_id='chat',thread_id='4641',session_key='owner-session')
    values.update(overrides)
    return recovery.validate_recovery_turn(conn, {'request_id':request['request_id'],'lease':request['lease']}, args, **values)


def test_recovery_deduplicates_without_mutating_original_cards_or_stage(recovery_db):
    conn,spec,args,execution,review,image = recovery_db
    before = {t: recovery.task_controller_readback(conn,t)['history']['sha256'] for t in [execution,review]}
    requested = recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert recovery._schedule_owner_stage(conn,spec['objective_id'])['request_id'] == requested['request_id']
    snapshot = json.loads(requested['snapshot'])
    assert snapshot['source_selectors']['original_source']['json_pointer'] == '/original_request'
    assert snapshot['source_selectors']['candidate_assets'][0]['acceptance_state'] == 'historical_candidate_not_accepted_package'
    assert snapshot['controller_source_packet']['original_request'] == 'Exact original EP source.'
    request = recovery.claim_recovery(conn,requested['request_id'])
    assert recovery.claim_recovery(conn,requested['request_id']) is None
    validate(conn,request,args)
    assert args['request_instance_id'] == 'gri_'+requested['request_id']
    assert conn.execute('SELECT count(*) FROM grace_objectives').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert {t: recovery.task_controller_readback(conn,t)['history']['sha256'] for t in before} == before
    assert conn.execute('SELECT block_recurrences FROM tasks WHERE id=?',(review,)).fetchone()[0] == 2
    assert not recovery.finish_recovery(conn,requested['request_id'],request['lease'])
    assert conn.execute('SELECT state FROM grace_objective_stage_wakeups').fetchone()[0] == 'attention'


@pytest.mark.parametrize('change', ['effects','asset','history','delegation'])
def test_stage_wakeup_refuses_changed_controller_sources(recovery_db,change):
    conn,spec,args,execution,review,image=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id'])
    if change=='asset':image.write_bytes(b'changed')
    elif change=='history':kb.add_comment(conn,review,'operator','changed')
    elif change=='delegation':conn.execute("UPDATE grace_delegations SET contract_snapshot='{}' WHERE delegation_id='old'");conn.commit()
    else:kb.record_external_effect(conn,execution,platform='facebook_page',state='created',external_id='effect')
    with pytest.raises(ValueError):recovery.claim_recovery(conn,row['request_id'])

@pytest.mark.parametrize('bad', ['approved','budget','stage','request','grant','callback','route','expired','refresh','private-refresh','credentials'])
def test_recovery_turn_cannot_expand_its_authority(recovery_db,bad):
    conn,spec,args,*_ = recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id'])
    if bad == 'approved': args['approved']=True
    elif bad == 'budget': args['external_effect_budget']=1
    elif bad == 'stage': args['objective_ref']['stage_key']='produce_package'
    elif bad == 'request': args['original_request']='Changed objective'
    elif bad == 'grant': args['approval_grant_id']='invented'
    elif bad == 'refresh': args['approval_refresh_token']='ungranted'
    elif bad == 'private-refresh': args['_approval_refresh_token']='ungranted'
    elif bad == 'credentials': args['credential_refs']=['ungranted']
    elif bad == 'callback': args['origin_callback_review_id']='old-review'
    elif bad == 'expired':
        conn.execute('UPDATE grace_objective_stage_wakeups SET lease_expires=?',(int(time.time())-1,));conn.commit()
    with pytest.raises(ValueError): validate(conn,request,args,**({'thread_id':'other'} if bad=='route' else {}))
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2


def test_human_cannot_impersonate_stage_wakeup_and_no_operator_api_exists(recovery_db):
    conn,spec,*_=recovery_db
    with pytest.raises(ValueError): recovery.recovery_context(recovery.ENVELOPE+'{}',False)
    assert not hasattr(recovery, 'request_recovery')


def test_finish_requires_a_real_matching_formal_delegation(recovery_db):
    conn,spec,args,execution,review,image=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id'])
    conn.execute("UPDATE grace_objective_stages SET delegation_id='old' WHERE objective_id='go_user_aaaaaaaaaaaaaaaaaaaaaaaa' AND stage_key='bind_source'")
    conn.commit()
    assert not recovery.finish_recovery(conn,row['request_id'],request['lease'])


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_origin', [False,True])
async def test_gateway_delivers_a_typed_internal_turn_and_records_formal_binding(recovery_db,wrong_origin):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from types import SimpleNamespace
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id'])
    request=recovery.claim_recovery(conn,row['request_id'])
    events=[]

    class Adapter:
        async def handle_message(self,event):
            events.append(event)
            context=recovery.recovery_context(event.text,event.internal)
            recovery.validate_recovery_turn(conn,context,args,platform='telegram',chat_id='chat',thread_id='4641',session_key='owner-session')
            execution=kb.create_task(conn,title='New bound execution')
            review=kb.create_task(conn,title='New bound review',parents=(execution,))
            with kb.write_txn(conn):
                conn.execute("""INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,
                  platform,chat_id,thread_id,session_key,session_id,resolved_route,state,objective_id,stage_key,
                  execution_task_id,review_task_id,created_at,updated_at)
                  VALUES ('new',? ,?,'telegram','chat','4641','owner-session','grace-session','{}','queued',
                  'go_user_aaaaaaaaaaaaaaaaaaaaaaaa','bind_source',?,?,1,1)""",('b'*64,args['request_instance_id'],execution,review))
                conn.execute("UPDATE grace_objective_stages SET status='queued',delegation_id='new',execution_task_id=?,review_task_id=? WHERE objective_id='go_user_aaaaaaaaaaaaaaaaaaaaaaaa' AND stage_key='bind_source'",(execution,review))

    runner=object.__new__(GatewayRunner)
    runner.adapters={Platform.TELEGRAM:Adapter()}
    runner.session_store=SimpleNamespace(_ensure_loaded=lambda:None,_entries={})
    if wrong_origin:
        from gateway.session import SessionSource
        runner.session_store._entries['owner-session']=SimpleNamespace(origin=SessionSource(platform=Platform.TELEGRAM,chat_id='foreign',chat_type='group',thread_id='other'))
    await runner._deliver_objective_recovery(kb.DEFAULT_BOARD,request)
    state=conn.execute('SELECT state FROM grace_objective_stage_wakeups').fetchone()[0]
    if wrong_origin:
        assert not events
        assert state=='attention'
    else:
        assert state=='delegated'
        assert events[0].internal and events[0].message_id is None
        assert events[0].internal_context['isolated_history']
        assert 'not a human message' in events[0].text
        assert conn.execute('SELECT count(*) FROM grace_objectives').fetchone()[0]==1


def test_bridge_rejects_recovery_scope_expansion_before_any_card_creation(recovery_db,monkeypatch):
    from plugins.openclaw_bridge import clawops_delegate as delegate
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id'])
    context={'request_id':row['request_id'],'lease':request['lease']}
    values={'HERMES_SESSION_PLATFORM':'telegram','HERMES_SESSION_CHAT_ID':'chat',
        'HERMES_SESSION_THREAD_ID':'4641','HERMES_SESSION_KEY':'owner-session',
        'HERMES_SESSION_ID':'grace-session','HERMES_SESSION_INTERNAL':'true',
        'HERMES_SESSION_MESSAGE_TEXT':recovery.ENVELOPE+json.dumps(context)+'\ncontroller snapshot'}
    monkeypatch.setattr(delegate,'get_session_env',lambda k,default='':values.get(k,default))
    args['objective_ref']['stage_key']='produce_package'
    result=json.loads(delegate.handle_clawops_delegate(args))
    assert result['status']=='rejected'
    assert 'exact existing Objective stage' in result['reason']
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==2


@pytest.mark.parametrize('change', ['missing','task','selectors'])
def test_recovery_requires_exact_sealed_controller_evidence(recovery_db,change):
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id'])
    if change=='missing':args.pop('verification')
    elif change=='task':args['verification']['evidence_required'].pop(0)
    else:args['verification']['evidence_required'][-1]='controller_source_selectors={}'
    with pytest.raises(ValueError,match='controller source selectors'):validate(conn,request,args)


def test_native_recovery_execution_checks_sealed_sources_again(recovery_db):
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id'])
    validate(conn,request,args)
    delegated={'request_instance_id':args['request_instance_id'],'objective_id':'go_user_'+'a'*24,'stage_key':'bind_source','contract_snapshot':json.dumps({'verification':args['verification']})}
    recovery.verify_recovery_execution(conn,delegated)
    conn.execute("UPDATE grace_delegations SET contract_snapshot='{}' WHERE delegation_id='old'");conn.commit()
    with pytest.raises(ValueError,match='Historical source delegation changed'):recovery.verify_recovery_execution(conn,delegated)


@pytest.mark.parametrize('field,value', [('revision',2),('current_stage_key','produce_package'),('thread_id','other'),('session_key','other'),('status','blocked')])
def test_recovery_execution_rejects_changed_objective_identity(recovery_db,field,value):
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id']);validate(conn,request,args)
    delegated={'request_instance_id':args['request_instance_id'],'objective_id':'go_user_'+'a'*24,'stage_key':'bind_source','contract_snapshot':json.dumps({'verification':args['verification']})}
    conn.execute(f'UPDATE grace_objectives SET {field}=? WHERE objective_id=?',(value,'go_user_'+'a'*24));conn.commit()
    with pytest.raises(ValueError,match='identity or revision'):recovery.verify_recovery_execution(conn,delegated)


def test_review_comparison_detects_candidate_mutation_without_attachment_or_metadata_changes(recovery_db):
    from hermes_cli.controller_readback import task_controller_readback, native_history_baseline, compare_native_history_baseline
    conn,spec,args,execution,review,image=recovery_db
    # Use the real bound controller fixture shape to isolate the asset comparison.
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);snap=json.loads(row['snapshot'])
    observed={'binding_verified':True,'task':{'id':execution,'current_run_id':7},'history':{'runs':[{'id':7}]},
        'delegation':{'delegation_id':'bound','contract_fingerprint':'sealed'},'referenced_task_readbacks':[], 'referenced_tasks_truncated':False}
    baseline={'source':'controller_pre_admission_snapshot','execution_task_id':execution,'execution_run_id':7,
        'delegation_id':'bound','delegation_contract_fingerprint':'sealed','truncated':False,'tasks':[],
        'candidate_assets':snap['source_selectors']['candidate_assets']}
    assert compare_native_history_baseline(observed,baseline,execution_run_id=7)['candidate_assets_unchanged']
    before=recovery.task_controller_readback(conn,execution)['history']['sha256'];image.write_bytes(b'changed after claim')
    comparison=compare_native_history_baseline(observed,baseline,execution_run_id=7)
    assert recovery.task_controller_readback(conn,execution)['history']['sha256']==before
    assert not comparison['candidate_assets_unchanged'] and not comparison['all_referenced_tasks_unchanged']


@pytest.mark.parametrize('change', ['blocked','bound','source_hash','no_pin','non_owner'])
def test_observer_requires_existing_authorized_idle_owner_state(recovery_db,change):
    conn,spec,args,*_=recovery_db
    root=spec['objective_id']
    if change=='blocked':conn.execute("UPDATE grace_objectives SET status='blocked' WHERE objective_id=?",(root,))
    elif change=='bound':conn.execute("UPDATE grace_objective_stages SET delegation_id='occupied' WHERE objective_id=? AND stage_key='bind_source'",(root,))
    elif change=='source_hash':conn.execute("UPDATE grace_objectives SET original_request_sha256='bad' WHERE objective_id=?",(root,))
    elif change=='no_pin':conn.execute('DELETE FROM grace_objective_behavior_pins WHERE objective_id=?',(root,))
    else:root='manual-root'
    conn.commit()
    with pytest.raises(ValueError):recovery._schedule_owner_stage(conn,root)
    assert conn.execute('SELECT count(*) FROM grace_objective_stage_wakeups').fetchone()[0]==0


def test_observer_cannot_skip_unaccepted_source_stage(recovery_db):
    conn,spec,*_=recovery_db
    conn.execute("UPDATE grace_objectives SET current_stage_key='produce_package' WHERE objective_id=?",(spec['objective_id'],));conn.commit()
    with pytest.raises(ValueError,match='Earlier Objective stages'):recovery._schedule_owner_stage(conn,spec['objective_id'])


def test_delivery_retry_is_bounded_and_preserves_old_business_counters(recovery_db,monkeypatch):
    conn,spec,args,execution,review,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id'])
    initial=int(time.time());monkeypatch.setattr(recovery.time,'time',lambda:initial)
    for attempt in range(3):
        request=recovery.claim_recovery(conn,row['request_id'])
        assert request
        assert not recovery.finish_recovery(conn,row['request_id'],request['lease'],'Temporary delivery failure')
        monkeypatch.setattr(recovery.time,'time',lambda n=attempt:initial+(n+1)*61)
        row=recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert row['state']=='attention'
    assert len(json.loads(row['snapshot'])['delivery_attempt_history'])==2
    assert conn.execute('SELECT block_recurrences FROM tasks WHERE id=?',(review,)).fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==2


@pytest.mark.parametrize('metadata', [
    {'loop_contract_blocked_result': 1},
    {'loop_contract_blocked_result': {'acceptanceEvidence': None}},
    {'loop_contract_blocked_result': {'acceptanceEvidence': {'image_assets': [None]}}},
    {'loop_contract_blocked_result': {'acceptanceEvidence': {'image_assets': [1] * 17}}},
])
def test_malformed_candidates_are_rejected_as_evidence(metadata):
    with pytest.raises(ValueError):
        recovery._candidate_list(metadata)


@pytest.mark.parametrize('value', [None, {}, 3, 'relative.png'])
def test_invalid_candidate_paths_do_not_abort_watcher(value):
    with pytest.raises(ValueError):
        recovery._candidate_digest(value)


def test_large_candidate_is_rejected_before_reading(recovery_db):
    *_, image = recovery_db
    with image.open('wb') as stream:
        stream.truncate(recovery.MAX_CANDIDATE_BYTES + 1)
    with pytest.raises(ValueError):
        recovery._candidate_digest(str(image))


def test_missing_historical_attachment_cannot_be_baselined(recovery_db, tmp_path):
    conn, spec, _, execution, _, _ = recovery_db
    kb.add_attachment(conn, execution, filename='gone', stored_path=str(tmp_path/'gone'), content_type='text/plain', size=1)
    with pytest.raises(ValueError, match='attachment bytes unavailable'):
        recovery._schedule_owner_stage(conn, spec['objective_id'])


def test_missing_historical_attachment_cannot_pass_claim(recovery_db, tmp_path):
    conn, spec, _, execution, _, _ = recovery_db
    path = tmp_path/'source.txt'; path.write_text('source')
    kb.add_attachment(conn, execution, filename='source', stored_path=str(path), content_type='text/plain', size=6)
    row = recovery._schedule_owner_stage(conn, spec['objective_id'])
    path.unlink()
    with pytest.raises(ValueError):
        recovery.claim_recovery(conn, row['request_id'])


@pytest.mark.asyncio
async def test_unsupported_board_is_not_scheduled_or_claimed(recovery_db, monkeypatch):
    from gateway.run import GatewayRunner
    conn, *_ = recovery_db
    monkeypatch.setattr(kb, 'list_boards', lambda **_: [{'slug': 'unsupported'}])
    monkeypatch.setattr(kb, 'connect_closing', lambda **_: pytest.fail('Unsupported board opened'))
    runner = object.__new__(GatewayRunner); runner.adapters = {}
    await runner._poll_objective_recoveries()
    assert not runner._objective_recovery_turns
    assert conn.execute('SELECT count(*) FROM grace_objective_stage_wakeups').fetchone()[0] == 0


def test_candidate_fifo_is_rejected_before_open(tmp_path):
    import os
    fifo = tmp_path/'fifo'; os.mkfifo(fifo)
    with pytest.raises(ValueError):
        recovery._candidate_digest(str(fifo))


@pytest.mark.parametrize('state', ['pending','processing','exhausted_attention','backoff_attention'])
def test_watcher_dedup_does_not_rehash_unneeded_evidence(recovery_db,monkeypatch,state):
    conn,spec,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id'])
    if state=='processing':recovery.claim_recovery(conn,row['request_id'])
    elif state.endswith('attention'):
        snap=json.loads(row['snapshot'])
        if state=='exhausted_attention':snap['delivery_attempt_history']=[{'at':int(time.time())-120}]*2
        conn.execute("UPDATE grace_objective_stage_wakeups SET state='attention',snapshot=? WHERE request_id=?",(json.dumps(snap),row['request_id']));conn.commit()
    monkeypatch.setattr(recovery,'task_controller_readback',lambda *_:pytest.fail('Unnecessary evidence read'))
    monkeypatch.setattr(recovery,'_candidate_digest',lambda *_:pytest.fail('Unnecessary candidate read'))
    result=recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert result['request_id']==row['request_id']


@pytest.fixture(autouse=True)
def approved_test_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_ATTACHMENTS_ROOT', str(tmp_path))


def test_historical_pair_shares_one_attachment_budget(recovery_db,tmp_path,monkeypatch):
    from hermes_cli import controller_readback as reader
    conn,spec,_,execution,review,_=recovery_db
    for task in [execution,review]:
        path=tmp_path/task;path.write_bytes(b'123')
        kb.add_attachment(conn,task,filename='source',stored_path=str(path),content_type='text/plain',size=3)
    monkeypatch.setattr(reader,'MAX_ATTACHMENT_FILES',1)
    with pytest.raises(ValueError,match='attachment bytes unavailable'):
        recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert conn.execute('SELECT count(*) FROM grace_objective_stage_wakeups').fetchone()[0]==0


@pytest.mark.parametrize('failed_role', ['review', 'execution'])
def test_prior_stage_requires_successful_exact_runs(recovery_db, failed_role):
    conn,spec,_,execution,review,*_=recovery_db
    parent = kb.latest_run(conn, execution)
    conn.execute("UPDATE task_runs SET status='done',outcome='completed' WHERE id=?",(parent.id,))
    parent = kb.get_run(conn,parent.id)
    metadata={'review_outcome':'accepted','workflow_review_source':{
        'parent_execution_task_id':execution,'parent_execution_run_id':parent.id,
        'parent_execution_evidence_sha256':kb.workflow_review_evidence_hash(parent)}}
    latest=kb.latest_run(conn,review)
    conn.execute("UPDATE task_runs SET status='done',outcome='completed',metadata=? WHERE id=?",(json.dumps(metadata),latest.id))
    conn.execute("UPDATE grace_objective_stages SET status='done',execution_task_id=?,review_task_id=? WHERE objective_id=? AND stage_key='bind_source'",(execution,review,spec['objective_id']))
    conn.execute("UPDATE grace_objectives SET current_stage_key='produce_package' WHERE objective_id=?",(spec['objective_id'],))
    failed = latest.id if failed_role=='review' else parent.id
    conn.execute("UPDATE task_runs SET status='blocked',outcome='blocked' WHERE id=?",(failed,))
    if failed_role=='execution':
        changed=kb.get_run(conn,parent.id);metadata['workflow_review_source']['parent_execution_evidence_sha256']=kb.workflow_review_evidence_hash(changed)
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',(json.dumps(metadata),latest.id))
    conn.commit()
    with pytest.raises(ValueError,match='Earlier Objective stages'):
        recovery._idle(conn,spec['objective_id'],kb.get_grace_objective(conn,spec['objective_id'])['revision'],'produce_package')


def test_recovery_rejects_source_card_with_second_delegation(recovery_db):
    conn,spec,args,execution,review,image=recovery_db
    other_review=kb.create_task(conn,title='Other unrelated review')
    row=dict(conn.execute("SELECT * FROM grace_delegations WHERE delegation_id='old'").fetchone())
    row.update(delegation_id='ambiguous-membership',review_task_id=other_review,request_instance_id='other-request',contract_fingerprint=hashlib.sha256(b'ambiguous-recovery-membership').hexdigest())
    names=list(row)
    conn.execute('INSERT INTO grace_delegations ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',tuple(row[n] for n in names))
    conn.commit()
    with pytest.raises(ValueError,match='ambiguous delegation membership'):
        recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert conn.execute('SELECT count(*) FROM grace_objective_stage_wakeups').fetchone()[0]==0


@pytest.mark.parametrize('instance',['','changed-instance'])
def test_recovery_claim_cannot_drop_instance_for_bound_wakeup(recovery_db,instance):
    conn,spec,args,*_=recovery_db
    row=recovery._schedule_owner_stage(conn,spec['objective_id']);request=recovery.claim_recovery(conn,row['request_id']);validate(conn,request,args)
    conn.execute("UPDATE grace_objective_stages SET delegation_id='bound-recovery' WHERE objective_id=? AND stage_key='bind_source'",(spec['objective_id'],))
    delegation={'delegation_id':'bound-recovery','request_instance_id':instance,'objective_id':spec['objective_id'],'stage_key':'bind_source'}
    with pytest.raises(ValueError,match='lost its exact request instance'):
        recovery.verify_recovery_execution(conn,delegation)


def test_recovery_source_requires_distinct_execution_and_review(recovery_db):
    conn,spec,args,execution,review,image=recovery_db
    conn.execute("UPDATE grace_delegations SET review_task_id=? WHERE delegation_id='old'",(execution,));conn.commit()
    with pytest.raises(ValueError,match='distinct execution and review'):
        recovery._schedule_owner_stage(conn,spec['objective_id'])
    assert conn.execute('SELECT count(*) FROM grace_objective_stage_wakeups').fetchone()[0]==0
