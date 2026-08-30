import json

from gateway.run import (
    _find_durable_clawops_approval_args,
    _find_bound_clawops_approval_args,
)
from hermes_cli import kanban_db as kb


def _tool_call(call_id, arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "function": {
                    "name": "clawops_delegate",
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def test_recovers_exact_contract_from_approval_required_result():
    token = "fe341e4c447cde20"
    contract = {
        "approved": False,
        "goal": {"objective": "建立草稿"},
        "scope": {"allowed": ["Facebook"]},
    }
    messages = [
        _tool_call("call-1", contract),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps(
                {
                    "status": "approval_required",
                    "approval_token": token,
                }
            ),
        },
    ]

    assert _find_bound_clawops_approval_args(messages, token) == contract


def test_recovery_ignores_unrelated_token():
    messages = [
        _tool_call(
            "call-old",
            {"approval_token": "aaaaaaaaaaaaaaaa", "approved": True},
        ),
        {
            "role": "tool",
            "tool_call_id": "call-old",
            "content": json.dumps(
                {"status": "rejected", "reason": "expired"}
            ),
        },
        {"role": "user", "content": "核准 fe341e4c447cde20"},
        _tool_call(
            "call-current",
            {"approval_token": "fe341e4c447cde20", "approved": True},
        ),
    ]

    assert (
        _find_bound_clawops_approval_args(
            messages, "fe341e4c447cde20"
        )
        is None
    )


def test_recovers_contract_from_durable_approval_challenge(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    contract = {
        "approved": False,
        "_approval_compiled_contract": {
            "contract_version": "1.0",
            "identity": {
                "platform": "telegram",
                "chat_id": "-1003938559457",
                "thread_id": "2",
                "topic_name": "二手拍賣",
                "project": "secondhand_commerce",
                "board": "default",
                "request_instance_id": "gri-test",
                "requested_by": "authenticated_user",
                "compiled_by": "Grace",
            },
            "original_request": "精確重刊",
            "grace_interpretation": "精確重刊",
            "trigger": "approval test",
            "goal": {
                "objective": "精確重刊",
                "deliverables": ["完成唯一目的地重刊"],
                "non_goals": ["不得新增其他目的地"],
            },
            "scope": {
                "allowed": ["唯一目的地 1333742673375089"],
                "forbidden": ["不得新增其他目的地"],
            },
            "verification": {
                "checks": ["讀回目的地"],
                "acceptance_criteria": ["目的地相符"],
                "evidence_required": ["Facebook 成功提示"],
            },
            "stop_rules": {
                "blocked": ["條件不符即停止"],
                "no_progress": ["不重試未知結果"],
                "success": ["送出完成"],
                "max_iterations": 1,
                "max_runtime_seconds": 900,
            },
            "memory": {
                "namespace": "telegram:-1003938559457:2/secondhand_commerce",
                "working": [],
                "promote_on_acceptance": [],
            },
            "routing": {
                "task_type": "facebook_marketplace_group_publish",
                "risk_level": "high",
                "resolved": {"status": "routed"},
            },
            "completion_mode": "terminal",
            "external_targets": ["37276725125275496", "1333742673375089"],
        },
        "task_type": "facebook_marketplace_group_publish",
        "external_targets": ["37276725125275496", "1333742673375089"],
        "goal": {"objective": "精確重刊"},
        "scope": {"allowed": ["唯一目的地 1333742673375089"]},
    }
    with kb.connect_closing(db_path) as conn:
        challenge = kb.create_grace_approval_challenge(
            conn,
            contract_fingerprint="a" * 64,
            platform="telegram",
            chat_id="-1003938559457",
            thread_id="2",
            session_key="agent:main:telegram:group:-1003938559457:2",
            session_id="grace-session-1",
            user_id_sha256="b" * 64,
            requested_message_id="7443",
            request_instance_id="gri-test",
            action_summary="精確重刊",
            approval_platform="37276725125275496、1333742673375089",
            approval_scope=json.dumps(
                ["唯一目的地 1333742673375089"],
                ensure_ascii=False,
            ),
            delegation_args=contract,
        )

    recovered = _find_durable_clawops_approval_args(challenge["token"])

    assert recovered is not None
    assert recovered["approved"] is False
    assert recovered["task_type"] == "facebook_marketplace_group_publish"
    assert recovered["external_targets"] == [
        "37276725125275496",
        "1333742673375089",
    ]
    assert recovered["_approval_compiled_contract"]["identity"][
        "request_instance_id"
    ] == "gri-test"
