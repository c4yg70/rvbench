# spec.hidden.json — 仅引用公开算子的私有测试用例

- 状态：格式初版已落仓（2026-10-08）。隐藏定义使用独立 [spec.hidden.schema.json](../../contracts/schemas/spec.hidden.schema.json)，不使用公开集合的 Schema。
- 本地实例：`<suite>/spec.hidden.json`，与同目录的公开 `spec.json` 配对；公开仓不含隐藏定义，完整集随 rvbench-official-suites 发布。具体 shape、属性组合与 seed 只保存在私有文件中，不列入本文或公开测试夹具。
- 公开算子与检查合同由同目录 `spec.json` 定义（公开仓示例为 [`examples/hello-world/spec.json`](../../examples/hello-world/spec.json)），参见 [公开格式说明](2026-10-08-spec-json格式.md)。

## 1. 两个文件的职责

`spec.json` 定义算子、调用绑定、返回结构、参照与检查合同，以及公开用例。`spec.hidden.json` 仅追加同一评测集中的不公开测试用例，不能新增算子，不能重定义 callable、reference、输出槽位、检查规则或容差。

隐藏集没有第二套 `operators`、`sources`、`policies` 或硬件目标。它引用同目录的 `spec.json`，绑定该公开定义的内容摘要。公开定义修改后，隐藏定义必须重新校验兼容性并显式更新引用摘要，不能静默指向新合同。

## 2. 独立 Schema

| 字段 | 意义 |
|---|---|
| `$schema / schema_version` | 指向独立隐藏格式；当前版本为 1.0.0，与公开格式分别管理 |
| `suite_id` | 必须与被引用的公开集合 ID 一致，隐藏集不是新的算子注册表 |
| `version / status` | 本文件自身内容版本与 draft/ready；当前为 0.2.1/draft |
| `public_spec.path / sha256` | 指向 `spec.json` 的相对路径及公开内容摘要；算法与公开校验工具一致 |
| `cases[]` | 私有测试用例数组，不嵌套算子定义 |

顶层 `schema_version` 与 `version` 均必填。`schema_version=1.0.0` 严格匹配隐藏 Schema（`$id=urn:rvbench:spec-hidden:1.0.0`）；`version` 是独立内容版本，采用无前导零的三段非负整数。公开格式现为 2.0.0，隐藏格式现为 1.0.0，不要求两者版本相等。本次隐藏内容版本升至 0.2.0，是因为在兼容性校验后显式重绑定公开 v2/0.2.0 摘要，私有参数未改变。以后更新私有用例或引用的公开摘要时均须更新自身 `version`。

2026-10-14 合仓时更新 Schema 相对路径，核验旧引用后绑定公开内容 0.2.1 的新摘要；隐藏内容版本同步升至 0.2.1，私有用例参数保持不变。

评测结果的 `specs.hidden` 保存隐藏文件的格式版本、自身版本与内容摘要，见 [eval_results.json 格式](2026-10-08-eval-results-json格式.md)。隐藏文件不写入评测结果或已通过标记。

每条私有用例仅包含：

| 字段 | 意义 |
|---|---|
| `id` | `suite.operator.Hxx`，与公开 Cxx 分开，全局唯一 |
| `operator` | 只能引用 `spec.json` 已定义的 operator ID |
| `contract_ref` | 指向该算子的一个公开用例合同，复用其输入种类/dtype、检查与容差引用 |
| `inputs / attrs` | 私有测试的实际输入构造和完整调用属性，显式保留生成 seed |
| `execution` | 私有档独立支持性探针的状态、原因与证据 |
| `derivation` | 私有用例设计依据；不修改算子语义 |

隐藏 Schema 有自己的根结构和 case 约束，不引用公开 Schema 的 case/operator 定义。Tensor/None、布局与 generator 的基础语义保持一致，避免两个输入构造口径；隐藏文件拒绝 `operators`、`call`、`verification`、`outputs` 等额外定义字段。

## 3. 关联与检查

校验器先验证独立 Schema，再加载公开 `spec.json` 并验证其结构和摘要，最后逐条验证 operator、contract_ref 与输入/属性的兼容性。

v1 要求私有输入名称、kind、dtype 对应所引用的公开合同。允许变 shape、seed、输入值域、布局与合法属性；改变 dtype 或输出 dtype 需先在公开 spec 中建立相应合同，再用 contract_ref 引用。数值合同仍需覆盖实际值域、归约/收缩规模，由执行端确认其适用范围，不能因“隐藏”而放宽容差。

预期 tensor 输出 shape 由公开算子的语义和隐藏输入/属性推导；返回槽位、输出 dtype、参照、检查规则来自公开合同。Flash 的五元组与尚未确认的辅助输出会原样保留为待确认项，不能通过隐藏文件删减。当前已实现这 8 个算子的输出形状推导；新增公开算子时需增加推导支持。

结构校验内部会构造单用例视图以复用跨字段检查，不生成合并后的公开 spec，也不写回公开文件。私有支持性证据独立保存；普通校验通过不代表私有用例已经可运行或数值通过。现有公开数值/辅助输出合同的 pending 也会阻塞隐藏集完整性验收。

禁止私有用例与任何公开用例拥有完全相同的 inputs/attrs；同算子的私有用例也不能只换 ID 而复制相同参数。这里按声明数据比较，不替代运行时输入内容摘要检查。

## 4. 私有文件的保存

真实 `spec.hidden.json` 默认由 `.gitignore` 排除版本控制。本次在本地建立首份实例，公开仓库可保留独立 Schema、工具和人工合成的校验测试，不保存实际私有参数。公开发布仅分发 `spec.json`，私有文件由评测方持有并交给评测端读取。

自动化测试使用从公开定义构造的人工夹具，不依赖实际私有文件，也不复制其 shape、属性组合或 seed。隐藏文件名和 Git 忽略只管理文件分发；服务端接入时仍须控制实际文件、输入及完整日志的读取范围。

## 5. 使用

```powershell
rvbench validate hidden <suite>/spec.hidden.json
rvbench validate hidden <suite>/spec.hidden.json --require-ready
cargo test --workspace --locked
```

退出码：0 为定义有效，1 为格式/关联/参数错误，2 为 `--require-ready` 检测到未完成的探针或合同。初版预期普通校验通过，完整性验收不通过。

实现见 [core/src/hidden.rs](../../core/src/hidden.rs)。正常输出只报告覆盖算子数、用例数和未决数量，不列出实际私有参数；失败诊断仅供评测方本地查看，不随公开报告发布。格式语义仍处于初版，运行 generator、reference/check adapter 与板上测量继续按既有阶段计划实施。
