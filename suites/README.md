# 评测集

评测集保存可分发的用例定义与来源，不存放某次运行的通过状态。
`kernels/` 是算子评测领域，当前 `kernels/typical8/spec.json` 收录 8 个典型用例，
覆盖目标仍为 80 条。定义仍为 draft，参照实现和数值合同的 pending 状态继续保留。

本地 `spec.hidden.json` 引用公共算子与检查合同，默认不入版本控制。
执行与结果合同分别由 core 与 schemas 维护。
