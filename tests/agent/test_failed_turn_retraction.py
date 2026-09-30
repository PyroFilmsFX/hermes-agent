"""An interrupted turn's boundary row is hidden on display once the request was answered."""

from agent.turn_failure_copy import (
    FAILED_TURN_DISPLAY_KIND,
    FAILED_TURN_NOTICE,
    retract_answered_failed_turns,
)
from tui_gateway import server


def _boundary():
    return {"role": "assistant", "content": FAILED_TURN_NOTICE, "display_kind": FAILED_TURN_DISPLAY_KIND}


def _kinds(rows):
    return [r.get("display_kind") for r in rows]


def test_interrupted_then_recovered_turn_that_answered_hides_the_boundary():
    rows = [
        {"role": "user", "content": "do it"},
        {"role": "system", "content": "interrupted, continuing…", "display_kind": "session_lifecycle"},
        _boundary(),
        {"role": "assistant", "content": "Done: here is the answer.", "display_kind": "sdk_background_result"},
    ]
    out = retract_answered_failed_turns(rows)
    assert _kinds(out) == [None, "session_lifecycle", "hidden", "sdk_background_result"]
    assert rows[2]["display_kind"] == FAILED_TURN_DISPLAY_KIND  # stored row untouched

    shown = server._history_to_messages(rows)
    assert FAILED_TURN_NOTICE not in [m["text"] for m in shown]
    assert shown[-1]["text"] == "Done: here is the answer."


def test_answer_after_peer_message_still_retracts_but_a_new_request_does_not():
    rows = [
        {"role": "user", "content": "a"},
        _boundary(),
        {"role": "user", "content": "note", "display_kind": "peer_message"},
        {"role": "assistant", "content": "answer to a"},
    ]
    assert _kinds(retract_answered_failed_turns(rows))[1] == "hidden"

    rows = [
        {"role": "user", "content": "a"},
        _boundary(),
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "answer to b"},
    ]
    assert _kinds(retract_answered_failed_turns(rows))[1] == FAILED_TURN_DISPLAY_KIND


def test_genuinely_unprocessed_request_still_shows_the_boundary():
    rows = [{"role": "user", "content": "do it"}, _boundary()]
    assert _kinds(retract_answered_failed_turns(rows)) == [None, FAILED_TURN_DISPLAY_KIND]
    shown = server._history_to_messages(rows)
    assert shown[-1]["display_kind"] == FAILED_TURN_DISPLAY_KIND
    assert shown[-1]["text"] == FAILED_TURN_NOTICE


def test_legacy_untyped_boundary_is_retracted_too():
    rows = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": FAILED_TURN_NOTICE},
        {"role": "assistant", "content": "the real answer"},
    ]
    assert _kinds(retract_answered_failed_turns(rows))[1] == "hidden"
