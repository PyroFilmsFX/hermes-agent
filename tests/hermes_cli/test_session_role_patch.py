"""PATCH /api/sessions/{id} {"role": ...} persists the explicit session role (D28).

``role`` follows the title's clear idiom: a role name sets it, ``""`` clears it
(Auto, from name), and an omitted field leaves it untouched."""

import pytest


class TestSessionPatchRole:
    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message(session_id="s1", role="user", content="hi")
        finally:
            db.close()

    def _listed(self):
        rows = self.client.get("/api/sessions?limit=100").json()["sessions"]
        return next(s for s in rows if s["id"] == "s1")

    def _stored_role(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            return db.get_session_role("s1")
        finally:
            db.close()

    def test_set_read_back_and_clear(self):
        resp = self.client.patch("/api/sessions/s1", json={"role": "worker"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "worker"
        assert self._stored_role() == "worker"
        assert self._listed()["session_role"] == "worker"

        # Omitting role leaves it alone (a pin must not clear the tag).
        assert self.client.patch("/api/sessions/s1", json={"pinned": True}).status_code == 200
        assert self._stored_role() == "worker"

        resp = self.client.patch("/api/sessions/s1", json={"role": ""})
        assert resp.status_code == 200
        assert resp.json()["role"] is None
        assert self._stored_role() is None
        assert self._listed()["session_role"] is None

    def test_unknown_role_is_a_400_and_stores_nothing(self):
        resp = self.client.patch("/api/sessions/s1", json={"role": "admin"})
        assert resp.status_code == 400
        assert self._stored_role() is None

    def test_role_alone_is_a_valid_update(self):
        # The "nothing to update" guard must count role as an update.
        resp = self.client.patch("/api/sessions/s1", json={"role": "stream"})
        assert resp.status_code == 200

    def test_title_is_untouched_by_a_role_update(self):
        self.client.patch("/api/sessions/s1", json={"title": "lane-manager"})
        resp = self.client.patch("/api/sessions/s1", json={"role": "worker"})
        assert resp.json()["title"] == "lane-manager"
