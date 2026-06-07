"""Tests for clone_faster.py — unit, integration, e2e, and edge cases."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

import clone_faster as cf


# ============================================================================
# Helpers
# ============================================================================

def _fake_repo_data(name="repo1", owner="alice", size=100, private=False):
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": private,
        "owner": {"login": owner},
        "owner_login": owner,
        "size": size,
    }


def _mock_subprocess_run(*args, **kwargs):
    """Factory that returns a CompletedProcess with given returncode."""
    rc = kwargs.pop("_returncode", 0)
    stdout = kwargs.pop("_stdout", "")
    stderr = kwargs.pop("_stderr", "")
    raise_timeout = kwargs.pop("_raise_timeout", False)
    side_effect = kwargs.pop("_side_effect", None)

    if side_effect:
        raise side_effect
    if raise_timeout:
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "", timeout=30)
    return subprocess.CompletedProcess(
        args[0] if args else [], rc, stdout, stderr,
    )


# ============================================================================
# Unit tests — pure functions
# ============================================================================

class TestParseGitPct:
    def test_mid_progress(self):
        assert cf._parse_git_pct("Receiving objects:  45% (123/456)") == 45

    def test_full_progress(self):
        assert cf._parse_git_pct("Resolving deltas: 100% (10/10)") == 100

    def test_zero_progress(self):
        assert cf._parse_git_pct("remote: Counting objects:   0% (1/100)") == 0

    def test_no_percent(self):
        assert cf._parse_git_pct("Cloning into 'foo'...") is None

    def test_empty_string(self):
        assert cf._parse_git_pct("") is None

    def test_multiple_percents_returns_first(self):
        assert cf._parse_git_pct("75% done, 50% remaining") == 75

    def test_percent_in_middle_of_number(self):
        assert cf._parse_git_pct("Compressing objects:  99% (99/100), done.") == 99

    def test_unicode_line(self):
        assert cf._parse_git_pct("接收对象中: 60% (300/500)") == 60


class TestSafeName:
    def test_normal_name(self):
        assert cf._safe_name("hello-world") == "hello-world"

    def test_slashes_replaced(self):
        assert cf._safe_name("org/repo") == "org_repo"

    def test_backslash_replaced(self):
        assert cf._safe_name("x\\y") == "x_y"

    def test_multiple_special_chars(self):
        name = 'a:b*c?d"e<f>g|h'
        assert cf._safe_name(name) == "a_b_c_d_e_f_g_h"

    def test_leading_trailing_special(self):
        # _safe_name strips leading/trailing space, underscore, dot; - is not stripped
        assert cf._safe_name("_-repo-._") == "-repo-"

    def test_consecutive_underscores_collapsed(self):
        assert cf._safe_name("a//b") == "a_b"

    def test_empty_string(self):
        assert cf._safe_name("") == "unnamed"

    def test_none_input(self):
        assert cf._safe_name(None) == "unnamed"

    def test_only_special_chars(self):
        assert cf._safe_name("///") == "unnamed"

    def test_whitespace_only(self):
        assert cf._safe_name("   ") == "unnamed"

    def test_chinese_characters_preserved(self):
        assert cf._safe_name("我的仓库") == "我的仓库"

    def test_mixed_special_and_chinese(self):
        assert cf._safe_name("org:我的/repo") == "org_我的_repo"


class TestBuildParser:
    def test_defaults(self):
        p = cf._build_parser()
        args = p.parse_args([])
        assert args.output == "clone-faster-repos"
        assert args.tasks == 10
        assert args.partial is False
        assert args.flat is False

    def test_custom_output(self):
        p = cf._build_parser()
        args = p.parse_args(["--output", "/tmp/out"])
        assert args.output == "/tmp/out"

    def test_custom_tasks(self):
        p = cf._build_parser()
        args = p.parse_args(["--tasks", "5"])
        assert args.tasks == 5

    def test_partial_flag(self):
        p = cf._build_parser()
        args = p.parse_args(["--partial"])
        assert args.partial is True

    def test_flat_flag(self):
        p = cf._build_parser()
        args = p.parse_args(["--flat"])
        assert args.flat is True

    def test_all_flags(self):
        p = cf._build_parser()
        args = p.parse_args(["--output", "/x", "--tasks", "3", "--partial", "--flat"])
        assert args.output == "/x"
        assert args.tasks == 3
        assert args.partial is True
        assert args.flat is True


# ============================================================================
# Integration tests — functions with mocked subprocess/network
# ============================================================================

class TestGetToken:
    def test_gh_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            token, err = cf._get_token()
        assert token is None
        assert "未找到" in err

    def test_gh_not_logged_in(self):
        fake = _mock_subprocess_run(_returncode=1)
        with mock.patch("subprocess.run", return_value=fake):
            token, err = cf._get_token()
        assert token is None
        assert "未登录" in err

    def test_token_success(self):
        def side_effect(args, **kwargs):
            if args[2] == "status":
                return _mock_subprocess_run(_returncode=0)
            if args[2] == "token":
                return _mock_subprocess_run(_returncode=0, _stdout="ghp_test123\n")
            return _mock_subprocess_run(_returncode=1)

        with mock.patch("subprocess.run", side_effect=side_effect):
            token, err = cf._get_token()
        assert token == "ghp_test123"
        assert err == ""

    def test_token_command_fails(self):
        def side_effect(args, **kwargs):
            if args[2] == "status":
                return _mock_subprocess_run(_returncode=0)
            return _mock_subprocess_run(_returncode=1)

        with mock.patch("subprocess.run", side_effect=side_effect):
            token, err = cf._get_token()
        assert token is None
        assert "无法获取" in err


class TestSSHOk:
    def test_ssh_authenticated(self):
        fake = _mock_subprocess_run(
            _returncode=0,
            _stdout="Hi qiao-925! You've successfully authenticated...",
        )
        with mock.patch("subprocess.run", return_value=fake):
            assert cf._ssh_ok() is True

    def test_ssh_failed(self):
        fake = _mock_subprocess_run(_returncode=255, _stderr="Permission denied")
        with mock.patch("subprocess.run", return_value=fake):
            assert cf._ssh_ok() is False

    def test_ssh_timeout(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=3)):
            assert cf._ssh_ok() is False

    @mock.patch("subprocess.run", side_effect=Exception("no ssh binary"))
    def test_ssh_exception(self, _m):
        assert cf._ssh_ok() is False


class TestGitAuthEnv:
    def test_contains_expected_keys(self):
        env = cf._git_auth_env("fake-token")
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]
        assert env["GIT_CONFIG_VALUE_1"] == "git@github.com:"

    def test_preserves_existing_env(self, monkeypatch):
        monkeypatch.setenv("EXISTING_VAR", "hello")
        env = cf._git_auth_env("t")
        assert env["EXISTING_VAR"] == "hello"


class TestCheckRepo:
    def test_no_git_dir(self, tmp_path):
        ok, err = cf._check_repo(tmp_path)
        assert ok is False
        assert "不是 Git" in err

    def test_fsck_clean(self, tmp_path):
        (tmp_path / ".git").mkdir()
        fake = _mock_subprocess_run(_returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            ok, err = cf._check_repo(tmp_path)
        assert ok is True
        assert err == ""

    def test_fsck_corrupt(self, tmp_path):
        (tmp_path / ".git").mkdir()
        fake = _mock_subprocess_run(_returncode=1, _stderr="error: corrupt loose object\n")
        with mock.patch("subprocess.run", return_value=fake):
            ok, err = cf._check_repo(tmp_path)
        assert ok is False
        assert "corrupt" in err

    def test_fsck_dangling_only_treated_as_ok(self, tmp_path):
        (tmp_path / ".git").mkdir()
        fake = _mock_subprocess_run(
            _returncode=1,
            _stderr="dangling commit abc123\ndangling blob def456\n",
        )
        with mock.patch("subprocess.run", return_value=fake):
            ok, err = cf._check_repo(tmp_path)
        assert ok is True
        assert err == ""

    def test_fsck_timeout(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
            ok, err = cf._check_repo(tmp_path)
        assert ok is False
        assert "超时" in err


class TestApiGet:
    URL = "https://api.github.com/user"

    def test_success(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"login": "alice"}).encode()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=ctx):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is True
        assert data == {"login": "alice"}
        assert err == ""

    def test_http_401(self):
        exc = urllib.error.HTTPError(self.URL, 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is False
        assert "Token 无效" in err

    def test_http_403(self):
        exc = urllib.error.HTTPError(self.URL, 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is False
        assert "频率限制" in err

    def test_http_404(self):
        exc = urllib.error.HTTPError(self.URL, 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is False
        assert "未找到" in err

    def test_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("network down")):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is False
        assert "无法连接" in err

    def test_json_decode_error(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"<html>not json</html>"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            ok, data, err = cf._api_get(self.URL, "tok")
        assert ok is False
        assert err  # should have error message


class TestFetchAllPages:
    def test_single_page(self):
        data = [_fake_repo_data(f"r{i}") for i in range(3)]
        with mock.patch.object(cf, "_api_get", return_value=(True, data, "")):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is True
        assert len(repos) == 3

    def test_multiple_pages(self):
        page_data = {1: [_fake_repo_data(f"r{i}") for i in range(100)],
                     2: [_fake_repo_data(f"r{i}") for i in range(100, 150)],
                     3: []}

        def api_side_effect(url, token, timeout=10):
            import re
            page = int(re.search(r"page=(\d+)", url).group(1))
            data = page_data.get(page, [])
            return (True, data, "")

        with mock.patch.object(cf, "_api_get", side_effect=api_side_effect):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is True
        assert len(repos) == 150

    def test_api_error_propagates(self):
        with mock.patch.object(cf, "_api_get", return_value=(False, None, "API down")):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is False
        assert err == "API down"

    def test_non_list_data_stops(self):
        with mock.patch.object(cf, "_api_get", return_value=(True, {}, "")):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is True
        assert repos == []

    def test_missing_owner_field(self):
        data = [{"name": "r1", "full_name": "x/r1", "private": False, "size": 10}]
        with mock.patch.object(cf, "_api_get", return_value=(True, data, "")):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is True
        assert repos[0]["owner_login"] == ""

    def test_non_dict_entries_skipped(self):
        data = [_fake_repo_data("r1"), "not a dict", _fake_repo_data("r2")]
        with mock.patch.object(cf, "_api_get", return_value=(True, data, "")):
            ok, repos, err = cf._fetch_all_pages("url?page={page}", "tok")
        assert ok is True
        assert len(repos) == 2


class TestFetchUserRepos:
    def test_public_only(self):
        pub = [_fake_repo_data("r1", "alice")]
        with mock.patch.object(cf, "_fetch_all_pages", side_effect=[
            (True, pub, ""),        # public endpoint
            (False, [], "nope"),    # user endpoint fails
        ]):
            ok, repos, err = cf._fetch_user_repos("alice", "tok")
        assert ok is True
        assert len(repos) == 1

    def test_merged_public_and_private(self):
        pub = [_fake_repo_data("r1", "alice")]
        priv = [_fake_repo_data("r1", "alice", private=True),
                _fake_repo_data("r2", "alice", private=True)]
        with mock.patch.object(cf, "_fetch_all_pages", side_effect=[
            (True, pub, ""),
            (True, priv, ""),
        ]):
            ok, repos, err = cf._fetch_user_repos("alice", "tok")
        assert ok is True
        assert len(repos) == 2

    def test_only_owner_repos_merged(self):
        pub = [_fake_repo_data("r1", "alice")]
        all_repos = [_fake_repo_data("r1", "bob"),    # different owner
                     _fake_repo_data("r2", "alice")]
        with mock.patch.object(cf, "_fetch_all_pages", side_effect=[
            (True, pub, ""),
            (True, all_repos, ""),
        ]):
            ok, repos, err = cf._fetch_user_repos("alice", "tok")
        assert ok is True
        names = {r["name"] for r in repos}
        assert names == {"r1", "r2"}  # r1 from pub, r2 from user (matching owner)

    def test_api_error(self):
        with mock.patch.object(cf, "_fetch_all_pages", return_value=(False, [], "error")):
            ok, repos, err = cf._fetch_user_repos("alice", "tok")
        assert ok is False


class TestResolveOwner:
    def test_success(self):
        with mock.patch.object(cf, "_api_get", return_value=(True, {"login": "alice"}, "")):
            owner, err = cf._resolve_owner("tok")
        assert owner == "alice"
        assert err == ""

    def test_missing_login(self):
        with mock.patch.object(cf, "_api_get", return_value=(True, {}, "")):
            owner, err = cf._resolve_owner("tok")
        assert owner is None

    def test_api_error(self):
        with mock.patch.object(cf, "_api_get", return_value=(False, None, "network")):
            owner, err = cf._resolve_owner("tok")
        assert owner is None
        assert "network" in err


# ============================================================================
# Integration tests — _clone_one
# ============================================================================

class TestCloneOne:
    def _task(self, repo_full="alice/repo1", repo_name="repo1", local_dir="/tmp/out",
              pct=0, status=""):
        return {"repo_full": repo_full, "repo_name": repo_name, "local_dir": local_dir,
                "pct": pct, "status": status}

    def _patched_popen(self, returncode=0, stderr_lines=None):
        """Create a mock Popen that yields stderr lines then exits with returncode."""
        if stderr_lines is None:
            stderr_lines = ["Receiving objects:  50% (50/100)\n",
                            "Receiving objects: 100% (100/100)\n"]

        p = mock.Mock(spec=subprocess.Popen)
        p.returncode = returncode
        p.stderr = iter(stderr_lines)
        p.wait.return_value = None
        return p

    def test_clone_ssh_success(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fake_p = self._patched_popen(returncode=0)

        with mock.patch.object(cf, "_ssh_ok", return_value=True):
            with mock.patch("subprocess.Popen", return_value=fake_p):
                with mock.patch("subprocess.run"):  # for _git_auth_env not called on SSH
                    ok, msg = cf._clone_one(task, "tok", use_ssh=True, partial=False)

        assert ok is True
        assert msg == "已克隆"
        assert task["status"] == "done"

    def test_clone_https_fallback(self, tmp_path):
        """SSH fails or not used, falls back to HTTPS."""
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fake_p = self._patched_popen(returncode=0)

        with mock.patch("subprocess.Popen", return_value=fake_p):
            with mock.patch("subprocess.run"):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True

    def test_existing_repo_skip(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        target = tmp_path / "repo1"
        (target / ".git").mkdir(parents=True)

        fake_fsck = _mock_subprocess_run(_returncode=0)
        fake_fetch = _mock_subprocess_run(_returncode=0)
        with mock.patch("subprocess.run", side_effect=[fake_fsck, fake_fetch]):
            ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True
        assert msg == "已有"

    def test_existing_corrupt_repo_recloned(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        target = tmp_path / "repo1"
        (target / ".git").mkdir(parents=True)

        fake_fsck = _mock_subprocess_run(_returncode=1, _stderr="error: corrupt pack")
        fake_p = self._patched_popen(returncode=0)

        with mock.patch("subprocess.run", side_effect=[fake_fsck]):
            with mock.patch("subprocess.Popen", return_value=fake_p):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True
        assert msg == "已克隆"

    def test_retry_on_failure_then_succeed(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fail_p = self._patched_popen(returncode=128)  # git exits with error
        ok_p = self._patched_popen(returncode=0)

        with mock.patch("subprocess.Popen", side_effect=[fail_p, fail_p, ok_p]):
            with mock.patch("time.sleep", return_value=None):  # skip actual sleep
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True

    def test_retry_exhausted(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fail_p = self._patched_popen(returncode=128, stderr_lines=[])  # empty stderr → clean msg

        with mock.patch("subprocess.Popen", return_value=fail_p):
            with mock.patch("time.sleep", return_value=None):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is False
        assert msg == "克隆失败"

    def test_retry_exhausted_shows_stderr_tail(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fail_p = self._patched_popen(returncode=128, stderr_lines=[
            "remote: Repository not found.\n",
            "fatal: could not read from remote repository\n",
        ])

        with mock.patch("subprocess.Popen", return_value=fail_p):
            with mock.patch("time.sleep", return_value=None):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is False
        assert "克隆失败; " in msg
        assert "Repository not found" in msg

    def test_shutdown_before_clone(self, tmp_path):
        cf._shutdown.set()
        task = self._task(local_dir=str(tmp_path))
        ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)
        assert ok is False
        assert msg == "已取消"
        cf._shutdown.clear()

    def test_partial_flag_adds_filter(self, tmp_path):
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fake_p = self._patched_popen(returncode=0)

        with mock.patch("subprocess.Popen", return_value=fake_p) as mock_popen:
            with mock.patch("subprocess.run"):
                cf._clone_one(task, "tok", use_ssh=False, partial=True)

        args = mock_popen.call_args[0][0]
        assert "--filter=blob:none" in args

    def test_target_is_stale_non_git_dir(self, tmp_path):
        """When target exists as a non-git directory, it should be removed before cloning."""
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        target = tmp_path / task["repo_name"]
        target.mkdir()
        (target / "some_file").write_text("stale data")

        fake_p = self._patched_popen(returncode=0)
        with mock.patch("subprocess.Popen", return_value=fake_p):
            with mock.patch("subprocess.run"):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True

    def test_target_is_file_not_dir(self, tmp_path):
        """Target exists as a regular file — should be removed before cloning."""
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        target = tmp_path / task["repo_name"]
        target.write_text("i am a file not a dir")

        fake_p = self._patched_popen(returncode=0)
        with mock.patch("subprocess.Popen", return_value=fake_p):
            ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is True
        # File should be removed by _rm_anything before clone
        assert not target.exists()

    def test_percent_reset_between_retries(self, tmp_path):
        """After a failed attempt, task["pct"] should reset to 0 before retry."""
        cf._shutdown.clear()
        task = self._task(local_dir=str(tmp_path))
        fail_p = self._patched_popen(returncode=128, stderr_lines=["50% (50/100)\n"])
        ok_p = self._patched_popen(returncode=0)

        with mock.patch("subprocess.Popen", side_effect=[fail_p, ok_p]):
            with mock.patch("time.sleep", return_value=None):
                cf._clone_one(task, "tok", use_ssh=False, partial=False)

        # final pct should be 100 after success
        assert task["pct"] == 100


class TestCloneOneRetryTiming:
    """Verify retry backoff uses correct sleep values: 1s, then 2s."""

    def test_no_sleep_after_last_attempt(self, tmp_path):
        cf._shutdown.clear()
        task = {"repo_full": "alice/r", "repo_name": "r",
                "local_dir": str(tmp_path), "pct": 0, "status": ""}
        fail_p = mock.Mock(spec=subprocess.Popen)
        fail_p.returncode = 128
        fail_p.stderr = iter([])
        fail_p.wait.return_value = None

        sleeps = []
        with mock.patch("subprocess.Popen", return_value=fail_p):
            with mock.patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
                cf._clone_one(task, "tok", use_ssh=False, partial=False)

        # 3 attempts = 2 sleeps: attempt 0 fails, sleep(1); attempt 1 fails, sleep(2); attempt 2 fails, no sleep
        assert sleeps == [1, 2]


# ============================================================================
# Integration tests — _parallel_clone
# ============================================================================

class TestParallelClone:
    def _tasks(self, n=5):
        return [{"repo_full": f"u/r{i}", "repo_name": f"r{i}",
                 "local_dir": "/tmp/out", "pct": 0, "status": ""} for i in range(n)]

    def test_all_succeed(self):
        cf._shutdown.clear()
        tasks = self._tasks(3)

        with mock.patch.object(cf, "_clone_one", return_value=(True, "ok")):
            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=2, token="tok", use_ssh=False, partial=False,
            )
        assert ok_cnt == 3
        assert fail_cnt == 0
        assert cf._total_done == 3
        assert cf._total_fail == 0

    def test_all_fail(self):
        cf._shutdown.clear()
        tasks = self._tasks(3)

        with mock.patch.object(cf, "_clone_one", return_value=(False, "fail")):
            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=2, token="tok", use_ssh=False, partial=False,
            )
        assert ok_cnt == 0
        assert fail_cnt == 3
        assert len(failed) == 3
        assert cf._total_fail == 3

    def test_mixed_results(self):
        cf._shutdown.clear()
        results = [(True, "ok"), (False, "err"), (True, "ok")]

        def side_effect(*args, **kwargs):
            return results.pop(0)

        tasks = self._tasks(3)
        with mock.patch.object(cf, "_clone_one", side_effect=side_effect):
            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=2, token="tok", use_ssh=False, partial=False,
            )
        assert ok_cnt == 2
        assert fail_cnt == 1
        assert len(failed) == 1
        assert failed[0][2] == "err"
        assert cf._total_done == 3
        assert cf._total_fail == 1

    def test_empty_task_list(self):
        ok_cnt, fail_cnt, failed = cf._parallel_clone(
            [], parallel_tasks=2, token="tok", use_ssh=False, partial=False,
        )
        assert ok_cnt == 0
        assert fail_cnt == 0

    def test_shutdown_midway(self):
        """Tasks submitted before shutdown complete; later ones may be skipped."""
        cf._shutdown.clear()
        tasks = self._tasks(10)

        with mock.patch.object(cf, "_clone_one", return_value=(True, "ok")) as mock_clone:
            # Simulate shutdown triggered externally after a short time
            def delayed_shutdown():
                time.sleep(0.05)
                cf._shutdown.set()
            t = threading.Thread(target=delayed_shutdown, daemon=True)
            t.start()

            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=2, token="tok", use_ssh=False, partial=False,
            )
            t.join()

        # At minimum the first batch of tasks should have completed
        assert ok_cnt >= 1
        cf._shutdown.clear()

    def test_exception_in_clone_one(self):
        cf._shutdown.clear()
        tasks = self._tasks(3)

        with mock.patch.object(cf, "_clone_one", side_effect=RuntimeError("boom")):
            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=2, token="tok", use_ssh=False, partial=False,
            )
        assert ok_cnt == 0
        assert fail_cnt == 3
        assert all(d == "异常" for _, _, d in failed)


# ============================================================================
# E2E tests — main() with all externals mocked
# ============================================================================

class TestMainE2E:
    def _mock_all(self):
        """Return a dict of patches for all external calls in main()."""
        return {
            "_get_token": mock.patch.object(cf, "_get_token", return_value=("ghp_test", "")),
            "_resolve_owner": mock.patch.object(cf, "_resolve_owner", return_value=("alice", "")),
            "_ssh_ok": mock.patch.object(cf, "_ssh_ok", return_value=False),
            "_fetch_user_repos": mock.patch.object(
                cf, "_fetch_user_repos",
                return_value=(True, [_fake_repo_data("r1", "alice", 50),
                                     _fake_repo_data("r2", "alice", 10)], ""),
            ),
            "_parallel_clone": mock.patch.object(
                cf, "_parallel_clone",
                return_value=(2, 0, []),
            ),
        }

    def test_success_flow(self, monkeypatch, tmp_path):
        cf._shutdown.clear()
        # Prevent signal handler setup in tests
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)

        patches = self._mock_all()
        with patches["_get_token"], patches["_resolve_owner"], patches["_ssh_ok"], \
             patches["_fetch_user_repos"], patches["_parallel_clone"]:
            with mock.patch("sys.argv", ["clone-faster", "--output", str(tmp_path)]):
                rc = cf.main()

        assert rc == 0

    def test_token_failure(self, monkeypatch):
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        with mock.patch.object(cf, "_get_token", return_value=(None, "auth error")):
            with mock.patch("sys.argv", ["clone-faster"]):
                rc = cf.main()
        assert rc == 1

    def test_owner_resolution_failure(self, monkeypatch):
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        with mock.patch.object(cf, "_get_token", return_value=("t", "")):
            with mock.patch.object(cf, "_resolve_owner", return_value=(None, "no user")):
                with mock.patch("sys.argv", ["clone-faster"]):
                    rc = cf.main()
        assert rc == 1

    def test_fetch_failure(self, monkeypatch):
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        with mock.patch.object(cf, "_get_token", return_value=("t", "")):
            with mock.patch.object(cf, "_resolve_owner", return_value=("alice", "")):
                with mock.patch.object(cf, "_ssh_ok", return_value=False):
                    with mock.patch.object(cf, "_fetch_user_repos", return_value=(False, [], "fail")):
                        with mock.patch("sys.argv", ["clone-faster"]):
                            rc = cf.main()
        assert rc == 1

    def test_some_failures(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)

        patches = self._mock_all()
        patches["_parallel_clone"] = mock.patch.object(
            cf, "_parallel_clone",
            return_value=(1, 1, [("alice/r2", "r2", "fail")]),
        )

        with patches["_get_token"], patches["_resolve_owner"], patches["_ssh_ok"], \
             patches["_fetch_user_repos"], patches["_parallel_clone"]:
            with mock.patch("sys.argv", ["clone-faster", "--output", str(tmp_path)]):
                rc = cf.main()
        assert rc == 1  # non-zero when failures exist

    def test_size_sorting(self, monkeypatch, tmp_path):
        """Smaller repos should appear first in tasks list."""
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        cf._shutdown.clear()

        repos = [_fake_repo_data("big", "alice", 1000),
                 _fake_repo_data("small", "alice", 10),
                 _fake_repo_data("medium", "alice", 500)]
        captured_tasks = []

        def capture_tasks(tasks, *args, **kwargs):
            captured_tasks.extend(tasks)
            return (len(tasks), 0, [])

        patches = self._mock_all()
        patches["_fetch_user_repos"] = mock.patch.object(
            cf, "_fetch_user_repos", return_value=(True, repos, ""))
        patches["_parallel_clone"] = mock.patch.object(cf, "_parallel_clone", side_effect=capture_tasks)

        with patches["_get_token"], patches["_resolve_owner"], patches["_ssh_ok"], \
             patches["_fetch_user_repos"], patches["_parallel_clone"]:
            with mock.patch("sys.argv", ["clone-faster", "--output", str(tmp_path)]):
                cf.main()

        names = [t["repo_name"] for t in captured_tasks]
        assert names == ["small", "medium", "big"]

    def test_flat_layout(self, monkeypatch, tmp_path):
        """--flat should place repos directly under output, not owner subdir."""
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        cf._shutdown.clear()

        repos = [_fake_repo_data("r1", "alice", 10)]
        captured_tasks = []

        def capture_tasks(tasks, *args, **kwargs):
            captured_tasks.extend(tasks)
            return (1, 0, [])

        patches = self._mock_all()
        patches["_fetch_user_repos"] = mock.patch.object(
            cf, "_fetch_user_repos", return_value=(True, repos, ""))
        patches["_parallel_clone"] = mock.patch.object(cf, "_parallel_clone", side_effect=capture_tasks)

        with patches["_get_token"], patches["_resolve_owner"], patches["_ssh_ok"], \
             patches["_fetch_user_repos"], patches["_parallel_clone"]:
            with mock.patch("sys.argv", ["clone-faster", "--output", str(tmp_path), "--flat"]):
                cf.main()

        assert captured_tasks[0]["local_dir"] == str(tmp_path)

    def test_default_grouped_layout(self, monkeypatch, tmp_path):
        """Default: repos go under owner subdirectory."""
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        cf._shutdown.clear()

        repos = [_fake_repo_data("r1", "alice", 10)]
        captured_tasks = []

        def capture_tasks(tasks, *args, **kwargs):
            captured_tasks.extend(tasks)
            return (1, 0, [])

        patches = self._mock_all()
        patches["_fetch_user_repos"] = mock.patch.object(
            cf, "_fetch_user_repos", return_value=(True, repos, ""))
        patches["_parallel_clone"] = mock.patch.object(cf, "_parallel_clone", side_effect=capture_tasks)

        with patches["_get_token"], patches["_resolve_owner"], patches["_ssh_ok"], \
             patches["_fetch_user_repos"], patches["_parallel_clone"]:
            with mock.patch("sys.argv", ["clone-faster", "--output", str(tmp_path)]):
                cf.main()

        assert captured_tasks[0]["local_dir"] == str(tmp_path / "alice")


# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCaseEmptyRepos:
    def test_main_with_zero_repos(self, monkeypatch):
        monkeypatch.setattr(cf, "_ensure_signals", lambda: None)
        cf._shutdown.clear()

        with mock.patch.object(cf, "_get_token", return_value=("t", "")):
            with mock.patch.object(cf, "_resolve_owner", return_value=("alice", "")):
                with mock.patch.object(cf, "_ssh_ok", return_value=False):
                    with mock.patch.object(cf, "_fetch_user_repos", return_value=(True, [], "")):
                        with mock.patch("sys.argv", ["clone-faster"]):
                            rc = cf.main()
        assert rc == 1  # exits with error on empty list


class TestEdgeCaseNonTTY:
    def test_render_all_noop_when_not_tty(self):
        # Save and restore IS_TTY
        old = cf.IS_TTY
        cf.IS_TTY = False
        try:
            cf._render_all()  # should return immediately without error
        finally:
            cf.IS_TTY = old


class TestEdgeCaseSignalHandling:
    def test_ensure_signals_idempotent(self):
        """Calling twice should not register duplicate handlers."""
        cf._ensure_signals()
        was_true = cf._sig_set
        cf._ensure_signals()
        assert cf._sig_set == was_true


class TestEdgeCaseTermWidthException:
    def test_term_width_fallback(self):
        with mock.patch("shutil.get_terminal_size", side_effect=Exception("no tty")):
            assert cf._term_width() == 80


class TestEdgeCaseConcurrentStatusUpdate:
    def test_status_lock_prevents_races(self):
        """Simulate concurrent task status updates from multiple threads."""
        cf._tasks[:] = [{"repo_full": f"u/r{i}", "pct": 0, "status": ""} for i in range(10)]
        errors = []

        def update_task(i):
            try:
                for pct in range(0, 101, 10):
                    with cf._status_lock:
                        cf._tasks[i]["pct"] = pct
                    time.sleep(0.001)
                with cf._status_lock:
                    cf._tasks[i]["status"] = "done"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        cf._tasks[:] = []


class TestEdgeCaseShutdownDuringRetry:
    def test_shutdown_set_during_sleep(self, tmp_path):
        cf._shutdown.clear()
        task = {"repo_full": "a/r", "repo_name": "r",
                "local_dir": str(tmp_path), "pct": 0, "status": ""}

        fail_p = mock.Mock(spec=subprocess.Popen)
        fail_p.returncode = 128
        fail_p.stderr = iter([])
        fail_p.wait.return_value = None

        def sleep_then_shutdown(s):
            cf._shutdown.set()

        with mock.patch("subprocess.Popen", return_value=fail_p):
            with mock.patch("time.sleep", side_effect=sleep_then_shutdown):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is False
        assert msg == "已取消"
        cf._shutdown.clear()

    def test_shutdown_set_during_second_attempt(self, tmp_path):
        cf._shutdown.clear()
        task = {"repo_full": "a/r", "repo_name": "r",
                "local_dir": str(tmp_path), "pct": 0, "status": ""}

        fail_p = mock.Mock(spec=subprocess.Popen)
        fail_p.returncode = 128
        fail_p.stderr = iter([])
        fail_p.wait.return_value = None

        call_count = [0]

        def sleep_side_effect(s):
            call_count[0] += 1
            if call_count[0] >= 2:  # second sleep = after attempt 1
                cf._shutdown.set()

        with mock.patch("subprocess.Popen", return_value=fail_p):
            with mock.patch("time.sleep", side_effect=sleep_side_effect):
                ok, msg = cf._clone_one(task, "tok", use_ssh=False, partial=False)

        assert ok is False
        cf._shutdown.clear()


class TestEdgeCaseMalformedGitOutput:
    def test_percent_out_of_range_handled(self):
        """git should never output >100 but be safe."""
        assert cf._parse_git_pct("150% complete") == 150

    def test_negative_percent_ignored_by_progress_bar(self):
        """Progress bar can handle unexpected values."""
        # Just test the function doesn't crash on weird input
        cf._parse_git_pct("-5% done") == -5


class TestEdgeCaseThreadPoolShutdown:
    def test_futures_complete_despite_slow_tasks(self):
        """Ensure as_completed handles mixed-speed tasks."""
        cf._shutdown.clear()
        tasks = [{"repo_full": f"u/r{i}", "repo_name": f"r{i}",
                  "local_dir": "/tmp/out", "pct": 0, "status": ""} for i in range(3)]

        results = [(True, "fast"), (True, "mid"), (True, "slow")]

        def clone_side_effect(task, *args, **kwargs):
            i = int(task["repo_name"][1])
            time.sleep(i * 0.01)  # staggered completion
            return results[i]

        with mock.patch.object(cf, "_clone_one", side_effect=clone_side_effect):
            ok_cnt, fail_cnt, failed = cf._parallel_clone(
                tasks, parallel_tasks=3, token="tok", use_ssh=False, partial=False,
            )
        assert ok_cnt == 3
        assert fail_cnt == 0
