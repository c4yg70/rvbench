# contracts

本仓全部合同正本的根目录。两类合同分开存放，互不引用：

| 子目录 | 合同类型 | 正本 | 消费方 |
|---|---|---|---|
| [`api/`](api/README.md) | 控制面 wire（服务间 HTTP JSON） | `api/openapi.json` | manager、worker、`rvbench-core-rust` |
| [`schemas/`](schemas/README.md) | 数据格式（spec / 结果 / baseline 文档） | `schemas/*.schema.json` | CLI 离线校验、examples、评测集 |

## 分工

- **wire 合同**描述服务之间传什么。两份服务 OpenAPI（manager 仓与 worker 仓的 `contract/openapi.yaml`）通过指向本仓 `main` 的 raw URL `$ref` 引用它，跨仓不复刻副本。
- **数据格式合同**描述落盘文档长什么样。JSON Schema Draft 2020-12，由 `cli` 在编译期嵌入，`rvbench-cli validate` 与两侧服务共用同一套校验器。

## 通用规则

1. 先改合同，再改实现。生成物与手写 DTO 不允许领先于合同。
2. 合同变更必须带版本：wire 走 `info.version`，数据格式走 `$id` 与文档内的 `schema_version`。
3. 正本只在本目录。协议仓 `rvbench-core-rust` 内的 `openapi.json` 是发布副本，用 `python tools/sync_contract.py` 单向同步，不反向写入。
4. 回归：`cargo test --workspace --locked`。`cli/tests/contracts.rs` 逐字节比对正本与副本，漂移即失败。
