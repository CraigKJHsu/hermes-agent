"""Controller wakeup of an already authorized owner Objective, never an operator grant.

The trusted Objective record authorizes its planned stage. No caller-controlled
operator identity, environment flag, CLI request or new approval is accepted.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
import re
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.controller_readback import task_controller_readback, evidence_file_sha256, MAX_EVIDENCE_FILE_BYTES
from proactive.behavior_profiles import registry as br

ENVELOPE = "[SYSTEM: Owner Objective stage wakeup]\n"
SCHEMA = """
CREATE TABLE IF NOT EXISTS grace_objective_stage_wakeups (
 request_id TEXT PRIMARY KEY, objective_id TEXT NOT NULL, revision INTEGER NOT NULL,
 stage_key TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL,
 lease TEXT, lease_expires INTEGER, created_at INTEGER NOT NULL, error TEXT,
 UNIQUE(objective_id,revision,stage_key)
);
"""



MAX_CANDIDATE_BYTES = MAX_EVIDENCE_FILE_BYTES


def _candidate_digest(value, *, _file_budget=None):
    digest = evidence_file_sha256(value, _file_budget=_file_budget)
    if digest is None:
        raise ValueError('Historical candidate bytes unavailable or outside bounded regular-file evidence')
    return digest


def _candidate_list(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('Historical execution metadata must be a mapping')
    blocked = metadata.get('loop_contract_blocked_result', {})
    if not isinstance(blocked, dict) or not isinstance(blocked.get('acceptanceEvidence', {}), dict):
        raise ValueError('Historical candidate evidence must be a mapping')
    assets = blocked.get('acceptanceEvidence', {}).get('image_assets', [])
    if not isinstance(assets, list) or len(assets) > 16:
        raise ValueError('Historical candidate list exceeds evidence budget')
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get('asset_family'), str) or not isinstance(asset.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']):
            raise ValueError('Historical candidate metadata is invalid')
    return assets


def _idle(conn, objective_id, revision, stage_key):
    from hermes_cli.objective_workflow import has_in_flight_delegation
    objective = kb.get_grace_objective(conn, objective_id)
    stage = conn.execute("SELECT * FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
                         (objective_id, stage_key)).fetchone()
    if not (objective and objective['status'] == 'active' and not objective['waiting_for'] and objective['revision'] == revision
            and objective['current_stage_key'] == stage_key and stage
            and stage['status'] == 'planned' and not any(stage[k] for k in
                ('delegation_id', 'execution_task_id', 'review_task_id', 'evidence'))
            and not has_in_flight_delegation(conn, objective_id)):
        raise ValueError('Recovery requires the exact idle, unbound current Objective stage')
    from hermes_cli.grace_review_metadata import grace_review_accepted
    for prior in conn.execute('SELECT * FROM grace_objective_stages WHERE objective_id=? AND position<?', (objective_id, stage['position'])):
        reviewed = kb.latest_run(conn, prior['review_task_id']) if prior['review_task_id'] else None
        parent = kb.reviewed_execution_run(conn, reviewed, prior['execution_task_id']) if reviewed and prior['execution_task_id'] else None
        if not (prior['status'] == 'done' and reviewed and reviewed.ended_at
                and reviewed.status == 'done' and reviewed.outcome == 'completed'
                and grace_review_accepted(reviewed.metadata or {}) and parent and parent.ended_at
                and parent.status == 'done' and parent.outcome == 'completed'):
            raise ValueError('Earlier Objective stages require exact accepted Grace review before continuation')
    if conn.execute("SELECT 1 FROM grace_loop_callbacks WHERE objective_id=? AND (state NOT IN ('delivered','cancelled') OR lease_owner IS NOT NULL) LIMIT 1",
                    (objective_id,)).fetchone():
        raise ValueError('An Objective callback still requires reconciliation')
    if conn.execute('SELECT 1 FROM task_external_effects e JOIN grace_objective_stages s ON e.task_id IN (s.execution_task_id,s.review_task_id) WHERE s.objective_id=? LIMIT 1', (objective_id,)).fetchone():
        raise ValueError('Objective stages have recorded external effects')
    return objective


def _verify_snapshot(conn, snap):
    objective = kb.get_grace_objective(conn, snap['objective_id'])
    if not objective or objective['revision'] != snap['revision'] or objective['status'] != 'active' or objective['current_stage_key'] != snap['stage_key'] or tuple(objective[k] for k in ('platform','chat_id','thread_id','session_key')) != tuple(snap[k] for k in ('platform','chat_id','thread_id','session_key')):
        raise ValueError('Objective recovery identity or revision changed')
    if hashlib.sha256(objective['objective'].encode()).hexdigest() != snap['original_request_sha256'] or br.digest(br.get_pin(conn, snap['objective_id'])) != snap['pin_hash']:
        raise ValueError('Objective recovery source or pin changed')
    br.verify_pin(br.get_pin(conn, snap['objective_id']))
    historical = conn.execute('SELECT * FROM grace_delegations WHERE delegation_id=?', (snap['historical_delegation_id'],)).fetchone()
    if not historical or br.digest(dict(historical)) != snap['historical_delegation_sha256']:
        raise ValueError('Historical source delegation changed before recovery')
    file_budget = {'files': 0, 'bytes': 0}
    for tid, expected in snap['historical_baseline'].items():
        readback = task_controller_readback(conn, tid, _file_budget=file_budget)
        if not readback['attachments_available'] or not snap['historical_attachment_available'][tid] or readback['history']['sha256'] != expected or readback['attachments_sha256'] != snap['historical_attachment_baseline'][tid]:
            raise ValueError('Historical source task changed before recovery')
    for asset in snap['source_selectors']['candidate_assets']:
        if _candidate_digest(asset['path'], _file_budget=file_budget) != asset['sha256']:
            raise ValueError('Historical candidate asset changed before recovery')


def _schedule_owner_stage(conn, objective_id):
    # This is an internal lifecycle observer, not a privileged operator API.
    # Authorization is the existing owner-created active Objective itself; waking
    # Grace grants no identity, new scope, approval, or external-effect authority.
    if not re.fullmatch(r'go_user_[0-9a-f]{24}', objective_id):
        raise ValueError('Only an existing owner Objective can drive its stage')
    with kb.write_txn(conn):
        objective = kb.get_grace_objective(conn, objective_id)
        if not objective:
            raise ValueError('Unknown existing Objective')
        expected_revision = objective['revision']
        stage_key = objective['current_stage_key']
        platform,chat_id,thread_id = (objective[k] for k in ('platform','chat_id','thread_id'))
        original_request_sha256 = objective['original_request_sha256']
        expected_pin_hash = br.digest(br.get_pin(conn, objective_id))
        _idle(conn, objective_id, expected_revision, stage_key)
        request_id = 'gsw_' + br.digest([objective_id, expected_revision, stage_key])[:24]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_stage_wakeups'").fetchone():
            existing = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['state'] != 'attention':
                    return dict(existing)
                prior = json.loads(existing['snapshot'])
                attempts = prior.get('delivery_attempt_history', [])
                last_attempt = attempts[-1]['at'] if attempts else existing['created_at']
                if len(attempts) >= 2 or existing['lease'] is not None or int(time.time()) - last_attempt < 60:
                    return dict(existing)
        if (objective['platform'], objective['chat_id'], objective['thread_id']) != (platform, chat_id, thread_id):
            raise ValueError('Recovery Topic mismatch')
        if objective['original_request_sha256'] != original_request_sha256 or hashlib.sha256(objective['objective'].encode()).hexdigest() != original_request_sha256:
            raise ValueError('Original owner request changed')
        pin = br.get_pin(conn, objective_id)
        if not pin or br.digest(pin) != expected_pin_hash:
            raise ValueError('Objective pin changed')
        br.verify_pin(pin)
        references = set(re.findall(r'\bt_[0-9a-f]{8}\b', objective['objective']))
        rows = [r for r in conn.execute('SELECT * FROM grace_delegations') if r['review_task_id'] in references and r['execution_task_id'] in references]
        if not rows:
            raise ValueError('Owner source references require one exact historical execution/review pair')
        source_review_task_id = rows[0]['review_task_id']
        latest_review = kb.latest_run(conn, source_review_task_id)
        source_review_run_id = latest_review.id if latest_review else None
        if len(rows) != 1 or tuple(rows[0][k] for k in ('platform','chat_id','thread_id')) != (platform,chat_id,thread_id):
            raise ValueError('Historical source must have one exact same-Topic delegation')
        for task_id in (rows[0]['execution_task_id'], rows[0]['review_task_id']):
            memberships = conn.execute('SELECT delegation_id FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?', (task_id, task_id)).fetchall()
            if len(memberships) != 1 or memberships[0]['delegation_id'] != rows[0]['delegation_id']:
                raise ValueError('Historical source has ambiguous delegation membership')
        if rows[0]['execution_task_id'] == rows[0]['review_task_id']:
            raise ValueError('Historical source requires distinct execution and review tasks')
        historical = dict(rows[0])
        review = kb.get_run(conn, source_review_run_id)
        if not review or review.task_id != source_review_task_id or not review.ended_at:
            raise ValueError('Historical review run mismatch')
        source = (review.metadata or {}).get('workflow_review_source') or {}
        if source.get('parent_execution_task_id') != historical['execution_task_id'] or type(source.get('parent_execution_run_id')) is not int:
            raise ValueError('Historical review lacks an exact execution source')
        parent = kb.reviewed_execution_run(conn, review, historical['execution_task_id'])
        if not parent or not parent.ended_at:
            raise ValueError('Historical review source changed')
        original_contract = historical['contract_snapshot'] or ''
        contract_hash = hashlib.sha256(original_contract.encode()).hexdigest()
        if contract_hash != historical['contract_fingerprint']:
            raise ValueError('Historical sealed contract changed')
        contract = json.loads(original_contract)
        ids = [historical['execution_task_id'], source_review_task_id]
        file_budget = {'files': 0, 'bytes': 0}
        histories = {tid: task_controller_readback(conn, tid, _file_budget=file_budget) for tid in ids}
        if any(r['external_effect_count'] for r in histories.values()) or (parent.metadata or {}).get('external_effects') != [] or (review.metadata or {}).get('external_effects', []) != []:
            raise ValueError('Historical source has recorded external effects')
        if not all(r['attachments_available'] for r in histories.values()):
            raise ValueError('Historical attachment bytes unavailable')
        assets = _candidate_list(parent.metadata or {})
        candidates = []
        for index, asset in enumerate(assets):
            if _candidate_digest(asset.get('path'), _file_budget=file_budget) != asset['sha256']:
                raise ValueError('Historical asset provenance changed')
            candidates.append({'run_id': parent.id, 'json_pointer': f'/loop_contract_blocked_result/acceptanceEvidence/image_assets/{index}',
                               'asset_family': asset.get('asset_family'), 'path': asset['path'], 'sha256': asset['sha256'],
                               'acceptance_state': 'historical_candidate_not_accepted_package'})
        snapshot = {'source': 'controller_database', 'objective_id': objective_id,
            'revision': expected_revision, 'stage_key': stage_key, 'platform': platform,
            'chat_id': chat_id, 'thread_id': thread_id, 'session_key': objective['session_key'],
            'pin_hash': expected_pin_hash, 'behavior_pin': pin, 'original_request_sha256': original_request_sha256,
            'authorization_source': 'existing_owner_objective', 'external_authority_granted': False,
            'stages': [dict(r) for r in conn.execute('SELECT * FROM grace_objective_stages WHERE objective_id=? ORDER BY position', (objective_id,))],
            'original_request': objective['objective'],
            'source_selectors': {
                'owner_request': {'table': 'grace_objectives', 'key': objective_id, 'column': 'objective', 'sha256': original_request_sha256},
                'full_original_contract': {'table': 'grace_delegations', 'key': historical['delegation_id'], 'column': 'contract_snapshot', 'sha256': contract_hash},
                'original_source': {'table': 'grace_delegations', 'key': historical['delegation_id'], 'column': 'contract_snapshot', 'json_pointer': '/original_request', 'sha256': hashlib.sha256(str(contract.get('original_request') or '').encode()).hexdigest()},
                'review_evidence': {'table': 'task_runs', 'key': review.id, 'column': 'metadata', 'execution_run_id': parent.id,
                    'sha256': hashlib.sha256(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (review.id,)).fetchone()[0].encode()).hexdigest()},
                'candidate_assets': candidates},
            'historical_task_ids': ids,
            'historical_delegation_id': historical['delegation_id'],
            'historical_delegation_sha256': br.digest(historical),
            'historical_baseline': {tid: r['history']['sha256'] for tid,r in histories.items()},
            'historical_attachment_baseline': {tid: r['attachments_sha256'] for tid,r in histories.items()},
            'historical_attachment_available': {tid: r['attachments_available'] for tid,r in histories.items()},
            'review_acceptance_state': 'not_established_by_recovery',
            'controller_source_packet': {'original_request': contract.get('original_request'),
                                         'full_original_contract_snapshot': original_contract,
                                         'review_source': source, 'review_outcome': (review.metadata or {}).get('review_outcome'),
                                         'candidate_assets': candidates}}
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 256 * 1024:
            raise ValueError('Recovery source packet exceeds the exact inline budget')
        request_id = 'gsw_' + br.digest([objective_id, expected_revision, stage_key])[:24]
        conn.execute(SCHEMA)
        earlier = conn.execute('SELECT snapshot FROM grace_objective_stage_wakeups WHERE objective_id=? ORDER BY created_at LIMIT 1', (objective_id,)).fetchone()
        if earlier:
            original = json.loads(earlier['snapshot'])
            if original['historical_baseline'] != snapshot['historical_baseline'] or original['historical_attachment_baseline'] != snapshot['historical_attachment_baseline'] or original['historical_delegation_sha256'] != snapshot['historical_delegation_sha256']:
                raise ValueError('Owner Objective cannot rebase immutable historical source evidence')
        previous = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if previous['state'] == 'attention':
                prior = json.loads(previous['snapshot'])
                _verify_snapshot(conn, prior)
                attempts = prior.setdefault('delivery_attempt_history', [])
                if len(attempts) < 2 and int(time.time()) - (attempts[-1]['at'] if attempts else previous['created_at']) >= 60:
                    attempts.append({'at': int(time.time()), 'previous_error': previous['error']})
                    updated = json.dumps(prior, ensure_ascii=False)
                    if len(updated.encode()) > 256 * 1024:
                        raise ValueError('Stage wakeup audit exceeds inline budget')
                    conn.execute("UPDATE grace_objective_stage_wakeups SET state='pending',snapshot=?,error=NULL WHERE request_id=? AND state='attention' AND lease IS NULL", (updated, request_id))
                    return dict(conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone())
            return dict(previous)
        conn.execute('INSERT INTO grace_objective_stage_wakeups VALUES (?,?,?,?,?,\'pending\',NULL,NULL,?,NULL)',
                     (request_id, objective_id, expected_revision, stage_key, json.dumps(snapshot, ensure_ascii=False), int(time.time())))
        return dict(conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone())


def claim_recovery(conn, request_id):
    with kb.write_txn(conn):
        row = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone()
        if not row or row['state'] != 'pending':
            return None
        _idle(conn, row['objective_id'], row['revision'], row['stage_key'])
        _verify_snapshot(conn, json.loads(row['snapshot']))
        token = uuid.uuid4().hex
        conn.execute("UPDATE grace_objective_stage_wakeups SET state='processing',lease=?,lease_expires=? WHERE request_id=? AND state='pending'",
                     (token, int(time.time())+900, request_id))
        return dict(conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (request_id,)).fetchone())


def verify_recovery_execution(conn, delegation):
    delegation = dict(delegation)
    instance = delegation['request_instance_id'] or ''
    if not instance.startswith('gri_gsw_'):
        if delegation.get('delegation_id') and conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_stage_wakeups'").fetchone():
            bound = conn.execute('SELECT w.request_id FROM grace_objective_stage_wakeups w JOIN grace_objective_stages s ON s.objective_id=w.objective_id AND s.stage_key=w.stage_key WHERE w.objective_id=? AND w.stage_key=? AND s.delegation_id=?', (delegation['objective_id'],delegation['stage_key'],delegation['delegation_id'])).fetchall()
            if bound:
                raise ValueError('Recovery delegation lost its exact request instance')
        return
    row = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (instance[4:],)).fetchone()
    if not row or row['state'] not in ('processing','delegated') or (row['objective_id'],row['stage_key']) != (delegation['objective_id'],delegation['stage_key']):
        raise ValueError('Execution has no matching existing owner stage wakeup')
    snapshot = json.loads(row['snapshot'])
    _verify_snapshot(conn, snapshot)
    _require_source_evidence(snapshot, json.loads(delegation['contract_snapshot'])['verification'])
    return snapshot


def source_evidence_requirement(snapshot):
    return 'controller_source_selectors=' + json.dumps(snapshot['source_selectors'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _require_source_evidence(snapshot, verification):
    required = verification.get('evidence_required') if isinstance(verification, dict) else None
    if not isinstance(required, list) or not all(t in required for t in snapshot['historical_task_ids']) or source_evidence_requirement(snapshot) not in required:
        raise ValueError('Recovery contract must preserve exact historical task IDs and controller source selectors')


def validate_recovery_turn(conn, context, args, *, platform, chat_id, thread_id, session_key):
    row = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=?', (context.get('request_id'),)).fetchone()
    if not row or row['state'] != 'processing' or row['lease'] != context.get('lease') or (row['lease_expires'] or 0) <= time.time():
        raise ValueError('Objective recovery lease is invalid or expired')
    snap = json.loads(row['snapshot'])
    if (snap['platform'],snap['chat_id'],snap['thread_id'],snap['session_key']) != (platform,chat_id,thread_id,session_key):
        raise ValueError('Objective recovery turn belongs to another Topic/session')
    _idle(conn, row['objective_id'], row['revision'], row['stage_key'])
    _verify_snapshot(conn, snap)
    if args.get('objective_ref') != {'objective_id': row['objective_id'], 'stage_key': row['stage_key']}:
        raise ValueError('Recovery must bind the exact existing Objective stage')
    if args.get('approved') or args.get('external_targets') or args.get('approval_token') or args.get('approval_grant_id') or args.get('origin_callback_review_id') or args.get('approval_refresh_token') or args.get('_approval_refresh_token') or args.get('credential_refs'):
        raise ValueError('Objective recovery supplies no approval or callback authority')
    if type(args.get('external_effect_budget')) is not int or args['external_effect_budget'] != 0:
        raise ValueError('Objective recovery is limited to zero external effects')
    if args.get('original_request') != snap['original_request']:
        raise ValueError('Recovery must preserve the original owner request')
    _require_source_evidence(snap, args.get('verification'))
    expected_instance = 'gri_' + row['request_id']
    if args.get('request_instance_id') not in (None, '', expected_instance):
        raise ValueError('Objective recovery request instance changed')
    args['request_instance_id'] = expected_instance
    return snap


def recovery_context(text, internal):
    if not text.startswith(ENVELOPE):
        return None
    if not internal:
        raise ValueError('A human message cannot impersonate a recovery envelope')
    context = json.loads(text[len(ENVELOPE):].split('\n',1)[0])
    if not isinstance(context, dict) or set(context) != {'request_id','lease'}:
        raise ValueError('Invalid typed Objective recovery envelope')
    return context


def finish_recovery(conn, request_id, lease, error=None):
    with kb.write_txn(conn):
        row = conn.execute('SELECT * FROM grace_objective_stage_wakeups WHERE request_id=? AND lease=? AND state=\'processing\'', (request_id, lease)).fetchone()
        if not row:
            raise ValueError('Objective recovery lease changed')
        stage = conn.execute('SELECT * FROM grace_objective_stages WHERE objective_id=? AND stage_key=?', (row['objective_id'],row['stage_key'])).fetchone()
        # Delegation state records construction, not task execution. It stays
        # queued as its stage runs/completes; cancelled/building rows are invalid.
        delegation = conn.execute("SELECT * FROM grace_delegations WHERE delegation_id=? AND request_instance_id=? AND objective_id=? AND stage_key=? AND state='queued'",
            (stage['delegation_id'] if stage else None, 'gri_'+request_id, row['objective_id'],row['stage_key'])).fetchone()
        delegated = bool(delegation and stage['status'] in ('queued','running','done')
            and stage['execution_task_id'] == delegation['execution_task_id']
            and stage['review_task_id'] == delegation['review_task_id']
            and delegation['execution_task_id'] and delegation['review_task_id']
            and conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',
                (delegation['execution_task_id'],delegation['review_task_id'])).fetchone())
        conn.execute('UPDATE grace_objective_stage_wakeups SET state=?,lease=NULL,lease_expires=NULL,error=? WHERE request_id=?',
                     ('delegated' if delegated else 'attention', None if delegated else error or 'Grace did not record a formal stage delegation', request_id))
        return delegated


