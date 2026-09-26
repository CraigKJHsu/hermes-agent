import sqlite3
import pytest
from hermes_cli import kanban_db as kb


@pytest.mark.parametrize('mode,repair,checkpoint_error,accepted', [
    ('wal', True, False, True),
    ('wal', False, False, False),
    ('wal', False, True, False),
    ('delete', True, False, False),
])
def test_checkpoint_header_requires_physical_recheck(tmp_path, monkeypatch, mode, repair, checkpoint_error, accepted):
    path = tmp_path / 'checkpoint.db'
    pages = bytearray(2 * 4096)
    pages[28:32] = (3).to_bytes(4, 'big')
    path.write_bytes(pages)
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchone(self): return self.rows[0] if self.rows else None
        def fetchall(self): return self.rows
    class Connection:
        in_transaction = False
        def execute(self, sql):
            if sql == 'PRAGMA database_list': return Result([(0, 'main', str(path))])
            if sql == 'PRAGMA page_size': return Result([(4096,)])
            if sql == 'PRAGMA journal_mode': return Result([(mode,)])
            # Even a logically valid WAL/cache snapshot is insufficient.
            if sql == 'PRAGMA quick_check': return Result([('ok',)])
            if sql == 'BEGIN IMMEDIATE': self.in_transaction = True; return Result([])
            if sql == 'ROLLBACK': self.in_transaction = False; return Result([])
            if sql == 'PRAGMA wal_checkpoint(PASSIVE)':
                assert conn.in_transaction
                if checkpoint_error: raise sqlite3.OperationalError('database is locked')
                if repair:
                    with path.open('ab') as f: f.write(bytes(4096))
                return Result([(0, 10, 10)])
            raise AssertionError(sql)
        def close(self): pass
    conn = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: conn)
    if accepted:
        kb._check_file_length_invariant(conn)
    else:
        with pytest.raises(sqlite3.DatabaseError, match='torn-extend|database is locked'):
            kb._check_file_length_invariant(conn)
    assert not conn.in_transaction


def test_real_wal_checkpoint_runs_with_diagnostic_reservation(tmp_path, monkeypatch):
    import os
    with kb.connect_closing(tmp_path / 'live-wal.db') as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        task = kb.create_task(conn, title='durable task')
        real_connect = sqlite3.connect
        checkpoint_states = []
        def checkpoint_connect(*args, **kwargs):
            checkpoint_states.append(conn.in_transaction)
            return real_connect(*args, **kwargs)
        monkeypatch.setattr(sqlite3, 'connect', checkpoint_connect)
        real_fstat = os.fstat
        first = True
        def short_first_sample(fd):
            nonlocal first
            stat = real_fstat(fd)
            if first:
                first = False
                values = list(stat)
                values[6] -= 4096
                return os.stat_result(values)
            return stat
        monkeypatch.setattr(os, 'fstat', short_first_sample)
        kb._check_file_length_invariant(conn)
        assert checkpoint_states == [True]
        assert not conn.in_transaction
        assert kb.get_task(conn, task).title == 'durable task'
