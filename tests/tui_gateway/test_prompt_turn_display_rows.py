from tui_gateway import server


def test_compressed_backbone_preserves_trailing_display_rows():
    display = {"role": "assistant", "content": "background", "display_kind": "sdk_background_result"}
    history = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}, display]
    summary = {"role": "user", "content": "summary"}
    assert server._restore_sdk_display_rows(history, [summary]) == [summary, display]
    assert server._restore_sdk_display_rows(history, []) == [display]


def test_display_rows_anchor_to_surviving_identity_and_deduplicate():
    first = {"role": "user", "content": "one", "_row_id": 1}
    removed = {"role": "assistant", "content": "two", "_row_id": 2}
    display = {"role": "assistant", "content": "background", "display_kind": "sdk_background_result", "id": 3}
    last = {"role": "user", "content": "three", "_row_id": 4}
    history = [first, removed, display, dict(display), last]
    backbone = [dict(first, content="rewritten"), last]
    assert server._restore_sdk_display_rows(history, backbone) == [backbone[0], display, last]


def test_display_rows_after_a_compressed_predecessor_restore_before_next_survivor():
    """G2.2: a display row whose preceding ordinary row was summarised away must
    sit beside the summary (before the next surviving row), not after the tail."""
    first = {"role": "user", "content": "one", "_row_id": 1}
    display = {"role": "assistant", "content": "bg", "display_kind": "sdk_background_result", "id": 2}
    last = {"role": "user", "content": "two", "_row_id": 3}
    summary = {"role": "user", "content": "summary"}
    assert server._restore_sdk_display_rows([first, display, last], [summary, last]) == [
        summary,
        display,
        last,
    ]


def test_display_row_before_any_surviving_row_keeps_its_position():
    display = {"role": "assistant", "content": "bg", "display_kind": "sdk_background_result", "id": 1}
    first = {"role": "user", "content": "one", "_row_id": 2}
    last = {"role": "user", "content": "two", "_row_id": 3}
    assert server._restore_sdk_display_rows([display, first, last], [first, last]) == [
        display,
        first,
        last,
    ]


def test_compaction_reassigned_survivor_ids_still_anchor_display_rows(tmp_path):
    """Real durable compaction assigns fresh IDs to retained rows."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sid = "DISPLAY_ROW_COMPACTION_REBIND"
        db.create_session(sid, source="desktop")
        db.append_message(sid, "user", "head", timestamp=1.0)
        db.append_message(
            sid, "assistant", "background", timestamp=2.0,
            display_kind="sdk_background_result",
        )
        db.append_message(sid, "assistant", "removed", timestamp=3.0)
        db.append_message(sid, "user", "tail", timestamp=4.0)
        history = db.get_messages_as_conversation(sid, include_row_ids=True)
        compacted = [
            dict(history[0]),
            {"role": "user", "content": "summary", "timestamp": 3.5},
            dict(history[-1]),
        ]

        db.archive_and_compact(sid, compacted)
        active = db.get_messages_as_conversation(sid, include_row_ids=True)
        assert [row["_row_id"] for row in active] != [row["_row_id"] for row in history]
        assert server._restore_sdk_display_rows(history, active) == [
            active[0], history[1], active[1], active[2]
        ]
    finally:
        db.close()


def test_display_row_after_removed_ordinary_row_anchors_before_next_survivor():
    head = {"role": "user", "content": "head", "_row_id": 1}
    removed = {"role": "assistant", "content": "removed", "_row_id": 2}
    display = {
        "role": "assistant", "content": "background",
        "display_kind": "sdk_background_result", "_row_id": 3,
    }
    tail = {"role": "user", "content": "tail", "_row_id": 4}
    summary = {"role": "user", "content": "summary"}

    assert server._restore_sdk_display_rows(
        [head, removed, display, tail], [head, summary, tail]
    ) == [head, summary, display, tail]


def test_identical_unidentified_display_rows_are_not_deduplicated():
    display = {
        "role": "assistant", "content": "same",
        "display_kind": "sdk_background_result", "timestamp": 1.0,
    }
    history = [dict(display), dict(display)]

    assert server._restore_sdk_display_rows(history, []) == history
