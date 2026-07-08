# 控制面模型

`docket` 是本地工作状态的控制面。它不取代代码、文档或外部系统的实现真相；
它只回答：有什么工作、谁在进行、什么被阻塞、什么证据支持关闭。

## 对象

| 对象 | 文件形态 | 角色 |
|---|---|---|
| Issue | `issues/ISSUE-42.md` | 一条工作单元 |
| Comment | `comments/ISSUE-42.md` | 按时间推进的讨论和证据 |
| Project | `projects/<key>.md` | 工作流和显示前缀元数据 |

issue 身份是 identity v3 三层模型：

| 层 | 字段/引用 | 角色 |
|---|---|---|
| 机器主键 | `uid: dkt_<32 hex>` | 永不改变，registry/artifact/自动化优先保存 |
| 人类引用 | `project_iid` + project prefix，例如 `WORK-12` | 项目内递增，给人沟通、commit/PR/worktree 命名 |
| 存储兼容 | `id` / 文件名，例如 `ISSUE-286`、`ERI-905` | legacy storage id，继续用于 issue/comment 文件和旧历史 |

`aliases` 保存历史引用。旧 display ref、legacy id、迁移前后的引用都不能复用给别的
issue。所有 CLI `<id>` 入口都应视作 `<ref>`，通过 resolver 归一：

```sh
docket resolve WORK-12 --json
```

resolver 的查找顺序是当前 PM root 优先；当前 root 查不到时，扫描
`~/.config/docket/tiers.toml` 中的 configured tiers。单 tier 命中返回该 issue，并在 JSON
里带 `root` / `tier`；多 tier 命中必须报 ambiguous。跨工具集成只消费这个 resolver，不再
自行推断 `WORK-12` 这种人类引用属于哪个 PM 根。

裸数字只在不歧义时可用；如果多个项目都存在 `project_iid: 12`，系统必须报 ambiguous，
不能猜。

## 状态

结构化状态拆成两层：

| 字段 | 含义 |
|---|---|
| `status` | 给人看的显示状态 |
| `state_type` | 逻辑使用的归一生命周期类别 |

关闭类工作使用 `completed` 或 `canceled`。除非命令显式要求包含关闭项，
读面会隐藏或降权这些工作。

## 工作面

不同命令只是同一份数据的不同切片：

| 工作面 | 回答的问题 |
|---|---|
| `overview` | 当前在飞什么、每个项目下一步是什么 |
| `active` | 现在可见的 open 工作有哪些 |
| `ready` | 哪些 Todo 没有 open blocker |
| `batch` | 当前滚动批次里有什么 |
| `groom` | 哪些 open issue 停滞太久 |
| `health --json` | agent 汇报前应该看哪些结构性信号 |

这些工作面只是文件上的 advisory view，不替代文件本身的真相地位。

## 隐藏或延后工作

两个可选字段用来减少工作面噪音，但不丢记录。

`wake` 把 open issue 睡到某天。日期到达后，issue 会重新出现在待关注面里。

`triage: true` 把 agent 自发提出的事项放进入口闸。它仍是普通 Markdown
issue，但在 human 接受前不会进入 active 工作面。未受理的 triage 项会在固定
TTL 后读时自愈退出 inbox。

## 依赖边

`blocked_by` 是单向列表，表示当前 issue 等待哪些 issue：

```yaml
blocked_by: [ISSUE-17, ISSUE-23]
```

反向依赖在读取时计算。缺失的 blocker 会保持 loud：系统把它视为未关闭，
避免因为加载不到依赖而把工作静默放行。

## 写入契约

每个写命令遵循同一套形状：

1. 读取目标文件。
2. 只修改请求涉及的字段或评论文本。
3. 原子替换该文件。
4. 只把该文件提交进 PM 数据仓历史。

这样既保留了审计历史，也避免把不相关的脏 worktree 混进同一个 commit。

## 证据契约

issue 只有在正文或评论说明“改了什么”和“凭什么算完成”后才应该关闭。
工具可以提示 stale work、孤儿 commit 引用和结构校验问题，但不能替人判断
证据是否在语义上足够。这个判断仍然需要人或 agent 审阅。

## Worktree 关闭闸

如果一个 issue 名下还有 registrar 登记的 active worktree，关闭 issue 会被阻断。
`finish`、`status completed|canceled` 和 `set --status Done|Canceled` 在写入前会调用：

```sh
registrar worktree reconcile <id> --format json
```

实际调用会传 uid、storage id、当前 display ref 和 aliases；因此 issue 从
`ERI-908` 迁移到 `WORK-12` 后，仍能找到挂在旧 owner_ref 或新 owner_uid 下的 worktree。
阻断输出会列出每个 worktree 的状态和下一步动作。正常处理顺序是先合并或删除这些
worktree，再重新关闭 issue；`DOCKET_WORKTREE_CLOSE_GATE=0` 只作为紧急逃生口。
