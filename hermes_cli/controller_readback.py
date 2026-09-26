"""Read controller-owned bindings and history without model-authored claims."""
import hashlib
import json
import re
import time

from hermes_cli.grace_review_metadata import grace_review_accepted


def task_controller_readback(conn, task_id):
    """Observe all fields in one snapshot, preserving an existing transaction."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute('BEGIN')
    try:
        return _snapshot_readback(conn, task_id)
    finally:
        if own_snapshot:
            conn.rollback()


def _snapshot_readback(conn, task_id, *, include_related=True):
    task = conn.execute("SELECT id,status,assignee,executor_backend,created_at,block_recurrences,idempotency_key,current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("Unknown task")
    delegations = conn.execute(
        "SELECT delegation_id,contract_fingerprint,state,objective_id,stage_key,execution_task_id,review_task_id,origin_review_task_id,origin_event_id,created_at "
        "FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?", (task_id, task_id)
    ).fetchall()
    binding = dict(delegations[0]) if len(delegations) == 1 else None
    objective = None
    stages = []
    binding_verified = False
    if binding and binding['objective_id']:
        row = conn.execute(
            "SELECT objective_id,status,current_stage_key,terminal_stage_key,required_stage_keys,original_request_sha256,created_at,revision "
            "FROM grace_objectives WHERE objective_id=?", (binding['objective_id'],)
        ).fetchone()
        objective = dict(row) if row else None
        stages = [dict(r) for r in conn.execute(
            "SELECT stage_key,position,status,delegation_id,execution_task_id,review_task_id,outcome_kind "
            "FROM grace_objective_stages WHERE objective_id=? ORDER BY position", (binding['objective_id'],)
        )]
        binding_verified = bool(objective and any(
            s['stage_key'] == binding['stage_key'] and all(s[k] == binding[k] for k in
                ('delegation_id', 'execution_task_id', 'review_task_id')) for s in stages
        ))
    runs = [dict(r) for r in conn.execute(
        "SELECT id,status,outcome,started_at,ended_at FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 100", (task_id,)
    )][::-1]
    events = [dict(r) for r in conn.execute(
        "SELECT id,run_id,kind,created_at FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 100", (task_id,)
    )][::-1]
    run_count = conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0]
    event_count = conn.execute('SELECT count(*) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
    # Stream complete controller rows in primary-key order; no full history is
    # materialized. Length framing makes table and row boundaries unambiguous.
    digest = hashlib.sha256()
    for table, key in (('tasks', 'id'), ('task_runs', 'task_id'),
                       ('task_events', 'task_id'), ('task_comments', 'task_id'),
                       ('task_external_effects', 'task_id')):
        columns = list(conn.execute(f'PRAGMA table_info({table})'))
        primary = sorted((r for r in columns if r['pk']), key=lambda r: r['pk'])
        ordering = ','.join('"' + r['name'] + '"' for r in (primary or columns))
        encoded_table = table.encode()
        digest.update(len(encoded_table).to_bytes(8, 'big'))
        digest.update(encoded_table)
        for row in conn.execute(f'SELECT * FROM {table} WHERE {key}=? ORDER BY {ordering}', (task_id,)):
            encoded = json.dumps(dict(row), sort_keys=True, separators=(',', ':')).encode()
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
    related = []
    if include_related and binding_verified:
        owner = conn.execute(
            'SELECT platform,chat_id,thread_id,contract_snapshot FROM grace_delegations WHERE delegation_id=?',
            (binding['delegation_id'],),
        ).fetchone()
        # References come from the sealed contract, never a caller-supplied list.
        references = sorted(set(re.findall(r'\bt_[0-9a-f]{8}\b', owner['contract_snapshot'] or '')))
        for referenced_id in references[:50]:
            if referenced_id == task_id:
                continue
            membership = conn.execute(
                'SELECT platform,chat_id,thread_id FROM grace_delegations '
                'WHERE (execution_task_id=? OR review_task_id=?) '
                'AND EXISTS(SELECT 1 FROM tasks WHERE id=?)',
                (referenced_id, referenced_id, referenced_id),
            ).fetchall()
            if len(membership) != 1 or any(
                (membership[0][key] or '') != (owner[key] or '')
                for key in ('platform', 'chat_id', 'thread_id')
            ):
                continue
            observed = _snapshot_readback(conn, referenced_id, include_related=False)
            prior = conn.execute(
                "SELECT id,metadata FROM task_runs WHERE task_id=? AND status='done' "
                "AND outcome='completed' ORDER BY id DESC LIMIT 1", (referenced_id,),
            ).fetchone()
            recorded_review = None
            if (prior and observed["binding_verified"]
                    and referenced_id == (observed["delegation"] or {}).get("review_task_id")):
                try:
                    metadata = json.loads(prior['metadata'] or '{}')
                except (TypeError, ValueError):
                    metadata = {}
                if grace_review_accepted(metadata):
                    recorded_review = {
                        'source': 'completed_review_run_metadata', 'run_id': prior['id'],
                        'review_outcome': 'accepted', 'verification': metadata.get('verification'),
                    }
            related.append({
                'task': observed['task'], 'delegation': observed['delegation'],
                'binding_verified': observed['binding_verified'],
                'history_sha256': observed['history']['sha256'],
                'external_effect_count': observed['external_effect_count'],
                'recorded_review': recorded_review,
            })
    return {'source': 'controller_database', 'observed_at': int(time.time()),
            'referenced_task_readbacks': related,
            'referenced_task_limit': 50,
            'referenced_tasks_truncated': bool(include_related and binding_verified and len(references) > 50),
            'task': dict(task), 'delegation': binding, 'objective': objective, 'stages': stages,
            'binding_verified': binding_verified, 'delegation_count': len(delegations),
            'history': {'sha256': digest.hexdigest(), 'run_count': run_count, 'event_count': event_count,
                        'runs': runs[-100:], 'events': events[-100:],
                        'truncated': run_count > 100 or event_count > 100},
            'external_effect_count': conn.execute('SELECT count(*) FROM task_external_effects WHERE task_id=?', (task_id,)).fetchone()[0]}


def native_history_baseline(conn, contract):
    """Capture sealed controller references; contract carries selectors only.

    Native admission already authenticates the execution card. The worker-safe
    input is a redacted projection (and corrections have a different child
    fingerprint); it cannot be hashed as the original sealed delegation. No
    goal, reference list or fingerprint supplied here enters this baseline.
    """
    receipt = contract.get('control_plane_receipt') or {}
    execution_id = receipt.get('execution_task_id')
    if not execution_id:
        return None
    readback = task_controller_readback(conn, execution_id)
    reference = contract.get('objective_ref') or {}
    binding = readback['delegation']
    if not readback['binding_verified'] or any((binding.get(key) or '') != (value or '')
        for key, value in (
            ('objective_id', reference.get('objective_id')),
            ('stage_key', reference.get('stage_key')),
            ('delegation_id', receipt.get('delegation_id')),
            ('review_task_id', receipt.get('grace_review_task_id')),
        )):
        raise ValueError('Native history baseline does not match the controller binding')
    if readback['task']['status'] != 'running' or not readback['task']['current_run_id']:
        raise ValueError('Native history baseline requires a running controller execution')
    return {
        'source': 'controller_pre_admission_snapshot',
        'observed_at': readback['observed_at'],
        'execution_task_id': execution_id,
        'execution_run_id': readback['task']['current_run_id'],
        'delegation_id': binding['delegation_id'],
        'delegation_contract_fingerprint': binding['contract_fingerprint'],
        'truncated': readback['referenced_tasks_truncated'],
        'tasks': [{'task_id': r['task']['id'], 'status': r['task']['status'],
                   'history_sha256': r['history_sha256']}
                  for r in readback['referenced_task_readbacks']],
    }


def compare_native_history_baseline(readback, baseline, *, execution_run_id):
    """Compare only an exact persisted run; missing evidence stays unverified."""
    binding = readback.get('delegation') or {}
    latest_run_id = (readback['history']['runs'][-1]['id']
        if readback['history']['runs'] else None)
    matched = type(execution_run_id) is int and execution_run_id > 0 and isinstance(baseline, dict) and readback.get('binding_verified') is True and (
        baseline.get('source') == 'controller_pre_admission_snapshot'
        and baseline.get('execution_task_id') == readback['task']['id']
        and baseline.get('execution_run_id') == execution_run_id
        and latest_run_id == execution_run_id
        and readback['task'].get('current_run_id') in (None, execution_run_id)
        and baseline.get('delegation_id') == binding.get('delegation_id')
        and baseline.get('delegation_contract_fingerprint') == binding.get('contract_fingerprint')
        and baseline.get('truncated') is False
        and not readback.get('referenced_tasks_truncated')
    )
    current = {r['task']['id']: r for r in readback['referenced_task_readbacks']}
    comparisons = []
    before = baseline.get('tasks') if matched else None
    if not isinstance(before, list) or any(not isinstance(r, dict) or not all(
        isinstance(r.get(k), str) for k in ('task_id', 'status', 'history_sha256')
    ) for r in before):
        matched = False
        before = None
    if isinstance(before, list):
        for row in before:
            now = current.get(row['task_id'])
            comparisons.append({**row,
                'current_status': now['task']['status'] if now else None,
                'current_history_sha256': now['history_sha256'] if now else None,
                'unchanged': bool(now and now['task']['status'] == row['status']
                    and now['history_sha256'] == row['history_sha256']),
            })
    return {'source': 'controller_run_snapshot_comparison',
            'baseline_binding_verified': bool(matched),
            'all_referenced_tasks_unchanged': bool(matched and isinstance(before, list)
                and set(current) == {r['task_id'] for r in before}
                and all(r['unchanged'] for r in comparisons)),
            'comparisons': comparisons}
