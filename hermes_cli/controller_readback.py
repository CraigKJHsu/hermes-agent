"""Read controller-owned bindings and history without model-authored claims."""
import hashlib
import json
import re
import time
import os
import stat
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from hermes_cli.grace_review_metadata import grace_review_accepted



MAX_EVIDENCE_FILE_BYTES = 32 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENT_FILES = 32


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _artifact_parent(value):
    """Walk from the filesystem root; no component may redirect through a link."""
    from hermes_cli import kanban_db as kb
    path = Path(value)
    roots = (kb.attachments_root().absolute(), Path.home()/'.openclaw/media/tool-image-generation')
    if len(value) > 4096 or '..' in path.parts or not any(path.is_relative_to(root) and path != root for root in roots):
        raise ValueError('Evidence path is outside approved artifact storage')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _evidence_file(value, max_bytes=MAX_EVIDENCE_FILE_BYTES, *, _file_budget=None, capture=False):
    """Return a hash and bytes read only for one stable bounded regular file."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None, 0, None
    budget = _file_budget if _file_budget is not None else {'files': 0, 'bytes': 0}
    if budget['files'] >= MAX_ATTACHMENT_FILES or budget['bytes'] >= MAX_ATTACHMENT_BYTES:
        return None, 0, None
    max_bytes = min(max_bytes, MAX_ATTACHMENT_BYTES - budget['bytes'])
    budget['files'] += 1
    total = 0
    parent = None
    try:
        parent, name = _artifact_parent(value)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None, 0, None
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                return None, 0, None
            digest = hashlib.sha256()
            captured = bytearray() if capture and before.st_size <= 65536 else None
            for chunk in iter(lambda: stream.read(min(65536, max_bytes - total + 1)), b''):
                total += len(chunk)
                if total > max_bytes:
                    return None, total, None
                digest.update(chunk)
                if captured is not None:
                    captured.extend(chunk)
            if total != before.st_size or _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
                return None, total, None
            # Rewalk the currently named directory without following links.
            # Renaming/replacing any parent between validation/read is failure.
            current_parent, current_name = _artifact_parent(value)
            try:
                if _file_identity(os.stat(current_name,dir_fd=current_parent,follow_symlinks=False)) != _file_identity(before):
                    return None, total, None
            finally:
                os.close(current_parent)
            return digest.hexdigest(), total, bytes(captured) if captured is not None else None
    except (OSError, ValueError):
        return None, total, None
    finally:
        if parent is not None:
            os.close(parent)
        budget['bytes'] += total


def evidence_file_sha256(value, *, _file_budget=None):
    """Unavailable, changed, special or oversized files cannot certify evidence."""
    return _evidence_file(value, _file_budget=_file_budget)[0]


def _native_admission_receipt(conn, task, binding):
    """Read one native start acknowledgement selected by controller identity."""
    if task['executor_backend'] != 'openclaw' or not binding:
        return None
    run = conn.execute(
        "SELECT id,backend_run_id,backend_agent_id,protocol_version FROM task_runs "
        "WHERE task_id=? AND executor_backend='openclaw' ORDER BY id DESC LIMIT 1",
        (task['id'],),
    ).fetchone()
    unavailable = {'source': 'native_bridge_idempotency_store', 'available': False}
    if run is None:
        return unavailable
    key = f"{task['id']}:run:{run['id']}:start"
    if run['backend_run_id'] != key or not run['backend_agent_id'] or not run['protocol_version']:
        return unavailable
    scratch = None
    try:
        path = Path.home() / '.openclaw/hermes-bridge-idempotency.sqlite'
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            return unavailable
        wal = Path(str(path) + '-wal')
        journal = Path(str(path) + '-journal')
        if journal.exists() and journal.stat().st_size:
            return unavailable
        sources = [path, wal]
        before = [p.stat() if p.exists() else None for p in sources]
        # ponytail: snapshots cap at 64 MiB; use a native receipt RPC if the store outgrows this cap.
        if any(info and (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ATTACHMENT_BYTES)
               for info in before) or sum(info.st_size for info in before if info) > MAX_ATTACHMENT_BYTES:
            return unavailable
        if wal.is_symlink():
            return unavailable
        # Query a stable main+WAL copy; SQLite's reader marks and sidecars stay
        # in private scratch storage, never in the native runtime directory.
        scratch = Path(tempfile.mkdtemp(prefix='hermes-native-receipt-'))
        snapshot = scratch / 'receipt.sqlite'
        for source, target, info in zip(sources, [snapshot, Path(str(snapshot) + '-wal')], before):
            if info is None:
                continue
            flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(source, flags), 'rb') as reader, target.open('wb') as writer:
                opened = os.fstat(reader.fileno())
                if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(info):
                    return unavailable
                remaining = info.st_size
                while remaining:
                    chunk = reader.read(min(65536, remaining))
                    if not chunk:
                        return unavailable
                    writer.write(chunk)
                    remaining -= len(chunk)
                if reader.read(1) or _file_identity(os.fstat(reader.fileno())) != _file_identity(info):
                    return unavailable
        with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True, timeout=0.2)) as native:
            row = native.execute(
                "SELECT CASE WHEN typeof(request_hash)='text' AND length(request_hash)=64 "
                "AND length(CAST(request_hash AS BLOB))<=256 THEN request_hash END,"
                "CASE WHEN length(CAST(result_json AS BLOB))<=65536 THEN CAST(result_json AS BLOB) END,"
                "CASE WHEN typeof(created_at)='text' AND length(CAST(created_at AS BLOB))<=80 THEN created_at END "
                "FROM hermes_bridge_idempotency "
                "WHERE idempotency_key=?", (key,),
            ).fetchone()
            encoding = native.execute('PRAGMA encoding').fetchone()[0]
        after = [p.stat() if p.exists() else None for p in sources]
        if [_file_identity(info) if info else None for info in before] != [
            _file_identity(info) if info else None for info in after
        ] or (journal.exists() and journal.stat().st_size):
            return unavailable
        if (not row or not isinstance(row[1], bytes) or not re.fullmatch(r'[0-9a-f]{64}', str(row[0]))
                or not isinstance(row[2], str) or not row[2] or len(row[2].encode()) > 80):
            return unavailable
        text = row[1].decode(encoding)
        result = json.loads(text)
        identity = result.get('executionIdentity') if isinstance(result, dict) else None
        backend = result.get('backendExecution') if isinstance(result, dict) else None
        if not isinstance(identity, dict) or not isinstance(backend, dict) or not (
            result.get('ok') is True and result.get('status') == 'accepted'
            and result.get('taskId') == 'openclaw.agent.loop_contract_start'
            and result.get('protocolVersion') == run['protocol_version']
            and identity.get('delegationId') == binding['delegation_id']
            and identity.get('attemptId') == f"{task['id']}:run:{run['id']}"
            and isinstance(identity.get('contractFingerprint'), str)
            and re.fullmatch(r'[0-9a-f]{64}', identity['contractFingerprint'])
            and backend.get('backendRunId') == key
            and backend.get('backendAgentId') == run['backend_agent_id']
        ):
            return unavailable
        return {'source': 'native_bridge_idempotency_store', 'available': True,
                'store_path': str(path), 'idempotency_key': key, 'run_id': run['id'],
                'request_hash': row[0], 'created_at': row[2], 'result_json': text,
                'result_json_encoding': encoding, 'result_json_byte_count': len(row[1]),
                'result_json_sha256': hashlib.sha256(row[1]).hexdigest(),
                'original_raw_arguments_stored': False}
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError, RecursionError, UnicodeError):
        return unavailable
    finally:
        if scratch is not None:
            (scratch / 'receipt.sqlite-shm').unlink(missing_ok=True)
            (scratch / 'receipt.sqlite-wal').unlink(missing_ok=True)
            (scratch / 'receipt.sqlite-journal').unlink(missing_ok=True)
            (scratch / 'receipt.sqlite').unlink(missing_ok=True)
            scratch.rmdir()


def task_controller_readback(conn, task_id, *, _file_budget=None, _expected_attachment_sha256=None):
    """Observe all fields in one snapshot, preserving an existing transaction."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute('BEGIN')
    try:
        return _snapshot_readback(conn, task_id, _file_budget=_file_budget, _expected_attachment_sha256=_expected_attachment_sha256)
    finally:
        if own_snapshot:
            conn.rollback()


def _snapshot_readback(conn, task_id, *, include_related=True, _file_budget=None, _expected_attachment_sha256=None):
    if _file_budget is None:
        _file_budget = {'files': 0, 'bytes': 0}
    task = conn.execute("SELECT id,status,assignee,executor_backend,created_at,block_recurrences,idempotency_key,current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("Unknown task")
    delegations = conn.execute(
        "SELECT delegation_id,contract_fingerprint,state,objective_id,stage_key,execution_task_id,review_task_id,origin_review_task_id,origin_event_id,created_at "
        "FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?", (task_id, task_id)
    ).fetchall()
    binding = dict(delegations[0]) if len(delegations) == 1 else None
    full_delegation = conn.execute('SELECT * FROM grace_delegations WHERE delegation_id=?', (binding['delegation_id'],)).fetchone() if binding else None
    delegation_sha256 = hashlib.sha256(json.dumps(dict(full_delegation),ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode()).hexdigest() if full_delegation else None
    objective = None
    stages = []
    binding_verified = False
    if binding and binding['objective_id']:
        row = conn.execute(
            "SELECT objective_id,status,current_stage_key,terminal_stage_key,required_stage_keys,acceptance_criteria,original_request_sha256,created_at,revision "
            "FROM grace_objectives WHERE objective_id=?", (binding['objective_id'],)
        ).fetchone()
        objective = dict(row) if row else None
        if objective is not None:
            raw_criteria = objective['acceptance_criteria']
            if isinstance(raw_criteria, str):
                objective['acceptance_criteria_sha256'] = hashlib.sha256(raw_criteria.encode()).hexdigest()
            else:
                objective['acceptance_criteria'] = None
                objective['acceptance_criteria_error'] = 'invalid_controller_text'
        stages = [dict(r) for r in conn.execute(
            "SELECT stage_key,position,status,delegation_id,execution_task_id,review_task_id,outcome_kind "
            "FROM grace_objective_stages WHERE objective_id=? ORDER BY position", (binding['objective_id'],)
        )]
        binding_verified = bool(objective and any(
            s['stage_key'] == binding['stage_key'] and all(s[k] == binding[k] for k in
                ('delegation_id', 'execution_task_id', 'review_task_id')) for s in stages
        ))
    behavior = {'pin': None, 'verified': False, 'errors': ['Objective binding unavailable']}
    if binding_verified:
        from proactive.behavior_profiles import registry as br
        try:
            pin = br.get_pin(conn, binding['objective_id'])
            inspected = br.inspect_pin(pin) if pin else None
            behavior = {'pin': pin, 'verified': bool(inspected and inspected['ok']),
                        'errors': inspected['errors'] if inspected else ['Objective behavior pin missing'],
                        'runtime': inspected.get('runtime') if inspected else None}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            behavior = {'pin': None, 'verified': False, 'errors': [str(exc)]}
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
    attachment_digest = hashlib.sha256()
    attachment_files = []
    attachment_count = 0
    attachments_available = True
    expected_attachments = {key: value["sha256"] for key, value in (_expected_attachment_sha256 or {}).items() if value["task_id"] == task_id}
    seen_attachments = set()
    for attachment in conn.execute('SELECT * FROM task_attachments WHERE task_id=? ORDER BY id', (task_id,)):
        attachment_count += 1
        record = dict(attachment)
        record['content_sha256'], _, content = _evidence_file(
            record['stored_path'], _file_budget=_file_budget,
            capture=record['filename'].endswith('.json') and record['content_type'] == 'application/json',
        )
        if len(attachment_files) < MAX_ATTACHMENT_FILES:
            item = {
                'id': record['id'], 'filename': record['filename'],
                'stored_path': record['stored_path'], 'size': record['size'],
                'sha256': record['content_sha256'],
            }
            if (content is None and record['content_sha256']
                    and record['filename'].endswith('.json')
                    and record['content_type'] == 'application/json'):
                item['json_claims_unavailable'] = 'attachment_exceeds_65536_bytes'
            if content is not None and record['content_sha256']:
                try:
                    report = json.loads(content)
                    model = report['result']['meta']['agentMeta']['model']
                    status = report['status']
                    if (isinstance(model, str) and len(model) <= 80
                            and isinstance(status, str) and len(status) <= 80):
                        item['attachment_json_claims'] = {
                            'status': status, 'model': model,
                            'model_source': 'result.meta.agentMeta.model',
                            'trust_scope': 'untrusted_attachment_content',
                        }
                except (ValueError, TypeError, KeyError, RecursionError):
                    pass
            attachment_files.append(item)
        if record['id'] in expected_attachments:
            if record['content_sha256'] != expected_attachments[record['id']]:
                raise ValueError('Accepted content attachment changed during claim baseline capture')
            seen_attachments.add(record['id'])
        attachments_available = attachments_available and record['content_sha256'] is not None
        encoded = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
        attachment_digest.update(len(encoded).to_bytes(8, 'big'))
        attachment_digest.update(encoded)
    if set(expected_attachments) != seen_attachments:
        raise ValueError('Accepted content attachment missing from claim baseline capture')
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
                'SELECT execution_task_id,review_task_id,platform,chat_id,thread_id FROM grace_delegations '
                'WHERE (execution_task_id=? OR review_task_id=?) '
                'AND EXISTS(SELECT 1 FROM tasks WHERE id=?)',
                (referenced_id, referenced_id, referenced_id),
            ).fetchall()
            if len(membership) != 1 or any(
                (membership[0][key] or '') != (owner[key] or '')
                for key in ('platform', 'chat_id', 'thread_id')
            ):
                continue
            observed = _snapshot_readback(conn, referenced_id, include_related=False, _file_budget=_file_budget, _expected_attachment_sha256=_expected_attachment_sha256)
            prior = conn.execute(
                "SELECT id,metadata FROM task_runs WHERE task_id=? AND status='done' "
                "AND outcome='completed' ORDER BY id DESC LIMIT 1", (referenced_id,),
            ).fetchone()
            recorded_review = None
            if prior and observed["binding_verified"] and (observed["delegation"] or {}).get("review_task_id") == referenced_id and membership[0]["review_task_id"] == referenced_id and membership[0]["execution_task_id"] != referenced_id:
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
                'delegation_sha256': observed['delegation_sha256'],
                'attachments_sha256': observed['attachments_sha256'],
                'attachments_available': observed['attachments_available'],
                'external_effect_count': observed['external_effect_count'],
                'native_admission_receipt': observed['native_admission_receipt'],
                'recorded_review': recorded_review,
            })
    return {'source': 'controller_database', 'observed_at': int(time.time()),
            'referenced_task_readbacks': related,
            'referenced_task_limit': 50,
            'referenced_tasks_truncated': bool(include_related and binding_verified and len(references) > 50),
            'task': dict(task), 'delegation': binding, 'delegation_sha256': delegation_sha256, 'objective': objective, 'stages': stages,
            'binding_verified': binding_verified, 'delegation_count': len(delegations),
            'behavior_readback': behavior,
            'native_admission_receipt': _native_admission_receipt(conn, task, binding) if binding_verified else None,
            'attachments_sha256': attachment_digest.hexdigest(),
            'attachment_files': attachment_files,
            'attachment_files_truncated': len(attachment_files) < attachment_count,
            'attachments_available': attachments_available,
            'history': {'sha256': digest.hexdigest(), 'run_count': run_count, 'event_count': event_count,
                        'runs': runs[-100:], 'events': events[-100:],
                        'truncated': run_count > 100 or event_count > 100},
            'external_effect_count': conn.execute('SELECT count(*) FROM task_external_effects WHERE task_id=?', (task_id,)).fetchone()[0]}


def native_history_baseline(conn, contract, *, expected_attachment_sha256=None):
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
    readback = task_controller_readback(conn, execution_id, _expected_attachment_sha256=expected_attachment_sha256)
    captured_ids = {execution_id, *(r["task"]["id"] for r in readback["referenced_task_readbacks"])}
    if any(item["task_id"] not in captured_ids for item in (expected_attachment_sha256 or {}).values()):
        raise ValueError("Accepted content source is missing from claim baseline capture")
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
                   'history_sha256': r['history_sha256'], 'delegation_sha256': r['delegation_sha256'], 'attachments_sha256': r['attachments_sha256'], 'attachments_available': r['attachments_available']}
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
                    and now['history_sha256'] == row['history_sha256']
                    and 'delegation_sha256' in row and now['delegation_sha256'] == row['delegation_sha256']
                    and (not row.get('attachments_sha256') or (now['attachments_sha256'] == row['attachments_sha256'] and row.get('attachments_available', True) and now['attachments_available']))),
                'delegation_unchanged': bool(now and 'delegation_sha256' in row and now['delegation_sha256'] == row['delegation_sha256']),
                'attachments_unchanged': bool(now and row.get('attachments_sha256')
                    and now['attachments_sha256'] == row['attachments_sha256'] and row.get('attachments_available', True) and now['attachments_available']),
            })
    candidates = baseline.get('candidate_assets', []) if isinstance(baseline, dict) else None
    assets_unchanged = matched and isinstance(candidates, list)
    asset_comparisons = []
    candidate_budget = {'files': 0, 'bytes': 0}
    if isinstance(candidates, list):
        for asset in candidates:
            if not isinstance(asset, dict):
                assets_unchanged = False
                continue
            actual = evidence_file_sha256(asset.get('path'), _file_budget=candidate_budget)
            unchanged = bool(actual and actual == asset.get('sha256'))
            assets_unchanged = assets_unchanged and unchanged
            asset_comparisons.append({'path': asset.get('path'), 'expected_sha256': asset.get('sha256'), 'current_sha256': actual, 'unchanged': unchanged})
    return {'source': 'controller_run_snapshot_comparison',
            'baseline_binding_verified': bool(matched),
            'candidate_assets_unchanged': bool(assets_unchanged),
            'candidate_asset_comparisons': asset_comparisons,
            'all_referenced_tasks_unchanged': bool(matched and assets_unchanged and isinstance(before, list)
                and set(current) == {r['task_id'] for r in before}
                and all(r['unchanged'] for r in comparisons)),
            'all_referenced_task_attachments_unchanged': bool(matched and isinstance(before, list)
                and set(current) == {r['task_id'] for r in before}
                and all(r['attachments_unchanged'] for r in comparisons)),
            'comparisons': comparisons}


def capture_execution_history(conn, task_id, run_id):
    """Persist a pre-execution baseline in a controller event, not worker metadata."""
    rows = conn.execute('SELECT * FROM grace_delegations WHERE execution_task_id=?', (task_id,)).fetchall()
    from hermes_cli.source_binding import verify_source_binding_execution, source_packets, accepted_source_package
    from hermes_cli.content_revision import verify_content_revision_execution
    revision_baseline = verify_content_revision_execution(conn, rows[0]) if len(rows) == 1 else None
    from hermes_cli import kanban_db as kb
    task = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    compiled = kb._grace_compiled_contract(task['body'] or '') if task and kb._grace_loop_stage_header(task['body'] or '') == 'execution' else None
    sealed_advertised = any(packet.get('source_kind') == 'accepted_source_binding' for row in rows for packet in source_packets(json.loads(row['contract_snapshot'] or '{}')))
    if not revision_baseline and (sealed_advertised or (compiled and (accepted_source_package(conn, compiled) is not None or any(packet.get('source_kind') == 'accepted_source_binding' for packet in source_packets(compiled))))):
        if compiled is None:
            raise ValueError("Accepted source binding compiled execution contract is missing")
        if len(rows) != 1:
            raise ValueError("Accepted source binding lacks one execution delegation")
        sealed = json.loads(rows[0]['contract_snapshot'] or '{}')
        if source_packets(compiled) != source_packets(sealed) or compiled.get('objective_ref') != sealed.get('objective_ref') or compiled.get('source_package_ref') != sealed.get('source_package_ref'):
            raise ValueError("Accepted source binding compiled contract differs from execution delegation")
    for row in rows:
        verify_source_binding_execution(conn, row)
    if len(rows) != 1:
        return
    row = rows[0]
    if not row['objective_id']:
        return
    from hermes_cli.objective_recovery import verify_recovery_execution
    recovery_snapshot = verify_recovery_execution(conn, row)
    expected_attachments = {}
    if revision_baseline:
        assets = revision_baseline.get("verified_attachments") or {}
        for asset in [*assets.get("assets", []), *([assets["body_artifact"]] if assets.get("body_artifact") else [])]:
            ids = [asset.get("attachment_id"), *asset.get("duplicate_attachment_ids", [])]
            if any(type(key) is not int or key <= 0 for key in ids) or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or "")):
                raise ValueError("Accepted content asset lacks a controller attachment binding")
            for key in ids:
                expected_attachments[key] = {"task_id": revision_baseline["execution_task_id"], "sha256": asset["sha256"]}
    existing = execution_history_baseline(conn, task_id, run_id)
    if existing is not None:
        if existing.get('delegation_id') != row['delegation_id'] or existing.get('delegation_contract_fingerprint') != row['contract_fingerprint']:
            raise ValueError('Existing controller history event has a different delegation')
        return
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='controller_execution_history_baseline'", (task_id,run_id)).fetchone():
        raise ValueError('Controller history events are ambiguous')
    baseline = native_history_baseline(conn, {
        'objective_ref': {'objective_id': row['objective_id'], 'stage_key': row['stage_key']},
        'control_plane_receipt': {'execution_task_id': task_id, 'delegation_id': row['delegation_id'],
                                 'grace_review_task_id': row['review_task_id']},
    }, expected_attachment_sha256=expected_attachments)
    from hermes_cli import kanban_db as kb
    if recovery_snapshot is not None:
        captured = {item['task_id']: item for item in baseline['tasks']}
        for historical_id, expected_history in recovery_snapshot['historical_baseline'].items():
            item = captured.get(historical_id)
            if not item or item['history_sha256'] != expected_history or not item['attachments_available'] or item['attachments_sha256'] != recovery_snapshot['historical_attachment_baseline'][historical_id] or item['delegation_sha256'] != recovery_snapshot['historical_delegation_sha256']:
                raise ValueError('Persisted controller baseline differs from authorized historical evidence')
        baseline['candidate_assets'] = recovery_snapshot['source_selectors']['candidate_assets']
    kb._append_event(conn, task_id, 'controller_execution_history_baseline', baseline, run_id=run_id)


def execution_history_baseline(conn, task_id, run_id):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='controller_execution_history_baseline'", (task_id,run_id)).fetchall()
    if len(rows) != 1:
        return None
    return json.loads(rows[0]['payload'])
