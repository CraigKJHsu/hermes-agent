import hashlib
import json
import os
import pytest

from tools import file_tools


def test_file_sha256_hashes_canonical_review_pages(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title='Exact Unicode review', body='GRACE_LOOP_CONTRACT_STAGE: grace_review\nreview')
        kb.add_comment(conn, task, 'worker', '完整😀\\n\r\n' * 4000)
        claimed = kb.claim_task(conn, task)
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claimed.current_run_id))
    page = json.loads(kt._handle_show({'task_id': task, 'limit': 1}))
    chunks = [page['review_evidence_json_chunk']]
    while not page['complete']:
        page = json.loads(kt._handle_show({'task_id': task, 'offset': page['next_offset']}))
        chunks.append(page['review_evidence_json_chunk'])
    raw = ''.join(chunks).encode('utf-8')
    result = json.loads(file_tools._handle_file_sha256({'review_task_id': task, 'expected_sha256': hashlib.sha256(raw).hexdigest()}))
    assert result.get('sha256') == hashlib.sha256(raw).hexdigest()
    assert result['size'] == len(raw)
    assert result['total_chars'] == len(raw.decode('utf-8'))
    assert result['source'] == 'canonical_kanban_review_pages'
    assert result['pages'] > 2
    assert result['complete'] is True


@pytest.mark.parametrize('failure', ['owner', 'missing_run', 'mixed', 'expected', 'stale_run', 'drift', 'gap', 'oversize', 'corrupt_bytes'])
def test_file_sha256_rejects_untrusted_review_pages(monkeypatch, failure):
    from tools import kanban_tools as kt
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_review')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '7')
    text = '測試😀'
    sha = hashlib.sha256(text.encode()).hexdigest()
    args = {'review_task_id': 't_review', 'expected_sha256': sha}
    if failure == 'owner': args['review_task_id'] = 't_other'
    if failure == 'missing_run': monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    if failure == 'mixed': args['text_chunks'] = [text]
    if failure == 'expected': args['expected_sha256'] = 'bad'

    def read(args):
        if failure in ('owner', 'missing_run', 'mixed', 'expected'):
            raise AssertionError('invalid review request must not read Kanban')
        offset = args['offset']
        end = 1 if offset == 0 else len(text)
        page = {'view': 'grace_review_evidence_chunk', 'task': {'id': 't_review', 'status': 'running', 'current_run_id': 7},
                'review_evidence_json_chunk': text[offset:end], 'review_evidence_json_sha256': sha,
                'offset': offset, 'next_offset': end, 'total_chars': len(text), 'complete': end == len(text)}
        if offset:
            if failure == 'stale_run': page['task']['current_run_id'] = 8
            if failure == 'drift': page['review_evidence_json_sha256'] = 'f' * 64
            if failure == 'gap': page['offset'] = offset + 1
            if failure == 'oversize': page['total_chars'] = 4 * 1024 * 1024 + 1
            if failure == 'corrupt_bytes': page['review_evidence_json_chunk'] = '改😀'
        return json.dumps(page, ensure_ascii=False)
    monkeypatch.setattr(kt, '_handle_show', read)
    assert 'error' in json.loads(file_tools._handle_file_sha256(args))


def test_file_sha256_hashes_ordered_utf8_chunks_without_file_io(monkeypatch):
    def reject_file_io(*args, **kwargs):
        raise AssertionError('text hashing must not access files')
    monkeypatch.setattr(file_tools, '_resolve_path_for_task', reject_file_io)
    chunks = ['{"訊息":"', '正確😀\\n', '"}\r\n']
    result = json.loads(file_tools._handle_file_sha256({'text_chunks': chunks}))
    raw = ''.join(chunks).encode('utf-8')
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['size'] == len(raw)
    assert result['source'] == 'provided_utf8_text_chunks'


@pytest.mark.parametrize('args', [
    {'text_chunks': None}, {'text_chunks': [1]}, {'text_chunks': ['\ud800']},
    {'text_chunks': ['x' * (4 * 1024 * 1024 + 1)]}, {'text_chunks': [''] * 1025},
    {'text_chunks': ['valid'], 'path': '/must-not-read'},
])
def test_file_sha256_rejects_invalid_text_mode(args):
    assert 'error' in json.loads(file_tools._handle_file_sha256(args))


def test_file_sha256_hashes_regular_file_and_rejects_directory(tmp_path):
    source = tmp_path / "evidence.yaml"
    source.write_bytes(b"model: gpt-6-luna\n")

    result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert result["size"] == source.stat().st_size
    assert result["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": str(tmp_path)}))

    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": str(fifo)}))
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": "~not-a-real-user/file"}))
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": "bad\0path"}))


def test_file_sha256_unsupported_platform_returns_error(tmp_path, monkeypatch):
    source = tmp_path / "evidence.txt"
    source.write_text("safe")
    with monkeypatch.context() as patch:
        patch.setattr(file_tools.sys, "platform", "win32")
        patch.delattr(file_tools.os, "O_NONBLOCK", raising=False)
        result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert "descriptor path verification unavailable" in result["error"]


def test_file_sha256_detects_path_replacement_during_hash(tmp_path, monkeypatch):
    folder = tmp_path / "sources"
    folder.mkdir()
    source = folder / "evidence.txt"
    source.write_text("original")
    real_sha256 = hashlib.sha256

    class MovingDigest:
        def __init__(self):
            self.digest = real_sha256()

        def update(self, chunk):
            folder.rename(tmp_path / "moved")
            folder.mkdir()
            source.write_text("replacement")
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    with monkeypatch.context() as patch:
        patch.setattr(file_tools.hashlib, "sha256", MovingDigest)
        result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert "file path changed during read" in result["error"]
