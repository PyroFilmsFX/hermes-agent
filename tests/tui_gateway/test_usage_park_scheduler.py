"""Profile-scoped polling for durable Claude usage parks."""

from contextlib import contextmanager
from pathlib import Path

from agent import claude_sdk_usage_park as usage_park
from tui_gateway import server, usage_park_scheduler


def test_poll_once_dispatches_due_park_from_each_served_profile(monkeypatch, tmp_path):
    launch_home = tmp_path / "launch"
    secondary_home = tmp_path / "secondary"
    launch_home.mkdir()
    secondary_home.mkdir()
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    monkeypatch.setattr(server, "_served_profile_homes", {secondary_home})

    scoped_homes = []

    @contextmanager
    def profile_scope(session):
        scoped_homes.append(Path(session["profile_home"]) if session["profile_home"] else launch_home)
        yield

    monkeypatch.setattr(server, "_session_profile_runtime_scope", profile_scope)
    usage_park.park("secondary-session", "sdk-2", 100, "five_hour",
                    stagger_max=0, home=secondary_home, now=1)
    fired = []

    consumed = usage_park_scheduler.poll_once(lambda row: fired.append(row) or True, now=101)

    assert consumed == ["secondary-session"]
    assert fired[0]["profile_home"] == str(secondary_home)
    assert secondary_home in scoped_homes
    assert usage_park.get("secondary-session", home=secondary_home) is None
