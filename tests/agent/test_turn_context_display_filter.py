from agent.claude_sdk_runtime_continuity import _render_continuity_digest
from agent.turn_context import build_api_messages
import pytest


class _Agent:
    ephemeral_system_prompt = ""

    def _copy_reasoning_content_for_api(self, source, target):
        if source.get("reasoning_content"):
            target["reasoning_content"] = source["reasoning_content"]

    def _should_sanitize_tool_calls(self):
        return False


def test_generic_api_messages_exclude_sdk_display_rows_before_provider_repair():
    messages = [
        {"role": "assistant", "content": "before"},
        {"role": "user", "content": "peer text", "display_kind": "peer_message"},
        {"role": "assistant", "content": "after"},
    ]
    api_messages, _ = build_api_messages(
        _Agent(), messages, current_turn_user_idx=None, ext_prefetch_cache=None,
        plugin_user_context=None, moa_config=None, active_system_prompt="",
    )

    assert [message["content"] for message in api_messages] == ["before", "after"]
    assert "peer text" not in repr(api_messages)

    digest = _render_continuity_digest(messages)
    assert "PEER TEXT" not in digest
    assert "ASSISTANT: before" in digest
    assert "ASSISTANT: after" in digest


@pytest.mark.parametrize("source", ["iteration", "database"])
def test_background_answer_excluded_before_alternation_repair(source, tmp_path):
    from agent.turn_iteration_prep import prepare_iteration
    from hermes_state import SessionDB

    history = [
        {"role": "assistant", "content": "ordinary answer A"},
        {"role": "assistant", "content": "background answer B", "display_kind": "sdk_background_result"},
        {"role": "user", "content": "question U"},
    ]
    agent = _Agent()
    agent.session_id = "display-repair"
    agent.step_callback = None
    agent._skill_nudge_interval = 0
    agent._drain_pending_steer = lambda: None
    agent._sanitize_tool_call_arguments = lambda *a, **k: 0
    agent._last_flushed_db_idx = 2
    if source == "iteration":
        prepared = prepare_iteration(
            agent, messages=history, api_call_count=1,
            user_message="question U", current_turn_user_idx=2,
        )
        messages = prepared.messages
    else:
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session(agent.session_id, source="cli")
            for row in history:
                db.append_message(session_id=agent.session_id, **row)
            messages, display = db.get_resume_conversations(agent.session_id)
            assert [row["content"] for row in display] == [row["content"] for row in history]
        finally:
            db.close()
    api_messages, _ = build_api_messages(
        agent, messages, current_turn_user_idx=None, ext_prefetch_cache=None,
        plugin_user_context=None, moa_config=None, active_system_prompt="",
    )
    assert [row["content"] for row in api_messages] == ["ordinary answer A", "question U"]
    digest = _render_continuity_digest(messages)
    assert "background answer B" not in digest
    assert "ordinary answer A" in digest and "question U" in digest
    if source == "iteration":
        assert prepared.current_turn_user_idx == 1
        assert agent._last_flushed_db_idx == 1
