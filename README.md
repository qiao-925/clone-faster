# CloneFaster

> 一条命令，批量高速克隆 GitHub 仓库。

## 用法

```bash
curl -fsSL https://raw.githubusercontent.com/qiao-925/CloneX/main/clone_faster.py | python3 -
```

零参数、零安装、零配置。CloneFaster 自动完成五件事：

1. **检测 git**——缺失则提示安装地址
2. **检测 gh CLI**——缺失则按 OS（macOS / Debian / Windows）输出精确安装命令
3. **检测登录态**——未登录则自动唤起浏览器执行 `gh auth login --web`
4. **拉取仓库列表**——从 GitHub API 分页拉取当前用户/组织的全部仓库（含私有仓库）
5. **并行克隆**——`ThreadPoolExecutor` 并行（默认 10 仓 × 20 git 连接），`--depth 1 --single-branch` 浅克隆，单行进度条原地刷新

```
Cloning  [████████████░░░░░░░░░░░░░░░░░░░░]  12/31  ✓ 10  ✗ 2
```

## 选项

```
--output DIR          输出目录（默认 ./clone-faster-repos）
--tasks N             并行仓库数（默认 10）
--connections N       每个克隆的 Git 并行连接（默认 20）
```

默认值对绝大多数场景已经够用。

## 设计

CloneFaster 是一个**零外部依赖的单文件工具**（394 行，纯 Python 标准库）。

| 环节 | 做法 | 原因 |
|---|---|---|
| 认证 | `gh auth token` | gh CLI 是 GitHub 用户的标配，不重复造 OAuth 轮子 |
| 网络 | `urllib.request` | 零外部 Python 依赖 |
| 克隆 | `--depth 1 --single-branch` | 只拉最新快照，速度优先 |
| 去重 | `git fsck --strict` | 已存在且完整的仓库直接跳过 |
| 协议 | SSH → HTTPS 自动回退 | 有 SSH 用免密 SSH，没有就走 token 认证 HTTPS |
| 并行 | `ThreadPoolExecutor` | git clone 是子进程阻塞调用，线程池正合适 |
| 进度 | 自适应终端宽度单行刷新 | 非 TTY 自动静默，TTY 下原地绘制动画条 |
| 分发 | `curl \| python3 -` | 零摩擦，不需要任何包管理器 |

## 宣传页

[`index.html`](index.html) — 深色主题单页，可直接在浏览器打开或部署到 GitHub Pages。

## 本地调试

```bash
git clone https://github.com/qiao-925/CloneX
cd CloneX
uv venv && source .venv/bin/activate && pip install pytest
python clone_faster.py --help     # 三种方式等价：
cat clone_faster.py | python - --help  # 模拟 curl
pytest tests/ -v                  # 测试
```

## 演化

| 时间 | 阶段 |
|---|---|
| 2025-11 | 🐚 Shell 脚本 — 784 行 Bash，REPO-GROUPS.md 分组契约 |
| 2025-12 | 🐍 Python CLI — 迁移到 Python |
| 2026-02 | 🎨 PyQt5 GUI — 删掉 CLI |
| 2026-03~04 | 🏷 CloneX 品牌化 — PyQt6 + Gist 云配置 + CI/CD + PyPI |
| 2026-04 | 🤖 MCP — 14 工具暴露给 AI Agent，CLI 复活 |
| 2026-06 | ⚡ clone-faster — 砍掉 GUI/MCP/Gist/分组/所有外部依赖，curl 单文件 |

## 许可

MIT
