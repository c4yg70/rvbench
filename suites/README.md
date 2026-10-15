# 评测集

评测集保存可分发的用例定义与来源，不存放某次运行的通过状态。
`kernels/` 是算子评测领域，当前 `kernels/typical8/spec.json` 收录 8 个典型用例，
覆盖目标仍为 80 条。定义仍为 draft，参照实现和数值合同的 pending 状态继续保留。

本地 `spec.hidden.json` 引用公共算子与检查合同，默认不入版本控制。
执行与结果合同分别由 core 与 schemas 维护。

spec 2.1.0 可在顶层声明可选 `baseline.entries[]`，提供基准代码清单、参考运行命令及历史测速文件引用；没有 baseline 的 2.0.0 定义继续有效。历史记录与当前通过状态分别管理，不自动写入 incumbent registry。

详见 [baseline 格式](../docs/specs/2026-10-16-suite-baseline格式.md) 与 [完整示例](../examples/baseline/spec.json)。

typical8 已接入 [PyTorch 原生 baseline](kernels/typical8/baselines/pytorch/README.md)：7 项完整接口延迟、1 项独立 SDPA out-only 采样均来自 HUMAN-K3。Abs/Aminmax/TopK 检查通过，待定数值合同与完整 Attention 接口仍保留未验证状态。
