"""CloneFaster — 批量克隆 GitHub 账号/组织下的全部仓库（零依赖，单文件）。

用法:
    curl -fsSL https://raw.githubusercontent.com/qiao-925/clone-faster/main/clone_faster.py | python3 -

前置条件: Python >= 3.10, git, gh CLI (gh auth login)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------

IS_TTY = sys.stderr.isatty()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


# ---------------------------------------------------------------------------
# 多行进度条
# ---------------------------------------------------------------------------

_tasks: List[dict] = []
_status_lock = threading.Lock()
_phase_done = threading.Event()
_lines_drawn = 0
_total_done = 0
_total_fail = 0


def _render_all() -> None:
    """总进度条 + 最多 9 个活跃仓库的详细进度。"""
    global _lines_drawn
    if not IS_TTY:
        return

    with _status_lock:
        seen: dict = {}
        for t in _tasks:
            seen[t["repo_full"]] = t
        active = [(v["pct"], v["repo_full"]) for v in seen.values() if v.get("status") != "done"]
        active.sort(key=lambda x: -x[0])
        active = active[:9]  # 最多 9 行（留 1 行给总进度）
        total, done, fail = len(_tasks), _total_done, _total_fail

    if _lines_drawn:
        sys.stderr.write(f"\033[{_lines_drawn}A")

    tw = _term_width()
    items = active
    max_name = max((len(rf) for _, rf in items), default=20)
    written = 0

    # ---- 第一行：总进度 ----
    total_bar_w = tw - 24
    total_bar_w = max(10, min(total_bar_w, 60))
    total_done_pct = int(done / total * 100) if total else 0
    total_filled = int(done / total * total_bar_w) if total else 0
    if done < total:
        arrow = ">" if total_filled < total_bar_w else ""
        total_bar = "=" * total_filled + arrow + " " * max(0, total_bar_w - total_filled - len(arrow))
    else:
        total_bar = "=" * total_bar_w

    tline = f"[{total_bar}] {total_done_pct:3d}% | {done}/{total} done"
    if fail:
        tline += f", {fail} failed"
    sys.stderr.write(f"\033[K{tline[: tw - 1]}\n")
    written += 1

    # ---- 后续行：每个活跃仓库 ----
    for pct, rf in items:
        bar_w = tw - 14 - max_name
        bar_w = max(10, min(bar_w, 50))
        filled = int(pct / 100 * bar_w) if pct else 0

        arrow = ">" if filled < bar_w else ""
        bar = "=" * filled + arrow + " " * max(0, bar_w - filled - len(arrow))
        line = f"  [{bar}] {pct:3d}% | {rf.ljust(max_name)[:max_name]}"
        sys.stderr.write(f"\033[K{line[: tw - 1]}\n")
        written += 1

    sys.stderr.flush()
    _lines_drawn = written


def _render_loop() -> None:
    while not _phase_done.is_set():
        _render_all()
        time.sleep(0.1)


def _parse_git_pct(line: str) -> Optional[int]:
    m = re.search(r"(\d+)%", line)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def _get_token() -> Tuple[Optional[str], str]:
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return None, "未找到 gh CLI。请安装: https://cli.github.com/"
    except Exception:
        return None, "无法运行 gh CLI"

    if r.returncode != 0:
        return None, "gh CLI 未登录。请运行 gh auth login 后重试"

    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), ""
    except Exception:
        pass
    return None, "无法获取 GitHub token"


# ---------------------------------------------------------------------------
# 路径安全化
# ---------------------------------------------------------------------------

_PATH_TRANS = str.maketrans({
    "/": "_", "\\": "_", ":": "_", "*": "_", "?": "_", '"': "_", "<": "_", ">": "_", "|": "_",
})


def _safe_name(name: str) -> str:
    cleaned = (name or "").strip().translate(_PATH_TRANS)
    cleaned = re.sub(r"_+", "_", cleaned).strip(" _.")
    return cleaned or "unnamed"


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

USER_AGENT = "CloneFaster"


def _api_get(url: str, token: str, timeout: int = 10) -> Tuple[bool, object, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        msgs = {401: "Token 无效", 403: "API 频率限制", 404: "未找到"}
        return False, None, msgs.get(e.code, f"HTTP {e.code}")
    except Exception as e:
        return False, None, f"无法连接 GitHub: {e}"


def _fetch_all_pages(url_template: str, token: str) -> Tuple[bool, List[dict], str]:
    repos: List[dict] = []
    page = 1
    while True:
        ok, data, err = _api_get(url_template.format(page=page), token=token)
        if not ok:
            return False, [], err
        if not isinstance(data, list) or not data:
            break
        for repo in data:
            if not isinstance(repo, dict):
                continue
            owner_login = (repo.get("owner") or {}).get("login", "") if isinstance(repo.get("owner"), dict) else ""
            repos.append({
                "name": repo.get("name") or "",
                "full_name": repo.get("full_name") or f"{owner_login}/{repo.get('name') or ''}",
                "private": bool(repo.get("private", False)),
                "owner_login": owner_login,
                "size": repo.get("size", 0),
            })
        if len(data) < 100:
            break
        page += 1
    return True, repos, ""


def _fetch_user_repos(owner: str, token: str) -> Tuple[bool, List[dict], str]:
    url = f"https://api.github.com/users/{owner}/repos?per_page=100&page={{page}}"
    ok, public_repos, err = _fetch_all_pages(url, token)
    if not ok:
        return False, [], err

    url2 = ("https://api.github.com/user/repos"
            "?visibility=all&affiliation=owner,collaborator,organization_member"
            "&per_page=100&page={page}")
    ok2, all_repos, _ = _fetch_all_pages(url2, token)
    if ok2:
        owner_key = owner.strip().casefold()
        merged: Dict[str, dict] = {str(r["name"]): r for r in public_repos if r["name"]}
        for r in all_repos:
            if str(r.get("owner_login", "")).casefold() == owner_key and r["name"]:
                merged[str(r["name"])] = r
        return True, list(merged.values()), ""
    return True, public_repos, ""


def _resolve_owner(token: str) -> Tuple[Optional[str], str]:
    ok, data, err = _api_get("https://api.github.com/user", token=token)
    if ok and isinstance(data, dict):
        login = str(data.get("login") or "")
        if login:
            return login, ""
    return None, f"无法获取登录用户: {err}"


# ---------------------------------------------------------------------------
# git 操作
# ---------------------------------------------------------------------------

_shutdown = threading.Event()
_sig_set = False


def _ensure_signals():
    global _sig_set
    if not _sig_set:
        signal.signal(signal.SIGINT,  lambda s, f: _shutdown.set())
        signal.signal(signal.SIGTERM, lambda s, f: _shutdown.set())
        _sig_set = True


def _git_auth_env(token: str) -> Dict[str, str]:
    env = os.environ.copy()
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
    env["GIT_CONFIG_KEY_1"] = "url.https://github.com/.insteadOf"
    env["GIT_CONFIG_VALUE_1"] = "git@github.com:"
    return env


def _ssh_ok() -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", "-T", "git@github.com"],
            capture_output=True, text=True, timeout=3,
        )
        return "successfully authenticated" in (r.stdout + r.stderr)
    except Exception:
        return False


def _check_repo(repo_path: Path) -> Tuple[bool, str]:
    if not (repo_path / ".git").exists():
        return False, "不是 Git 仓库"
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "fsck", "--no-progress", "--strict"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True, ""
        non_dangling = [l for l in (r.stderr or "").splitlines() if l.strip() and "dangling" not in l]
        return not bool(non_dangling), non_dangling[0][:200] if non_dangling else ""
    except subprocess.TimeoutExpired:
        return False, "检查超时"
    except Exception as e:
        return False, str(e)


def _clone_one(task: dict, token: str, use_ssh: bool, partial: bool) -> Tuple[bool, str]:
    rf, rn, ld = task["repo_full"], task["repo_name"], task["local_dir"]

    if _shutdown.is_set():
        return False, "已取消"

    target = Path(ld) / _safe_name(rn)

    if target.exists() and (target / ".git").exists():
        ok, err = _check_repo(target)
        if ok:
            try:
                subprocess.run(
                    ["git", "-C", str(target), "fetch", "--depth", "1", "origin"],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass
            with _status_lock:
                task["pct"] = 100
                task["status"] = "done"
            return True, "已有"
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        shutil.rmtree(target, ignore_errors=True)

    Path(ld).mkdir(parents=True, exist_ok=True)

    candidates: List[Tuple[str, Optional[Dict[str, str]]]] = []
    if use_ssh:
        candidates.append((f"git@github.com:{rf}.git", None))
    candidates.append((f"https://github.com/{rf}.git", _git_auth_env(token)))

    clone_args = ["git", "clone", "--depth", "1", "--single-branch", "--progress"]
    if partial:
        clone_args.append("--filter=blob:none")

    max_retries = 3
    for attempt in range(max_retries):
        if _shutdown.is_set():
            return False, "已取消"

        # 重置进度，避免上次失败残留
        task["pct"] = 0

        for url, env in candidates:
            if _shutdown.is_set():
                return False, "已取消"

            try:
                p = subprocess.Popen(
                    clone_args + [url, str(target)],
                    env=env, stderr=subprocess.PIPE, text=True, bufsize=1,
                )
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                continue

            def _read_stderr():
                try:
                    for line in p.stderr:
                        pct = _parse_git_pct(line)
                        if pct is not None:
                            with _status_lock:
                                task["pct"] = pct
                except Exception:
                    pass

            reader = threading.Thread(target=_read_stderr, daemon=True)
            reader.start()
            p.wait()
            reader.join(timeout=2)

            if p.returncode == 0:
                with _status_lock:
                    task["pct"] = 100
                    task["status"] = "done"
                return True, "已克隆"
            shutil.rmtree(target, ignore_errors=True)

        if attempt < max_retries - 1:
            time.sleep(1 << attempt)  # 1s, 2s

    with _status_lock:
        task["pct"] = 100
        task["status"] = "done"
    return False, "克隆失败"


# ---------------------------------------------------------------------------
# 并行调度
# ---------------------------------------------------------------------------

def _parallel_clone(
    tasks: List[dict], parallel_tasks: int, token: str, use_ssh: bool, partial: bool,
) -> Tuple[int, int, List[Tuple[str, str, str]]]:
    total = len(tasks)
    if not total:
        return 0, 0, []

    global _tasks, _lines_drawn, _phase_done, _total_done, _total_fail
    _tasks = tasks
    _lines_drawn = 0
    _total_done = 0
    _total_fail = 0
    _phase_done.clear()

    render_t = threading.Thread(target=_render_loop, daemon=True)
    render_t.start()

    ok = fail = 0
    failed: List[Tuple[str, str, str]] = []

    with ThreadPoolExecutor(max_workers=parallel_tasks) as pool:
        futures = {
            pool.submit(_clone_one, t, token, use_ssh, partial): t
            for t in tasks
        }
        for f in as_completed(futures):
            if _shutdown.is_set():
                break
            t = futures[f]
            try:
                success, detail = f.result()
            except Exception:
                success, detail = False, "异常"
            if success:
                ok += 1
                with _status_lock:
                    _total_done += 1
            else:
                fail += 1
                with _status_lock:
                    _total_fail += 1
                    _total_done += 1
                failed.append((t["repo_full"], t["repo_name"], detail))

    _phase_done.set()
    render_t.join(timeout=2)
    if IS_TTY and _lines_drawn:
        sys.stderr.write(f"\033[{_lines_drawn}A\033[J")
        sys.stderr.flush()
        _lines_drawn = 0

    return ok, fail, failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clone-faster",
        description="CloneFaster — 批量高速克隆 GitHub 账号/组织下的全部仓库",
    )
    p.add_argument("--output", default="clone-faster-repos", help="输出目录（默认 ./clone-faster-repos）")
    p.add_argument("--tasks", type=int, default=10, help="并行仓库数（默认 10）")
    p.add_argument("--partial", action="store_true", help="部分克隆（--filter=blob:none，按需下载 blob）")
    p.add_argument("--flat", action="store_true", help="平铺到输出目录（默认按 owner 分组）")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    _ensure_signals()

    token, err = _get_token()
    if not token:
        _log(f"✗ {err}")
        return 1

    owner, err = _resolve_owner(token)
    if not owner:
        _log(f"✗ {err}")
        return 1

    use_ssh = _ssh_ok()

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    _log(f"{owner} -> {output_dir}")

    ok, repos, err = _fetch_user_repos(owner, token)
    if not ok or not repos:
        _log(f"✗ 拉取失败: {err or '无公开仓库'}")
        return 1

    repos.sort(key=lambda r: r.get("size", 0))

    tasks = []
    for r in repos:
        local_dir = str(output_dir) if args.flat else str(output_dir / _safe_name(r.get("owner_login", owner)))
        tasks.append({
            "repo_full": r["full_name"], "repo_name": r["name"],
            "local_dir": local_dir, "pct": 0, "status": "",
        })

    ok_cnt, fail_cnt, failed = _parallel_clone(tasks, args.tasks, token, use_ssh, args.partial)

    if failed:
        print(f"\n✗ {fail_cnt} 个失败:")
        for rf, _rn, detail in failed:
            print(f"  {rf}  ({detail})")

    print(f"\n✓ {ok_cnt}  ✗ {fail_cnt}  /  {len(tasks)} 个仓库")
    return 0 if fail_cnt == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
