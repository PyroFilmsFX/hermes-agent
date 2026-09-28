"""Regression tests for the dashboard API, using an isolated Hermes state DB."""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    plugin_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL, "
            "last_activity_at REAL, message_count INTEGER, git_branch TEXT, git_repo_root TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions(id, title) VALUES(?,?)",
            [("session-1", "One"), ("session-2", "Two")],
        )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("CNTRL_GROUPS_DB", raising=False)
    spec = importlib.util.spec_from_file_location("cntrl_groups_test_api", plugin_root / "dashboard" / "api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_routes_tag_list_preferences_rename_and_untag(client):
    assert client.put("/groups/Research/sessions/session-1").status_code == 200
    assert client.put("/groups/Research/sessions/session-2").status_code == 200
    assert client.patch("/groups/Research", json={"pinned": True, "order": 3}).status_code == 200
    rows = client.get("/groups").json()
    assert rows == [{"name": "Research", "session_ids": ["session-1", "session-2"], "pinned": True, "order": 3}]

    assert client.patch("/groups/Research", json={"name": "Planning"}).status_code == 200
    assert client.get("/groups").json()[0]["name"] == "Planning"
    assert client.delete("/groups/Planning/sessions/session-1").status_code == 200
    assert client.get("/groups").json()[0]["session_ids"] == ["session-2"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/groups/../sessions/session-1", None),
        ("put", "/groups/Good/sessions/not%2Fid", None),
        ("patch", "/groups/Good", {"order": -1}),
        ("patch", "/groups/Good", {"unexpected": True}),
    ],
)
def test_routes_reject_bad_input(client, method, path, body):
    response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert response.status_code in {400, 404, 422}


def test_tag_rejects_unknown_session(client):
    assert client.put("/groups/Good/sessions/missing").status_code == 404
