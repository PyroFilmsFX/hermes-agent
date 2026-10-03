"""Tests for opt-in CI checks rollup, rate-limit, and backoff in review_pr_list."""

import json
import time

import pytest

from hermes_cli import web_git


@pytest.fixture(autouse=True)
def reset_conductors_state():
    web_git._reset_conductors_gh_state_for_testing()
    yield
    web_git._reset_conductors_gh_state_for_testing()


def test_pr_query_with_checks_off_matches_flag_absent():
    branches = ["feature/branch-1", "feature/branch-2"]
    numbers = [42, 99]

    baseline = web_git._pr_query("testowner", "testrepo", branches, numbers)
    with_false = web_git._pr_query("testowner", "testrepo", branches, numbers, with_checks=False)

    assert with_false == baseline
    assert "statusCheckRollup" not in baseline
    assert "rateLimit" not in baseline


def test_pr_query_with_checks_on_adds_rollup_to_branches_only_and_rate_limit():
    branches = ["feature/branch-1"]
    numbers = [101]

    query = web_git._pr_query("testowner", "testrepo", branches, numbers, with_checks=True)

    assert "commits(last:1){nodes{commit{statusCheckRollup{state}}}}" in query
    assert "rateLimit{remaining resetAt cost}" in query

    # Branch query must include statusCheckRollup
    assert 'headRefName: "feature/branch-1"' in query

    # Number query must NOT include statusCheckRollup
    lines = query.splitlines()
    number_line = next(line for line in lines if "pullRequest(number: 101)" in line)
    assert "statusCheckRollup" not in number_line


def test_review_pr_list_parses_checks_state_and_rate_limit(monkeypatch, tmp_path):
    repo_dir = str(tmp_path)
    recorded_calls = []

    def mock_gh(cwd, args):
        recorded_calls.append(args)
        assert isinstance(args, list)
        if args[:2] == ["repo", "view"]:
            return True, "testowner/testrepo\n"
        return False, ""

    def mock_gh_json(cwd, args):
        recorded_calls.append(args)
        assert isinstance(args, list)
        if args[:2] == ["api", "graphql"]:
            return {
                "data": {
                    "rateLimit": {
                        "remaining": 4500,
                        "resetAt": "2026-09-29T12:00:00Z",
                        "cost": 1,
                    },
                    "repository": {
                        "b0": {
                            "nodes": [
                                {
                                    "number": 42,
                                    "headRefName": "feature/branch-1",
                                    "isDraft": False,
                                    "isCrossRepository": False,
                                    "state": "OPEN",
                                    "title": "PR 42",
                                    "url": "https://github.com/testowner/testrepo/pull/42",
                                    "commits": {
                                        "nodes": [
                                            {
                                                "commit": {
                                                    "statusCheckRollup": {
                                                        "state": "SUCCESS"
                                                    }
                                                }
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    },
                }
            }
        return None

    monkeypatch.setattr(web_git, "_gh", mock_gh)
    monkeypatch.setattr(web_git, "_gh_json", mock_gh_json)

    # 1. With with_checks=True
    res_on = web_git.review_pr_list(repo_dir, ["feature/branch-1"], with_checks=True)
    assert res_on["ghReady"] is True
    assert len(res_on["prs"]) == 1
    assert res_on["prs"][0]["checks_state"] == "SUCCESS"
    assert res_on["rate_limit"] == {
        "remaining": 4500,
        "resetAt": "2026-09-29T12:00:00Z",
        "cost": 1,
    }

    # 2. With with_checks=False (sidebar pattern: byte-identical shape without checks_state / rate_limit)
    res_off = web_git.review_pr_list(repo_dir, ["feature/branch-1"], with_checks=False)
    assert res_off["ghReady"] is True
    assert len(res_off["prs"]) == 1
    assert "checks_state" not in res_off["prs"][0]
    assert "rate_limit" not in res_off


def test_rate_limit_suspension_when_remaining_under_200(monkeypatch, tmp_path):
    repo_dir = str(tmp_path)
    graphql_calls = []

    def mock_gh(cwd, args):
        if args[:2] == ["repo", "view"]:
            return True, "testowner/testrepo\n"
        return False, ""

    def mock_gh_json(cwd, args):
        graphql_calls.append(args)
        return {
            "data": {
                "rateLimit": {
                    "remaining": 150,
                    "resetAt": "2026-09-29T12:00:00Z",
                    "cost": 1,
                },
                "repository": {},
            }
        }

    monkeypatch.setattr(web_git, "_gh", mock_gh)
    monkeypatch.setattr(web_git, "_gh_json", mock_gh_json)
    monkeypatch.setattr(time, "time", lambda: 1790670000.0)  # Before resetAt

    # First call records rate_limit remaining 150 < 200
    first = web_git.review_pr_list(repo_dir, ["feature/branch-1"], with_checks=True)
    assert first["ghReady"] is True
    assert first["rate_limit"]["remaining"] == 150
    assert len(graphql_calls) == 1

    # Second call before resetAt is suspended: does NOT call gh
    second = web_git.review_pr_list(repo_dir, ["feature/branch-1"], with_checks=True)
    assert second["suspended"] is True
    assert second["error"] == "rate_limited"
    assert len(graphql_calls) == 1  # No new call made!


def test_per_repo_failure_backoff_sequence(monkeypatch, tmp_path):
    repo_dir = str(tmp_path)
    graphql_calls = []
    current_time = 1000.0

    def mock_time():
        return current_time

    def mock_gh(cwd, args):
        if args[:2] == ["repo", "view"]:
            return True, "testowner/testrepo\n"
        return False, ""

    should_fail = True

    def mock_gh_json(cwd, args):
        graphql_calls.append(args)
        if should_fail:
            return None
        return {"data": {"repository": {}}}

    monkeypatch.setattr(web_git, "_gh", mock_gh)
    monkeypatch.setattr(web_git, "_gh_json", mock_gh_json)
    monkeypatch.setattr(time, "time", mock_time)

    # Failure 1: backoff 1 min (60s)
    web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert len(graphql_calls) == 1

    # At 30s: still backed off
    current_time += 30.0
    res = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert res.get("error") == "backoff"
    assert len(graphql_calls) == 1

    # At 61s: 1 min backoff expired, call 2 allowed, fails -> Failure 2: 2 min backoff
    current_time += 31.0
    web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert len(graphql_calls) == 2

    # At 60s into failure 2: still backed off
    current_time += 60.0
    res = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert res.get("error") == "backoff"
    assert len(graphql_calls) == 2

    # At 121s: 2 min backoff expired, call 3 allowed, fails -> Failure 3: 5 min backoff
    current_time += 61.0
    web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert len(graphql_calls) == 3

    # At 180s into failure 3: still backed off
    current_time += 180.0
    res = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert res.get("error") == "backoff"
    assert len(graphql_calls) == 3

    # At 301s: 5 min backoff expired, call 4 allowed, fails -> Failure 4: 15 min backoff
    current_time += 121.0
    web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert len(graphql_calls) == 4

    # At 600s into failure 4: still backed off
    current_time += 600.0
    res = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert res.get("error") == "backoff"
    assert len(graphql_calls) == 4

    # At 901s: 15 min backoff expired, call 5 succeeds -> resets backoff!
    current_time += 301.0
    should_fail = False
    success_res = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert success_res["ghReady"] is True
    assert len(graphql_calls) == 5

    # Next immediate call is allowed (not backed off)
    web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert len(graphql_calls) == 6


def test_gh_missing_or_unauthenticated_returns_gh_unavailable(monkeypatch, tmp_path):
    repo_dir = str(tmp_path)

    def mock_gh(cwd, args):
        # gh missing or not authenticated
        return False, ""

    monkeypatch.setattr(web_git, "_gh", mock_gh)

    # When with_checks=True
    res_on = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=True)
    assert res_on["ghReady"] is False
    assert res_on["error"] == "gh_unavailable"
    assert res_on["gh_unavailable"] is True

    # When with_checks=False
    res_off = web_git.review_pr_list(repo_dir, ["feature/a"], with_checks=False)
    assert res_off["ghReady"] is False
    assert res_off["prs"] == []
    assert "error" not in res_off


def test_argv_arrays_only(monkeypatch, tmp_path):
    repo_dir = str(tmp_path)
    recorded_argvs = []

    def mock_gh(cwd, args):
        recorded_argvs.append(args)
        if args[:2] == ["repo", "view"]:
            return True, "owner/repo\n"
        return False, ""

    def mock_gh_json(cwd, args):
        recorded_argvs.append(args)
        return {"data": {"repository": {}}}

    monkeypatch.setattr(web_git, "_gh", mock_gh)
    monkeypatch.setattr(web_git, "_gh_json", mock_gh_json)

    web_git.review_pr_list(repo_dir, ["main"], with_checks=True)

    assert len(recorded_argvs) >= 2
    for argv in recorded_argvs:
        assert isinstance(argv, list), "Must be an argv array"
        assert all(isinstance(arg, str) for arg in argv)
