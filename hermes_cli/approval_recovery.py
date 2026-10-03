"""Durable owner decisions; dispatch retries never extend a challenge's expiry."""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import time

from hermes_cli import kanban_db as kb


_MAX_AUTOMATIC_DISPATCH_ATTEMPTS = 5


def valid_receipt(conn, token, fingerprint, message_id):
    return conn.execute(
        "SELECT 1 FROM grace_approval_receipts r JOIN grace_approval_challenges c ON c.token=r.token "
        "WHERE r.token=? AND r.contract_fingerprint=? AND r.approved_message_id=? "
        "AND r.state='pending' "
        "AND c.contract_fingerprint=r.contract_fingerprint AND c.state IN ('pending','consumed') "
        "AND r.accepted_at>=c.created_at AND r.accepted_at<c.expires_at",
        (token, fingerprint, message_id),
    ).fetchone() is not None


def record(conn, *, token, fingerprint, context, args, observed_at=None):
    """Native controller only: caller already verified the exact owner message.

    observed_at is only for importing a proven pre-fix gateway rejection. It is
    never exposed as a model argument, and does not change challenge expiry.
    """
    now = int(time.time())
    accepted_at = now if observed_at is None else observed_at
    message_id = context.get('message_id', '')
    with kb.write_txn(conn):
        challenge = kb.get_grace_approval_challenge(conn, token)
        if not challenge:
            raise ValueError('Approval challenge not found')
        if (challenge['contract_fingerprint'] != fingerprint
                or any(context.get(k, '') != challenge[k] for k in
                       ('platform', 'chat_id', 'thread_id', 'session_key'))
                or not context.get('owner_user_id')
                or context.get('user_id') != context['owner_user_id']
                or hashlib.sha256(context['user_id'].encode()).hexdigest() != challenge['user_id_sha256']
                or not message_id or message_id == challenge['requested_message_id']):
            raise ValueError('Approval receipt identity or contract mismatch')
        from plugins.openclaw_bridge.clawops_delegate import _is_safe_approval_message, _is_same_compression_lineage
        if (not _is_safe_approval_message(context.get('message_text', ''), token)
                or not _is_same_compression_lineage(challenge['session_id'], context.get('session_id', ''))):
            raise ValueError('Approval receipt message or session mismatch')
        existing = conn.execute('SELECT * FROM grace_approval_receipts WHERE token=?', (token,)).fetchone()
        if existing:
            if existing['approved_message_id'] != message_id or existing['contract_fingerprint'] != fingerprint:
                raise ValueError('Approval receipt cannot be rebound')
            if existing['state'] != 'pending':
                raise ValueError('Approval receipt is not dispatchable')
            return
        if (challenge['state'] != 'pending' or type(accepted_at) is not int
                or not challenge['created_at'] <= accepted_at < challenge['expires_at']
                or accepted_at > now):
            raise ValueError('Approval was not received within the challenge lifetime')
        sealed = json.loads(challenge['delegation_args'] or '{}').get('_approval_compiled_contract')
        replay_args = dict(args)
        if isinstance(sealed, dict):
            replay_args['_approval_compiled_contract'] = sealed
        conn.execute(
            'INSERT INTO grace_approval_receipts(token,contract_fingerprint,approved_message_id,accepted_at,context,args,next_retry_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (token, fingerprint, message_id, accepted_at, json.dumps(context), json.dumps(replay_args), now + 30),
        )


def recover_pending(board=None):
    """One bounded native dispatch per tick, reusing the original authenticated turn.

    Does not inject a user message. A fresh Context isolates stored provenance
    from the notifier; existing reservation/build idempotency prevents duplicates.
    """
    from hermes_cli.objective_workflow import has_in_flight_delegation
    now = int(time.time())
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute("SELECT * FROM grace_approval_receipts WHERE state='pending' AND next_retry_at<=? ORDER BY accepted_at LIMIT 20", (now,)).fetchall()
        claimed = None
        for row in rows:
            with kb.write_txn(conn):
                current = conn.execute(
                    "SELECT * FROM grace_approval_receipts "
                    "WHERE token=? AND state='pending' AND next_retry_at<=?",
                    (row['token'], now),
                ).fetchone()
                if current is None:
                    continue
                if int(current['attempts']) >= _MAX_AUTOMATIC_DISPATCH_ATTEMPTS:
                    exhausted = json.dumps({
                        'status': 'attention',
                        'reason': 'automatic dispatch attempt limit reached',
                    })
                    conn.execute(
                        "UPDATE grace_approval_receipts SET state='attention',last_result=? "
                        "WHERE token=? AND state='pending' AND attempts>=?",
                        (exhausted, row['token'], _MAX_AUTOMATIC_DISPATCH_ATTEMPTS),
                    )
                    continue
                args = json.loads(current['args'])
                objective_id = (args.get('_approval_compiled_contract', {}).get('objective_ref') or args.get('objective_ref') or {}).get('objective_id')
                objective = kb.get_grace_objective(conn, objective_id) if objective_id else None
                delegation = kb.get_grace_delegation(conn, contract_fingerprint=current['contract_fingerprint'])
                if delegation and delegation.get('state') == 'queued':
                    conn.execute("UPDATE grace_approval_receipts SET state='queued' WHERE token=? AND state='pending'", (row['token'],))
                    continue
                if delegation and delegation.get('state') == 'cancelled':
                    conn.execute("UPDATE grace_approval_receipts SET state='cancelled' WHERE token=? AND state='pending'", (row['token'],))
                    continue
                if objective and objective['status'] in ('cancelled', 'completed'):
                    conn.execute("UPDATE grace_approval_receipts SET state='cancelled' WHERE token=? AND state='pending'", (row['token'],))
                    continue
                # An authorized reservation is our own saga, not another task.
                if objective_id and has_in_flight_delegation(conn, objective_id,
                        excluding_delegation_id=(delegation or {}).get("delegation_id", "")):
                    conn.execute("UPDATE grace_approval_receipts SET next_retry_at=? WHERE token=? AND state='pending'", (now+60, row['token']))
                    continue
                cur = conn.execute("UPDATE grace_approval_receipts SET attempts=attempts+1,next_retry_at=? WHERE token=? AND state='pending' AND next_retry_at<=? AND attempts<?", (now+600, row['token'], now, _MAX_AUTOMATIC_DISPATCH_ATTEMPTS))
                if cur.rowcount:
                    claimed = dict(current)
                    claimed['claim_attempt'] = int(current['attempts']) + 1
                    claimed['claim_next_retry_at'] = now + 600
                    break
        if claimed is None:
            return None
    def dispatch():
        from gateway.session_context import set_session_vars, clear_session_vars
        from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate
        tokens = set_session_vars(**json.loads(claimed['context']))
        try:
            return json.loads(handle_clawops_delegate(json.loads(claimed['args'])))
        finally:
            clear_session_vars(tokens)
    try:
        result = contextvars.Context().run(dispatch)
    except Exception as exc:
        result = {
            'status': 'rejected',
            'reason': str(exc),
            'retryable': isinstance(exc, (OSError, sqlite3.Error, RuntimeError)),
        }
    attempt_count = int(claimed['attempts']) + 1
    state = ('queued' if result.get('status') == 'queued' else
             'pending' if result.get('retryable') is True
             and attempt_count < _MAX_AUTOMATIC_DISPATCH_ATTEMPTS else 'attention')
    retry_delay = min(3600, 60 * (2 ** min(int(claimed['attempts']), 6)))
    with kb.connect_closing(board=board) as conn, kb.write_txn(conn):
        conn.execute(
            'UPDATE grace_approval_receipts SET state=?,next_retry_at=?,last_result=? '
            'WHERE token=? AND state=\'pending\' AND attempts=? AND next_retry_at=?',
            (
                state,
                int(time.time()) + retry_delay,
                json.dumps(result),
                claimed['token'],
                claimed['claim_attempt'],
                claimed['claim_next_retry_at'],
            ),
        )
    return result


def hold(conn, token, *, fingerprint, reason):
    """Operator hold before a first dispatch; retain the original authorization.

    Refuse any attempted receipt: a dispatcher may already be outside the DB
    transaction. Holding its row would not stop that external operation.
    """
    if not str(reason or '').strip():
        raise ValueError('A recovery hold requires a reason')
    with kb.write_txn(conn):
        row = conn.execute('SELECT * FROM grace_approval_receipts WHERE token=?', (token,)).fetchone()
        if row is None or row['contract_fingerprint'] != fingerprint:
            raise ValueError('Retained approval identity mismatch')
        if row['state'] != 'pending' or row['attempts'] != 0:
            raise ValueError('Only an unattempted pending receipt can be held')
        if kb.get_grace_delegation(conn, contract_fingerprint=fingerprint):
            raise ValueError('A bound delegation must be reconciled before holding its receipt')
        result = {'status': 'held', 'reason': reason.strip()}
        conn.execute("UPDATE grace_approval_receipts SET state='attention',last_result=? WHERE token=?",
                     (json.dumps(result), token))
        return dict(result, state='attention', approved_message_id=row['approved_message_id'],
                    contract_fingerprint=row['contract_fingerprint'])


def resume(conn, token):
    """Explicit operator recovery after fixing an attention-state fault.

    This resets dispatch attempts, never owner identity, scope, or token expiry.
    The normal dispatcher still checks cancellation, other tasks and publication.
    """
    with kb.write_txn(conn):
        row = conn.execute("SELECT r.*,c.state challenge_state FROM grace_approval_receipts r "
                           "JOIN grace_approval_challenges c ON c.token=r.token WHERE r.token=?", (token,)).fetchone()
        if row is None or row['state'] != 'attention' or row['challenge_state'] not in ('pending','consumed'):
            raise ValueError('Only a retained approval in attention can resume')
        args = json.loads(row['args'])
        objective_id = (args.get('_approval_compiled_contract', {}).get('objective_ref') or args.get('objective_ref') or {}).get('objective_id')
        objective = kb.get_grace_objective(conn, objective_id) if objective_id else None
        if objective and objective['status'] in ('cancelled','completed'):
            raise ValueError('Cannot resume a closed objective')
        conn.execute("UPDATE grace_approval_receipts SET state='pending',attempts=0,next_retry_at=? WHERE token=?", (int(time.time()),token))
        return {'state':'pending','approved_message_id':row['approved_message_id'],
                'contract_fingerprint':row['contract_fingerprint']}


def import_gateway_rejection(conn, state_conn, *, tool_message_id, owner_user_id):
    """Operator migration of one historical native in-flight rejection.

    Require the adjacent persisted user/call/result triple and exact sealed
    contract. Free-form assistant reports are never authorization evidence.
    """
    result = state_conn.execute('SELECT * FROM messages WHERE id=?', (tool_message_id,)).fetchone()
    if not result or result['role'] != 'tool' or result['tool_name'] != 'clawops_delegate':
        raise ValueError('Expected a native delegate tool result')
    payload = json.loads(result['content'])
    if payload != {'reason': 'Objective already has an in-flight delegation; observe it before publishing',
                   'status': 'rejected', 'task_created': False}:
        raise ValueError('Only the proven pre-dispatch in-flight rejection can be imported')
    call = state_conn.execute('SELECT * FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 1',
                              (result['session_id'], result['id'])).fetchone()
    user = state_conn.execute('SELECT * FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 1',
                              (result['session_id'], call['id'])).fetchone() if call else None
    if not call or call['role'] != 'assistant' or not user or user['role'] != 'user':
        raise ValueError('Missing adjacent authenticated gateway approval transcript')
    timestamps = (user['timestamp'], call['timestamp'], result['timestamp'])
    if (any(type(stamp) not in (int, float) for stamp in timestamps)
            or not timestamps[0] <= timestamps[1] <= timestamps[2]):
        raise ValueError('Approval transcript chronology is invalid')
    calls = json.loads(call['tool_calls'])
    if len(calls) != 1 or calls[0]['function']['name'] != 'clawops_delegate':
        raise ValueError('Ambiguous approval tool call')
    args = json.loads(calls[0]['function']['arguments'])
    token = args.get('approval_token', '')
    challenge = kb.get_grace_approval_challenge(conn, token)
    if not challenge or json.loads(challenge['delegation_args']).get('_approval_compiled_contract') != args.get('_approval_compiled_contract'):
        raise ValueError('Historical call differs from the sealed challenge')
    prefix = f'gateway_approval_{token}_'
    call_id = calls[0]['id']
    if not call_id.startswith(prefix) or not call_id.endswith('_1') or result['tool_call_id'] != call_id:
        raise ValueError('Missing native gateway approval call identity')
    message_id = call_id[len(prefix):-2]
    text = user['content'] or ''
    # The gateway stores display-name framing in transcripts; the original
    # guarded tool result proves the stripped message passed owner validation.
    if text.startswith('[') and '] ' in text:
        text = text.split('] ', 1)[1]
    context = {k: challenge[k] for k in ('platform','chat_id','thread_id','session_key')}
    context.update(source='telegram', session_id=result['session_id'], message_id=message_id,
                   message_text=text, user_id=owner_user_id, owner_user_id=owner_user_id)
    record(conn, token=token, fingerprint=challenge['contract_fingerprint'], context=context,
           args=args, observed_at=int(user['timestamp']))
    return {'approved_message_id': message_id, 'tool_message_id': tool_message_id,
            'contract_fingerprint': challenge['contract_fingerprint'], 'accepted_at': int(user['timestamp'])}


def main():
    import argparse
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('recover', 'import-rejection', 'resume', 'hold'))
    parser.add_argument('--board', default='default')
    parser.add_argument('--token')
    parser.add_argument('--fingerprint')
    parser.add_argument('--reason')
    parser.add_argument('--state-db')
    parser.add_argument('--tool-message-id', type=int)
    parser.add_argument('--owner-user-id')
    args = parser.parse_args()
    if args.command == 'recover':
        print(json.dumps(recover_pending(args.board), ensure_ascii=False))
    elif args.command == 'hold':
        if not all((args.token, args.fingerprint, args.reason)):
            parser.error('hold requires token, fingerprint and reason')
        with kb.connect_closing(board=args.board) as conn:
            print(json.dumps(hold(conn, args.token, fingerprint=args.fingerprint, reason=args.reason)))
    elif args.command == 'resume':
        if not args.token:
            parser.error('resume requires the retained receipt token')
        with kb.connect_closing(board=args.board) as conn:
            print(json.dumps(resume(conn, args.token)))
    else:
        if not all((args.state_db, args.tool_message_id, args.owner_user_id)):
            parser.error('import requires state DB, tool message ID and configured owner ID')
        with sqlite3.connect(f'file:{args.state_db}?mode=ro', uri=True) as state:
            state.row_factory = sqlite3.Row
            with kb.connect_closing(board=args.board) as conn:
                print(json.dumps(import_gateway_rejection(conn, state, tool_message_id=args.tool_message_id,
                                                         owner_user_id=args.owner_user_id)))


if __name__ == '__main__':
    main()
