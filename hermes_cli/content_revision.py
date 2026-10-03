"""Owner-requested correction of a delivered content package, preserving its history.

This is planning/admission only. It grants no external capability and never alters
an accepted review, callback, retry count, or historical stage.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from hermes_cli import kanban_db as kb
from hermes_cli.grace_review_metadata import grace_review_accepted
from proactive.loop_contract import contract_fingerprint


def validate_delivered_content_revision_callback(conn, *, review_task_id, event_id,
        platform, chat_id, thread_id, session_id):
    row = conn.execute("""SELECT c.*,e.run_id AS origin_review_run_id
        FROM grace_loop_callbacks c JOIN task_events e ON e.id=?
        WHERE c.review_task_id=? AND c.state='delivered'
          AND c.last_event_id=? AND c.outcome_event_id=? AND c.outcome_kind='closed'
          AND c.completion_mode='terminal' AND c.user_report_event_id=?
          AND c.user_report_delivered_at IS NOT NULL
          AND c.platform=? AND c.chat_id=? AND c.thread_id=? AND c.session_id=?
          AND e.task_id=c.review_task_id AND e.kind='completed'""",
        (event_id, review_task_id, event_id, event_id, event_id,
         platform, chat_id, thread_id, session_id)).fetchone()
    if row is None:
        raise ValueError("Content revision requires an exact delivered accepted terminal callback")
    callback = dict(row)
    review = kb.get_run(conn, callback['origin_review_run_id'])
    if not (review and review.task_id == review_task_id and review.status == 'done'
            and review.outcome == 'completed' and grace_review_accepted(review.metadata)
            and isinstance((review.metadata or {}).get('workflow_review_source'), dict)):
        raise ValueError("Content revision requires an immutable exact-run accepted review")
    review_task = kb.get_task(conn, review_task_id)
    latest_review = kb.latest_run(conn, review_task_id)
    if not (review_task and review_task.status == 'done' and latest_review and latest_review.id == review.id):
        raise ValueError('Content revision review has been superseded')
    execution_id = callback['execution_task_id']
    execution = kb.reviewed_execution_run(conn, review, execution_id)
    task = kb.get_task(conn, execution_id)
    metadata = (execution.metadata or {}) if execution else {}
    contract = kb._grace_compiled_contract(task.body or '') if task else None
    if not (task and task.status == 'done' and task.executor_backend == 'openclaw'
            and execution and execution.status == 'done' and execution.outcome == 'completed'
            and isinstance(contract, dict)
            and (contract.get('user_facing_delivery') or {}).get('kind') == 'content_package'
            and type(metadata.get('external_effect_budget')) is int
            and metadata['external_effect_budget'] == 0
            and metadata.get('execution_card_fingerprint') == contract_fingerprint(contract)
            and metadata.get('external_effects') == []
            and not kb.list_external_effects(conn, execution_id)):
        raise ValueError("Content revision requires controller-bound zero-effect content evidence")
    source = review.metadata['workflow_review_source']
    if (source.get('parent_task_body_sha256') != hashlib.sha256(task.body.encode()).hexdigest()
            or source.get('review_task_body_sha256') != hashlib.sha256((review_task.body or "").encode()).hexdigest()):
        raise ValueError('Content revision accepted task bodies have changed')
    reference = contract.get('objective_ref') or {}
    stage = conn.execute("SELECT * FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
        (callback['objective_id'], callback['stage_key'])).fetchone()
    delegation = conn.execute("SELECT * FROM grace_delegations WHERE delegation_id=?",
        (stage['delegation_id'] if stage else '',)).fetchone()
    if not (stage and stage['status'] == 'done' and stage['outcome_kind'] == 'closed'
            and stage['execution_task_id'] == execution_id and stage['review_task_id'] == review_task_id
            and reference == {'objective_id': callback['objective_id'], 'stage_key': callback['stage_key']}
            and delegation and delegation['execution_task_id'] == execution_id
            and delegation['review_task_id'] == review_task_id
            and delegation['objective_id'] == callback['objective_id']
            and delegation['stage_key'] == callback['stage_key']
            and delegation['contract_fingerprint'] == callback['contract_fingerprint']
            and all(delegation[k] == callback[k] for k in ('platform','chat_id','thread_id'))):
        raise ValueError("Content revision has no exact canonical Objective/stage/delegation lineage")
    if kb.grace_callback_has_outstanding_approval(conn, review_task_id=review_task_id, event_id=event_id):
        raise ValueError("Content revision cannot consume or bypass an outstanding approval")
    return callback


def reopen_delivered_content(conn, *, callback, objective_ref, source_message_id,
        owner_user_id, user_id, session_key, reason, acceptance_criteria, request_instance_id, preview=False, baseline=None):
    """Append a terminal revision for one authenticated fresh owner message."""
    if not (owner_user_id and owner_user_id == user_id and source_message_id
            and source_message_id != str(callback.get('message_id') or '')
            and (not callback.get('user_id') or callback['user_id'] == user_id)
            and session_key == callback['session_key'] and str(reason).strip()):
        raise ValueError("Content revision requires a fresh authenticated owner in the same Topic")
    objective_id = callback['objective_id']
    stage_key = objective_ref.get('stage_key') if isinstance(objective_ref, dict) else None
    if not (objective_ref and objective_ref.get('objective_id') == objective_id
            and isinstance(stage_key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', stage_key)
            and stage_key != callback['stage_key']):
        raise ValueError("Content revision requires the same Objective and a new terminal stage")
    from hermes_cli.objective_workflow import migrate
    with kb.write_txn(conn):
        callback = validate_delivered_content_revision_callback(conn,
            review_task_id=callback['review_task_id'], event_id=callback['last_event_id'],
            platform=callback['platform'], chat_id=callback['chat_id'], thread_id=callback['thread_id'],
            session_id=callback['session_id'])
        if not preview:
            if not baseline or baseline != materialize_content_revision_baseline(conn, callback):
                raise ValueError("Content revision accepted baseline changed before reservation")
            migrate(conn)
        objective = kb.get_grace_objective(conn, objective_id)
        if not objective or any(objective[k] != callback[k] for k in ('platform','chat_id','thread_id','session_key')):
            raise ValueError("Content revision Objective belongs to another Topic")
        rows = (conn.execute("SELECT * FROM grace_objective_revisions WHERE objective_id=? AND source_message_id=?",
            (objective_id, source_message_id)).fetchall() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='grace_objective_revisions'").fetchone() else [])
        if rows:
            prior = dict(rows[-1]); snapshot = json.loads(prior['snapshot'])
            if not (prior['restart_stage_key'] == stage_key
                    and snapshot.get('content_revision_origin') == {
                        'review_task_id': callback['review_task_id'], 'event_id': callback['last_event_id'],
                        'owner_sha256': hashlib.sha256(owner_user_id.encode()).hexdigest()}
                    and (preview or snapshot.get('accepted_baseline_sha256') == _baseline_sha256(baseline))
                    and snapshot.get('request_instance_id') == request_instance_id
                    and objective['current_stage_key'] == stage_key
                    and objective['revision'] == prior['revision']):
                raise ValueError("Fresh owner message already materialized another content revision")
            return {'objective_id': objective_id, 'stage_key': stage_key, 'revision': prior['revision']}
        if not (objective['status'] == 'completed' and objective['completed_at']
                and objective['current_stage_key'] == callback['stage_key']
                and objective['terminal_stage_key'] == callback['stage_key']):
            raise ValueError("Only the current delivered terminal content can be revised")
        if conn.execute("SELECT 1 FROM grace_objective_stages WHERE objective_id=? AND stage_key=?",
                (objective_id, stage_key)).fetchone():
            raise ValueError("Content revision stage already exists; preserve its history")
        stages = [dict(r) for r in conn.execute("SELECT * FROM grace_objective_stages WHERE objective_id=? ORDER BY position", (objective_id,))]
        revision = objective.get('revision', 1) + 1
        if preview:
            return {'objective_id': objective_id, 'stage_key': stage_key, 'revision': revision}
        snapshot = {'objective': objective, 'stages': stages, 'request_instance_id': request_instance_id, 'accepted_baseline_sha256': _baseline_sha256(baseline), 'content_revision_origin': {
            'review_task_id': callback['review_task_id'], 'event_id': callback['last_event_id'],
            'owner_sha256': hashlib.sha256(owner_user_id.encode()).hexdigest()}}
        now = int(time.time())
        conn.execute("INSERT INTO grace_objective_revisions (objective_id,revision,restart_stage_key,reason,source_message_id,snapshot,created_at) VALUES (?,?,?,?,?,?,?)",
            (objective_id, revision, stage_key, reason, source_message_id, json.dumps(snapshot, ensure_ascii=False), now))
        conn.execute("INSERT INTO grace_objective_stages (objective_id,stage_key,position,status,created_at,updated_at) VALUES (?,?,?,'planned',?,?)",
            (objective_id, stage_key, max(s['position'] for s in stages)+1, now, now))
        required = json.loads(objective['required_stage_keys']) + [stage_key]
        criteria = list(dict.fromkeys([*json.loads(objective['acceptance_criteria']), *acceptance_criteria]))
        conn.execute("UPDATE grace_objectives SET revision=?,status='active',current_stage_key=?,terminal_stage_key=?,required_stage_keys=?,acceptance_criteria=?,completed_at=NULL,next_action=?,waiting_for='',updated_at=? WHERE objective_id=?",
            (revision, stage_key, stage_key, json.dumps(required), json.dumps(criteria, ensure_ascii=False), reason, now, objective_id))
        return {'objective_id': objective_id, 'stage_key': stage_key, 'revision': revision}


def validate_content_revision_admission(conn, delegation):
    """Recheck the journal-bound revision at the normal durable queue boundary."""
    origin = kb.get_grace_loop_callback(conn, delegation['origin_review_task_id'])
    if not origin or origin['session_key'] != delegation['session_key']:
        raise ValueError("Content revision queue admission belongs to another Topic")
    callback = validate_delivered_content_revision_callback(conn,
        review_task_id=delegation['origin_review_task_id'], event_id=delegation['origin_event_id'],
        platform=delegation['platform'], chat_id=delegation['chat_id'], thread_id=delegation['thread_id'],
        session_id=origin['session_id'])
    objective = kb.get_grace_objective(conn, delegation['objective_id'])
    row = conn.execute("SELECT * FROM grace_objective_revisions WHERE objective_id=? AND revision=?",
        (delegation['objective_id'], objective['revision'] if objective else -1)).fetchone()
    snapshot = json.loads(row['snapshot']) if row else {}
    origin = snapshot.get('content_revision_origin') or {}
    contract = json.loads(delegation.get('contract_snapshot') or '{}')
    budget = contract.get('external_effect_budget')
    zero_budget = (type(budget) is int and budget == 0) or (
        isinstance(budget, dict) and type(budget.get('max_effects')) is int and budget['max_effects'] == 0)
    if not (objective and objective['status'] in kb._ACTIVE_GRACE_OBJECTIVE_STATUSES
            and callback['objective_id'] == delegation['objective_id']
            and objective['current_stage_key'] == delegation['stage_key']
            and objective['terminal_stage_key'] == delegation['stage_key']
            and row and row['restart_stage_key'] == delegation['stage_key']
            and (contract.get('durable_evidence_snapshot') or {}).get('accepted_content_revision') == content_revision_binding(callback, {'objective_id': delegation['objective_id'], 'stage_key': delegation['stage_key']}, snapshot.get('accepted_baseline_sha256'))
            and origin.get('review_task_id') == callback['review_task_id']
            and origin.get('event_id') == callback['last_event_id']
            and snapshot.get('request_instance_id') == delegation['request_instance_id']
            and not delegation.get('approval_required')
            and zero_budget and (contract.get('identity') or {}).get('requested_by') == 'authenticated_user'
            and (contract.get('user_facing_delivery') or {}).get('kind') == 'content_package'):
        raise ValueError("Content revision queue admission lacks its exact authenticated zero-effect journal")
    callback['_accepted_content_revision_baseline'] = _verify_baseline_snapshot(conn, callback, snapshot)
    return callback


def _baseline_sha256(baseline):
    return hashlib.sha256(json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verify_baseline_snapshot(conn, callback, snapshot):
    expected = snapshot.get("accepted_baseline_sha256")
    baseline = materialize_content_revision_baseline(conn, callback)
    if not expected or expected != _baseline_sha256(baseline):
        raise ValueError("Content revision accepted baseline changed before execution admission")
    return baseline


def content_revision_binding(callback, objective_ref, baseline_sha256):
    return {**objective_ref, 'origin_review_task_id': callback['review_task_id'],
        'origin_event_id': callback['last_event_id'], 'accepted_baseline_sha256': baseline_sha256}


def verify_content_revision_execution(conn, delegation):
    """A sealed content-revision claim must have its current durable journal."""
    contract = json.loads(delegation['contract_snapshot'] or '{}')
    marker = (contract.get('durable_evidence_snapshot') or {}).get('accepted_content_revision')
    origin = kb.get_grace_loop_callback(conn, delegation['origin_review_task_id']) if delegation['origin_review_task_id'] else None
    revision_lane = bool(origin and origin['outcome_kind'] == 'closed' and origin['completion_mode'] == 'terminal'
        and origin['objective_id'] == delegation['objective_id'] and origin['stage_key'] != delegation['stage_key'])
    if marker or revision_lane:
        if not isinstance(marker, dict):
            raise ValueError('Content revision claim lacks its sealed baseline binding')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_revisions'").fetchone():
            raise ValueError('Content revision claim lacks its durable journal')
        return validate_content_revision_admission(conn, dict(delegation))['_accepted_content_revision_baseline']


def materialize_content_revision_baseline(conn, callback, source_ref=None):
    """Read the exact previously delivered bytes as a revision baseline, never a new acceptance."""
    callback = validate_delivered_content_revision_callback(conn,
        review_task_id=callback['review_task_id'], event_id=callback['last_event_id'],
        platform=callback['platform'], chat_id=callback['chat_id'], thread_id=callback['thread_id'],
        session_id=callback['session_id'])
    review = kb.get_run(conn, callback['origin_review_run_id'])
    execution = kb.reviewed_execution_run(conn, review, callback['execution_task_id'])
    expected = {'execution_task_id': execution.task_id, 'review_task_id': review.task_id,
        'execution_run_id': execution.id, 'review_run_id': review.id}
    if source_ref is not None and (source_ref != expected or any(type(source_ref.get(k)) is not int for k in ('execution_run_id','review_run_id'))):
        raise ValueError("Content revision source is not its exact previously delivered accepted run")
    report = kb.grace_inline_content_package_report(conn, execution.task_id, execution_run=execution)
    if not report or not isinstance(report.get('body'), str) or not report['body'].strip():
        raise ValueError("Content revision baseline has no controller-materialized body")
    delivery = kb.grace_user_facing_delivery_contract(conn, execution.task_id)
    assets = None
    if (delivery or {}).get('delivery') == 'inline_with_attachment':
        assets = kb.grace_content_package_attachment_readback(conn, execution.task_id, execution_run=execution)
        if not assets:
            raise ValueError("Content revision baseline attachments no longer verify")
    return {**expected, 'objective_id': callback['objective_id'],
        'source_kind': 'accepted_content_revision_baseline', 'body': report['body'],
        'utf8_sha256': hashlib.sha256(report['body'].encode()).hexdigest(),
        'verified_attachments': assets, 'new_acceptance': False}
