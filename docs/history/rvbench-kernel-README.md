# rvbench-kernel

> 合仓前的 README 历史快照。现行定位与命令以主仓 README 为准；文中的原仓边界属于历史记录。

面向 RISC-V 芯片的**算子评测集**的设计与实现正本。

本仓记录"测哪些算子、按哪些 shape/dtype 档位测、拿什么做参照判定对错、按什么口径计时"的取定与理由，
以及这些取定的落地实现。目标平台为 RISC-V（当前实机口径在 SpacemiT K3 上取定）。

## 定位与边界

| 维度 | 本仓（rvbench-kernel） | 评测服务端（evald） |
|---|---|---|
| 职责 | 评测集设计：算子清单与入选依据、shape/dtype 档位、参考实现、数值判定容差、计时口径 | 运行时：作业队列与车道并发、部署-恢复、计时互斥、结果落盘与查询 |
| 产出 | 评测集定义、参考实现与容差正本、设计文档（含被否方案） | 服务进程与配套脚本 |
| 关系 | 定义"测什么、怎么判" | 执行"怎么跑"，按本仓定义产出结果并回传 |

评测服务端正本见 [rvbench-kernel-server](https://github.com/spacemit-com/rvbench-kernel-server)；
本仓不承载服务端进程与其部署单元。

## 内容范围

- **评测集设计**：算子范围与入选/排除依据；shape、dtype、规模档位（含主口径档）；对照参照的选择
  （框架参考实现 / 回退路径）；数值判定容差（分 dtype 的 rtol/atol 口径与取值理由）。
- **实现**：机器可读的评测集定义；参考实现与输入构造；容差与数值契约正本；单算子计时入口
  （微基准口径）与全量验收口径的驱动。
- **设计记录**：口径变更史与被否方案，保证"为什么这样测"可追溯；影响不可逆的取定按 ADR 记录。
- **流程**：条目纳入流程（proposal → dev → beta → release）见
  [docs/specs/2026-09-20-条目纳入流程.md](../specs/2026-09-20-条目纳入流程.md)（待审批）。
- **评测集计划**：典型 8 算子（`abs`、`aminmax`、`softmax`、`mv`、`mm`、`conv2d`、`topk`、
  `_flash_attention_forward`），先复现 8 个来源典型用例，再扩至每算子 10 个用例（80 条计划记录），见
  [docs/specs/2026-10-08-典型8算子评测集.md](../specs/2026-10-08-典型8算子评测集.md)（待评审）。
  该计划替代 [原 micro 集规划](../specs/2026-09-20-micro集.md)，旧文档保留作历史记录。
- **机器可读定义**：[首份 spec.json](../../suites/kernels/typical8/spec.json) 收录上述 8 个 C01 典型用例；
  [格式说明](../specs/2026-10-08-spec-json格式.md) 和 [JSON Schema](../../schemas/spec.schema.json)
  规定输入、调用绑定、输出与检查合同。当前为 draft，待补板上支持性与 fp16 数值合同。
- **隐藏用例**：本地 `definitions/typical8/spec.hidden.json` 仅保存不公开测试用例，引用公开定义中已有的算子，
  使用独立 [隐藏格式与校验约束](../specs/2026-10-08-spec-hidden-json格式.md)。真实私有文件默认不入版本控制。
- **版本与结果合同**：公开和隐藏文件均必填顶层 `schema_version`（匹配各自 Schema）与 `version`（自身内容版本）；
  当前公开格式为 2.0.0、隐藏格式为 1.0.0。结果使用独立 [eval_results.schema.json](../../schemas/eval_results.schema.json)，
  详见 [结果格式说明](../specs/2026-10-08-eval-results-json格式.md) 和 [synthetic 示例](../../examples/eval_results.json)。

本地校验：`python tools/validate_spec.py definitions/typical8/spec.json`；依赖见
[requirements-dev.txt](../../requirements-dev.txt)。完整性门禁使用 `--require-ready`（当前初版预期未通过）。

结果校验：`python tools/validate_eval_results.py examples/eval_results.json --spec definitions/typical8/spec.json`。
示例仅用于合同校验，不是板上测量；Schema 校验不替代执行端证据验证。

## 相关仓

- [rvbench-kernel-server](https://github.com/spacemit-com/rvbench-kernel-server) — 评测服务端（evald）：
  算子代码评估服务器，客户端提交候选代码 → 部署-恢复 → 异步取回计时与正确性结果。
