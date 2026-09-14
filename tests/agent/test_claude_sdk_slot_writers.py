"""G7.9: every writer that retires agent._claude_sdk_session must detach with the
identity-checked helper so a replacement published concurrently survives."""

import threading


class _Session:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class _RacingAgent:
    """The slot getter hands out the OLD session once, then a replacement is
    published (as a concurrent rotation would) before the writer's clear runs."""

    def __init__(self, old, replacement):
        self._old = old
        self._replacement = replacement
        self._reads = 0
        self._slot = old
        self._claude_sdk_session_lock = threading.RLock()

    @property
    def _claude_sdk_session(self):
        self._reads += 1
        if self._reads == 1:
            value = self._slot
            self._slot = self._replacement  # published right after the read
            return value
        return self._slot

    @_claude_sdk_session.setter
    def _claude_sdk_session(self, value):
        self._slot = value


def test_agent_close_does_not_erase_a_replacement_published_after_its_read():
    from run_agent import AIAgent

    old, new = _Session("old"), _Session("new")
    agent = _RacingAgent(old, new)
    AIAgent._close_claude_sdk_session(agent)
    assert old.closed is True
    assert agent._slot is new
    assert new.closed is False


def test_fallback_retire_does_not_erase_a_replacement_published_after_its_read():
    from agent.claude_sdk_runtime_fallback import _retire_live_sdk_session

    old, new = _Session("old"), _Session("new")
    agent = _RacingAgent(old, new)
    _retire_live_sdk_session(agent)
    assert old.closed is True
    assert agent._slot is new
    assert new.closed is False


def test_fallback_retire_noop_without_live_session():
    from agent.claude_sdk_runtime_fallback import _retire_live_sdk_session

    agent = _RacingAgent(None, None)
    _retire_live_sdk_session(agent)
    assert agent._slot is None


def test_session_lock_is_created_once_under_concurrent_first_use(monkeypatch):
    """G7.8: two first-time callers must receive the same agent-level lock."""
    import time
    import threading as _threading

    from agent import claude_sdk_runtime_continuity as continuity

    real_rlock = _threading.RLock

    def slow_rlock():
        time.sleep(0.05)  # widen the create-then-publish window
        return real_rlock()

    monkeypatch.setattr(continuity.threading, "RLock", slow_rlock)

    class _Agent:
        pass

    agent = _Agent()
    seen = []
    barrier = _threading.Barrier(2)

    def first_use():
        barrier.wait(timeout=2.0)
        seen.append(continuity._claude_sdk_session_lock(agent))

    threads = [_threading.Thread(target=first_use) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=3.0)
    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert agent._claude_sdk_session_lock is seen[0]
