# contracts/schemas

suite 定义与结果文档的 JSON Schema 正本。JSON Schema Draft 2020-12。

| 文件 | `$id` | 覆盖的文档 |
|---|---|---|
| `spec.schema.json` | `urn:rvbench:spec:2.1.0` | 公开评测集定义 `spec.json`，接受 `schema_version` 2.0.0 / 2.1.0 |
| `spec.hidden.schema.json` | `urn:rvbench:spec-hidden:1.0.0` | 隐藏用例定义 `spec.hidden.json`，禁止出现算子定义 |
| `eval_results.schema.json` | `urn:rvbench:eval-results:1.1.0` | 评测结果 `eval_results.json`，兼容 1.0.0 |
| `baseline_results.schema.json` | `urn:rvbench:baseline-results:1.0.0` | 历史基准测量 `baseline-results.json` |

Schema 只约束结构与类型；跨字段语义（工作量身份、摘要绑定、状态机）在 `cli/src/` 里校验。两边都过才算合同通过。

## 谁消费它

| 消费方 | 方式 |
|---|---|
| `cli/src/lib.rs` `schema_errors` | `include_str!` 编译期嵌入，按 kind 取用，运行时不读盘 |
| `rvbench-cli validate` | `-- spec` / `hidden` / `results` / `baseline-results` 四种模式 |
| `examples/**/*.json` | 顶层 `$schema` 相对路径指向本目录（深度：`../../contracts/schemas/`） |
| manager 与 worker | 经 `rvbench-protocol` 复用同一套校验器 |

```sh
cargo run -p rvbench-cli --locked -- validate spec examples/hello-world/spec.json
cargo run -p rvbench-cli --locked -- validate results examples/hello-world/eval_results.json --spec examples/hello-world/spec.json
```

## 变更规则

1. 结构变化必须升版本：公开 spec 用 2.1.0 这类次版本，结果格式用独立 `$id`。旧版本定义继续有效，消费者拒绝未知版本。
2. 字段只增不删，除非同时升主版本并同步 `docs/specs/` 的格式说明。
3. 校验器行为由 `cli/tests/parity.rs` 的 110 组回归样本锁定（fixture 在 `cli/tests/fixtures/contracts.json`），改 Schema 后必须重跑。
4. 隐藏用例的 Schema 与公开集合分开，避免把算子定义漏进 `spec.hidden.json`。

## 移动过路径

`2026-10-19` 由仓根 `schemas/` 迁到 `contracts/schemas/`。`examples/**` 的 `$schema` 相对路径已同步改为 `../../contracts/schemas/`。
