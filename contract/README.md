# rvbench control-plane contracts v1.0.0

2026-10-17 实现类型扩展：JobRequest 可选 implementation=native-c/triton，与 K1/K3
硬件独立。省略时 K1 使用 native-c，K3 使用 triton，保持既有请求摘要与行为。
能力探测、准入与调度约定见 ../worker/contract/backend.md。

2026-10-17 观测扩展：两端 HTTP contract 版本为 1.1.0，保留 1.0.0 wire 注册兼容。
新增指标、分页结果、排行榜和固定历史查询，见 [observability.md](observability.md)
与 [metrics.json](metrics.json)。字段仍从本目录 OpenAPI 生成。

用户已于 2026-10-17 批准实施。先建立本目录与两端 contract，再生成 wire 类型、运行 conformance 测试，最后实现。

人工权威：

- `openapi.json`：共享 JSON Schema / wire 模型。
- `../worker/contract/openapi.yaml`：worker API（JSON 语法的合法 YAML）。
- `../manager/contract/openapi.yaml`：manager API（JSON 语法的合法 YAML）。
- `invariants.md` 和两端 `state-machine.md`：一致性与状态转换。
- `../worker/contract/backend.md`：compiler / evaluator 语义。
- `examples/`：正反例。现有 suites / baseline / eval-results 继续以 `../schemas/` 为权威。

`openapi-to-rust` 0.15.0 从共享 OpenAPI 生成 `generated/types.rs`；生成物禁止手改，不新增平行 wire DTO。
使用 `openapi-to-rust generate -c contract/codegen.toml --check` 检查新鲜度。
`rvbench-protocol` 是共享类型、JSON Schema 校验、canonical JSON / SHA-256 和事务存储的消费入口。

接口无认证、无认证配置、无静态准入名单，允许任意 worker 注册。
仍严格校验请求、能力、容量、idempotency、job / attempt / instance / lease / epoch 归属。
worker 独立完成编译和评估；manager 统一提交和调度，Push / Pull 使用同一原子分配路径。
`func` 是 compiler 验证；evaluator 只有 correctness、performance。

变更顺序始终为 contract → 生成 → conformance → 实现 → 实际验证。详见 `../docs/manager-worker.md`。
