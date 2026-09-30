import pytest
from hermes_state import SessionDB
from tests.agent.test_compression_concurrent_fork import _build_agent_with_db

@pytest.mark.parametrize('thread', ['1','4641'])
def test_compression_child_preserves_authoritative_topic_before_tool_call(tmp_path,thread):
    db=SessionDB(tmp_path/'state.db')
    peer={'session_key':f'agent:main:telegram:group:-1001:{thread}', 'chat_id':'-1001', 'chat_type':'group','thread_id':thread,'user_id':'owner'}
    db.create_session('parent',source='telegram',**peer)
    agent=_build_agent_with_db(db,'parent');agent.platform='telegram'
    agent._compress_context([{'role':'user','content':'technical context'}]*20,'sys',approx_tokens=120000)
    assert agent.session_id!='parent'
    child=db.get_session(agent.session_id)
    assert {k:child[k] for k in peer}==peer
    assert child['parent_session_id']=='parent'
    db.close()

def test_compression_does_not_inherit_cross_platform_peer(tmp_path):
    db=SessionDB(tmp_path/'state.db');db.create_session('parent',source='telegram',chat_id='-1001',thread_id='4641',session_key='telegram-lane')
    agent=_build_agent_with_db(db,'parent');agent.platform='cli'
    agent._compress_context([{'role':'user','content':'technical context'}]*20,'sys',approx_tokens=120000)
    child=db.get_session(agent.session_id)
    assert child['chat_id'] is None and child['thread_id'] is None
    db.close()
