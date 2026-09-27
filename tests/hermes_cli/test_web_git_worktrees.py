"""Regression coverage for merge status in the desktop worktree listing."""

import subprocess

from hermes_cli import web_git


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_worktree_list_marks_branches_merged_into_default_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.test")
    (repo / "README").write_text("root\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "root")
    root = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "feature/merged")
    (repo / "merged.txt").write_text("merged\n")
    _git(repo, "add", "merged.txt")
    _git(repo, "commit", "-m", "merged change")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--ff-only", "feature/merged")
    _git(repo, "branch", "feature/unmerged", root)

    merged_path = tmp_path / "merged"
    unmerged_path = tmp_path / "unmerged"
    _git(repo, "worktree", "add", str(merged_path), "feature/merged")
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "feature/unmerged-worktree",
        str(unmerged_path),
        "feature/unmerged",
    )
    (unmerged_path / "pending.txt").write_text("pending\n")
    _git(unmerged_path, "add", "pending.txt")
    _git(unmerged_path, "commit", "-m", "unmerged change")

    trees = web_git.worktree_list(str(repo))
    by_branch = {tree["branch"]: tree for tree in trees}

    assert by_branch["feature/merged"]["merged"] is True
    assert by_branch["feature/unmerged-worktree"]["merged"] is False
    assert by_branch["main"]["merged"] is False


def test_worktree_list_ignores_missing_configured_default_and_uses_present_trunk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "init.defaultBranch", "missing-trunk")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.test")
    (repo / "README").write_text("root\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "root")
    _git(repo, "switch", "-c", "feature/merged")
    (repo / "merged.txt").write_text("merged\n")
    _git(repo, "add", "merged.txt")
    _git(repo, "commit", "-m", "merged")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--ff-only", "feature/merged")
    merged_path = tmp_path / "merged"
    _git(repo, "worktree", "add", str(merged_path), "feature/merged")

    trees = web_git.worktree_list(str(repo))
    assert next(tree for tree in trees if tree["branch"] == "feature/merged")["merged"] is True


def test_worktree_list_marks_nothing_merged_when_no_default_resolves(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "topic")
    _git(repo, "config", "init.defaultBranch", "missing-trunk")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.test")
    (repo / "README").write_text("root\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "root")
    _git(repo, "branch", "feature/branch")
    _git(repo, "worktree", "add", str(tmp_path / "feature"), "feature/branch")

    assert all(tree["merged"] is False for tree in web_git.worktree_list(str(repo)))
