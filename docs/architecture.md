# 架构

本文描述 release-safety 清理后的当前实现。它是给开发者看的地图，不是
本地 PM 数据或历史证据的转储。

## 鸟瞰

`docket` 把本地 PM 数据仓里的 Markdown issue、评论和项目文件组织成一套
可被人和 agent 共用的工作状态控制面。普通读写路径仍以 Markdown 文件为
唯一真相源。

## 模块地图

| 模块 | 职责 |
|---|---|
| `src/docket/cli.py` | Typer 命令注册、`docket`/`pm` 入口、telemetry 包装 |
| `src/docket/commands.py` | list、batch、new、set、comment、validate、health、groom 等命令实现 |
| `src/docket/artifact.py` | issue-owned artifact repo 的 init/path/show/list/sync |
| `src/docket/issue.py` | issue 解析、frontmatter 往返、id 归一化、root 查找 |
| `src/docket/fsops.py` | 不依赖 PM 模型的文件系统原语，例如原子写文件 |
| `src/docket/projects.py` | 项目加载、overview、项目钻取、tree 视图 |
| `src/docket/gitops.py` | 窄 Git auto-commit、issue history、sync |
| `src/docket/states.py` | 状态和优先级归一化 |
| `src/docket/render.py` | 表格布局、颜色、截断、状态样式 |
| `src/docket/ui.py` | 给人浏览用的只读 Textual TUI |
| `src/docket/telemetry.py` | 本地 SQLite 命令诊断账本 |

## 关键路径

普通 CLI 路径：

1. `main_docket` 或 `main_pm` 处理入口差异。
2. `run_wrapped` 初始化颜色、stdout/stderr 采样和本地 telemetry。
3. Typer 解析命令并调用对应 `cmd_*`。
4. 命令通过 `issue.py` 和 `projects.py` 读取 PM 数据。
5. 写命令改一个文件，并调用 `gitops.auto_commit` 记录窄提交。
6. 读命令和校验命令输出稳定文本，供人和 agent 消费。

artifact 路径不同：`docket artifact init <id>` 从当前 PM root 派生同级目录
`<DOCKET_ROOT>-artifacts/<ID>/`，并在该 issue 目录内初始化独立 Git repo。
`docket sync` 会先收编 PM 数据仓里的直接写入，再同步 dirty artifact repo，但
artifact payload 不进入 PM 数据仓提交。

TUI 路径不同：`docket ui` 和裸 `pm` 会在 stdout/stderr 被包装之前直接启动
Textual，因为全屏终端 UI 必须占用真实终端。

## 核心不变量

| 不变量 | 破坏后果 |
|---|---|
| PM 数据仓仍是唯一真相源 | 普通读面和其它数据源会互相污染 |
| artifact payload 位于 PM 数据仓同级目录，不在 PM Git repo 内 | 大文件、交接包或证据会污染 PM 历史和发布安全边界 |
| 每个 artifact 目录都是独立 Git repo | 长交接和证据缺少独立审计历史，`artifact sync` 无法窄提交 |
| `Issue.render()` 尽量保持未改字段和正文的原样往返 | 手工编辑和 CLI 写入会产生无意义 diff |
| 写操作只 stage 当前目标文件 | 并发写或脏 worktree 会被误收进同一 commit |
| TUI 启动不能经过 stdout/stderr tee | 终端 UI 的控制序列会被采样包装破坏 |
| telemetry 只写本机 SQLite，不写网络 | 发布仓不能依赖外部私有诊断服务 |
| 关单前必须完成 worktree reconcile | 已登记 worktree 不会因 issue 误关而失去收口路径 |

## 改什么去哪

| 我想改 / 加 | 从这里入手 | 备注 |
|---|---|---|
| 加一个 CLI 命令 | `src/docket/cli.py` + `src/docket/commands.py` | 命令注册和实现分开 |
| 改 issue artifact 行为 | `src/docket/artifact.py` | 保持 `<DOCKET_ROOT>-artifacts/<ID>/` 外置路径和 per-issue Git repo |
| 改 issue frontmatter 解析或 id 规则 | `src/docket/issue.py` | 注意 round-trip 和 mixed prefix 行为 |
| 改写操作的 Git 行为 | `src/docket/gitops.py` | 不要把窄 pathspec 改成全仓 add |
| 改 overview / project / tree | `src/docket/projects.py` | 需要同步项目加载和展示语义 |
| 改状态或优先级映射 | `src/docket/states.py` | 测试里有大量别名和大小写覆盖 |
| 改 TUI | `src/docket/ui.py` | 保持只读浏览面，不在 TUI 偷写数据 |
| 改本地命令诊断 | `src/docket/telemetry.py` | ledger 可能含输出样本，必须留在本机 |
| 加测试 fixture | `tests/` | 只能使用合成数据，不放真实 PM 内容 |

## 本地 telemetry

telemetry ledger 是本机 SQLite 文件。它记录命令耗时、退出码、字节数和少量
stdout/stderr 样本，用来诊断 CLI 行为。它不写网络。

| 控制项 | 效果 |
|---|---|
| `DOCKET_TELEMETRY_OFF=1` | 关闭记录 |
| `DO_NOT_TRACK=1` | 关闭记录 |
| `DOCKET_TELEMETRY_DB=/path/to/file.db` | 指定本地 ledger 路径 |

ledger 可能包含命令输出样本，必须留在工具仓之外，并继续被 Git 忽略。

## Worktree close gate

进入 Done 或 Canceled 前，`worktree_gate.py` 调用 registrar 的 owner reconcile。
默认给该受控子进程 30 秒预算，可用
`DOCKET_WORKTREE_CLOSE_GATE_TIMEOUT_SECONDS` 在 1–120 秒内覆盖。超时预算只保护
调用方；它不改变 reconcile 的阻断语义，也不能替代清理 worktree。

## CI 和本地检查

当前 GitHub Actions 只跑普通公开 action：

```sh
uvx ruff@0.15.16 check .
uv run pytest
```

发布前还必须额外跑 repo release gate 的当前文件面和历史面扫描。

## 非目标

本仓不承载真实 PM 数据，不承载公司业务样例，不承载本机运行输出，也不负责
把旧 Git 历史自动清干净。历史清洗或 clean export 是单独 gate。
