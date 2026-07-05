# docket — for agents

开发 docket。内容不在这里重复,按需去对应文档:

| 要… | 看 |
|---|---|
| 用法 / 安装 / 命令表 | `README.md` |
| 设计概念与取舍(file=issue、id/前缀、auto-commit…) | `docs/overview.md` |
| 改哪个模块 / codemap / 加 verb 的流程 / 核心不变量 | `docs/architecture.md` |
| 某条命令具体怎么用 | `docket <命令> --help` |

下面几条文档里没有、但开发时要守的:

- **文档跟代码同改、且实跑**:改了命令 / flag / 默认 / 可见行为,同一个 PR 里改
  `README.md` 和 `docs/`(`overview.md` 概念、`architecture.md` 不变量),并对临时仓
  (`DOCKET_ROOT=$(mktemp -d)`)实跑每个动过的例子、确认输出仍对。
- **别手动 `git add` / `commit` 数据文件**:docket 每次写自动 commit 一个文件,手
  commit 会破坏它;手改了文件就跑 `docket validate`。
- **提 PR 前 `uv run poe check` 过**(lint + typecheck + test)。
