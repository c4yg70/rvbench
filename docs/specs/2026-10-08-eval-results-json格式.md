# eval_results.json — 评测结果 v1 合同

状态：Schema、语义校验器与 synthetic 示例已落仓（2026-10-08）；Rust manager/worker 和 Python runner 的结果写入尚未实现。

- 结构正本：[eval_results.schema.json](../../contracts/schemas/eval_results.schema.json)，JSON Schema Draft 2020-12，`$id=urn:rvbench:eval-results:1.1.0`，兼容 1.0.0。
- 语义校验：[core/src/results.rs](../../core/src/results.rs)，不加载 torch、不执行算子。
- 示例：[examples/hello-world/eval_results.json](../../examples/hello-world/eval_results.json)，`run.purpose=synthetic`，其中时间、摘要、环境及指标均为测试夹具，不是实际评测证据。

## 1. 版本与职责

`eval_results.json` 是一次 Run 的可查询结果快照：保留所有所选 case 的终态、全部历史 attempt 与有效结果选择，并由同一批 case 重算汇总。Run 完成表示流程结束，允许包含 failed/skip/infra_error；不等同于所有测试通过。尚未执行或丢失的 case 仍保留，用 `infra_error / canceled / unverifiable` 和原因解释，不能从分母删除。运行中的调度状态由服务状态库管理，不把 queued/running 伪装成终态。

`$schema` 指向 `eval_results.schema.json`；`schema_version` 接受 1.0.0/1.1.0；顶层 `version` 是该结果文件自身的内容修订版本（初始可为 0.1.0）。重试、选择变化或重新生成快照须递增自身 `version`，不得修改原始 attempt 的证据。版本采用无前导零的三段非负整数，未知格式拒绝。

`specs.public`、`specs.hidden` 各保存 `{suite_id, schema_version, version, sha256}`。结果格式 1.0.0 仍只接受公开格式 2.0.0；1.1.0 可接受 2.0.0/2.1.0，以支持可选 baseline。隐藏格式保持 1.0.0；`hidden=null` 表示未加载隐藏集。内容摘要使用现有 `spec_digest` 的 canonical JSON v1 算法，包含版本字段，绝不只凭名称或自身版本认定内容相同。结果文件自身版本、Schema 版本与测试定义版本各有职责。Rust 实现仍须通过跨语言 digest golden corpus 门禁。

## 2. 顶层结构

除明确开放的数据映射外，对象拒绝未知字段；所有约定字段显式填写，缺值用 null，不用 0、空字符串或 NaN 代替未知。

| 字段 | 约定 |
|---|---|
| `run` | ID、standalone/distributed、manager ISA、completed/incomplete/canceled、UTC 起止时间、measured/synthetic、public/private 可见性 |
| `specs` | 固定公开与可选隐藏定义的格式版本、自身版本、canonical 摘要 |
| `execution` | func/acc/perf、实际 warmup/repetitions、计时合同摘要、完整选择计划摘要、候选 kind/ID/代码摘要 |
| `environments[]` | 仅 RV64 执行环境；worker/boot 身份、环境 fingerprint、CPU/ISA/核集、Python/torch/Triton/FlagGems/runner/adapter/generator 版本和隔离方式 |
| `cases[]` | 每个所选 case 一条，public/hidden 来源、operator、公开 contract_ref、outcome、reason、脱敏标记、所选 attempt ID、历史 attempts |
| `summary` | 固定总数、六种 outcome 计数、按环境分组的有效 case 数与几何平均 speedup |

standalone 的 `manager_arch / job_id / lease_epoch` 为 null，worker/boot 可为 null；distributed 的 manager ISA 为 amd64/arm64/rv64，worker/boot/job/lease_epoch 必填。不向结果写入可用于认证的租约 token；LeaseEpoch 仅用于追溯，manager 的持久化 fencing 记录才证明租约有效。

UTC 时间采用 `YYYY-MM-DDTHH:MM:SS[.fraction]Z`；完成/取消的 Run 有结束时间。环境 fingerprint 应由版本化的环境快照生成；不能把“同为 RV64”当作同环境，候选代码摘要单独保存。`candidate.kind=reference` 允许代码摘要 null，其他候选必须有代码摘要。

## 3. Case 与 Attempt

Case outcome 固定为 `passed / failed / skipped / unverifiable / infra_error / canceled`。非通过 case 和 attempt 均有 `{code,message}` 原因；公开隐藏汇总的脱敏例外见第 5 节。func 的 passed 仅代表调用成功，数值通过必须结合 acc/perf 模式判断。

每个 attempt 保存以下证据：

| 字段 | 约定 |
|---|---|
| 身份与执行 | 唯一 ID、job、lease epoch、environment_id、起止时间、status、trusted、exit_code |
| 摘要 | 完整 CasePlan 的 plan_sha256、实际加载的 loaded_candidate_sha256；与 Run 候选摘要核对 |
| `inputs[] / attrs` | 每个输入的 name/kind、实际 shape/dtype/strides/content SHA、generator（含公开或私有 seed）、完整调用属性；None 的元数据显式 null |
| `outputs[]` | 按合同返回槽位顺序记录 name/kind、produced/missing、实际 shape/dtype/content SHA；missing 不得声称有内容摘要 |
| `checks[]` | ID、numeric/semantic/execution、method、所检输出名、合同文件 SHA 或 null、实际参数（如 atol/rtol）、passed/failed/unverifiable、reason、数值或 null 的 metrics |
| `timing` | 未测量为 null；否则保存 ms/call/median/preparation excluded、同步方式、warmup/repetitions、成对 reference/candidate 原始样本、各自 median 与 nullable speedup |
| `cleanup` | ok/failed/unknown，后两者必须说明原因；可信完成要求 cleanup_ok |
| `artifacts[]` | 相对本次结果目录的路径、原始文件 SHA-256、字节数；拒绝绝对路径、上级跳转和 Windows drive/反斜线路径 |

`generator / attrs / check.parameters` 为显式数据映射，允许不同算子与生成器的参数；运行端仍须按对应注册合同校验。输出内容 SHA 的序列化口径由版本固定的 runner/adapter 定义，不能把原始 JSON 哈希当作 Tensor 值哈希。

所选 attempt 必须存在、与 Case outcome 相同、trusted 且 cleanup=ok；历史失败、旧租约或未清理 attempt 可保留为不可信证据，但不能成为有效结果。passed 还要求 exit_code=0、所有返回项 produced、输入内容摘要齐全、检查全部通过；acc/perf 每个返回项均须数值或语义检查，execution check 不足以证明正确性。绑定 spec 时检查 case/合同身份、输入属性及输出槽位与元数据；`purpose=measured` 的通过记录不能绑定 draft/pending 公开合同。synthetic 夹具可展示将来的记录形状。

## 4. 计时与汇总

perf 必须引用计时合同；perf passed 有成对有效样本。样本必须为有限正数，数量等于实际 repetitions；median 由样本重算，speedup=`reference_median_ms/candidate_median_ms`。func/acc 或未通过、未可信、缺任何一侧测量时 speedup=null。零耗时、NaN、Infinity 和无法计算的比值均不能伪装成有效值；可将相应测量置 null，并用 unverifiable/infra_error 与原因解释。

汇总只统计所选可信、已通过的 perf attempt；历史重试不重复计数。`timing_groups` 按同一 Run 内的 environment_id 分组，并重算 eligible_case_count 与几何平均；不跨主机/环境合并比值。root 的固定 spec/候选/计时协议与 case 合同进一步限定可比性。全部所选 case 均进入六种 outcome 分母；若没有有效 timing，就没有 timing group。公开脱敏视图不把隐藏细节重新纳入可复算的性能分组。

## 5. 隐藏用例与公开导出

完整证据（输入 shape、seed、attrs、输出与检查详情、日志）保存在 `run.visibility=private` 的受控结果目录。public 视图中的 hidden case 必须 `redacted=true`，只保留身份/合同引用与 outcome；`attempts=[]`、`selected_attempt_id=null`、`reason=null`，不输出可能泄漏参数的自由文本。公开 case 不允许借 redacted 掩盖缺失证据。隐藏 source 必须有隐藏 spec 摘要引用。

公开导出还须限制日志、artifact 下载、报错和报告模板的可见性；不能将私有原始文件挂在公开 URL。Schema/校验器只能检查 JSON 的约定，不能替代文件访问控制或证明日志已经脱敏。结果不嵌入整个隐藏 spec。

## 6. 校验与服务集成

```powershell
rvbench validate results examples/hello-world/eval_results.json
rvbench validate results examples/hello-world/eval_results.json --spec examples/hello-world/spec.json
cargo test --workspace --locked
```

可选 `--hidden-spec` 必须同时提供 `--spec`，并重新核验隐藏引用摘要。CLI 仅报告通过或违反合同的数量，避免把 Schema 错误里的私有参数回显；需要详情时在私有审计环境调用 Python API `validate_eval_results(result, public_spec, hidden_spec, public_base_dir=...)`。返回码 0 表示结构与语义一致，1 表示无效或读文件失败。严格 JSON 读取拒绝重复键和非有限常量，日历检查不依赖 jsonschema 的可选 format 包。

服务按 Schema + 语义规则写入和读取结果，完成快照采用原子写入；逐 attempt 证据存于 `attempts/`，可用同格式的一 case 切片存档（非 Run 全量快照不可用于整体计数）。`eval_results.json` 是最终查询/报告正本，JSONL 与 HTML 为派生视图；迁移期保留历史 raw-v1/result-v2 只读适配，不能将其丢失的字段补造为新格式可信证据。

Schema 一致性不能证明执行发生、参照可靠、Lease 有效、环境指纹真实、artifact 内容正确或采样公平。manager/runner 必须验证持久化租约、加载与计划摘要、来源合同、平台能力、原始 artifact 字节和清理证据；本仓 Rust evald 已实现队列、候选部署/恢复与旧作业结果；此处新 Run/Case/Attempt 合同的完整 runner 与证据生产仍未实现。
