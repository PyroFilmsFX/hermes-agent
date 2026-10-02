"""A host turn whose prompt is a peer envelope ends on the result the CLI attributes to it.

Live 2026-09-30 (Hermes session 20260924_200208_c68a80, CLI transcript rows 7725 and 7810): peer
mailbox row 1101 was delivered live as the host's own query (a ``<cross-session-message>``
envelope). The CLI parsed the envelope and stamped the echo AND the terminal ResultMessage with
``origin={'kind': 'peer', 'from': 'hermes-session:<sender>', 'hostInjected': True}``. The host turn
routed its own result to the background lane and stayed "running" for 1174 s, and every owner
message sent to the session in that window sat in the server queue behind it.
"""

from __future__ import annotations

import time

from agent.transports.claude_sdk_peer_envelope import build
from tests.agent.claude_sdk_fakes import AssistantMessage, ResultMessage, TextBlock, _make_session


def test_host_peer_envelope_turn_accepts_the_result_the_cli_reattributes_to_it():
    prompt = build(sender="20260909_193713_ce3d96", label="manager", msg_id=1101, body="five owner bugs")
    own_result = ResultMessage(result="on it", uuid="host-peer-res-1")
    own_result.origin = {"kind": "peer", "from": "hermes-session:20260909_193713_ce3d96", "hostInjected": True}
    delivered = []
    session, _holder = _make_session(
        script=[AssistantMessage(content=[TextBlock("on it")]), own_result],
        on_unsolicited_result=lambda texts, items=None: delivered.append(texts))
    try:
        started = time.time()
        turn = session.run_turn(prompt, turn_timeout=3.0)
        elapsed = time.time() - started
    finally:
        session.close()
    assert turn.error is None
    assert turn.final_text == "on it"
    assert elapsed < 2.5, "the host turn must end on its own result, not wait out the idle limit"
    assert all("on it" not in " ".join(texts) for texts in delivered)


def test_host_peer_envelope_turn_still_routes_another_senders_result_away():
    """Only the result the CLI attributes to THIS turn's envelope sender ends the turn."""
    prompt = build(sender="sender-a", label="a", msg_id=7, body="hello")
    stray = ResultMessage(result="other peer answer", uuid="stray-res")
    stray.origin = {"kind": "peer", "from": "hermes-session:sender-b", "hostInjected": True}
    own = ResultMessage(result="host answer", uuid="own-res")
    own.origin = {"kind": "peer", "from": "hermes-session:sender-a", "hostInjected": True}
    session, _holder = _make_session(
        script=[stray, AssistantMessage(content=[TextBlock("host answer")]), own],
        on_unsolicited_result=lambda texts, items=None: None)
    try:
        turn = session.run_turn(prompt, turn_timeout=5.0)
    finally:
        session.close()
    assert turn.final_text == "host answer"


def test_plain_host_turn_never_takes_a_host_injected_peer_result():
    """A plain owner prompt is not an envelope: a host-injected peer result is never its answer."""
    stray = ResultMessage(result="peer answer", uuid="stray-2")
    stray.origin = {"kind": "peer", "from": "hermes-session:sender-a", "hostInjected": True}
    session, _holder = _make_session(
        script=[stray, AssistantMessage(content=[TextBlock("owner answer")]),
                ResultMessage(result="owner answer", uuid="owner-res")],
        on_unsolicited_result=lambda texts, items=None: None)
    try:
        turn = session.run_turn("owner question", turn_timeout=5.0)
    finally:
        session.close()
    assert turn.final_text == "owner answer"
