"""Owner-forward manager proposal tool is a pure, desktop-only proposal surface."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

from tools import owner_forward_propose as propose


REPO = Path(__file__).resolve().parents[2]


def test_returns_proposal_shape_with_catalog_scope_policy(monkeypatch):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})

    result = json.loads(propose.registry.dispatch("owner_forward_propose", {
        "targets": [{"profile": "default", "session_id": "abc12345"}],
        "text": "deploy after review 🟢",
        "scopes": ["conductor:prod:target", "conductor:gate:review-budget-enable"],
        "subject": "release 4.2",
    }))

    assert result["owner_forward_proposal"] == 1
    assert result["status"] == "awaiting_owner"
    assert result["proposal_id"]
    assert result["targets"] == [{"profile": "default", "session_id": "abc12345"}]
    assert result["text_sha256"] == hashlib.sha256("deploy after review 🟢".encode()).hexdigest()
    assert result["text_len"] == len("deploy after review 🟢".encode("utf-8"))
    assert result["scopes"] == [
        {"scope": "conductor:prod:target", "class": "prod", "ttl_ms": 900000, "single_use": True},
        {"scope": "conductor:gate:review-budget-enable", "class": "gate", "ttl_ms": 43200000, "single_use": False},
    ]
    assert result["subject"] == "release 4.2"
    assert propose.RESULT_TEXT in result["message"]


@pytest.mark.parametrize("targets", [[], [{}] * 6])
def test_rejects_target_count_outside_one_to_five(monkeypatch, targets):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    assert "error" in json.loads(propose.owner_forward_propose(targets, "hello"))


@pytest.mark.parametrize("target", [
    {"profile": "../outside", "session_id": "abc12345"},
    {"profile": "default", "session_id": "../outside"},
    {"profile": "default", "session_id": ""},
])
def test_rejects_malformed_profile_or_session_id(monkeypatch, target):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    assert "error" in json.loads(propose.owner_forward_propose([target], "hello"))


def test_rejects_unknown_scope_namespace(monkeypatch):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    result = json.loads(propose.owner_forward_propose(
        [{"profile": "default", "session_id": "abc12345"}], "hello", ["other:prod:target"], "release"))
    assert "error" in result


def test_rejects_prod_scope_without_subject(monkeypatch):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    result = json.loads(propose.owner_forward_propose(
        [{"profile": "default", "session_id": "abc12345"}], "hello", ["conductor:prod:target"]))
    assert "error" in result


@pytest.mark.parametrize("text", ["", "   ", "x" * 8001, "/run this command"])
def test_rejects_invalid_text(monkeypatch, text):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    result = json.loads(propose.owner_forward_propose(
        [{"profile": "default", "session_id": "abc12345"}], text))
    assert "error" in result


def test_tool_module_has_no_delivery_or_stamp_imports():
    path = REPO / "tools/owner_forward_propose.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(name == "tui_gateway.owner_forward" for name in imports)
    assert not any(
        alias.name == "OwnerForwardStamp"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"deliver", "forward_rpc", "sign"}
        for node in ast.walk(tree)
    )


def test_proposal_has_no_filesystem_or_gateway_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": True, "max_chars": 8000})
    gateway_calls = []
    delivery_path = types.ModuleType("tui_gateway.owner_forward")
    delivery_path.deliver = lambda *args, **kwargs: gateway_calls.append((args, kwargs))
    delivery_path.forward_rpc = delivery_path.deliver
    import tui_gateway

    monkeypatch.setitem(sys.modules, "tui_gateway.owner_forward", delivery_path)
    monkeypatch.setattr(tui_gateway, "owner_forward", delivery_path, raising=False)

    def reject_file_access(*args, **kwargs):
        raise AssertionError("proposal tool must not access the filesystem")

    monkeypatch.setattr(builtins, "open", reject_file_access)
    monkeypatch.setattr(os, "open", reject_file_access)
    monkeypatch.setattr(Path, "write_text", reject_file_access)
    monkeypatch.setattr(Path, "write_bytes", reject_file_access)
    before = set(tmp_path.rglob("*"))
    result = json.loads(propose.owner_forward_propose(
        [{"profile": "default", "session_id": "abc12345"}], "hello"))
    assert result["status"] == "awaiting_owner"
    assert set(tmp_path.rglob("*")) == before
    assert gateway_calls == []


def test_tool_exposure_is_desktop_only_and_service_gated(monkeypatch):
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    entry = propose.registry.get_entry("owner_forward_propose")
    assert entry is not None
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is propose.check_owner_forward_available
    assert "owner_forward_propose" in TOOLSETS["desktop_ui"]["tools"]
    assert "owner_forward_propose" not in _HERMES_CORE_TOOLS

    monkeypatch.setattr(propose, "_owner_forward_policy", lambda: {"enabled": False, "max_chars": 8000})
    assert not propose.check_owner_forward_available()
