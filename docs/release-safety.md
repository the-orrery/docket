# 发布安全

这个仓库可以准备进入 GitHub public 仓。public 发布规则必须严格：
工具代码可以在审查后发布；公司数据、真实 PM 数据、本机运行输出和机器
特定路径不能发布。

## 边界

| 内容 | 发布态度 |
|---|---|
| `src/`、`tests/`、包元数据 | 扫描通过后可以发布 |
| 合成文档 | 扫描通过后可以发布 |
| PM 数据仓 | 不随工具仓发布 |
| 本地 telemetry ledger | 不发布 |
| 私有评测语料、运行输出、export bundle | 除非改写成合成 fixture，否则不发布 |
| 旧 Git 历史 | 单独 gate；不能假设干净 |

## 必跑门禁

至少跑两类扫描：

```sh
repo-release-gate scan --repo .
repo-release-gate scan --repo . --history
```

第一条检查当前文件面。第二条检查历史提交里是否还存在高风险词或高风险形态。
当前文件面通过，不代表历史提交安全。

如果本机有私有敏感词词典，通过 scanner 的配置机制传入。该词典应留在本地，
不要提交进任何可能被导出的仓库。

## 推荐发布形态

对本仓来说，较稳妥的路径是：

1. 清理期间源仓只留在本机。
2. 删除或改写当前文件面的 blocker。
3. 历史 blocker 作为单独决策处理。
4. 如果旧历史仍有风险，优先创建 clean export 仓。
5. 只有在 current-tree 和 history gate 都被明确接受后，才更新第三方远端。

这样不把全历史改写当作默认动作。历史改写可能仍然需要做，但它应该是单独
授权的高风险操作。

## 发布前 checklist

| 检查项 | 预期结果 |
|---|---|
| tracked files | 没有 PM 数据、运行输出、私有评测语料或生成 bundle |
| docs | 只包含合成示例 |
| tests 和 fixtures | 只使用合成数据 |
| dependencies | 没有本地路径依赖或私有 Git 依赖 |
| CI | 不继承私有 workflow 上下文 |
| ignore 规则 | 本地数据库、环境文件、缓存和私有评测目录被忽略 |
| current scan | blocker=0 且 confirm=0 |
| history scan | 要么干净，要么有明确 gate 和 clean-export 方案 |

扫描输出是证据，不是证明。没有进入扫描规则的业务事实仍然需要人工审查。
