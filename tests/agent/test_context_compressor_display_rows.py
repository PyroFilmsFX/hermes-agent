from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB


def test_durable_adoption_filters_display_rows_before_checkpoint_and_summary(tmp_path: Path):
    from run_agent import AIAgent

    sentinel = "SENTINEL DURABLE SDK DISPLAY ANSWER"
    captured: dict[str, object] = {}
    sid = "DISPLAY_ROW_CHECKPOINT_BOUNDARY"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(sid, source="desktop")
        db.append_message(sid, "user", "question", timestamp=1.0)
        db.append_message(
            sid, "assistant", sentinel, timestamp=2.0,
            display_kind="sdk_background_result",
        )
        snapshot = db.get_messages_as_conversation(sid, include_row_ids=True)
        db.append_message(sid, "assistant", "concurrent durable row", timestamp=3.0)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key", base_url="https://openrouter.ai/api/v1",
                model="test/model", quiet_mode=True, session_db=db,
                session_id=sid, skip_context_files=True, skip_memory=True,
            )
        compressor = MagicMock()

        def compress(messages, **_kwargs):
            captured["summary_input"] = messages
            return [{"role": "user", "content": "SAFE SUMMARY"}]

        compressor.compress.side_effect = compress
        compressor.compression_count = 1
        compressor.last_prompt_tokens = 0
        compressor.last_completion_tokens = 0
        compressor._last_summary_error = None
        compressor._last_compress_aborted = False
        compressor._last_aux_model_failure_model = None
        compressor._last_aux_model_failure_error = None
        agent.context_compressor = compressor
        agent._compression_feasibility_checked = True
        agent.compression_in_place = False
        agent.compression_checkpoint_required = True

        class CheckpointStub:
            def supports_pre_compress_checkpoint(self, version):
                return version == 2

            def on_pre_compress(self, messages, *, evidence_messages, **_kwargs):
                captured["raw"] = messages
                captured["evidence"] = evidence_messages
                return "SAFE MEMORY CONTEXT"

        agent._memory_manager = CheckpointStub()
        agent.commit_memory_session = lambda _messages: None
        agent._persist_user_message_idx = len(snapshot)

        agent._compress_context(snapshot, "sys", approx_tokens=120_000)

        for key in ("raw", "evidence", "summary_input"):
            assert sentinel not in repr(captured[key]), (
                f"display-only row leaked into {key}: {captured[key]!r}"
            )
        assert any(row.get("content") == sentinel for row in snapshot)
    finally:
        db.close()
