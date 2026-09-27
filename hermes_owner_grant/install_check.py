"""Check the root-owned verifier install against the manifest the installer wrote (U11b).

The anchor install (Electron ``owner-grant-anchor-install.ts``, one admin prompt) co-installs,
next to ``anchor.json`` and all root:wheel, dirs 0755, files 0644:

* ``hermes_owner_verify.py``: the tiny launcher that ``/usr/bin/python3 -I -S`` runs. Its
  sha256 is ``anchor.verifier_sha256``.
* ``verifier/hermes_owner_grant/``: this package (``PACKAGE_FILES``).
* ``pycache/``: the bytecode cache the launcher points ``sys.pycache_prefix`` at. Root fills it
  at install time with py_compile over the CLI's static import closure (modulefinder; nothing is
  executed), so it holds this package AND every stdlib module the CLI imports. That matters: the Command Line Tools stdlib ships without ``__pycache__`` and is
  root-owned, so without it every hook run recompiles argparse, dataclasses, inspect, typing...
  (D13: ~105 ms full process; with the cache ~40 ms).
* ``manifest.sha256``: ``<sha256>  <relative path>`` for the launcher, every package file and
  every ``.pyc``, written by the root script after it verified the staged bytes.

``check_installed_verifier`` re-hashes every listed file (each read under the same StrictModes
checks as the anchor), refuses a manifest naming anything outside that fixed layout, and treats
any file under ``verifier/`` that the manifest does not list as a mismatch. It is read-only and
stdlib-only (it ships inside the verifier, for ``anchor-status``).
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import stat
from typing import Any, Dict, List, Optional, Tuple

from . import anchor as _anchor

INSTALL_DIR = _anchor.ANCHOR_DIR
LAUNCHER_NAME = "hermes_owner_verify.py"
LAUNCHER_PATH = INSTALL_DIR + "/" + LAUNCHER_NAME
MANIFEST_NAME = "manifest.sha256"
VERIFIER_DIRNAME = "verifier"
PYCACHE_DIRNAME = "pycache"
PACKAGE_REL = VERIFIER_DIRNAME + "/hermes_owner_grant"
# Exactly what is installed, in manifest order. builder.py and doctor.py are tooling only.
PACKAGE_FILES = (
    "__init__.py",
    "anchor.py",
    "cli.py",
    "ed25519_pure.py",
    "envelope.py",
    "install_check.py",
    "quote.py",
    "scopes.json",
    "scopes.py",
    "verify.py",
)
MAX_FILE_BYTES = 1024 * 1024  # the root script copies at most this much per file
MAX_MANIFEST_BYTES = 64 * 1024

STATE_MATCH = "match"
STATE_MISMATCH = "mismatch"
STATE_MISSING = "missing"
STATE_UNTRUSTED = "untrusted"

_LINE = re.compile(r"\A([0-9a-f]{64})  ([ -~]+)\Z")
_PYC = re.compile(r"\A" + PYCACHE_DIRNAME + r"/[ -~]+\.pyc\Z")
_REQUIRED = (LAUNCHER_NAME,) + tuple(PACKAGE_REL + "/" + name for name in PACKAGE_FILES)


class _Malformed(Exception):
    pass


def _allowed(rel: str) -> bool:
    if rel in _REQUIRED:
        return True
    parts = rel.split("/")
    return (
        bool(_PYC.match(rel))
        and "\\" not in rel
        and all(part not in ("", ".", "..") for part in parts)
    )


def parse_manifest(data: bytes) -> List[Tuple[str, str]]:
    """``[(relative path, sha256)]``. Raises ``_Malformed`` for anything outside the layout."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise _Malformed("manifest is not ASCII") from None
    if not text.endswith("\n"):
        raise _Malformed("manifest does not end with a newline")
    entries: List[Tuple[str, str]] = []
    seen = set()
    for number, line in enumerate(text[:-1].split("\n"), 1):
        match = _LINE.match(line)
        if not match:
            raise _Malformed("manifest line %d is malformed" % number)
        digest, rel = match.group(1), match.group(2)
        if not _allowed(rel):
            raise _Malformed(
                "manifest line %d names %r, outside the verifier layout" % (number, rel)
            )
        if rel in seen:
            raise _Malformed("manifest lists %r twice" % rel)
        seen.add(rel)
        entries.append((rel, digest))
    missing = [rel for rel in _REQUIRED if rel not in seen]
    if missing:
        raise _Malformed("manifest does not list %s" % ", ".join(missing))
    return entries


def _walk_files(fs: Any, top: str, rel: str) -> List[str]:
    """Relative paths of every non-directory under ``top/rel`` (symlinks are not followed)."""
    out: List[str] = []
    for name in sorted(fs.listdir(posixpath.join(top, rel))):
        child = posixpath.join(rel, name)
        st = fs.lstat(posixpath.join(top, child))
        if stat.S_ISDIR(st.st_mode):
            out.extend(_walk_files(fs, top, child))
        else:
            out.append(child)
    return out


def _result(
    state: str,
    detail: Optional[str] = None,
    *,
    files: int = 0,
    precompiled: bool = False,
    mismatched: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "state": state,
        "detail": detail,
        "files": files,
        "precompiled": precompiled,
        "mismatched": sorted(mismatched or []),
    }


def check_installed_verifier(
    anchor: Any, *, install_dir: str = INSTALL_DIR, fs: Any = None
) -> Dict[str, Any]:
    """Compare the installed launcher, package and ``.pyc`` files with ``manifest.sha256``.

    ``state`` is ``match``, ``mismatch`` (a file differs, is missing, is extra, or the launcher
    is not the one ``anchor.verifier_sha256`` pins; ``mismatched`` names them), ``missing`` (no
    manifest) or ``untrusted`` (a path fails the root-ownership checks, or the manifest is
    malformed). ``install_dir`` and ``fs`` exist for tests; production uses the constant.
    """
    fs = _anchor.OsFileSystem() if fs is None else fs
    try:
        raw = _anchor.read_root_owned_file(
            posixpath.join(install_dir, MANIFEST_NAME), fs, MAX_MANIFEST_BYTES
        )
    except _anchor.AnchorError as exc:
        missing = exc.reason == _anchor.REASON_ANCHOR_MISSING
        return _result(STATE_MISSING if missing else STATE_UNTRUSTED, exc.detail)
    try:
        entries = parse_manifest(raw)
    except _Malformed as exc:
        return _result(STATE_UNTRUSTED, str(exc))

    mismatched: List[str] = []
    for rel, want in entries:
        try:
            data = _anchor.read_root_owned_file(
                posixpath.join(install_dir, rel), fs, MAX_FILE_BYTES
            )
        except _anchor.AnchorError as exc:
            if exc.reason == _anchor.REASON_ANCHOR_MISSING:
                mismatched.append(rel)
                continue
            return _result(STATE_UNTRUSTED, exc.detail)
        if hashlib.sha256(data).hexdigest() != want:
            mismatched.append(rel)

    listed = {rel for rel, _ in entries}
    for top in (VERIFIER_DIRNAME, PYCACHE_DIRNAME):
        try:
            found = _walk_files(fs, install_dir, top)
        except FileNotFoundError:
            found = []
        except OSError as exc:
            return _result(
                STATE_UNTRUSTED, "cannot list %s: %s" % (top, exc.strerror or exc)
            )
        mismatched.extend(rel for rel in found if rel not in listed)

    pinned = getattr(anchor, "verifier_sha256", None)
    if pinned != dict(entries)[LAUNCHER_NAME] and LAUNCHER_NAME not in mismatched:
        mismatched.append(LAUNCHER_NAME)

    precompiled = any(rel.endswith(".pyc") for rel in listed)
    if mismatched:
        return _result(
            STATE_MISMATCH,
            "installed files differ from the manifest or the anchor",
            files=len(entries),
            precompiled=precompiled,
            mismatched=mismatched,
        )
    return _result(STATE_MATCH, files=len(entries), precompiled=precompiled)
