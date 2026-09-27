"""U11b: the root-owned verifier install tree, its launcher and the installed-manifest check.

The anchor install (apps/desktop/electron/owner-grant-anchor-install.ts) co-installs, root-owned:

* ``hermes_owner_verify.py``: a tiny launcher that ``/usr/bin/python3 -I -S`` runs;
* ``verifier/hermes_owner_grant/``: the stdlib-only package;
* ``pycache/``: the root-filled ``sys.pycache_prefix`` (the package and the stdlib it imports);
* ``manifest.sha256``: the sha256 of every installed launcher, package and ``.pyc`` file.

Isolation: nothing here reads or writes the real ``/Library``, the Keychain, ``~/.hermes`` or
``~/.claude``. Install trees are built in ``tmp_path`` by ``builder.build_install_tree`` (the
same layout the root script writes), root ownership is simulated with an injected fs, and the
timing run patches the anchor loader to a fixture anchor in-process.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_owner_grant import anchor as anchor_mod
from hermes_owner_grant import builder
from hermes_owner_grant import cli as cli_mod
from hermes_owner_grant import doctor
from hermes_owner_grant import install_check

SYSTEM_PYTHON = Path("/usr/bin/python3")
HAVE_SYSTEM_PYTHON = sys.platform == "darwin" and SYSTEM_PYTHON.is_file()
needs_system_python = pytest.mark.skipif(
    not HAVE_SYSTEM_PYTHON, reason="needs macOS /usr/bin/python3 (the hook interpreter)"
)


# -- helpers -------------------------------------------------------------------------------


class RootFs:
    """Answers like the real fs, except every path reports root ownership and no group/other
    write bit, unless it is listed in ``user_owned`` or ``writable``."""

    def __init__(self):
        self.real = anchor_mod.OsFileSystem()
        self.user_owned = set()
        self.writable = set()

    def _fake(self, st, path):
        mode = st.st_mode if path in self.writable else st.st_mode & ~0o022
        uid = os.getuid() if path in self.user_owned else 0
        return SimpleNamespace(
            st_mode=mode,
            st_uid=uid,
            st_dev=st.st_dev,
            st_ino=st.st_ino,
            st_size=st.st_size,
        )

    def lstat(self, path):
        return self._fake(os.lstat(path), path)

    def read_nofollow(self, path, limit):
        st, data = self.real.read_nofollow(path, limit)
        return self._fake(st, path), data

    def listdir(self, path):
        return os.listdir(path)


def _anchor_for(
    tree: Path, *, verifier_sha256=None, grants_dir="/Users/owner/.hermes/owner-grants"
):
    if verifier_sha256 is None:
        verifier_sha256 = hashlib.sha256(
            (tree / "hermes_owner_verify.py").read_bytes()
        ).hexdigest()
    return SimpleNamespace(verifier_sha256=verifier_sha256, grants_dir=grants_dir)


def _tree(tmp_path, *, compile_python=None) -> Path:
    return builder.build_install_tree(
        tmp_path / "owner-grant", compile_python=compile_python
    )


def _check(tree, anchor=None, fs=None):
    return install_check.check_installed_verifier(
        anchor if anchor is not None else _anchor_for(tree),
        install_dir=str(tree),
        fs=fs if fs is not None else RootFs(),
    )


# -- the tree the installer writes ---------------------------------------------------------


def test_install_tree_layout_and_manifest_cover_launcher_and_package(tmp_path):
    tree = _tree(tmp_path)
    pkg = tree / "verifier" / "hermes_owner_grant"

    assert (tree / "hermes_owner_verify.py").read_text(
        "utf-8"
    ) == builder.LAUNCHER_SOURCE
    assert sorted(p.name for p in pkg.iterdir()) == sorted(builder.PACKAGE_FILES)
    for name in builder.PACKAGE_FILES:
        assert (pkg / name).read_bytes() == (builder.PACKAGE_DIR / name).read_bytes()
    # builder.py and doctor.py are tooling, never installed.
    assert "builder.py" not in builder.PACKAGE_FILES
    assert "doctor.py" not in builder.PACKAGE_FILES

    lines = (tree / "manifest.sha256").read_text("ascii").splitlines()
    want = ["hermes_owner_verify.py"] + [
        "verifier/hermes_owner_grant/" + name for name in builder.PACKAGE_FILES
    ]
    assert [line.split("  ", 1)[1] for line in lines] == want
    for line in lines:
        digest, rel = line.split("  ", 1)
        assert digest == hashlib.sha256((tree / rel).read_bytes()).hexdigest()


def test_every_module_the_cli_imports_is_installed():
    shipped = {name[:-3] for name in builder.PACKAGE_FILES if name.endswith(".py")}
    assert set(builder.MODULES) <= shipped
    assert "scopes.json" in builder.PACKAGE_FILES


def test_launcher_is_tiny_reads_no_env_and_imports_only_sys():
    source = builder.LAUNCHER_SOURCE
    assert len(source.encode("utf-8")) < 3000
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"sys", "json", "hermes_owner_grant"}
    for banned in (
        "environ",
        "getenv",
        "getcwd",
        "PYTHONPATH",
        "os.path",
        "__import__",
        "exec(",
    ):
        assert banned not in source, banned
    compile(source, "hermes_owner_verify.py", "exec")


# -- the launcher runs only the root-owned package -----------------------------------------


def _shadow_package(where: Path, marker: Path) -> None:
    pkg = where / "hermes_owner_grant"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "open(%r, 'a').write('shadow-init\\n')\n" % str(marker), encoding="utf-8"
    )
    (pkg / "cli.py").write_text(
        "open(%r, 'a').write('shadow-cli\\n')\n"
        "def main(argv=None):\n"
        '    print(\'{"ok": true, "shadow": true}\')\n'
        "    return 0\n" % str(marker),
        encoding="utf-8",
    )


def _hostile_env(tmp_path: Path, marker: Path) -> tuple[Path, dict]:
    cwd = tmp_path / "agent-cwd"
    cwd.mkdir()
    _shadow_package(cwd, marker)
    hostile = tmp_path / "hostile-pythonpath"
    hostile.mkdir()
    _shadow_package(hostile, marker)
    for name in ("sitecustomize.py", "usercustomize.py"):
        (hostile / name).write_text(
            "open(%r, 'a').write(%r)\n" % (str(marker), name + "\n"), encoding="utf-8"
        )
        (cwd / name).write_text(
            "open(%r, 'a').write(%r)\n" % (str(marker), name + "\n"), encoding="utf-8"
        )
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(hostile) + os.pathsep + str(cwd),
        "PYTHONSTARTUP": str(hostile / "sitecustomize.py"),
        "PYTHONHOME": str(hostile),
    }
    return cwd, env


@needs_system_python
def test_launcher_ignores_cwd_and_pythonpath_shadows(tmp_path):
    tree = _tree(tmp_path, compile_python=str(SYSTEM_PYTHON))
    launcher = tree / "hermes_owner_verify.py"
    marker = tmp_path / "shadow-ran"
    cwd, env = _hostile_env(tmp_path, marker)
    env.pop(
        "PYTHONHOME"
    )  # -I ignores it too, but a bogus home would stop the interpreter itself

    # A usage error never reads the anchor, so the real /Library is not consulted.
    proc = subprocess.run(
        [str(SYSTEM_PYTHON), "-I", "-S", str(launcher), "verify"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    body = json.loads(proc.stdout)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert body["schema"] == "hermes-owner-verify/v1"
    assert body["reason"] == "usage_error"
    assert "shadow" not in body

    # Which files were actually imported: only the root-owned tree.
    probe = (
        "import sys\n"
        "ns = {'__name__': 'probe', '__file__': %r}\n"
        "exec(compile(open(%r, 'rb').read(), %r, 'exec'), ns)\n"
        "cli, why = ns['_load']()\n"
        "mods = sorted(m.__file__ for n, m in sys.modules.items()\n"
        "              if n.startswith('hermes_owner_grant') and getattr(m, '__file__', None))\n"
        "print('\\n'.join([cli.__file__] + mods))\n"
    ) % (str(launcher), str(launcher), str(launcher))
    proc = subprocess.run(
        [str(SYSTEM_PYTHON), "-I", "-S", "-c", probe],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    files = proc.stdout.split()
    root = str(tree / "verifier" / "hermes_owner_grant") + "/"
    assert files and all(f.startswith(root) for f in files), files
    assert not marker.exists(), marker.read_text()


@needs_system_python
def test_launcher_refuses_to_run_without_isolation(tmp_path):
    tree = _tree(tmp_path, compile_python=str(SYSTEM_PYTHON))
    marker = tmp_path / "shadow-ran"
    cwd, env = _hostile_env(tmp_path, marker)
    env.pop("PYTHONHOME")
    for name in ("sitecustomize.py", "usercustomize.py"):
        # Without -S these would run before any launcher code: that is exactly why the hook
        # path mandates -I -S. Keep them out so the test isolates the launcher's own refusal.
        (tmp_path / "hostile-pythonpath" / name).unlink()
        (cwd / name).unlink()

    for flags in ([], ["-I"], ["-S"], ["-E", "-s", "-S"]):
        proc = subprocess.run(
            [
                str(SYSTEM_PYTHON),
                *flags,
                str(tree / "hermes_owner_verify.py"),
                "verify",
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        body = json.loads(proc.stdout)
        assert proc.returncode == 3, (flags, proc.stdout, proc.stderr)
        assert body["ok"] is False and body["reason"] == "verifier_untrusted", flags
    assert not marker.exists(), marker.read_text()


@needs_system_python
def test_the_root_warm_step_compiles_but_never_executes_the_staged_code(tmp_path):
    """Root fills pycache/ from files that came from an agent-writable tree, so it must only
    compile them: modulefinder scans bytecode, py_compile writes .pyc, nothing is imported."""
    verifier = tmp_path / "verifier"
    pkg = verifier / "hermes_owner_grant"
    pkg.mkdir(parents=True)
    marker = tmp_path / "ran-as-root"
    payload = "open(%r, 'a').write(__name__ + '\\n')\n" % str(marker)
    (pkg / "__init__.py").write_text(payload, encoding="utf-8")
    (pkg / "cli.py").write_text(
        payload + "from . import helper\nimport argparse\n", encoding="utf-8"
    )
    (pkg / "helper.py").write_text(payload, encoding="utf-8")
    prefix = tmp_path / "pycache"
    proc = subprocess.run(
        [
            str(SYSTEM_PYTHON),
            "-I",
            "-S",
            "-X",
            "pycache_prefix=" + str(prefix),
            "-c",
            builder.WARM_PROGRAM,
            str(verifier),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), marker.read_text()
    names = {p.name for p in prefix.rglob("*.pyc")}
    for module in ("__init__", "cli", "helper", "argparse"):
        assert module + ".cpython-39.pyc" in names, module


def _timing_fixture(tmp_path):
    tv = pytest.importorskip("tests.hermes_owner_grant.test_verify")
    owner = tv.Owner()
    grants = tmp_path / "owner-grants"
    grants.mkdir()
    now = int(time.time() * 1000)
    uid = os.getuid()
    env = tv.seal(owner, tv.make_payload(issued_at=now - tv.MIN, owner_uid=uid))
    tv.write_grant(grants, env)
    wire = env.to_json()
    # The same 200-file grants dir as G-16 (test_verify), so the numbers compare.
    for index in range(199):
        suffix = base64.b32encode(index.to_bytes(4, "big")).decode("ascii").lower()
        fake_id = "og_" + (suffix + "a" * 26)[:26]
        (grants / ("%d-%s.json" % (now - tv.MIN, fake_id))).write_text(
            wire, encoding="utf-8"
        )
    anchor_doc = {
        "format": "hermes-owner-anchor/v1",
        "owner_uid": uid,
        "grants_dir": os.path.realpath(str(grants)),
        "keys": [owner.anchor_key()],
        "verifier_sha256": "ab" * 32,
    }
    return anchor_doc, tv.SESSION, tv.sha()


_TIMED_LAUNCHER = (
    "import sys\n"
    "L = %r\n"
    "ns = {'__name__': 'hermes_owner_verify_timing', '__file__': L}\n"
    "exec(compile(open(L, 'rb').read(), L, 'exec'), ns)\n"
    "cli, why = ns['_load']()\n"
    "A = sys.modules['hermes_owner_grant.anchor']\n"
    "fixture = A.parse_anchor(%r.encode())\n"
    "A.load_trusted_anchor = lambda fs=None: fixture\n"
    "raise SystemExit(ns['_main']())\n"
)


def _median_ms(argv, runs=9):
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, cwd="/")
        times.append(time.perf_counter() - start)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["ok"] is True, proc.stdout
    return statistics.median(times) * 1000


@needs_system_python
def test_launcher_full_process_verify_is_under_100ms(tmp_path):
    """D13 / G-16: the whole ``/usr/bin/python3 -I -S <launcher> verify ...`` process, against
    a fixture anchor (the loader is patched in-process; the real /Library is never read)."""
    anchor_doc, session, text_sha = _timing_fixture(tmp_path)
    tree = _tree(tmp_path, compile_python=str(SYSTEM_PYTHON))
    pycs = sorted(p.name for p in (tree / "pycache").rglob("*.pyc"))
    for module in ("cli", "verify", "argparse", "dataclasses", "inspect"):
        assert module + ".cpython-39.pyc" in pycs, module
    launcher = tree / "hermes_owner_verify.py"
    args = ["verify", "--session", session, "--text-sha", text_sha]

    code = _TIMED_LAUNCHER % (str(launcher), json.dumps(anchor_doc))
    launcher_ms = _median_ms([str(SYSTEM_PYTHON), "-I", "-S", "-c", code, *args])

    # For comparison only: the single-file bundle (D13 measured ~145 ms).
    bundle = builder.build(tmp_path / "bundle" / "hermes_owner_verify.py")
    bundle_code = (
        "import sys\n"
        "B = %r\n"
        "ns = {'__name__': 'bundle_timing', '__file__': B}\n"
        "exec(compile(open(B, 'rb').read(), B, 'exec'), ns)\n"
        "A = sys.modules['hermes_owner_grant.anchor']\n"
        "fixture = A.parse_anchor(%r.encode())\n"
        "A.load_trusted_anchor = lambda fs=None: fixture\n"
        "raise SystemExit(sys.modules['hermes_owner_grant.cli'].main())\n"
    ) % (str(bundle), json.dumps(anchor_doc))
    bundle_ms = _median_ms(
        [str(SYSTEM_PYTHON), "-I", "-S", "-c", bundle_code, *args], runs=5
    )

    sys.__stdout__.write(
        "\nU11b full-process verify (200 grants, median): launcher %.1f ms, "
        "single-file bundle %.1f ms\n" % (launcher_ms, bundle_ms)
    )
    assert launcher_ms < 100, "launcher verify took %.1f ms" % launcher_ms


# -- the installed-manifest check (doctor / anchor-status) ---------------------------------


def test_installed_tree_matches_its_manifest(tmp_path):
    tree = _tree(tmp_path)
    result = _check(tree)
    assert result["state"] == "match", result
    assert result["files"] == 1 + len(builder.PACKAGE_FILES)
    assert result["precompiled"] is False
    assert result["mismatched"] == []


@needs_system_python
def test_installed_tree_with_pyc_matches_and_reports_precompiled(tmp_path):
    tree = _tree(tmp_path, compile_python=str(SYSTEM_PYTHON))
    result = _check(tree)
    assert result["state"] == "match", result
    assert result["precompiled"] is True
    pyc = sorted((tree / "pycache").rglob("*.pyc"))[0]
    pyc.write_bytes(pyc.read_bytes() + b"\0")
    result = _check(tree)
    assert result["state"] == "mismatch"
    assert result["mismatched"] == [str(pyc.relative_to(tree))]


def test_a_changed_installed_file_is_a_mismatch_naming_the_file(tmp_path):
    tree = _tree(tmp_path)
    target = tree / "verifier" / "hermes_owner_grant" / "verify.py"
    target.write_text(target.read_text("utf-8") + "\n# patched\n", encoding="utf-8")
    result = _check(tree)
    assert result["state"] == "mismatch"
    assert result["mismatched"] == ["verifier/hermes_owner_grant/verify.py"]


def test_a_missing_installed_file_is_a_mismatch(tmp_path):
    tree = _tree(tmp_path)
    (tree / "verifier" / "hermes_owner_grant" / "quote.py").unlink()
    result = _check(tree)
    assert result["state"] == "mismatch"
    assert result["mismatched"] == ["verifier/hermes_owner_grant/quote.py"]


def test_an_extra_file_in_the_verifier_dir_is_a_mismatch(tmp_path):
    tree = _tree(tmp_path)
    (tree / "verifier" / "hermes_owner_grant" / "evil.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tree / "verifier" / "sitecustomize.py").write_text("x = 1\n", encoding="utf-8")
    (tree / "pycache" / "x").mkdir(parents=True)
    (tree / "pycache" / "x" / "argparse.cpython-39.pyc").write_bytes(b"\0")
    result = _check(tree)
    assert result["state"] == "mismatch"
    assert result["mismatched"] == [
        "pycache/x/argparse.cpython-39.pyc",
        "verifier/hermes_owner_grant/evil.py",
        "verifier/sitecustomize.py",
    ]


def test_launcher_hash_must_equal_the_anchor_verifier_sha256(tmp_path):
    tree = _tree(tmp_path)
    result = _check(tree, anchor=_anchor_for(tree, verifier_sha256="0" * 64))
    assert result["state"] == "mismatch"
    assert "hermes_owner_verify.py" in result["mismatched"]
    unpinned = _check(
        tree, anchor=SimpleNamespace(verifier_sha256=None, grants_dir="/x")
    )
    assert unpinned["state"] == "mismatch"


def test_a_user_owned_or_writable_installed_path_is_untrusted(tmp_path):
    tree = _tree(tmp_path)
    for rel in (
        "verifier/hermes_owner_grant/cli.py",
        "verifier/hermes_owner_grant",
        "verifier",
        "manifest.sha256",
        "hermes_owner_verify.py",
    ):
        fs = RootFs()
        fs.user_owned.add(str(tree / rel))
        assert _check(tree, fs=fs)["state"] == "untrusted", rel
        fs = RootFs()
        os.chmod(tree / rel, os.stat(tree / rel).st_mode | stat.S_IWOTH)
        fs.writable.add(str(tree / rel))
        assert _check(tree, fs=fs)["state"] == "untrusted", rel
        os.chmod(tree / rel, os.stat(tree / rel).st_mode & ~stat.S_IWOTH)


def test_no_manifest_means_missing(tmp_path):
    tree = _tree(tmp_path)
    (tree / "manifest.sha256").unlink()
    assert _check(tree)["state"] == "missing"


@pytest.mark.parametrize(
    "line",
    [
        "%s  ../anchor.json" % ("a" * 64),
        "%s  /etc/passwd" % ("a" * 64),
        "%s  verifier/hermes_owner_grant/builder.py" % ("a" * 64),
        "%s  verifier/hermes_owner_grant/__pycache__/cli.cpython-39.pyc" % ("a" * 64),
        "%s  pycache/../x.pyc" % ("a" * 64),
        "%s  pycache//x.pyc" % ("a" * 64),
        "%s  pycache/x.py" % ("a" * 64),
        "%s verifier/hermes_owner_grant/cli.py" % ("a" * 64),
        "%s  verifier/hermes_owner_grant/cli.py" % ("A" * 64),
    ],
)
def test_a_malformed_manifest_is_untrusted(tmp_path, line):
    tree = _tree(tmp_path)
    manifest = tree / "manifest.sha256"
    manifest.write_text(manifest.read_text("ascii") + line + "\n", encoding="ascii")
    assert _check(tree)["state"] == "untrusted"


def test_duplicate_or_missing_required_manifest_entries_are_untrusted(tmp_path):
    tree = _tree(tmp_path)
    manifest = tree / "manifest.sha256"
    lines = manifest.read_text("ascii").splitlines()
    manifest.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="ascii")
    assert _check(tree)["state"] == "untrusted"
    manifest.write_text("\n".join(lines[:-1]) + "\n", encoding="ascii")
    assert _check(tree)["state"] == "untrusted"


def test_install_check_production_path_is_the_constant():
    assert install_check.INSTALL_DIR == anchor_mod.ANCHOR_DIR
    assert (
        install_check.LAUNCHER_PATH == anchor_mod.ANCHOR_DIR + "/hermes_owner_verify.py"
    )
    assert doctor.VERIFIER_PATH == install_check.LAUNCHER_PATH


# -- doctor and anchor-status report it ----------------------------------------------------


def test_doctor_fails_and_reports_mismatch_when_an_installed_file_differs(
    tmp_path, monkeypatch
):
    tree = _tree(tmp_path)
    grants = tmp_path / "grants"
    grants.mkdir()
    anchor = SimpleNamespace(
        owner_uid=0,
        grants_dir=str(grants),
        keys=(SimpleNamespace(kid="key-1", status="active"),),
        verifier_sha256=hashlib.sha256(
            (tree / "hermes_owner_verify.py").read_bytes()
        ).hexdigest(),
        sha256="anchor-hash",
    )
    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", lambda: anchor)
    monkeypatch.setattr(doctor, "VERIFIER_PATH", str(tree / "hermes_owner_verify.py"))
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (True, "same"))
    real_check = install_check.check_installed_verifier
    monkeypatch.setattr(
        doctor.install_check,
        "check_installed_verifier",
        lambda a: real_check(a, install_dir=str(tree), fs=RootFs()),
    )

    ok = doctor.run_doctor()
    assert ok["ok"] is True, ok
    assert ok["checks"]["verifier_install"] == "match"

    cli_py = tree / "verifier" / "hermes_owner_grant" / "cli.py"
    cli_py.write_bytes(cli_py.read_bytes().replace(b"return", b"return ", 1))
    bad = doctor.run_doctor()
    assert bad["ok"] is False
    assert bad["checks"]["verifier_install"] == "mismatch"
    assert bad["checks"]["verifier_mismatched"] == [
        "verifier/hermes_owner_grant/cli.py"
    ]


def test_anchor_status_reports_the_verifier_install_state(tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    anchor = SimpleNamespace(
        owner_uid=501,
        sha256="ab" * 32,
        verifier_sha256=hashlib.sha256(
            (tree / "hermes_owner_verify.py").read_bytes()
        ).hexdigest(),
        grants_dir="/x",
    )
    monkeypatch.setattr(cli_mod.anchor_mod, "load_trusted_anchor", lambda: anchor)
    real_check = install_check.check_installed_verifier
    monkeypatch.setattr(
        cli_mod.install_check,
        "check_installed_verifier",
        lambda a: real_check(a, install_dir=str(tree), fs=RootFs()),
    )
    import io

    out = io.StringIO()
    assert cli_mod.main(["anchor-status"], stdout=out) == 0
    body = json.loads(out.getvalue())
    assert body["ok"] is True
    assert body["verifier_install"] == "match"

    shutil.rmtree(tree / "verifier" / "hermes_owner_grant")
    out = io.StringIO()
    assert cli_mod.main(["anchor-status"], stdout=out) == 0
    assert json.loads(out.getvalue())["verifier_install"] == "mismatch"
