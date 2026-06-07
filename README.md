# CloneFaster

> 一条命令，批量高速克隆 GitHub 仓库。

```bash
curl -fsSL https://raw.githubusercontent.com/qiao-925/clone-faster/main/clone_faster.py | python3 -
```

## 前置条件

- Python >= 3.10
- git
- [gh CLI](https://cli.github.com/) 已登录: `gh auth login`

## 用法

无参数。自动克隆当前 GitHub 登录用户/组织的全部仓库到 `./clone-faster-repos/`。

```
--output DIR          输出目录（默认 ./clone-faster-repos）
--tasks N             并行仓库数（默认 10）
--connections N       每个克隆的 Git 并行连接（默认 20）
```

## 设计

单文件 ~420 行，零外部 Python 依赖（仅标准库 `urllib` + `subprocess` + `concurrent.futures`）。

```
gh auth status 校验 → gh auth token 拿 token → urllib GET 分页拉仓库列表
→ ThreadPoolExecutor 并行克隆（SSH 优先，HTTPS 回退，已存在则 skip）
```

- 无配置文件，无分组，无 Gist
- `--depth 1 --single-branch` 浅克隆
- 自适应终端宽度的单行进度条

[`index.html`](index.html) — 宣传页，已部署 [GitHub Pages](https://qiao-925.github.io/clone-faster/)。

## 许可

MIT
