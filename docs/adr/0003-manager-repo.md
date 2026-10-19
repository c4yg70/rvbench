# ADR-0003：manager 独立成仓

日期：2026-10-19。状态：已执行。取代 [ADR-0001](0001-monorepo.md) 中「manager 与 CLI 同仓」的部分。

## 背景

ADR-0002 之后主仓同时承载 CLI、运维工具与 `rvbench-manager`。manager 与 worker 只通过
`rvbench-protocol` 通信，与 CLI 没有代码依赖，但共享同一个 workspace、`Cargo.lock` 与 CI 矩阵。
控制面服务的发布节奏、依赖面（axum / fs2 / sqlite）与评测工具链不同步，继续合仓会让
`cargo test --workspace` 成为二者的强制耦合点。

## 决策

manager 拆为独立仓 `rvbench-official-manager`，单 crate 包，`Cargo.toml` 显式声明依赖，
只依赖 `rvbench-protocol`（git 依赖固定 rev）。主仓 workspace 只保留 `cli` 与 `tools`。

拆分后仓库边界：

| 仓 | 责任 |
|---|---|
| `rvbench` | CLI、运维工具、JSON Schema 与 wire 正本、示例评测集 |
| `rvbench-official-manager` | 控制面：提交、队列、准入、授予、账本、观测汇总 |
| `rvbench-spacemit-worker` | 板端执行与本地评估 |
| `rvbench-core-rust` | `rvbench-protocol` 与随 crate 发布的 wire 副本 |

## 影响

- wire 正本仍在主仓 `contracts/api/openapi.json`；manager 仓的 `contract/openapi.yaml` 以
  raw URL `$ref` 指向它，与 worker 仓同构，跨仓不复刻 schema 副本。
- `rvbench-tools bundle` 增加 `--manager-root`；省略时输出 worker-only 包，给出时缺
  RISC-V manager 二进制直接报错，不静默降级。
- `tools/deploy_cluster.py` / `deploy_observability.py` 按 `RVBENCH_MANAGER_ROOT`
  （默认同级 `../rvbench-official-manager`）定位 manager 二进制，worker 仍取本仓 `target/`。
- `docs/manager-worker.md`、`docs/observability.md` 留在主仓，作为跨服务集成与集群部署手册。
- manager 的 contract、配置样例、conformance 测试随服务迁出，主仓不再保留副本。

## 验证

- 新仓：`cargo fmt --check`、`cargo test --locked`、`cargo clippy --all-targets -- -D warnings`、`cargo build --release --locked`。
- 主仓：`cargo fmt --check`、`cargo test --workspace --locked`、`cargo clippy --workspace --all-targets -- -D warnings`。
- 契约一致性：`rvbench-cli` 仓（2026-10-19 起独立）的 `tests/contracts.rs` 逐字节比对仓内 `contracts/api/openapi.json` 副本与 `rvbench_protocol::SPEC`。
