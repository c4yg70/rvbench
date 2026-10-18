# ADR-0002：Rust 主程序和控制面，保留 Python 算子桥接

日期：2026-10-14。状态：用户确认并已在本地实现。

## 决策与实现

主仓为 `rvbench`，Cargo workspace 包含 `rvbench-core`、`rvbench-runtime`、`rvbench-cli`、`rvbench-tools` 和 `rvbench-k3`。

| 入口/模块 | Rust 职责 |
|---|---|
| rvbench-core | JSON Schema 与跨字段校验、8 算子输出推导、隐藏绑定、结果证据、canonical SHA-256、旧合同兼容 |
| rvbench-runtime | HTTP、认证、队列、持久化、worker、路径/计时锁、候选部署恢复、进程树超时、Prometheus |
| rvbench | 校验、作业 HTTP 客户端、suite 展开、持久化 ticket pipeline、旧结果与 registry |
| rvbench-evald | SpacemiT K3 平台配置、资源分区、请求及路径策略、执行器接入 |
| rvbench-tools | 全仓检查、RISC-V 部署包、trace 审计、原生 SSH/SFTP、预编译调度 |

服务 API 保留 `X-K3-Token`、`/healthz`、`/metrics` 与作业接口。执行机制从 K3 服务抽到 runtime 的 Backend/Executor 边界；平台实现归入 backends，core 不反向依赖平台。

## Python 边界

活跃 Python 源码仅有 6 个：`adapters/board/` 的 bench_op、bench_gemm、torch_reference、fallback_refs，以及 `adapters/python/` 的 warmd 和 precompile_driver。它们执行 torch/Triton/FlagGems 调用、参照、计时、fork 和 JIT；HTTP、调度、CLI、校验与 SSH 均为 Rust。

warmd 源码从 evald 移至 adapters/python，部署打包时复制回 evald/warmd.py。其父进程只预加载不变量；候选模块仍在每次 fork 的子进程内加载。Rust 负责进程生命周期、协议校验和失败回退。

## 合同与历史管线

spec/eval_results 新合同仍以 schemas 和 core 语义为正本，摘要保持原有 Python 排序 UTF-8 JSON 规则，包括浮点表示。新格式校验不补造真实输入、计时、租约或清理证据。

旧 suite-v1、raw-v1/result-v2、registry 与 ticket 仍可由 Rust 客户端使用。原生 pipeline 使用新的 `rvbench.pipeline-v1` 状态文件，按 ticket 的 wave 路径归档结果；候选缺失/摘要冲突会阻止提交。跨 wave 相同内容使用独立请求身份，重试保持同一身份。

历史 relay-chain、campaign 特定 memory gate、旧 inflight state 与一次性脚本不作为新控制入口。它们完整保存在本地旧源码快照和原 Git 历史中；若要继续旧战役，先对照旧状态与队列，再采用新状态格式。

## 保存与迁移

38 个旧 Python 控制、测试和项目元数据文件按原相对路径归档到本地忽略目录 `artifacts/python-legacy-2026-10-14/`；manifest.json 保存逐文件 SHA-256，归档前后逐一复核。原两个 checkout 的完整备份在工作区 `.repo-backups/2026-10-14/`；两套历史保留在合仓提交中。

真实 config.json、evald.env 和 spec.hidden.json 继续忽略入库。回归 fixtures 仅为公开/人工合成数据，不读取真实隐藏用例。

## 验证与实际边界

- 原实现对照：110 组核心合同样例（含摘要）、46 组请求校验、41 组 suite/档案/旧结果样例，共 197 组。
- 原生服务测试：认证、幂等、候选摘要及恢复、同路径串行、不同路径并行、队列限流与取消、失败/超时清理、无 journal 的重启恢复。
- 原生进程/执行器测试：输出解析、缺结果拒绝、超时及子孙进程管道清理。
- 客户端/维护测试：独立 cwd 校验、readiness 与 digest、候选缺失阻断、跨 wave 重试身份、同主机改善及跨主机重新基准、SSH 配置解析、trace D4、包目标/摘要/私有排除/覆盖拒绝。
- 25 项 Rust 测试通过；4 个 ignored 标记为被集成测试显式调用的子进程入口。`cargo fmt`、`cargo clippy -D warnings` 和原生工具公开合同检查通过。
- 本地主机 Rust stable 1.96.0 / Windows MSVC；以 cargo-zigbuild 0.23.4 + Zig 0.14.0 成功构建 `riscv64gc-unknown-linux-gnu` release。跨平台 CI 已配置，尚未在 GitHub 执行。

历史名为 good 的 trace fixture 在原 Python v1.1 中也因 D4 的 memory_root 规则返回 6 个错误。Rust 保留该结果；修正为自己的 runs 前缀后通过。bad fixture 保留 9 个错误、1 个警告。

这些验证未连接 K3 真机。RISC-V 编译不证明目标 glibc、环境、fork/Triton 行为或性能数值已经通过。新 spec 的完整 Run/Case/Attempt runner、generator/reference/check adapter 与典型 8 算子测量仍按既有阶段计划实施。公开定义仍为 draft（24 个 readiness blocker），本地隐藏定义同样未宣布 ready。

SSH 使用原生 Rust 连接和 SFTP，支持私钥/密码与 known_hosts；当前不支持 agent/ProxyJump，未执行远程维护或预编译。现有 Web UI 保持监控功能，评测交互页面后续在 webui 设计。

用户于 2026-10-14 授权构建、提交并推送。构建与验证产物保存在本地 artifacts；板端尚未部署。具体提交与发布状态以 Git 历史和远程分支为准。
