"""G-4, G-5, G-6: the root-owned owner-grant anchor (verify addendum §1.3, §1.5, §2.4 step 3).

No test here needs root or writes under /Library. The anchor loader takes an injected
filesystem (``fs=``) so a fake one can present root-owned versus user-owned chains at the
real, hard-coded anchor path. The only real-filesystem checks are read-only ``lstat``s.
"""

import ast
import base64
import hashlib
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hermes_owner_grant import anchor

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "hermes_owner_grant"
EXPECTED_DIR = "/Library/Application Support/Hermes/owner-grant"
EXPECTED_PATH = EXPECTED_DIR + "/anchor.json"
CHAIN_DIRS = (
    "/",
    "/Library",
    "/Library/Application Support",
    "/Library/Application Support/Hermes",
    EXPECTED_DIR,
)


def _kid(pub):
    # Spelled out rather than imported so these tests stand alone from U1.
    return "ok_" + hashlib.sha256(pub).hexdigest()[:16]


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


PUB_A = bytes(range(32))
PUB_B = bytes(range(1, 33))
PUB_C = bytes(range(2, 34))
KID_A = _kid(PUB_A)
KID_B = _kid(PUB_B)
KID_C = _kid(PUB_C)
DAY_MS = 24 * 3600 * 1000


def _key(raw_pub, status="active", not_before=1_000, retired_at=None, **extra):
    entry = {
        "kid": _kid(raw_pub),
        "alg": "Ed25519",
        "pub": _b64(raw_pub),
        "status": status,
        "not_before": not_before,
        "retired_at": retired_at,
    }
    entry.update(extra)
    return entry


def _anchor_doc(keys=None, **overrides):
    doc = {
        "format": "hermes-owner-anchor/v1",
        "owner_uid": 501,
        "grants_dir": "/Users/owner/.hermes/owner-grants",
        "keys": [_key(PUB_A)] if keys is None else keys,
        "verifier_sha256": "ab" * 32,
    }
    doc.update(overrides)
    return doc


def _anchor_bytes(doc=None):
    return json.dumps(_anchor_doc() if doc is None else doc).encode("utf-8")


# -- fake filesystem ----------------------------------------------------------------------


def _st(kind, uid=0, perm=0o755, ino=1, size=0):
    return types.SimpleNamespace(
        st_mode=kind | perm, st_uid=uid, st_gid=0, st_dev=1, st_ino=ino, st_size=size
    )


class FakeFS:
    """Path -> (lstat result, content). ``opened`` lets a test fake a swap after lstat."""

    def __init__(self, content=None):
        content = _anchor_bytes() if content is None else content
        self.entries = {}
        for ino, path in enumerate(CHAIN_DIRS, start=10):
            self.entries[path] = [_st(stat.S_IFDIR, ino=ino), None]
        self.entries[EXPECTED_PATH] = [
            _st(stat.S_IFREG, perm=0o644, ino=99, size=len(content)),
            content,
        ]
        self.opened = None  # optional override of the fstat seen after open
        self.read_error = None
        self.touched = []

    def lstat(self, path):
        self.touched.append(path)
        if path not in self.entries:
            raise FileNotFoundError(2, "No such file or directory", path)
        return self.entries[path][0]

    def read_nofollow(self, path, limit):
        self.touched.append(path)
        if self.read_error is not None:
            raise self.read_error
        if path not in self.entries:
            raise FileNotFoundError(2, "No such file or directory", path)
        st, content = self.entries[path]
        if stat.S_ISLNK(st.st_mode):
            raise OSError(62, "Too many levels of symbolic links", path)
        return (self.opened or st), (content or b"")[:limit]

    def set(self, path, **fields):
        old = self.entries[path][0]
        kind = fields.pop("kind", stat.S_IFMT(old.st_mode))
        perm = fields.pop("perm", stat.S_IMODE(old.st_mode))
        self.entries[path][0] = _st(
            kind,
            uid=fields.pop("uid", old.st_uid),
            perm=perm,
            ino=old.st_ino,
            size=old.st_size,
        )
        assert not fields


def _load(fs):
    return anchor.load_trusted_anchor(fs=fs)


def _reason(fs):
    with pytest.raises(anchor.AnchorError) as err:
        _load(fs)
    return err.value.reason


# -- G-4: StrictModes-style chain ---------------------------------------------------------


def test_g4_root_owned_chain_is_trusted_and_parsed():
    content = _anchor_bytes()
    loaded = _load(FakeFS(content))
    assert loaded.owner_uid == 501
    assert loaded.grants_dir == "/Users/owner/.hermes/owner-grants"
    assert loaded.sha256 == hashlib.sha256(content).hexdigest()
    assert loaded.verifier_sha256 == "ab" * 32
    key = loaded.key(KID_A)
    assert key is not None and key.pub == PUB_A and key.status == "active"
    assert loaded.active_key == key
    assert loaded.key(KID_B) is None


@pytest.mark.parametrize(
    "path, fields",
    [
        (EXPECTED_PATH, {"uid": 501}),  # user-owned anchor file (T-3)
        (EXPECTED_PATH, {"perm": 0o664}),  # group-writable
        (EXPECTED_PATH, {"perm": 0o646}),  # other-writable
        (EXPECTED_PATH, {"perm": 0o666}),
        (EXPECTED_PATH, {"kind": stat.S_IFLNK, "perm": 0o755}),  # symlink
        (EXPECTED_PATH, {"kind": stat.S_IFDIR}),
        (EXPECTED_PATH, {"kind": stat.S_IFIFO, "perm": 0o644}),
        (EXPECTED_DIR, {"uid": 501}),  # user-owned parent
        (EXPECTED_DIR, {"perm": 0o775}),  # group-writable parent
        ("/Library/Application Support/Hermes", {"uid": 501}),
        ("/Library/Application Support/Hermes", {"perm": 0o757}),
        ("/Library/Application Support", {"perm": 0o777}),
        ("/Library", {"kind": stat.S_IFLNK}),  # symlinked parent
        ("/Library", {"kind": stat.S_IFREG, "perm": 0o644}),  # parent not a directory
        ("/", {"perm": 0o1777}),  # sticky + world-writable root
        ("/", {"uid": 501}),
    ],
)
def test_g4_untrusted_chain_is_rejected(path, fields):
    fs = FakeFS()
    fs.set(path, **fields)
    assert _reason(fs) == "anchor_untrusted"


@pytest.mark.parametrize(
    "missing", [EXPECTED_PATH, EXPECTED_DIR, "/Library/Application Support/Hermes"]
)
def test_g4_missing_anchor_is_anchor_missing(missing):
    fs = FakeFS()
    del fs.entries[missing]
    assert _reason(fs) == "anchor_missing"


def test_g4_a_swap_between_lstat_and_open_is_rejected():
    fs = FakeFS()
    fs.opened = _st(
        stat.S_IFREG, perm=0o644, ino=12345
    )  # a different inode than lstat saw
    assert _reason(fs) == "anchor_untrusted"


def test_g4_opened_file_must_itself_be_root_owned_and_not_writable():
    fs = FakeFS()
    fs.opened = _st(stat.S_IFREG, uid=501, perm=0o644, ino=99)
    assert _reason(fs) == "anchor_untrusted"
    fs.opened = _st(stat.S_IFREG, perm=0o666, ino=99)
    assert _reason(fs) == "anchor_untrusted"


@pytest.mark.parametrize(
    "error, reason",
    [
        (OSError(62, "Too many levels of symbolic links"), "anchor_untrusted"),
        (PermissionError(13, "Permission denied"), "anchor_untrusted"),
        (FileNotFoundError(2, "No such file or directory"), "anchor_missing"),
    ],
)
def test_g4_open_errors_fail_closed(error, reason):
    fs = FakeFS()
    fs.read_error = error
    assert _reason(fs) == reason


def test_g4_oversized_anchor_is_rejected():
    fs = FakeFS(b" " * (anchor.MAX_ANCHOR_BYTES + 1))
    assert _reason(fs) == "anchor_untrusted"


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"format":"hermes-owner-anchor/v1","format":"hermes-owner-anchor/v1"}',
        _anchor_bytes(_anchor_doc(format="hermes-owner-anchor/v2")),
        _anchor_bytes(_anchor_doc(owner_uid=True)),
        _anchor_bytes(_anchor_doc(owner_uid=-1)),
        _anchor_bytes(_anchor_doc(owner_uid="501")),
        _anchor_bytes(_anchor_doc(grants_dir="relative/owner-grants")),
        _anchor_bytes(_anchor_doc(grants_dir="/Users/owner/../x")),
        _anchor_bytes(_anchor_doc(keys=[])),
        _anchor_bytes(_anchor_doc(keys="nope")),
        _anchor_bytes(
            _anchor_doc(keys=[_key(PUB_A, kid=KID_B)])
        ),  # kid not derived from pub
        _anchor_bytes(
            _anchor_doc(keys=[_key(PUB_A), _key(PUB_A, status="retired", retired_at=5)])
        ),
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A), _key(PUB_B)])),  # two active keys
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A, status="disabled")])),
        _anchor_bytes(
            _anchor_doc(keys=[_key(PUB_A, status="retired")])
        ),  # retired without retired_at
        _anchor_bytes(
            _anchor_doc(keys=[_key(PUB_A, retired_at=5)])
        ),  # active with retired_at
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A, alg="RS256")])),
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A, pub=_b64(PUB_A[:31]))])),
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A, not_before=True)])),
        _anchor_bytes(_anchor_doc(keys=[_key(PUB_A, not_before=1.5)])),
        _anchor_bytes(_anchor_doc(verifier_sha256="XYZ")),
        b"\xff\xfe",
    ],
)
def test_g4_malformed_anchor_contents_are_untrusted(content):
    assert _reason(FakeFS(content)) == "anchor_untrusted"


def test_g4_a_real_user_owned_anchor_file_is_rejected(tmp_path):
    if os.getuid() == 0:
        pytest.skip("runs as root; a tmp file would be root-owned")
    path = tmp_path / "anchor.json"
    path.write_bytes(_anchor_bytes())
    path.chmod(0o644)
    with pytest.raises(anchor.AnchorError) as err:
        anchor._load_anchor_at(str(path), anchor.OsFileSystem())
    assert err.value.reason == "anchor_untrusted"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system directory layout")
def test_g4_real_macos_system_parents_meet_the_rule_read_only():
    # Read-only lstat of the parents the installer relies on; nothing is written.
    anchor._check_dir_chain("/Library/Application Support", anchor.OsFileSystem())


def test_g4_real_default_filesystem_reports_missing_when_not_installed():
    if os.path.lexists(EXPECTED_PATH):
        pytest.skip("an anchor is installed on this machine")
    with pytest.raises(anchor.AnchorError) as err:
        anchor.load_trusted_anchor()
    assert err.value.reason == "anchor_missing"


# -- G-5: no env var or flag can move the anchor ------------------------------------------


def test_g5_anchor_path_is_the_hard_coded_constant():
    assert anchor.ANCHOR_DIR == EXPECTED_DIR
    assert anchor.ANCHOR_PATH == EXPECTED_PATH


def test_g5_loader_has_no_path_parameter():
    params = inspect.signature(anchor.load_trusted_anchor).parameters
    assert list(params) == ["fs"]
    assert params["fs"].kind is inspect.Parameter.KEYWORD_ONLY


_OVERRIDE_ENV_NAMES = (
    "HERMES_OWNER_GRANT_ANCHOR",
    "HERMES_OWNER_ANCHOR",
    "HERMES_ANCHOR_PATH",
    "HERMES_OWNER_GRANT_DIR",
    "HERMES_HOME",
    "HOME",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
)


def test_g5_env_cannot_redirect_the_in_process_loader(monkeypatch, tmp_path):
    for name in _OVERRIDE_ENV_NAMES:
        if not name.startswith("PYTHONHOME"):
            monkeypatch.setenv(name, str(tmp_path / "anchor.json"))
    fs = FakeFS()
    _load(fs)
    allowed = set(CHAIN_DIRS) | {EXPECTED_PATH}
    assert fs.touched and set(fs.touched) <= allowed


def test_g5_env_cannot_redirect_a_fresh_isolated_interpreter(tmp_path):
    # A shadow package on PYTHONPATH and an anchor-looking env var: neither may matter.
    shadow = tmp_path / "shadow" / "hermes_owner_grant"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    (shadow / "anchor.py").write_text("ANCHOR_PATH = 'HIJACKED'\n")
    fake_anchor = tmp_path / "anchor.json"
    fake_anchor.write_bytes(_anchor_bytes())
    env = {
        name: str(fake_anchor) for name in _OVERRIDE_ENV_NAMES if name != "PYTHONHOME"
    }
    env["PYTHONPATH"] = str(tmp_path / "shadow")
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    code = (
        "import json, sys; sys.path.insert(0, %r)\n"
        "from hermes_owner_grant import anchor\n"
        "class Rec(anchor.OsFileSystem):\n"
        "    seen = []\n"
        "    def lstat(self, p):\n"
        "        Rec.seen.append(p); return anchor.OsFileSystem.lstat(self, p)\n"
        "    def read_nofollow(self, p, n):\n"
        "        Rec.seen.append(p); return anchor.OsFileSystem.read_nofollow(self, p, n)\n"
        "try:\n"
        "    anchor.load_trusted_anchor(fs=Rec()); reason = None\n"
        "except anchor.AnchorError as exc:\n"
        "    reason = exc.reason\n"
        "print(json.dumps({'path': anchor.ANCHOR_PATH, 'file': anchor.__file__, 'seen': Rec.seen,"
        " 'reason': reason}))\n"
    ) % str(REPO_ROOT)
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout)
    assert result["path"] == EXPECTED_PATH
    assert Path(result["file"]).resolve() == (PACKAGE_DIR / "anchor.py").resolve()
    assert set(result["seen"]) <= set(CHAIN_DIRS) | {EXPECTED_PATH}
    if not os.path.lexists(EXPECTED_PATH):
        assert result["reason"] == "anchor_missing"
    assert str(fake_anchor) not in out.stdout


def _package_sources():
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "hermes_owner_grant package is missing"
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in files
    ]


def test_g5_no_package_module_reads_the_environment():
    banned = {"environ", "environb", "getenv", "getenvb", "putenv"}
    for path, tree in _package_sources():
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name.split(".")[-1]
            assert name not in banned, "%s:%s reads the environment (%s)" % (
                path.name,
                getattr(node, "lineno", "?"),
                name,
            )


def test_g5_no_package_module_defines_an_anchor_flag():
    for path, tree in _package_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.lower()
                assert not (text.startswith("-") and "anchor" in text), (
                    "%s:%s defines %r" % (path.name, node.lineno, node.value)
                )


@pytest.mark.parametrize(
    "argv",
    [
        ["anchor-status", "--anchor", "/tmp/x.json"],
        ["verify", "--anchor", "/tmp/x.json", "--session", "s", "--text-sha", "0" * 64],
        [
            "verify",
            "--anchor-path=/tmp/x.json",
            "--session",
            "s",
            "--text-sha",
            "0" * 64,
        ],
    ],
)
def test_g5_cli_rejects_an_anchor_flag(argv):
    # Activates when U7 lands hermes_owner_grant/cli.py; argparse usage errors exit 2.
    if importlib.util.find_spec("hermes_owner_grant.cli") is None:
        pytest.skip("hermes_owner_grant.cli is built by U7")
    code = (
        "import sys; sys.path.insert(0, %r); import runpy; runpy.run_module('hermes_owner_grant.cli', run_name='__main__')"
        % str(REPO_ROOT)
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code] + argv,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


# -- G-6: key status ----------------------------------------------------------------------

RETIRED_AT = 50 * DAY_MS
NOT_BEFORE = 10 * DAY_MS


def _status_anchor():
    return anchor.parse_anchor(
        _anchor_bytes(
            _anchor_doc(
                keys=[
                    _key(PUB_A, status="active", not_before=NOT_BEFORE),
                    _key(
                        PUB_B, status="retired", not_before=1_000, retired_at=RETIRED_AT
                    ),
                    _key(PUB_C, status="revoked", not_before=1_000),
                ]
            )
        )
    )


def test_g6_active_key_inside_validity_is_ok():
    a = _status_anchor()
    assert (
        anchor.check_key(a, KID_A, issued_at=NOT_BEFORE + 1, now=NOT_BEFORE + 2) is None
    )


def test_g6_unknown_kid():
    a = _status_anchor()
    assert (
        anchor.check_key(a, "ok_ffffffffffffffff", issued_at=NOT_BEFORE, now=NOT_BEFORE)
        == "unknown_kid"
    )


def test_g6_revoked_key_fails_for_every_grant_even_backdated():
    a = _status_anchor()
    for issued_at in (1_000, 2_000, NOT_BEFORE, RETIRED_AT + 1):
        assert (
            anchor.check_key(a, KID_C, issued_at=issued_at, now=issued_at + 1)
            == "key_revoked"
        )


def test_g6_retired_key_verifies_only_grants_issued_before_retirement():
    a = _status_anchor()
    assert (
        anchor.check_key(a, KID_B, issued_at=RETIRED_AT - 1, now=RETIRED_AT + DAY_MS)
        is None
    )
    assert (
        anchor.check_key(a, KID_B, issued_at=RETIRED_AT, now=RETIRED_AT + 1)
        == "key_retired"
    )
    assert (
        anchor.check_key(a, KID_B, issued_at=RETIRED_AT + 5, now=RETIRED_AT + 6)
        == "key_retired"
    )


def test_g6_retired_key_is_dead_once_the_longest_ttl_after_retirement_has_passed():
    # A thief holding a retired key can backdate issued_at and pick any expires_at; the
    # verifier bounds that window at retired_at + the longest grant TTL class.
    a = _status_anchor()
    limit = RETIRED_AT + anchor.MAX_GRANT_TTL_MS
    assert anchor.MAX_GRANT_TTL_MS == 7 * DAY_MS
    assert anchor.check_key(a, KID_B, issued_at=RETIRED_AT - 1, now=limit) is None
    assert (
        anchor.check_key(a, KID_B, issued_at=RETIRED_AT - 1, now=limit + 1)
        == "key_retired"
    )


def test_g6_key_not_yet_valid():
    a = _status_anchor()
    assert (
        anchor.check_key(a, KID_A, issued_at=NOT_BEFORE, now=NOT_BEFORE - 1)
        == "key_not_yet_valid"
    )
    # A grant claiming to predate its key is backdated.
    assert (
        anchor.check_key(a, KID_A, issued_at=NOT_BEFORE - 1, now=NOT_BEFORE + 1)
        == "key_not_yet_valid"
    )


def test_g6_revocation_outranks_other_key_failures():
    a = anchor.parse_anchor(
        _anchor_bytes(
            _anchor_doc(keys=[_key(PUB_C, status="revoked", not_before=NOT_BEFORE)])
        )
    )
    assert anchor.check_key(a, KID_C, issued_at=0, now=0) == "key_revoked"
    assert a.active_key is None


@pytest.mark.parametrize("bad", [True, None, 1.0, "1"])
def test_g6_non_integer_times_are_refused(bad):
    a = _status_anchor()
    with pytest.raises(TypeError):
        anchor.check_key(a, KID_A, issued_at=bad, now=NOT_BEFORE + 1)
    with pytest.raises(TypeError):
        anchor.check_key(a, KID_A, issued_at=NOT_BEFORE + 1, now=bad)
