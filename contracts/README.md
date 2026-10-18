# contracts

跨服务 wire 合同的正本目录。

| 文件 | 地位 |
|---|---|
| `openapi.json` | 共享 wire 模型的唯一正本。OpenAPI 3.1，36 个 `components/schemas` 模型，无 `paths` |

## 谁消费它

| 消费方 | 方式 |
|---|---|
| `manager/contract/openapi.yaml` | `$ref: ../../contracts/openapi.json#/components/schemas/...`，本地相对路径，离线可解析 |
| worker 仓 `contract/openapi.yaml` | raw URL 指向本仓 `main` 的该文件 |
| [`rvbench-core-rust`](https://github.com/rvbench/rvbench-core-rust) | `openapi-to-rust` 的输入；`SPEC` 常量在编译期嵌入该文档 |

协议仓内的 `openapi.json` 是随版本发布的副本，不是正本。`cli/tests/contracts.rs` 的 `shipped_spec_matches_contract` 逐字节比对两侧，漂移即测试失败。

## 变更流程

```sh
# 1. 改正本
$EDITOR contracts/openapi.json

# 2. 同步协议仓副本（默认 ../rvbench-core-rust）
python tools/sync_contract.py

# 3. 在协议仓重新生成类型
cd ../rvbench-core-rust && openapi-to-rust generate -c codegen.toml

# 4. 回归
cargo test --workspace --locked
```

生成物禁止手改。任何 wire 变更先落到本目录，再生成、测试、实现。
