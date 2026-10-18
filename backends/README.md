# 平台执行实现

按厂商与具体硬件组织，例如 `spacemit/k3/`。
后端消费 core 合同，负责平台能力、实际调用、环境与清理证据。

当前新版 worker 已接入 K1、K3；两种硬件都可选择 native C 或 Triton 实现。
native C 支持 float32 add/abs；Triton 支持既有 14 个算子及 float32/float16，
实际能力以各 worker 的独立运行环境探测为准。接口和环境要求见
[manager/worker 说明](../docs/manager-worker.md)。
`spacemit/k3/` 保留旧 evald 兼容实现；SG2042 与通用 RVV 档案不表示已接入新版合同。

`backends` 表示平台适配边界；worker 表示执行进程角色。
未来有实际注册、发现与加载接口时，再决定是否开放第三方 plugins。
