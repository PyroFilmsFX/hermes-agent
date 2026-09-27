"""The owner-grant package imports nothing outside the stdlib and stays Python 3.9 compatible.

The canonical hook path runs the verifier under ``/usr/bin/python3 -I -S`` (3.9.6 on the
owner's machine), with no site-packages. A third-party import would break that path, and
syntax newer than 3.9 would fail to compile there. (Addendum §3.1, G-3 shape.)
"""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = "hermes_owner_grant"
PACKAGE_DIR = Path(__file__).resolve().parents[2] / PACKAGE
# `cryptography` is the documented optional fast path (§3.1). It may be imported only inside
# a try/except ImportError, never at the top level of a module.
OPTIONAL = {"cryptography"}


def _modules():
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "hermes_owner_grant package is missing"
    return files


def _guarded_by_import_error(node, parents):
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try):
            for handler in current.handlers:
                names = []
                if isinstance(handler.type, ast.Name):
                    names = [handler.type.id]
                elif isinstance(handler.type, ast.Tuple):
                    names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
                if {"ImportError", "ModuleNotFoundError"} & set(names):
                    return True
        current = parents.get(current)
    return False


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_imports_only_the_stdlib(path):
    stdlib = set(sys.stdlib_module_names)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                continue
            roots = [(node.module or "").split(".")[0]]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "__import__"
        ):
            pytest.fail("%s:%d uses __import__" % (path.name, node.lineno))
        else:
            continue
        for root in roots:
            if root == PACKAGE or root in stdlib or root == "__future__":
                continue
            if root in OPTIONAL and _guarded_by_import_error(node, parents):
                continue
            pytest.fail("%s:%d imports non-stdlib %r" % (path.name, node.lineno, root))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_parses_as_python_3_9(path):
    ast.parse(
        path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9)
    )
