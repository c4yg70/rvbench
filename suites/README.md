# 评测集

评测集保存可分发的用例定义与来源，不存放某次运行的通过状态。

公开仓只内置一个最小示例：`examples/hello-world/spec.json`（1 个 `add` 算子、1 条 `hello-world.add.C01`），
用于跑通提交、编译、执行与结果落盘链路，配套 synthetic 结果见 `examples/hello-world/eval_results.json`。
它不是性能评测集：`suite.status` 为 draft，计时合同仍为 pending。

完整的算子评测集、基准代码与历史测速随 [rvbench-official-suites](https://github.com/rvbench/rvbench-official-suites) 开源；
本仓 `suites/` 只保留评测集体系的定义说明，不再存放具体用例。

隐藏定义（`spec.hidden.json`）引用公开算子与检查合同，默认不入版本控制。

执行与结果合同由 `rvbench-protocol` 与本仓 `schemas/` 维护。

spec 2.1.0 可在顶层声明可选 `baseline.entries[]`，提供基准代码清单、参考运行命令及历史测速文件引用；没有 baseline 的 2.0.0 定义继续有效。历史记录与当前通过状态分别管理，不自动写入 incumbent registry。

详见 [baseline 格式](../docs/specs/2026-10-16-suite-baseline格式.md) 与 [完整示例](../examples/baseline/spec.json)。
