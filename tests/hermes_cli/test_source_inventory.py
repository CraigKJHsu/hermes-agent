import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from hermes_cli.source_inventory import runtime_source_inventory


def test_source_receipt_computes_bytes_and_git_state_without_mutation(tmp_path):
    home = tmp_path / 'home'
    profile = home / 'profiles' / 'research' / 'config.yaml'
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b'private-config-value\n')
    (home / 'config.yaml').write_bytes(b'default\n')
    locator = home / 'operator-evidence' / 'test-source-locators.json'
    locator.parent.mkdir()
    locator.write_bytes(b'{"paths": []}\n')
    repository = tmp_path / 'repo'
    repository.mkdir()
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(repository), *args], text=True).strip()
    git('init', '-q')
    (repository / 'source.txt').write_text('initial')
    git('add', 'source.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial')
    index = repository / '.git' / 'index'
    before = index.read_bytes()
    (repository / 'untracked.txt').write_text('new')
    receipt = runtime_source_inventory(home=home, repository=repository)
    assert receipt['complete'] is True
    assert receipt['profiles'] == [{'path': str(profile), 'sha256': hashlib.sha256(profile.read_bytes()).hexdigest()}]
    assert receipt['source_locators'][0]['sha256'] == hashlib.sha256(locator.read_bytes()).hexdigest()
    assert receipt['installed_checkout']['head'] == git('rev-parse', 'HEAD')
    assert receipt['installed_checkout']['head_stable'] is True
    assert receipt['installed_checkout']['working_tree_dirty'] is True
    assert index.read_bytes() == before
    assert 'private-config-value' not in json.dumps(receipt)


def test_source_receipt_does_not_claim_git_success_for_non_repository(tmp_path):
    result = runtime_source_inventory(home=tmp_path, repository=tmp_path)
    assert result['complete'] is False
    assert result['installed_checkout']['verified'] is False
    assert 'working_tree_dirty' not in result['installed_checkout']


def test_source_receipt_requires_and_binds_authoritative_objective(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    contract = {'identity': {'platform': 'telegram', 'chat_id': 'chat', 'thread_id': 'topic', 'project': 'hub_ops'},
                'verification': {'evidence_required': ['controller_runtime_source_inventory']}}
    with kb.connect() as conn:
        with pytest.raises(ValueError, match='canonical Objective'):
            _objective_durable_evidence_snapshot(conn, contract)
        kb.create_grace_objective(conn, objective_id='source-test', platform='telegram', chat_id='chat',
            thread_id='topic', session_key='source-test', title='Source receipt', objective='Verify sources',
            original_request_sha256='a' * 64, required_stage_keys=('research',), terminal_stage_key='research',
            acceptance_criteria=('Read-only source evidence',), current_stage_key='research')
        contract['objective_ref'] = {'objective_id': 'source-test', 'stage_key': 'research'}
        snapshot = _objective_durable_evidence_snapshot(conn, contract)
        assert snapshot['objective_id'] == 'source-test'
        assert snapshot['stage_key'] == 'research'
        assert snapshot['runtime_source_inventory']['source'] == 'hermes_controller_computed_readonly'
        contract['identity']['thread_id'] = 'different-topic'
        with pytest.raises(ValueError, match='another chat or Topic'):
            _objective_durable_evidence_snapshot(conn, contract)


def test_source_receipt_rejects_ancestor_repository(tmp_path):
    subprocess.check_call(['/usr/bin/git', 'init', '-q', str(tmp_path)])
    nested = tmp_path / 'package'
    nested.mkdir()
    receipt = runtime_source_inventory(home=tmp_path, repository=nested)
    assert receipt['installed_checkout']['verified'] is False
    assert receipt['installed_checkout']['error_type'] == 'ValueError'


def test_source_receipt_reports_scan_and_hash_errors(tmp_path, monkeypatch):
    from hermes_cli import source_inventory as module
    root = tmp_path / 'profiles' / 'research'
    root.mkdir(parents=True)
    (root / 'config.yaml').write_text('config')
    def unreadable(path):
        raise PermissionError('denied')
    monkeypatch.setattr(module, '_file_receipt', unreadable)
    receipt = runtime_source_inventory(home=tmp_path, repository=tmp_path)
    assert receipt['complete'] is False
    assert receipt['profile_glob_complete'] is False
    assert receipt['source_locators_complete'] is False
    assert receipt['profiles'] == []
    assert any(e['error_type'] == 'PermissionError' for e in receipt['errors'])


@pytest.mark.parametrize('required', [None, 'xcontroller_runtime_source_inventoryx',
                                     {'controller_runtime_source_inventory': False}, [None]])
def test_source_inventory_rejects_malformed_evidence_request(required):
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot
    with pytest.raises(ValueError, match='sequence of exact'):
        _objective_durable_evidence_snapshot(None, {'verification': {'evidence_required': required}})
