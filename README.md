# rvbench

面向 RISC-V 的评测合同、Rust 控制面与平台执行后端。主仓地址为 https://github.com/c4yg70/rvbench；两仓历史已合并，远程更名由用户完成。

## 目录与职责

```text
rvbench/
├── core/                       Rust 通用合同、校验、摘要、旧结果兼容
├── runtime/                    Rust HTTP、队列、worker、锁、超时与恢复
├── cli/                        rvbench：校验、作业客户端、suite 与 pipeline
├── tools/                      rvbench-tools：检查、部署打包、trace、SSH/SFTP
├── backends/spacemit/k3/
│   ├── src/                    rvbench-evald、配置、平台策略、执行器
│   ├── evald/                  启停脚本与配置模板
│   ├── adapters/board/         Python 算子调用、参照与计时桥接
│   ├── adapters/python/        Python warmfork 与预编译桥接
│   ├── profiles/               平台档案说明
│   ├── monitor/                Prometheus 配置
│   └── tests/                  Rust 服务、执行器与兼容回归
├── schemas/                    JSON Schema 格式正本
├── suites/kernels/typical8/     典型 8 算子公开定义与本地隐藏定义
├── examples/                   synthetic 结果示例
├── webui/                      服务器前端页面与设计稿
├── docs/                       格式、架构决策与历史记录
└── artifacts/                  本地构建、部署包与旧 Python 快照（忽略入库）
```

`core` 不依赖厂商实现；`runtime` 调用 Backend/Executor 接口；K3 后端实现路径白名单、资源分配和算子执行策略。worker 是执行角色。当前通过 Cargo crate 显式接入后端。

主程序与维护工具不调用 Python 控制程序。Python 仅保留 torch/Triton/FlagGems 的算子执行、计时、参照、warmfork 和 JIT 预编译桥接。

## 构建与检查

使用 Rust stable（本地验证版本为 1.96.0）。依赖版本由 Cargo.lock 固定。

```powershell
cargo build --workspace --locked
cargo run -p rvbench-cli -- validate spec suites/kernels/typical8/spec.json
cargo run -p rvbench-cli -- validate results examples/eval_results.json --spec suites/kernels/typical8/spec.json
cargo run -p rvbench-cli -- validate spec examples/baseline/spec.json
cargo run -p rvbench-cli -- validate baseline-results examples/baseline/baseline-results.json --spec examples/baseline/spec.json
cargo run -p rvbench-tools -- check
cargo run -p rvbench-cli -- ev --help
```

构建生成 `rvbench`、`rvbench-evald`、`rvbench-tools`，位于 `target/debug/`；Windows 后缀为 `.exe`。安装后可直接执行对应命令。

公开 spec 仍为 draft，`validate spec ... --require-ready` 返回 2。合同合法与完成板上验证是不同状态。隐藏定义 `suites/kernels/typical8/spec.hidden.json`、真实 `evald/config.json` 和环境文件默认忽略入库。

## 作业与管线

```sh
rvbench --base http://127.0.0.1:8788 ev status
rvbench --base http://127.0.0.1:8788 ev run --op abs --shape 1024,1024 --lane correctness
rvbench --workspace /path/to/campaign pipeline --once
rvbench-tools trace --root /path/to/campaign/runs --json
```

token 使用 `EV_TOKEN`，客户端也支持本地私有 K3 配置；工作区使用 `EV_WORKSPACE`，平台使用 `EV_BACKEND`。服务保留 `X-K3-Token`、`/healthz`、`/metrics`、`/jobs` API。

原生管线保存 `runs/.rvbench-pipeline.json`，扫描 ticket 后提交、轮询、写入 raw-v1/result-v2 并更新 registry；候选缺失或摘要不符会阻止提交。它不自动导入历史 Python campaign 的运行中状态；历史 relay-chain 和专用 memory gate 保留为归档资料。

## 板端与前端

[RISC-V 构建及部署约定](backends/spacemit/k3/README.md)。部署包只在本地生成，使用 RISC-V Linux ELF，排除私有配置。板端仍需匹配的 Python/torch/Triton/FlagGems 环境。

[webui/](webui/README.md) 是前端统一目录；现有 dashboard 通过 Prometheus 展示监控，设计稿保留供后续评测交互页面使用。

## 合同与迁移

新 spec/eval_results 合同与旧 suite-v1/result-v2 分别校验。Rust 兼容层保留旧结果视图，不将缺失证据补造为新格式可信结果。新合同的完整 case runner 和真实证据采集仍按典型 8 算子阶段计划推进。

- [Rust 迁移决策与验证范围](docs/adr/0002-rust-control-plane.md)
- [合仓决策](docs/adr/0001-monorepo.md)
- [架构图](docs/architecture.html)
- [spec 格式](docs/specs/2026-10-08-spec-json格式.md)
- [结果格式](docs/specs/2026-10-08-eval-results-json格式.md)
- [可选 baseline 定义与历史测速](docs/specs/2026-10-16-suite-baseline格式.md)

原两仓完整本地备份在工作区 `.repo-backups/2026-10-14/`；本次迁移的旧 Python 控制代码按原路径保存于 `artifacts/python-legacy-2026-10-14/`，附逐文件 SHA-256 清单。历史文档位于 `docs/history/` 和 `docs/backends/`。
