"""Local conductor artifact access stays inside the session workspace."""

import base64
import hashlib
import json
import os

import pytest

import tui_gateway.server as server
from tui_gateway.contracts.conductor_artifacts import ArtifactListResult, ArtifactReadResult


JOB_ID = "w_20260101T000000Z_abcd"


@pytest.fixture
def artifact_workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    state = root / ".claude" / "state" / "worker-spawn"
    (state / "jobs").mkdir(parents=True)
    (state / "logs").mkdir()
    record = {
        "job_id": JOB_ID,
        "worker": "codex",
        "status": "running",
        "data_class": "B",
        "log_path": str(state / "logs" / f"{JOB_ID}.log"),
    }
    (state / "jobs" / f"{JOB_ID}.json").write_text(json.dumps(record), encoding="utf-8")
    (state / "logs" / f"{JOB_ID}.log").write_bytes(b"first\nsecond\n")
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (object(), {"cwd": str(root)}))
    return root, state, record


def _list(job_id=JOB_ID):
    return server._methods["conductor_artifacts.list"](1, {"session_id": "session", "job_id": job_id})


def _read(**params):
    return server._methods["conductor_artifacts.read"](1, {"session_id": "session", **params})


def test_local_list_contract_and_fixed_durable_unavailable(artifact_workspace):
    result = _list()["result"]
    assert result["durable"] == {"state": "unavailable", "reason": "not_configured"}
    assert result["local"][0]["variant"] == "log"
    assert result["local"][0]["bytes"] == len(b"first\nsecond\n")
    assert ArtifactListResult.model_validate(result)


def test_record_path_mismatch_never_reads_record_log_path(artifact_workspace):
    root, state, record = artifact_workspace
    record["log_path"] = "/etc/passwd"
    (state / "jobs" / f"{JOB_ID}.json").write_text(json.dumps(record), encoding="utf-8")
    result = _list()["result"]
    assert result["local"] is None
    assert result["local_reason"] == "path_mismatch"


@pytest.mark.parametrize("entry_type", ["symlink", "fifo"])
def test_refuses_symlink_and_fifo_logs(artifact_workspace, tmp_path, entry_type):
    _, state, _ = artifact_workspace
    log = state / "logs" / f"{JOB_ID}.log"
    log.unlink()
    if entry_type == "symlink":
        target = tmp_path / "outside.log"
        target.write_text("outside", encoding="utf-8")
        log.symlink_to(target)
    else:
        os.mkfifo(log)
    result = _list()["result"]
    assert result["local"] is None
    assert result["local_reason"] == "unreadable"


@pytest.mark.parametrize("data_class", ["A", None, "unknown"])
def test_class_a_missing_or_unknown_exposes_metadata_only(artifact_workspace, data_class):
    _, state, record = artifact_workspace
    if data_class is None:
        record.pop("data_class")
    else:
        record["data_class"] = data_class
    (state / "jobs" / f"{JOB_ID}.json").write_text(json.dumps(record), encoding="utf-8")
    item = _list()["result"]["local"][0]
    assert item["viewable"] == "metadata"
    assert item["sha256"] == hashlib.sha256(b"first\nsecond\n").hexdigest()
    response = _read(source="local", job_id=JOB_ID, variant="log", mode="text")
    assert response["error"]["code"] == 4093
    assert "text" not in response.get("result", {})


def test_text_window_redacts_without_changing_raw_byte_offsets(artifact_workspace):
    _, state, _ = artifact_workspace
    raw = b"one\nsk-12345678901234567890\nthree\n"
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)
    result = _read(source="local", job_id=JOB_ID, variant="log", mode="text", offset=4, length=24)["result"]
    assert result["offset"] == 4
    assert result["length"] == 24
    assert "sk-12345678901234567890" not in result["text"]
    assert result["redacted"] is True


def test_bytes_windows_round_trip_without_trimming_partial_lines(artifact_workspace):
    _, state, _ = artifact_workspace
    raw = b"".join(f"line {index:06d} ".encode() + b"x" * 100 + b"\n" for index in range(6000))
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)

    chunks = []
    offset = 0
    eof = False
    while not eof:
        result = _read(source="local", job_id=JOB_ID, variant="log", mode="bytes", offset=offset, length=256 * 1024)["result"]
        chunk = base64.b64decode(result["base64"])
        chunks.append(chunk)
        offset = result["offset"] + result["length"]
        eof = result["eof"]

    assert b"".join(chunks) == raw


def test_text_windows_redact_secrets_crossing_window_edges(artifact_workspace):
    _, state, _ = artifact_workspace
    pem_body = b"\n".join([b"MIIEpAIBAAKCAQEA_private_key_body_must_not_escape"] * 20)
    pem = b"-----BEGIN RSA PRIVATE KEY-----\n" + pem_body + b"\n-----END RSA PRIVATE KEY-----\n"
    raw = b"prefix\n" + pem + b"middle\n" + b"sk-ant-api03-" + b"A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + b"\n" + b"ghp_" + b"A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + b"\n" + b"OPENAI_API_KEY=" + b"very-secret-key-value-1234567890\n"
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)

    pem_tail = _read(source="local", job_id=JOB_ID, variant="log", mode="text", offset=raw.index(pem_body) + 8, length=30)["result"]
    assert pem_body.decode() not in pem_tail["text"]
    assert pem_tail["offset"] == raw.index(pem_body) + 8

    for token, fragment in (
        (b"sk-ant-api03-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", b"A1b2C3d4"),
        (b"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", b"A1b2C3d4"),
        (b"OPENAI_API_KEY=very-secret-key-value-1234567890", b"very-sec"),
    ):
        token_start = raw.index(token)
        for offset, length in ((token_start + 6, 18), (token_start, 12)):
            window = _read(source="local", job_id=JOB_ID, variant="log", mode="text", offset=offset, length=length)["result"]
            visible = window["text"]
            assert fragment.decode() not in visible
            expected_offset = offset
            if offset and raw[offset - 1:offset] != b"\n":
                newline = raw.find(b"\n", offset, offset + length)
                if newline >= 0:
                    expected_offset = newline + 1
            assert window["offset"] == expected_offset
            assert window["offset"] + window["length"] <= offset + length


def test_text_windows_report_withheld_long_line_edges(artifact_workspace):
    _, state, _ = artifact_workspace
    raw = b"x" * 20_000
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)
    result = _read(source="local", job_id=JOB_ID, variant="log", mode="text", offset=9_000, length=100)["result"]
    assert result["elided"] is True
    assert result["text"] == ""
    assert result["offset"] == 9_100
    assert result["length"] == 0


@pytest.mark.parametrize(
    ("raw", "offset"),
    [
        (b"header\n-----BEGIN RSA PRIVATE KEY-----\n" + b"MII_PRIVATE_BODY\n" * 600, 50),
        (b"MII_PRIVATE_BODY\n" * 20 + b"-----END RSA PRIVATE KEY-----\n", 0),
    ],
)
def test_unmatched_pem_markers_redact_to_the_context_edge(artifact_workspace, raw, offset):
    _, state, _ = artifact_workspace
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)
    result = _read(source="local", job_id=JOB_ID, variant="log", mode="text", offset=offset, length=100)["result"]
    assert "MII_PRIVATE_BODY" not in result["text"]


def test_window_size_is_limited_to_one_mibibyte(artifact_workspace):
    _, state, _ = artifact_workspace
    raw = b"x" * (2 * 1024 * 1024)
    (state / "logs" / f"{JOB_ID}.log").write_bytes(raw)
    result = _read(source="local", job_id=JOB_ID, variant="log", mode="bytes", offset=0, length=2 * 1024 * 1024)
    assert result["error"]["code"] == 4092


def test_durable_reads_are_not_configured(monkeypatch):
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (object(), {"cwd": "/tmp"}))
    result = _read(source="durable", artifact_id="00000000-0000-0000-0000-000000000000", mode="text")
    assert result["error"]["code"] == 4095


def test_methods_are_long_owned_and_contract_validated(artifact_workspace, monkeypatch):
    assert {"conductor_artifacts.list", "conductor_artifacts.read"} <= server._LONG_HANDLERS
    assert {"conductor_artifacts.list", "conductor_artifacts.read"} <= server._methods.keys()
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (None, None))
    assert _list()["error"]["code"] == 4001
