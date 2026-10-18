# 多评测后端可比性口径：registry 键升格与 failover 同 host 纪律

ADR-0012（工作区）为单板双簇确立了"跨簇数字永不直比"（RVV 算子跨簇 3–40% 系统差）；推广到多评测后端（k1/k3/sg2042/rvv_general）后，该口径从约定升格为**代码结构**：`clients/backends.py` 的 `registry_key(cond, op, backend)`——k3 后端保持旧键格式（`KOPE/<op>`、`astra5/r1/E/<op>` 等，424 个既有键零漂移），非 k3 后端一律 `b:<backend>/<cond>/<op>` 前缀，物理上杜绝跨后端共用 incumbent 参照。lane 轴由键值里的 host 标签承载（k3 计时 vs k3-acc 正确性），故键 + host 标签合起来即 (backend, lane, op) 三元组。`comparable()` 只认同 host 标签：failover（一台 k3 换另一台 k3）允许继承参照；**跨后端/跨 host 只许重定基，不许比较**（`judge()` 的 host 变化重定基分支，eval_pipeline.py 原语义保留）。决策只在客户端侧落地（B7）：不动 evald.py/字节冻结，registry 是客户端工件（`runs/E1/.incumbent-registry.json`），服务端无需感知 backend。

被否方案：服务端请求体加 backend 字段（破坏"不带 lane 字段=旧版逐字节兼容"的契约风格，且 Byte-freeze 门禁代价与收益不成比例）；registry 全量迁移到 (backend, lane, op) 显式三元组结构（424 键历史是可比性 longitudinal 数据本身，格式迁移的漂移风险大于收益——前缀方案对旧键零改动）。
