# docket 概览

`docket` 是一个 local-first 的项目管理 CLI。这个仓库只放工具代码、
测试和可发布的说明文档；真实 PM 数据放在另一个本地数据仓里。

核心边界是“工具”和“数据”分开：

| 区域 | 放在哪里 | 用途 |
|---|---|---|
| 工具代码 | 本仓库 | CLI、TUI、校验、渲染、测试 |
| PM 数据 | 独立本地数据仓 | issue、评论、项目文件和审计历史 |
| issue artifact | PM 数据仓同级的 `<DOCKET_ROOT>-artifacts/<ID>/` | 长 handoff、RDP bundle、证据等大载荷；每个 issue 一个独立 Git repo |
| 本机诊断 | 用户数据目录 | 命令耗时、退出码和少量本地错误样本 |

## 数据模型

issue 是带 frontmatter 的 Markdown 文件。frontmatter 是结构化契约，正文是
给人看的上下文。

```md
---
domain: pm
id: ISSUE-42
title: "改写发布说明"
status: Todo
state_type: unstarted
priority: High
project: release
parent: ~
labels: []
---

## Objective

发布一份已经审过的发布说明。
```

评论单独放在 `comments/ISSUE-42.md`，项目说明放在 `projects/<key>.md`。
每次写操作都会在 PM 数据仓里生成一个只包含目标文件的 Git commit。

大载荷 artifact 不写进 PM 数据仓。`docket artifact init <id>` 会在 PM 数据仓
同级目录 `<DOCKET_ROOT>-artifacts/<ID>/` 创建独立 Git repo；`path`、`show`、
`list` 和 `sync` 用来查看路径、状态、列表，并把直接编辑的 artifact 文件提交到
artifact 自己的历史。PM issue 仍是工作状态真相源，artifact repo 只承载长交接、
需求 bundle 和验证证据。

## 两个入口

`docket` 是 agent 和脚本入口，命令显式、输出稳定，适合自动化。

`pm` 是人用入口。裸 `pm` 打开 TUI，`pm <project>` 进入项目视图，
`pm <id>` 打印 issue 文件路径。

两个入口共用同一套命令实现和同一份数据模型，差别只在交互方式。

## Root 选择

CLI 按这个顺序选择 PM 数据仓：

1. 如果设置了 `DOCKET_ROOT` 且目录存在，就使用它。
2. 否则从当前目录向上找最近的 Git 仓。

自动化场景建议显式设置：

```sh
export DOCKET_ROOT=/path/to/pm-data
```

## 安全边界

这个工具仓可以在审查后公开发布。PM 数据仓不应该跟着发布。真实任务、
评论、运行输出、业务上下文和本机路径都应该留在 PM 数据仓、外置 artifact repo
或被 Git 忽略的本地文件里。

发布到任何第三方远端前，先按 [发布安全](release-safety.md) 跑当前文件面
和历史面的门禁。当前文件面通过不代表历史提交已经安全。
