# docket

`docket` 是一个 local-first 的项目管理 CLI。它把 issue、评论、项目、状态和历史都存
在一个普通 Git 仓里，数据是 Markdown + frontmatter，可以用文本工具直接查看、审阅
和迁移。

这个仓只放工具代码；PM 数据建议放在另一个独立的数据仓里。

## 适合什么场景

- 想用 Git 管理本地 issue，而不是依赖云端 SaaS。
- 希望 agent、脚本和人共用同一套任务状态。
- 希望每次任务状态变更都有 Git 历史。
- 希望 issue 本体是可读、可 diff、可手工修复的 Markdown 文件。

## 两个入口

| 入口 | 用途 |
|---|---|
| `docket` | 面向 agent、脚本和自动化；命令显式、输出稳定。 |
| `pm` | 面向人；提供 overview、项目钻取、文件路径和只读 TUI。 |

## 核心能力

- Markdown issue + frontmatter。
- 评论单独存为 Markdown。
- 项目总览、批次视图、进行中列表、issue 树。
- 状态流转和数据校验。
- `blocked_by` 依赖边。
- `wake` 快照(把被外部卡住的 issue 睡到某天,临时移出活跃视图)。
- `triage` 入口闸(agent 自发提议的 issue 先落待审态,principal `accept`/`decline` 才进工作面;14 天 TTL 读时自愈)。
- 写操作自动生成 Git commit，保留本地审计历史。
- 本地 telemetry，用来排查 CLI 性能和错误率。
- 终端自动上色，管道输出保持纯文本。

## 安装

```sh
uv tool install --force .
```

本地开发：

```sh
uv sync
uv run docket --help
uv run pytest
```

## 数据仓

`docket` 需要一个带 `issues/` 目录的 PM 数据仓。推荐通过 `DOCKET_ROOT` 显式指定：

```sh
export DOCKET_ROOT=/path/to/pm-data
docket overview
```

工具仓和数据仓分开，是为了让工具迭代、任务状态和协作历史各自清楚。

## 常用命令

| 命令 | 作用 |
|---|---|
| `docket overview` | 查看进行中事项、本批事项和项目进度(`--no-projects` 省略末尾项目段)。 |
| `docket list [--status X] [--project Y] [--triage]` | 表格列出 issue;`--triage` 只列待审项(含已过 TTL 的,审计/查看入口)。 |
| `docket active [--all]` | 查看活跃事项。 |
| `docket project <key>` | 查看单个项目。 |
| `docket projects [--all]` | 列出所有项目，按当前活跃度(进行中+待办数)降序；`--all` 含已归档。 |
| `docket projects new <key> --title T --prefix P` | 注册新项目(写 `projects/<key>.md`,body 含默认 plan 骨架:目标 / 为什么现在成一束 / 范围·边界 / done 口径)。 |
| `docket tree <id>` | 查看 issue 子树。 |
| `docket show <id>` | 查看 issue 正文和评论。 |
| `docket new "标题" [--type task/bug]` | 新建 issue;省略 `--body` 时按 `--type` 吐 by-construction body 骨架(`task`=SCQA 六轴,默认;`bug`=复现核心),写=填空。传 `--body` 则原样用。 |
| `docket set <id> ...` | 修改 issue 字段(含 `--wake YYYY-MM-DD` 睡到某天 / `--unwake` 清除)。 |
| `docket start <id>` | 标记为进行中。 |
| `docket finish <id>` | 标记为完成。 |
| `docket triage [--gc]` | 列待审 inbox(agent 自发提议、未受理的 issue);`--gc` 把过 TTL 的过期项物化成 canceled。 |
| `docket accept <id> [--backlog]` | 受理待审项 → Todo(`--backlog` 落 Backlog)。 |
| `docket decline <id> [理由]` | 拒绝待审项 → Canceled,并自动留一条 `declined from triage: <理由>` 评论。 |
| `docket comment <id> "内容"` | 追加评论。 |
| `docket artifact init <id> --template handoff\|requirement` | 在当前 PM 仓的 sibling 目录 `<DOCKET_ROOT>-artifacts/<ID>/` 创建该 issue 的独立 artifact Git repo；payload 不进入 PM 仓目录。 |
| `docket artifact path/show/list/sync` | 查看 artifact repo 路径/状态/列表，并把直接编辑的 artifact repo 脏改提交到它自己的 Git 历史。 |
| `docket validate` | 校验所有 issue。 |
| `docket health [--project Y] [--json]` | agent-facing 工作健康信号。`--json` 输出稳定 envelope,包含长期工作结构缺口信号(缺当前状态卡、阶段出口、下一步最小动作、实现闸门、split 出口变化),供 agent 生成进度/漂移/总控报告。只读、advisory,不进入 validate。 |
| `docket groom [--project Y] [--today YYYY-MM-DD] [--json]` | 周期性梳理:列出所有非-done issue 的停滞表(状态/停滞天数/项目/父子/讨论数,按停滞排序)+ validate 摘要;`--json` 仍只出停滞记录。表格模式页脚复用 `docket health` 的长期工作信号,并汇总写作健康信号(未填骨架 section 数 + comment 长度分布/超长计数),用于调试/卫生盘点,不是 principal 报告主入口。 |
| `docket orphans [--repo PATH] [--limit N] [--json]` | 『提交了没关单』检测:扫某个**代码仓**最近 N 个 commit(默认 cwd、200 个),抽出 message 里引用的 issue id,和 `$DOCKET_ROOT` 里仍 open 的 issue 交叉比对,报出被提交引用却没关单的(work-closeout Git 闸自动化)。只读,不改状态;`--json` 出结构化记录。 |
| `docket stats` | 查看本地 CLI telemetry 汇总。 |

`<id>` 支持 `286`、`DEMO-286`、`demo-286`、`TEAM-286` 这类形式。数字是唯一锚点，
前缀只用于显示。

`docket new --project <key>` 要求 `<key>` 已注册(用 `docket projects new` 建)；
未注册会直接报错以避免产生孤儿 issue，加 `--new-project` 可在建 issue 时顺手注册。

被外部卡住、暂时推不动的 issue 可以「睡到某天」：`docket set <id> --wake 2026-07-01`。
wake 在未来时，这条 open issue 从 `active` / `overview` 隐藏（它在睡觉，不占注意力）；
wake 到期（≤ 今天）或没设 wake 就正常显示，已睡醒的会在 `active` / `overview` 顶部
汇总成一行「N 个到期待看」提示。`--unwake` 清除。wake 是可选字段，不带它的 issue 一切照旧。
agent、hook 或自动化集成自发提议建的 issue 默认先进**待审态**（可选字段 `triage: true`，底层仍是 `unstarted`，不是第 6 个状态）：它从 `active`/`ready`/`overview`/`groom`/项目进度等全部工作面隐藏，只在 `docket triage` inbox 和 TUI「待审」桶里出现，`docket list` 里带 `📥` 标记以免被误 `start`。principal `docket accept <id>` 揭掉字段放它进 Todo（或 `--backlog`），`docket decline <id> [理由]` → Canceled 并留痕评论。入口闸是 **fail-closed** 的：`docket new` 在 agent 上下文（`--actor` 或环境探测出非 human）默认进闸，principal 点名建用 `--directed` 直进 Todo，`--triage`/`--no-triage` 显式覆盖。未受理的待审项 14 天（TTL）后读时自动视作已弃——退出 inbox、退出 `active`/`overview` 顶部的「⚠ N 条待审」提示行、退出全部工作面（`docket triage --gc` 可选地把过期项物化成 canceled）。`triage` 是可选字段,不带它的 issue 一切照旧。

`pm`/`docket ui` 批次 Tab 左栏末尾的「全部」桶会把所有 open（进行中 + 待办 + 暂存）一把列全，无视 batch，snoozed 的标「💤 睡到 X」；左栏另有「待审」桶列未过 TTL 的 triage 项。项目 Tab 高亮一个项目时，右侧详情面板渲该项目自身的说明（`projects/<key>.md` 的 body），其中 `## 现状` 活状态段被提到最前突出显示，charter / 里程碑 / `## 现状·历史` 跟随其后；再高亮某条 issue 则切回该 issue 的正文。

## Root 解析

CLI 查找 PM 数据仓的顺序：

1. 如果设置了 `$DOCKET_ROOT`，使用它。
2. 否则从当前目录向上找最近的 Git 仓。

多人协作时，建议显式设置 `DOCKET_ROOT`，减少误操作。

## 开发检查

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions 会运行 lint 和 tests。`format --check` 可作为本地加严项在
整理格式后启用。

## 许可证

MIT
