# CloneFaster

一条命令，批量高速克隆 GitHub 仓库。

```bash
curl -fsSL https://raw.githubusercontent.com/qiao-925/clone-faster/main/clone_faster.py | python3 -
```

## 前置

- Python >= 3.10
- git
- [gh CLI](https://cli.github.com/) 已登录 (`gh auth login`)

## 用法

无参数。自动克隆当前登录用户/组织的全部仓库到 `./clone-faster-repos/`。

```
--output DIR          输出目录（默认 ./clone-faster-repos）
--tasks N             并行仓库数（默认 10）
--connections N       Git 并行连接数（默认 20）
```

## 许可

MIT
