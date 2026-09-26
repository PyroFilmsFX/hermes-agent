"""The desktop / dashboard control-plane env deny list — the ONE policy every spawn path applies.

Dependency-free on purpose (stdlib only): bootstrap-time spawners import it without pulling the
provider registry. ``tools.environments.local_env_policy`` re-exports it and folds the exact names
into Tier 1 (``_ALWAYS_STRIP_KEYS``). Contract test: ``tests/tools/test_desktop_control_plane_env_scrub.py``.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import Optional, TypeVar

_EnvT = TypeVar("_EnvT", bound=MutableMapping)

# Desktop / dashboard CONTROL PLANE: credentials and debug surfaces that let a process drive
# the owner's Hermes UI, never task inputs. P0 2026-09-26: every Claude SDK agent Hermes spawned
# inherited HERMES_DASHBOARD_SESSION_TOKEN (authenticates to the gateway as the desktop:
# prompt.submit / approval.respond on ANY session = forged owner turns and approvals) and
# HERMES_DESKTOP_CDP_PORT (the Electron renderer's remote-debugging port = code execution in the
# renderer). No child of Hermes needs any of these, so this is the ONE deny list every spawn path
# applies LAST (after passthrough, ``_HERMES_FORCE_`` unwrapping and operator env), via
# :func:`scrub_desktop_control_plane_env`; the Claude SDK path, which can only override, blanks
# them with :func:`desktop_control_plane_env_blanks`; and the desktop backend pops the
# process-sealed subset from its own ``os.environ`` at startup
# (``hermes_cli.process_identity.seal_desktop_control_plane_env``) so implicit inheritance
# (a spawn without ``env=``) is clean too. ``tests/tools/test_desktop_control_plane_env_scrub.py``
# fails when a new ``os.environ`` copy bypasses it.
DESKTOP_CONTROL_PLANE_ENV_KEYS: frozenset[str] = frozenset({
    "HERMES_DASHBOARD_SESSION_TOKEN",  # desktop -> local backend gateway auth
    "HERMES_DESKTOP_REMOTE_TOKEN",  # desktop -> remote backend gateway auth
    "HERMES_DESKTOP_CDP_PORT",  # renderer remote-debugging (CDP) port
    # Dashboard auth-provider secrets: the server process reads them, children never do.
    "HERMES_DASHBOARD_DRAIN_SECRET", "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "HERMES_DASHBOARD_OIDC_CLIENT_SECRET",
})
# Subset the backend itself never re-reads from ``os.environ`` once serving: popped from the
# process env by the startup seal (the token's value is kept process-locally first). The
# dashboard auth-provider secrets are NOT sealed: plugins read them from the env at runtime.
PROCESS_SEALED_CONTROL_PLANE_ENV_KEYS: frozenset[str] = frozenset({
    "HERMES_DASHBOARD_SESSION_TOKEN", "HERMES_DESKTOP_REMOTE_TOKEN", "HERMES_DESKTOP_CDP_PORT",
})
# The dashboard auth-provider secrets plugins read from ``os.environ`` at runtime: the only
# control-plane names the seal leaves in the backend's own env (children still never get them).
_RUNTIME_READ_CONTROL_PLANE_ENV_KEYS: frozenset[str] = (
    DESKTOP_CONTROL_PLANE_ENV_KEYS - PROCESS_SEALED_CONTROL_PLANE_ENV_KEYS)
# Shape rule so a control-plane name added later is covered without editing the list: a
# desktop/dashboard-owned name carrying a credential or debugger marker.
_CONTROL_PLANE_ENV_PREFIXES = ("HERMES_DASHBOARD_", "HERMES_DESKTOP_")
_CONTROL_PLANE_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CDP", "DEBUG", "INSPECT")


def is_desktop_control_plane_env(name: str) -> bool:
    """True for a desktop/dashboard control-plane credential or debug variable (case-folded:
    the Windows env block is case-insensitive). ``HERMES_DESKTOP``/``HERMES_DESKTOP_TERMINAL``
    and other non-credential markers stay False — children legitimately read them."""
    upper = str(name).upper()
    if upper in DESKTOP_CONTROL_PLANE_ENV_KEYS:
        return True
    return upper.startswith(_CONTROL_PLANE_ENV_PREFIXES) and any(
        marker in upper[len("HERMES_"):] for marker in _CONTROL_PLANE_ENV_MARKERS)


def is_process_sealed_control_plane_env(name: str) -> bool:
    """True for a control-plane name the backend keeps OUT of its own ``os.environ`` once sealed:
    every control-plane name except the auth-provider secrets plugins read at runtime. The
    shape rule applies, so a future ``HERMES_DESKTOP_*_TOKEN`` is sealed without a list edit."""
    return is_desktop_control_plane_env(name) and str(name).upper() not in _RUNTIME_READ_CONTROL_PLANE_ENV_KEYS


# Set once by the backend's startup seal (``process_identity.seal_desktop_control_plane_env``).
# After it, the dotenv loader refuses to re-publish sealed names: ``tui_gateway.server`` (and
# every other late ``load_hermes_dotenv``) runs AFTER the seal, and a token persisted in ``.env``
# would otherwise come straight back into ``os.environ`` for every implicit-inherit spawn.
_CONTROL_PLANE_SEALED = False


def mark_control_plane_sealed() -> None:
    global _CONTROL_PLANE_SEALED
    _CONTROL_PLANE_SEALED = True


def control_plane_sealed() -> bool:
    return _CONTROL_PLANE_SEALED


def scrub_desktop_control_plane_env(env: _EnvT) -> _EnvT:
    """Delete every control-plane variable from *env* in place and return it. Apply LAST on
    any child env: nothing (passthrough, force prefix, operator overrides) may re-add them."""
    for key in [k for k in env if is_desktop_control_plane_env(k)]:
        del env[key]
    return env


def desktop_control_plane_env_blanks(environ: Optional[MutableMapping] = None) -> dict[str, str]:
    """``{name: ""}`` for every control-plane variable present in *environ* (default
    ``os.environ``) — for spawners that merge ``{**os.environ, **overrides}`` (the Claude
    Agent SDK) where a key can only be neutralised, not removed."""
    source = os.environ if environ is None else environ
    return {key: "" for key in source if is_desktop_control_plane_env(key)}
