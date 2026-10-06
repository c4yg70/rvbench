# ADR 索引

本仓 ADR 现状（2026-10-07 对账）。**本仓只收与 evald 服务端直接相关的 8 份**；工作区
`autoke/rv-k3-kernel-opti/docs/adr/` 另有 0001-0010、0012-0015（战役/论文侧决策），除下表
标注"双宿主"的以外**都不在本仓**，引用时须回工作区查。

| # | 标题 | 状态 | 日期 | 备注 |
|---|---|---|---|---|
| [0001](0001-eval-job-deploy-restore.md) | 评估作业自包含部署-恢复 | 生效 | 09-07 | |
| [0002](0002-shared-timing-mutex.md) | 评估服务器沿用 `/tmp/k3_bench.lock` | 生效（已修订） | 09-07 | 按分区互斥见 0014；文末有修订注记 |
| [0003](0003-stdlib-python-eval-server.md) | 评估服务器用 stdlib Python 而非框架/Rust | 生效 | 09-07 | |
| [0004](0004-warmfork-invariant-preload.md) | 预热执行器只预热不变量 | 生效 | 09-09 | ⚠ 与工作区 `0004-full-230-panel-*` 撞号（同号不同决策，有意不合并） |
| [0014](0014-dual-timing-lanes-a100.md) | 双计时车道：分核并行 ＋ 锁分区 ＋ 路径互斥 | 生效（K3 生产已启用） | 09-09 | 自工作区双宿主入仓 |
| [0016](0016-backend-comparability-registry.md) | 多评测后端可比性：registry 键升格与 failover 同 host 纪律 | 生效（客户端侧） | 10-07 | ADR-0012（工作区）的多后端推广；B3 |
| [0017](0017-result-v2-unified-schema.md) | 统一结果 schema result-v2（指标可 null） | 生效（客户端侧） | 10-07 | B2；读侧双 schema 兼容 |
| [0018](0018-suite-v1-ticket-expansion.md) | 评测集 suite-v1：loader 展开为 ticket 复用全链路 | 生效（客户端侧） | 10-07 | A1+A2；suite 不进服务端 |

**编号空缺**：0005-0013 未入本仓；**0011 在工作区也不存在**；0015 只在工作区（results-consolidation），
本仓 0016 起跳号避开双_corpus 撞号。文档/代码里出现的 `ADR-0012`
引用（`0014`、`clients/eval_pipeline.py`、`ops/precompile.py`）指向工作区那侧，本仓无副本。

**本仓 ADR 未采用 Status/Date 头字段**：状态与日期以上表为准（对账时依据正文与生产事实判定）。

**尚未立 ADR 的既定决策**（已做但未记录，其"为什么"目前散在 README／CHANGELOG／spec）：
四分区布局与 2026-09-20 收拢（含 bin/ 垫片退场）、字节冻结／部署单元不变量（spec §3 P1）。
