"""CloneFaster — 批量高速克隆 GitHub 仓库（零依赖，单文件）。

用法:
    curl -fsSL https://raw.githubusercontent.com/qiao-925/CloneX/main/clone_faster.py | python3 -

    gh CLI 未安装或未登录时，CloneFaster 自动引导。

前置条件: Python >= 3.10, git, gh CLI
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 终端输出
# ---------------------------------------------------------------------------

IS_TTY = sys.stderr.isatty()
_HAS_UNICODE = sys.stdout.encoding and "utf" in sys.stdout.encoding.lower()

C_GREEN  = "\033[32m"
C_RED    = "\033[31m"
C_YELLOW = "\033[33m"
C_CYAN   = "\033[36m"
C_RESET  = "\033[0m"

def _c(c: str, msg: str) -> str:
    if os.name == "nt":
        return msg
    return f"{c}{msg}{C_RESET}"


def _log(msg: str, color: str = "") -> None:
    print(_c(color, msg), file=sys.stderr)


# ---------------------------------------------------------------------------
# 进度条（单行原地刷新）
# ---------------------------------------------------------------------------

BAR_FILLED = "█" if _HAS_UNICODE else "="
BAR_EMPTY  = "░" if _HAS_UNICODE else "-"


def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _draw_bar(done: int, total: int, ok: int, fail: int) -> None:
    """绘制并刷新单行进度条: Cloning  [████░░░░]  5/10  ✓ 4  ✗ 1"""
    if not IS_TTY:
        return

    tw = _term_width()
    # 右侧: 计数部分（先拼出来量长度，ANSI 码不计宽度）
    counts = f" {done}/{total}  {_c(C_GREEN, '✓')} {ok}  {_c(C_RED, '✗')} {fail}"
    counts_clean = f" {done}/{total}  ✓ {ok}  ✗ {fail}"  # 无 ANSI 版本用于测量
    prefix = "Cloning  ["
    suffix = f"]{counts}"

    bar_w = tw - len(prefix) - len(counts_clean) - 1  # -1 for "]"
    bar_w = max(8, min(bar_w, 60))  # 8~60 之间

    filled = int(done / total * bar_w) if total else 0
    bar = BAR_FILLED * filled + BAR_EMPTY * (bar_w - filled)

    line = f"{prefix}{bar}]{counts}"
    if len(line.replace(C_GREEN, "").replace(C_RED, "").replace(C_RESET, "")) > tw:
        line = line[: tw - 1]
    sys.stderr.write(f"\r\033[K{line}")
    sys.stderr.flush()


def _finish_bar(done: int, total: int, ok: int, fail: int) -> None:
    """进度条最终态，换行结束。"""
    if not IS_TTY:
        return
    _draw_bar(done, total, ok, fail)
    sys.stderr.write("\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 工具链自检 & 自动安装
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS   = platform.system() == "Darwin"
IS_LINUX   = platform.system() == "Linux"


def _check_git() -> bool:
    return shutil.which("git") is not None


def _check_gh() -> bool:
    return shutil.which("gh") is not None


def _gh_install_hint() -> str:
    if IS_MACOS:
        return "  brew install gh"
    if IS_LINUX:
        return (
            "  # Debian/Ubuntu:\n"
            "  (type -p wget >/dev/null || sudo apt-get install wget -y) && \\\n"
            "  sudo mkdir -p -m 755 /etc/apt/keyrings && \\\n"
            "  out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg && \\\n"
            "  cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \\\n"
            "  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \\\n"
            "  echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg]"
            " https://cli.github.com/packages stable main\" |"
            " sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \\\n"
            "  sudo apt-get update && sudo apt-get install gh -y"
        )
    return "  winget install --id GitHub.cli"


def _ensure_toolchain() -> Tuple[Optional[str], str]:
    """检查 git + gh CLI，若缺失则引导安装。返回 (token, error)。"""
    if not _check_git():
        return None, "未找到 git。请先安装: https://git-scm.com/downloads"

    if not _check_gh():
        hint = _gh_install_hint()
        _log(f"未找到 GitHub CLI。请安装后运行 gh auth login:\n{hint}", C_RED)
        return None, "gh CLI 未安装"

    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
    except Exception:
        r = None

    if not r or r.returncode != 0:
        _log("gh CLI 未登录。正在启动浏览器登录...", C_CYAN)
        try:
            result = subprocess.run(
                ["gh", "auth", "login", "--hostname", "github.com", "--web"], timeout=120
            )
        except subprocess.TimeoutExpired:
            return None, "登录超时，请手动运行 gh auth login 后重试"
        except FileNotFoundError:
            return None, "gh CLI 未安装"
        if result.returncode != 0:
            return None, "登录失败，请手动运行 gh auth login 后重试"

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
# GitHub API（纯 urllib）
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


def _clone_repo(
    repo_full: str, repo_name: str, local_dir: str, connections: int, token: str,
) -> Tuple[bool, str]:
    """克隆单个仓库。返回 (成功, 状态描述)。不再自己打印日志。"""
    if _shutdown.is_set():
        return False, "已取消"

    target = Path(local_dir) / _safe_name(repo_name)

    # 已存在且完整 → 跳过
    if target.exists() and (target / ".git").exists():
        ok, err = _check_repo(target)
        if ok:
            return True, "已有"
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        shutil.rmtree(target, ignore_errors=True)

    Path(local_dir).mkdir(parents=True, exist_ok=True)

    use_ssh = _ssh_ok()
    candidates: List[Tuple[str, Optional[Dict[str, str]]]] = []
    if use_ssh:
        candidates.append((f"git@github.com:{repo_full}.git", None))
    candidates.append((f"https://github.com/{repo_full}.git", _git_auth_env(token)))

    for url, env in candidates:
        if _shutdown.is_set():
            return False, "已取消"
        try:
            r = subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 "--jobs", str(connections), url, str(target)],
                env=env, capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(target, ignore_errors=True)
            continue
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            continue

        if r.returncode == 0:
            return True, "已克隆"
        shutil.rmtree(target, ignore_errors=True)

    return False, "克隆失败"


# ---------------------------------------------------------------------------
# 并行克隆 + 进度条
# ---------------------------------------------------------------------------

def _parallel_clone(
    tasks: List[dict], parallel_tasks: int, connections: int, token: str,
) -> Tuple[int, int, List[Tuple[str, str, str]]]:
    """并行克隆，返回 (成功数, 失败数, [(repo_full, repo_name, 状态)])。"""
    total = len(tasks)
    if not total:
        return 0, 0, []

    results: List[Tuple[str, str, str]] = []
    ok = fail = done = 0

    _draw_bar(0, total, 0, 0)

    with ThreadPoolExecutor(max_workers=parallel_tasks) as pool:
        futures = {
            pool.submit(_clone_repo, t["repo_full"], t["repo_name"], t["local_dir"], connections, token): t
            for t in tasks
        }
        for f in as_completed(futures):
            if _shutdown.is_set():
                break
            t = futures[f]
            rf, rn = t["repo_full"], t["repo_name"]
            try:
                success, detail = f.result()
            except Exception:
                success, detail = False, "异常"
            done += 1
            if success:
                ok += 1
            else:
                fail += 1
                results.append((rf, rn, detail))
            _draw_bar(done, total, ok, fail)

    _finish_bar(done, total, ok, fail)
    return ok, fail, results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clone-faster",
        description="CloneFaster — 批量高速克隆 GitHub 仓库",
    )
    p.add_argument("--output", default="clone-faster-repos", help="输出目录（默认 ./clone-faster-repos）")
    p.add_argument("--tasks", type=int, default=10, help="并行仓库数（默认 10）")
    p.add_argument("--connections", type=int, default=20, help="Git 并行连接数（默认 20）")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    _ensure_signals()

    # 1. 自检 & 认证
    token, err = _ensure_toolchain()
    if not token:
        _log(f"环境检查失败: {err}", C_RED)
        return 1

    # 2. 获取 owner
    owner, err = _resolve_owner(token)
    if not owner:
        _log(err, C_RED)
        return 1
    _log(f"用户: {owner}", C_CYAN)

    # 3. 拉取仓库列表
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    _log("拉取仓库列表...", C_CYAN)
    ok, repos, err = _fetch_user_repos(owner, token)
    if not ok or not repos:
        _log(f"拉取失败: {err or '无公开仓库'}", C_RED)
        return 1

    tasks = [{"repo_full": r["full_name"], "repo_name": r["name"], "local_dir": str(output_dir)} for r in repos]
    _log(f"{len(tasks)} 个仓库 → {output_dir}", C_CYAN)

    # 4. 并行克隆
    ok_cnt, fail_cnt, failed = _parallel_clone(tasks, args.tasks, args.connections, token)

    # 5. 失败详情
    if failed:
        print(f"\n{C_RED}失败 {fail_cnt} 个:{C_RESET}")
        for rf, _rn, detail in failed:
            print(f"  ✗ {rf}  ({detail})")

    print(f"\n合计: {len(tasks)} 个仓库  ✓ {ok_cnt}  ✗ {fail_cnt}")
    return 0 if fail_cnt == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
