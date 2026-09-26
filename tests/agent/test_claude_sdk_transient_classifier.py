"""D0 classification for Claude Agent SDK API failures."""

from types import SimpleNamespace

import pytest

from tests.agent.claude_sdk_fakes import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    _make_session,
)


def test_synthetic_network_error_is_error_not_success():
    text = "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
    synthetic = AssistantMessage(content=[TextBlock(text)], model="<synthetic>")
    synthetic.error = "server_error"
    session, _ = _make_session(
        script=[
            synthetic,
            ResultMessage(result=text, is_error=True, subtype="success", errors=[]),
        ]
    )
    try:
        turn = session.run_turn("hi")
    finally:
        session.close()

    assert turn.error == f"Claude API error (connection): {text}"
    assert turn.final_text == ""
    assert not any(row.get("role") == "assistant" for row in turn.projected_messages)


def test_contradictory_success_without_synthetic_stays_success():
    session, _ = _make_session(
        script=[
            AssistantMessage(content=[TextBlock("nightly summary saved")]),
            ResultMessage(
                result="nightly summary saved", is_error=True, subtype="success"
            ),
        ]
    )
    try:
        turn = session.run_turn("consolidate")
    finally:
        session.close()
    assert turn.error is None
    assert turn.final_text == "nightly summary saved"


def test_unsolicited_synthetic_api_error_is_delivered_as_error_record():
    from tests.agent.claude_sdk_fakes import SystemMessage

    delivered = []
    session, _ = _make_session(
        script=[], on_unsolicited_result=lambda texts, items, *_: delivered.append((texts, items))
    )
    text = "API Error: Can't reach the API server (ENOTFOUND)"
    synthetic = AssistantMessage(content=[TextBlock(text)], model="<synthetic>")
    synthetic.error = "server_error"
    session._handle_unsolicited(synthetic)
    session._handle_unsolicited(
        SystemMessage(subtype="api_retry", data={"attempt": 1, "error_status": None})
    )
    session._handle_unsolicited(
        ResultMessage(result=text, is_error=True, subtype="success", errors=[])
    )
    try:
        assert len(delivered) == 1
        texts, items = delivered[0]
        assert texts == []
        assert any(
            item.get("event") == "api_error"
            and "Claude API error (connection)" in item.get("error", "")
            for item in items
        )
        assert not any(text in item.get("text", "") for item in items)
    finally:
        session.close()


def test_unsolicited_woken_turn_error_delivers_buffered_items_and_drops_synthetic_text():
    from tests.agent.claude_sdk_fakes import (
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    delivered = []
    session, _ = _make_session(
        script=[],
        on_unsolicited_result=lambda texts, items, *_: delivered.append((texts, items)),
    )
    # 1. Peer in
    peer_msg = UserMessage("ignored envelope text")
    peer_msg.uuid = "peer-in-1"
    peer_msg.origin = {
        "kind": "peer",
        "from": "peer-id",
        "name": "Peer Name",
        "fromSession": "peer-session",
        "body": "incoming task",
    }
    session._handle_unsolicited(peer_msg)
    # 2. Real text + Bash tool call
    session._handle_unsolicited(
        AssistantMessage(
            [
                TextBlock("I am running the command"),
                ToolUseBlock("tool-1", "Bash", {"command": "ls"}),
            ]
        )
    )
    # 3. Tool result
    tool_res = UserMessage(
        [ToolResultBlock("tool-1", [{"type": "text", "text": "file.txt"}])]
    )
    tool_res.uuid = "tool-result-1"
    session._handle_unsolicited(tool_res)

    # 4. Synthetic error AssistantMessage
    error_text = "API Error: Can't reach the API server (ENOTFOUND)"
    synthetic = AssistantMessage([TextBlock(error_text)], model="<synthetic>")
    synthetic.error = "server_error"
    session._handle_unsolicited(synthetic)
    # 5. ResultMessage
    session._handle_unsolicited(
        ResultMessage(result=error_text, is_error=True, subtype="success", errors=[])
    )
    try:
        assert len(delivered) == 1
        texts, items = delivered[0]
        # Real text delivered, synthetic error text dropped
        assert texts == ["I am running the command"]
        # Peer in, tool rows, and real text delivered
        assert any(item.get("kind") == "peer_in" and item.get("text") == "incoming task" for item in items)
        assert any(item.get("kind") == "text" and item.get("text") == "I am running the command" for item in items)
        assert any(
            item.get("kind") == "tool"
            and item.get("name") == "Bash"
            and item.get("result") == "file.txt"
            for item in items
        )
        # api_error record appended
        assert any(
            item.get("kind") == "lifecycle"
            and item.get("event") == "api_error"
            and "Claude API error (connection)" in item.get("error", "")
            for item in items
        )
        # Synthetic error text is NOT delivered in items
        assert not any(error_text in item.get("text", "") for item in items)
    finally:
        session.close()


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ({"result_text": "ENOTFOUND"}, ("transient", "connection")),
        ({"result_text": "ECONNREFUSED"}, ("transient", "connection")),
        *[
            ({"api_error_status": status}, ("transient", "server_error"))
            for status in (500, 502, 503)
        ],
        ({"api_error_status": 504}, ("transient", "timeout")),
        ({"api_error_status": 529}, ("transient", "overloaded")),
        ({"api_error_kind": "overloaded"}, ("transient", "overloaded")),
        ({"api_error_status": 408}, ("transient", "timeout")),
        (
            {
                "api_error_status": 429,
                "rate_limit_rejected": {"rate_limit_type": "five_hour", "resets_at": 150},
                "now": 100,
            },
            ("transient", "rate_limit", 50),
        ),
        (
            {
                "api_error_status": 429,
                "api_error_kind": "rate_limit",
                "result_text": "You have exceeded the rate limit ... of 450,000 input tokens per minute.",
            },
            ("transient", "rate_limit"),
        ),
        (
            {
                "api_error_status": 429,
                "api_error_kind": "rate_limit",
                "result_text": "You have exceeded the rate limit ... of 80,000 output tokens per minute.",
            },
            ("transient", "rate_limit"),
        ),
        ({"result_text": "prompt is too long"}, ("permanent", "validation")),
        ({"api_error_status": 401}, ("permanent", "auth")),
        ({"api_error_status": 403}, ("permanent", "auth")),
        *[
            ({"api_error_status": status}, ("permanent", "validation"))
            for status in (400, 404, 413, 422)
        ],
        ({"api_error_kind": "invalid_request"}, ("permanent", "validation")),
        ({"api_error_kind": "billing_error"}, ("permanent", "billing")),
        ({"result_text": "out of extra usage", "api_error_status": 429}, ("permanent", "billing")),
        (
            {"rate_limit_rejected": {"rate_limit_type": "overage", "resets_at": None}},
            ("permanent", "billing"),
        ),
        ({"result_text": "certificate verify failed"}, ("permanent", "ssl")),
        ({"result_text": "error_max_turns"}, ("permanent", "other")),
        ({"turn": SimpleNamespace(interrupted=True)}, ("none", "interrupt")),
    ],
)
def test_failure_classifier_first_match_table(signals, expected):
    from agent.claude_sdk_transient import classify_sdk_api_failure

    verdict, klass, wait_hint = classify_sdk_api_failure(signals)
    assert (verdict, klass) == expected[:2]
    assert wait_hint == (expected[2] if len(expected) > 2 else None)


def test_error_classifier_matches_cli_connection_text():
    from agent.error_classifier import classify_api_error

    classified = classify_api_error(
        RuntimeError("Claude API error (connection): Can't reach the API server (ENOTFOUND)")
    )
    assert classified.reason.value == "timeout"
