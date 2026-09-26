import pytest
from hermes_cli import kanban_db as kb
from plugins.openclaw_bridge.clawops_delegate import _ensure_external_action_objective_ref


@pytest.mark.parametrize('foreign', [False, True])
def test_user_objective_recovery_uses_exact_identity(tmp_path, monkeypatch, foreign):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'control.db'))
    oid = 'go_user_' + 'a' * 24
    with kb.connect_closing() as conn:
        kb.create_grace_objective(conn, objective_id=oid, platform='telegram',
            chat_id='other' if foreign else 'chat', thread_id='1', session_key='session',
            title='check', objective='connection check', original_request_sha256='a'*64,
            required_stage_keys=['prepare', 'execute'], terminal_stage_key='execute',
            acceptance_criteria=['verified'])
        conn.execute("UPDATE grace_objective_stages SET status='done',outcome_kind='intermediate_blocked' WHERE objective_id=? AND stage_key='prepare'", (oid,))
        conn.execute("UPDATE grace_objectives SET status='blocked' WHERE objective_id=?", (oid,))
        conn.commit()
    args = {'original_request': f'Continue {oid} after installed repair',
            'objective_ref': {'objective_id': oid, 'stage_key': 'prepare_r2'}}
    def resolve():
        return _ensure_external_action_objective_ref(args, platform='telegram',chat_id='chat',
            thread_id='1',session_key='session',topic_name='Topic',goal={},scope={},verification={},
            internal_only_contract=True)
    if foreign:
        with pytest.raises(ValueError, match='another chat or topic'):
            resolve()
    else:
        assert resolve() is None
        assert args['objective_ref']['objective_id'] == oid
    with kb.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM grace_objectives').fetchone()[0] == 1
