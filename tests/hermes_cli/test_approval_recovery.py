import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import approval_recovery as ar


@pytest.fixture
def case(tmp_path):
    with kb.connect_closing(tmp_path/'board.db') as conn:
        challenge = kb.create_grace_approval_challenge(conn, contract_fingerprint='f'*64,
            request_instance_id='request', platform='telegram', chat_id='chat', thread_id='2',
            session_key='key', session_id='session', user_id_sha256=hashlib.sha256(b'owner').hexdigest(),
            requested_message_id='request', action_summary='Publish exact listing',
            approval_platform='facebook', approval_scope='["group:123"]',
            delegation_args={'_approval_compiled_contract': {'exact': 'contract'}})
        context = dict(platform='telegram', source='telegram', chat_id='chat', thread_id='2',
                       session_key='key', session_id='session', user_id='owner', owner_user_id='owner',
                       message_id='9198', message_text=f"核准 {challenge['token']}")
        yield conn, challenge, context


@pytest.mark.parametrize('change', ['owner', 'message', 'session', 'scope', 'late'])
def test_receipt_rejects_unverified_or_late_decision(case, change):
    conn, challenge, context = case
    if change == 'owner': context['user_id'] = 'other'
    if change == 'message': context['message_text'] += ' and publish elsewhere'
    if change == 'session': context['session_id'] = ''
    fingerprint = 'x'*64 if change == 'scope' else 'f'*64
    observed = challenge['expires_at'] if change == 'late' else None
    with pytest.raises(ValueError):
        ar.record(conn, token=challenge['token'], fingerprint=fingerprint, context=context, args={}, observed_at=observed)
    assert conn.execute('select count(*) from grace_approval_receipts').fetchone()[0] == 0


def test_hold_preserves_authorization_and_prevents_dispatch(case, monkeypatch):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    before = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    ar.hold(conn, challenge['token'], fingerprint='f'*64, reason='Accepted posts overlap this request')
    after = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    assert after['state'] == 'attention'
    for key in before.keys() - {'state', 'last_result'}:
        assert after[key] == before[key]
    assert kb.get_grace_approval_challenge(conn, challenge['token']) == challenge
    monkeypatch.setattr(ar.time, 'time', lambda: before['next_retry_at'] + 1)
    board = conn.execute('pragma database_list').fetchone()['file']
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: Path(board))
    assert ar.recover_pending() is None


def test_cancelled_bound_delegation_cancels_receipt_without_dispatch(case, monkeypatch):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    delegation = kb.reserve_grace_delegation(
        conn, contract_fingerprint='f'*64, request_instance_id='request',
        platform='telegram', chat_id='chat', thread_id='2', session_key='key',
        session_id='session', resolved_route={'agent': 'research'}, approval_required=False,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE grace_delegations SET state='cancelled' WHERE delegation_id=?",
            (delegation['delegation_id'],),
        )
        conn.execute(
            "UPDATE grace_approval_receipts SET next_retry_at=0 WHERE token=?",
            (challenge['token'],),
        )
    board = conn.execute('pragma database_list').fetchone()['file']
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: Path(board))
    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        lambda _args: pytest.fail('cancelled authorization must not dispatch'),
    )

    assert ar.recover_pending() is None
    receipt = conn.execute(
        "SELECT state,attempts FROM grace_approval_receipts WHERE token=?",
        (challenge['token'],),
    ).fetchone()
    assert receipt['state'] == 'cancelled'
    assert receipt['attempts'] == 0


@pytest.mark.parametrize('change', ['attempted', 'queued', 'fingerprint', 'reason', 'bound'])
def test_hold_refuses_dispatch_races_and_bad_identity(case, change):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    if change == 'bound':
        kb.reserve_grace_delegation(conn, contract_fingerprint='f'*64,
            request_instance_id='request', platform='telegram', chat_id='chat', thread_id='2',
            session_key='key', session_id='session', resolved_route={'agent': 'research'}, approval_required=False)
    with kb.write_txn(conn):
        if change == 'attempted':
            conn.execute('update grace_approval_receipts set attempts=1')
        if change == 'queued':
            conn.execute("update grace_approval_receipts set state='queued'")
    before = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    with pytest.raises(ValueError):
        ar.hold(conn, challenge['token'], fingerprint='bad' if change == 'fingerprint' else 'f'*64,
                reason='' if change == 'reason' else 'Reconcile first')
    assert dict(conn.execute('select * from grace_approval_receipts').fetchone()) == before


def test_receipt_is_immutable_and_bound_to_original_message(case):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    assert json.loads(conn.execute('select args from grace_approval_receipts').fetchone()[0])['_approval_compiled_contract'] == {'exact':'contract'}
    assert ar.valid_receipt(conn, challenge['token'], 'f'*64, '9198')
    assert not ar.valid_receipt(conn, challenge['token'], 'f'*64, 'different')
    context['message_id'] = 'different'
    with pytest.raises(ValueError, match='rebound'):
        ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})


def test_attention_receipt_cannot_validate_or_be_recorded_again(case):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    ar.hold(conn, challenge['token'], fingerprint='f'*64, reason='Reconcile first')
    assert not ar.valid_receipt(conn, challenge['token'], 'f'*64, '9198')
    with pytest.raises(ValueError, match='not dispatchable'):
        ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})


def test_attention_receipt_cannot_be_consumed_by_reservation(case):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    ar.hold(conn, challenge['token'], fingerprint='f'*64, reason='Reconcile first')
    with pytest.raises(ValueError, match='invalid, expired'):
        kb.reserve_grace_delegation(
            conn, contract_fingerprint='f'*64, request_instance_id='request',
            platform='telegram', chat_id='chat', thread_id='2', session_key='key',
            session_id='session', resolved_route={'backend':'openclaw'}, approval_required=True,
            challenge_token=challenge['token'], user_id_sha256=hashlib.sha256(b'owner').hexdigest(),
            approved_message_id='9198',
        )
    assert kb.get_grace_approval_challenge(conn, challenge['token'])['state'] == 'pending'


@pytest.mark.parametrize('fault', [None, 'late', 'result', 'contract', 'owner', 'call_id'])
def test_import_requires_original_native_rejection_triple(case, fault):
    conn, challenge, context = case
    state = sqlite3.connect(':memory:'); state.row_factory = sqlite3.Row
    state.execute('create table messages(id integer,session_id text,role text,tool_name text,content text,tool_calls text,tool_call_id text,timestamp real)')
    token = challenge['token']; cid = f'gateway_approval_{token}_9198_1'
    args = dict(approval_token=token, _approval_compiled_contract={'exact':'contract'})
    if fault == 'contract': args['_approval_compiled_contract'] = {'other':'scope'}
    calls = [{'id': cid, 'function': {'name':'clawops_delegate','arguments':json.dumps(args)}}]
    result = dict(reason='Objective already has an in-flight delegation; observe it before publishing', status='rejected', task_created=False)
    if fault == 'result': result['task_created'] = True
    stamp = challenge['expires_at'] if fault == 'late' else int(time.time())
    state.executemany('insert into messages values (?,?,?,?,?,?,?,?)', [
        (1,'session','user',None,context['message_text'],None,None,stamp),
        (2,'session','assistant',None,'',json.dumps(calls),None,stamp),
        (3,'session','tool','clawops_delegate',json.dumps(result),None,'other' if fault=='call_id' else cid,stamp)])
    kwargs=dict(tool_message_id=3,owner_user_id='wrong' if fault=='owner' else 'owner')
    if fault:
        with pytest.raises(ValueError): ar.import_gateway_rejection(conn, state, **kwargs)
    else:
        assert ar.import_gateway_rejection(conn,state,**kwargs)['approved_message_id']=='9198'
        assert ar.valid_receipt(conn, token, 'f'*64, '9198')


def test_import_uses_owner_message_time_before_delayed_tool_result(case, monkeypatch):
    conn, challenge, context = case
    state = sqlite3.connect(':memory:'); state.row_factory = sqlite3.Row
    state.execute('create table messages(id integer,session_id text,role text,tool_name text,content text,tool_calls text,tool_call_id text,timestamp real)')
    token = challenge['token']; cid = f'gateway_approval_{token}_9198_1'
    args = dict(approval_token=token, _approval_compiled_contract={'exact':'contract'})
    calls = [{'id': cid, 'function': {'name':'clawops_delegate','arguments':json.dumps(args)}}]
    result = dict(reason='Objective already has an in-flight delegation; observe it before publishing', status='rejected', task_created=False)
    owner_time = challenge['expires_at'] - 1
    monkeypatch.setattr(ar.time, 'time', lambda: challenge['expires_at'] + 2)
    state.executemany('insert into messages values (?,?,?,?,?,?,?,?)', [
        (1,'session','user',None,context['message_text'],None,None,owner_time),
        (2,'session','assistant',None,'',json.dumps(calls),None,challenge['expires_at']),
        (3,'session','tool','clawops_delegate',json.dumps(result),None,cid,challenge['expires_at'] + 1)])

    imported = ar.import_gateway_rejection(
        conn, state, tool_message_id=3, owner_user_id='owner'
    )

    assert imported['accepted_at'] == owner_time
    assert ar.valid_receipt(conn, token, 'f'*64, '9198')


def test_blocked_receipts_do_not_starve_later_requests(case, monkeypatch):
    from contextlib import contextmanager
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    base = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    challenge_base = dict(conn.execute('select * from grace_approval_challenges').fetchone())
    conn.execute('delete from grace_approval_receipts')
    for i in range(21):
        c = {**challenge_base, 'token':f'token{i}', 'contract_fingerprint':f'fingerprint{i}'}
        conn.execute('insert into grace_approval_challenges('+','.join(c)+') values ('+','.join('?' for _ in c)+')', tuple(c.values()))
        row = {**base, 'token':c['token'], 'contract_fingerprint':c['contract_fingerprint'],
               'accepted_at':i,'next_retry_at':0,'args':json.dumps({'objective_ref':{'objective_id':'free' if i==20 else 'busy'}})}
        conn.execute('insert into grace_approval_receipts('+','.join(row)+') values ('+','.join('?' for _ in row)+')', tuple(row.values()))
    @contextmanager
    def connect(**kwargs): yield conn
    monkeypatch.setattr(kb,'connect_closing',connect)
    monkeypatch.setattr(kb,'get_grace_delegation',lambda *a,**kw:None)
    monkeypatch.setattr(kb,'get_grace_objective',lambda *a:{'status':'active'})
    monkeypatch.setattr('hermes_cli.objective_workflow.has_in_flight_delegation',lambda c,o,**kw:o=='busy')
    monkeypatch.setattr('plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',lambda args:json.dumps({'status':'queued'}))
    assert ar.recover_pending() is None
    assert ar.recover_pending()['status']=='queued'
    assert conn.execute("select count(*) from grace_approval_receipts where attempts=0 and next_retry_at>0").fetchone()[0]==20


def test_transient_dispatch_failures_remain_pending_with_bounded_backoff(case, monkeypatch):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    board = conn.execute('pragma database_list').fetchone()['file']
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: Path(board))
    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        lambda args: json.dumps({
            'status': 'rejected', 'reason': 'temporary outage', 'retryable': True,
        }),
    )
    for _ in range(4):
        conn.execute(
            "UPDATE grace_approval_receipts SET next_retry_at=0 WHERE token=?",
            (challenge['token'],),
        )
        assert ar.recover_pending()['status'] == 'rejected'
    row = conn.execute(
        "SELECT state,attempts,next_retry_at FROM grace_approval_receipts WHERE token=?",
        (challenge['token'],),
    ).fetchone()
    assert row['state'] == 'pending'
    assert row['attempts'] == 4
    assert 0 < row['next_retry_at'] - int(time.time()) <= 3600

    conn.execute(
        "UPDATE grace_approval_receipts SET next_retry_at=0 WHERE token=?",
        (challenge['token'],),
    )
    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        lambda args: json.dumps({
            'status': 'rejected', 'reason': 'sealed contract changed',
            'retryable': False,
        }),
    )
    assert ar.recover_pending()['status'] == 'rejected'
    assert conn.execute(
        "SELECT state FROM grace_approval_receipts WHERE token=?",
        (challenge['token'],),
    ).fetchone()['state'] == 'attention'


def test_transient_dispatch_moves_to_attention_after_bounded_attempts(case, monkeypatch):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    board = conn.execute('pragma database_list').fetchone()['file']
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: Path(board))
    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        lambda args: json.dumps({
            'status': 'rejected', 'reason': 'temporary outage', 'retryable': True,
        }),
    )
    for attempt in range(1, ar._MAX_AUTOMATIC_DISPATCH_ATTEMPTS + 1):
        conn.execute(
            "UPDATE grace_approval_receipts SET next_retry_at=0 WHERE token=?",
            (challenge['token'],),
        )
        assert ar.recover_pending()['status'] == 'rejected'
        row = conn.execute(
            "SELECT state,attempts FROM grace_approval_receipts WHERE token=?",
            (challenge['token'],),
        ).fetchone()
        assert row['attempts'] == attempt
        assert row['state'] == (
            'attention' if attempt == ar._MAX_AUTOMATIC_DISPATCH_ATTEMPTS else 'pending'
        )


def test_crashed_recovery_cannot_dispatch_after_attempt_cap(case, monkeypatch):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    conn.execute(
        "UPDATE grace_approval_receipts SET attempts=?,next_retry_at=0 WHERE token=?",
        (ar._MAX_AUTOMATIC_DISPATCH_ATTEMPTS, challenge['token']),
    )
    board = conn.execute('pragma database_list').fetchone()['file']
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: Path(board))
    calls = []
    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        lambda args: calls.append(args),
    )

    assert ar.recover_pending() is None
    row = conn.execute(
        "SELECT state,attempts FROM grace_approval_receipts WHERE token=?",
        (challenge['token'],),
    ).fetchone()
    assert dict(row) == {
        'state': 'attention',
        'attempts': ar._MAX_AUTOMATIC_DISPATCH_ATTEMPTS,
    }
    assert calls == []


def test_stale_recovery_worker_cannot_overwrite_a_newer_claim(case, monkeypatch):
    conn, challenge, context = case
    ar.record(
        conn,
        token=challenge['token'],
        fingerprint='f' * 64,
        context=context,
        args={},
    )
    conn.execute(
        "UPDATE grace_approval_receipts SET next_retry_at=0 WHERE token=?",
        (challenge['token'],),
    )
    board = Path(conn.execute('pragma database_list').fetchone()['file'])
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: board)
    newer_deadline = int(time.time()) + 1200

    def supersede_claim(_args):
        with kb.connect_closing() as other, kb.write_txn(other):
            other.execute(
                "UPDATE grace_approval_receipts "
                "SET attempts=attempts+1,next_retry_at=? WHERE token=?",
                (newer_deadline, challenge['token']),
            )
        return json.dumps({
            'status': 'rejected',
            'reason': 'older dispatch finished late',
            'retryable': True,
        })

    monkeypatch.setattr(
        'plugins.openclaw_bridge.clawops_delegate.handle_clawops_delegate',
        supersede_claim,
    )

    assert ar.recover_pending()['status'] == 'rejected'
    row = conn.execute(
        "SELECT state,attempts,next_retry_at,last_result "
        "FROM grace_approval_receipts WHERE token=?",
        (challenge['token'],),
    ).fetchone()
    assert dict(row) == {
        'state': 'pending',
        'attempts': 2,
        'next_retry_at': newer_deadline,
        'last_result': None,
    }


def test_attention_receipt_can_resume_without_changing_authorization(case):
    conn, challenge, context = case
    ar.record(conn, token=challenge['token'], fingerprint='f'*64, context=context, args={})
    conn.execute("update grace_approval_receipts set state='attention',attempts=3")
    before = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    assert ar.resume(conn, challenge['token'])['state']=='pending'
    after = dict(conn.execute('select * from grace_approval_receipts').fetchone())
    assert after['attempts']==0
    for key in ('token','contract_fingerprint','approved_message_id','accepted_at','context','args'):
        assert after[key]==before[key]
    assert kb.get_grace_approval_challenge(conn, challenge['token'])['expires_at']==challenge['expires_at']
    with pytest.raises(ValueError): ar.resume(conn, challenge['token'])
