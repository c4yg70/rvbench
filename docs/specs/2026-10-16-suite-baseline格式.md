# Suite baseline：可运行代码、参考命令与历史测速

公开 spec 格式新增 **2.1.0**，顶层 `baseline` 可选；仍接受没有该字段的 2.0.0。旧 `suite-v1` 不变。结构正本为 [spec.schema.json](../../schemas/spec.schema.json)，语义正本为 [core/src/baseline.rs](../../core/src/baseline.rs)。

已实现定义、文件及摘要校验、历史记录格式和 CLI；Rust CLI 不自动执行声明的命令，不改动 pipeline 的 incumbent registry。[examples/baseline](../../examples/baseline) 保留 Abs 探针与 synthetic 教学示例；典型 8 算子的 [PyTorch baseline](../../suites/kernels/typical8/baselines/pytorch/README.md) 提供原生算子代码和真实 baseline-results 采集入口。

## 1. 可选声明

```json
{
  "baseline": {
    "entries": [{
      "id": "torch_abs",
      "operator_id": "abs",
      "case_ids": ["baseline_abs_demo.abs.C01"],
      "code": {
        "root": "code",
        "files": [{"path": "abs.py", "sha256": "<文件 SHA-256>"}]
      },
      "run": {
        "cwd": ".",
        "argv": ["python", "abs.py", "--spec", "{spec}", "--case-id", "{case_id}", "--output", "{output}"],
        "env": {"OMP_NUM_THREADS": "4"}
      },
      "reference_results": []
    }]
  }
}
```

省略 `baseline` 表示未提供基准包，不能填 null。存在时 `entries` 非空，每个 entry 显式提供以上字段；`reference_results=[]` 表示尚未登记测速。缺少 baseline 或历史测速不增加 readiness blocker；提供的声明有误仍导致定义校验失败。

| 字段 | 语义 |
|---|---|
| `id` | suite 内唯一，匹配 `[a-z][a-z0-9_-]*` |
| `operator_id / case_ids` | 引用现有算子及其公开 case；列表非空、无重复、无通配符 |
| `code.root` | 相对 spec 所在目录的已有代码目录，可为 `.` |
| `code.files[]` | 相对代码根目录的文件路径及文件字节 SHA-256；清单非空、路径唯一 |
| `run.cwd` | 相对代码根目录的已有工作目录，可为 `.` |
| `run.argv` | 非空参数数组；允许的占位符仅 `{spec}`、`{case_id}`、`{output}` |
| `run.env` | 显式环境变量映射，可为 `{}`；变量名为 POSIX 标识符 |
| `reference_results[]` | 相对 spec 所在目录的结果文件路径及文件字节 SHA-256；路径唯一 |

代码入口、本地 helper、依赖锁文件均应纳入代码清单。校验器核验清单中的文件，不推断 Python import 图。外部依赖是否安装由执行环境负责，结果记录实际版本。

路径使用 POSIX 分隔符；禁止绝对路径、盘符、反斜线、空组件、`..` 和 NUL。除根路径/工作目录的单独 `.` 外，不接受点组件。解析 symlink 后仍须位于所属根目录。所有被引用文件必须存在、为普通文件且摘要匹配；`--require-ready` 不是绕过文件验证的开关。

命令占位符按参数边界替换，不做 shell 拼接。环境变量值不展开占位符。需要 shell 时显式使用 `bash run.sh`，并将脚本纳入清单。校验器不导入或执行代码。多个 baseline 覆盖同一 case 是合法的；执行方须显式选择 ID，没有默认选中第一项的语义。

baseline 表示可复现的性能基准实现，现有 `call.implementations.reference` 仍是数值参照声明。历史结果不代表当前测试通过，也不自动成为性能比较的参照值。

## 2. 独立测速记录

格式为 [baseline_results.schema.json](../../schemas/baseline_results.schema.json)，`schema_version=1.0.0`，`$id=urn:rvbench:baseline-results:1.0.0`。

| 字段 | 要求 |
|---|---|
| `$schema / schema_version` | 指向 baseline_results.schema.json，格式固定 1.0.0 |
| `purpose` | measured 或 synthetic；示例数据必须标明 synthetic |
| `suite_id / baseline_id` | 与定义一致 |
| `implementation_sha256` | 绑定代码清单和运行声明，算法见下节 |
| `environments[]` | 非空，环境 ID 唯一，环境指纹必须可重算 |
| `measurements[]` | 非空；同 case、环境和 UTC 时间组合唯一 |

环境复用 eval_results 的硬件、运行时、亲和性和隔离字段，增加必填的 `backend`、`host_tag`、`omp_threads`。当前仍只接受 RV64 环境。`fingerprint_sha256` 对除 `id`、`fingerprint_sha256` 外的整个环境对象计算 canonical 摘要；worker/boot 身份存在时也参与。指纹 v1 是完整快照身份，不是跨机器性能等价证明。

每条 measurement 包含：

| 字段 | 要求 |
|---|---|
| `case_id / case_contract_sha256` | 属于所选 baseline，绑定有效 case 合同 |
| `inputs[] / attrs / inputs_sha256` | 实际输入、完整属性和聚合摘要；不能只提供 seed |
| `environment_id / recorded_at` | 环境存在；时间为 `YYYY-MM-DDTHH:MM:SS[.fraction]Z` |
| `correctness` | `{status, reason, checks}`；status 为 passed/failed/unverifiable |
| `timing` | 下述计时对象；无有效计时可为 null，但 passed 必须有计时 |
| `artifacts[]` | 相对结果所在目录的 `{path,sha256,size_bytes}`，校验真实文件字节 |

输入沿用 eval_results 的 `name/kind/shape/dtype/strides/content_sha256/generator`，另增加必填 `storage_offset`。tensor 输入的维度、dtype、generator、strides、offset 必须匹配 case；None 的元数据均为 null。contiguous stride 按行主序从末维起计算，空维按 `max(dim,1)` 推导。实际输入摘要对 `{inputs,attrs}` 计算。

检查记录沿用 eval_results 的 check 结构。passed 要求非空且全部 passing 的检查，无错误原因，并以 numeric/semantic 检查覆盖所有返回项。其他状态必须提供 `{code,message}` 原因。measured passed 还要求有 passing 的 execution 检查、没有 pending 用例合同、已绑定计时合同且存在证据 artifact。baseline 的执行证据独立于候选 `case.execution`，不会将 FlagGems 候选的 pending_probe 改成 ready。synthetic 可以展示尚未 ready 的将来记录形状。文件和摘要一致性不能证明声明的检查确实执行过。

计时对象固定 `unit=ms`、`scope=call`、`statistic=median`、`preparation=excluded`，还需记录同步方式 `synchronization`、`timing_contract_sha256`、`warmup`、`repetitions`、`samples_ms` 和 `median_ms`。样本必须为有限非负数，数量与 repetitions 相等；偶数样本取中间两项平均。记录的中位数须与重算值一致，容差为 `max(1e-12,1e-9*abs(median))`。已绑定计时合同的 spec 必须匹配该摘要。

失败、不可验证、synthetic 记录用于历史展示或排查，不能升级为可信测量。没有结果时保持引用列表为空，不用延迟 0 表示缺失。测速文件不保存预设 speedup；正式比较需显式选择基准、核对代码、case、实际输入、计时规则与环境可比性。

## 3. 摘要与无环引用

所有对象摘要使用项目已有 canonical JSON：按键排序、紧凑 UTF-8、Python 兼容数字表示。文件摘要针对文件原始字节。数组顺序参与对象摘要。

- `implementation_sha256`：对 `{id,operator_id,code,run}` 求摘要；排除 `case_ids` 和 `reference_results`。
- `case_contract_sha256`：对 `{case,operator:{id,call,returns},target:suite.target,policies}` 求摘要。完整选中 case、调用、输出、策略及已声明的合同文件摘要进入计算；排除 baseline、其他 case、suite.status 与顶层 version。
- `inputs_sha256`：对 `{inputs,attrs}` 求摘要，包含实际 tensor 内容摘要和布局。
- 环境指纹：对环境对象移除 id 和 fingerprint_sha256 后求摘要。

spec 固定结果文件的字节摘要，结果文件绑定上述实现和 case 摘要，不反向绑定含结果引用的完整 spec 摘要。因此，新增历史记录不会要求重写既有记录。整体 `spec_digest` 仍包含 baseline 和 reference_results；公开 spec 改动后，使用它的 hidden 定义须重新登记 public_spec.sha256。

摘要辅助函数位于 `rvbench_core::baseline`，供未来 runner 复用。它们只计算摘要；应先执行定义及文件验证，再用于记录绑定。

## 4. CLI 与兼容

```sh
rvbench validate spec examples/baseline/spec.json
rvbench digest examples/baseline/spec.json --baseline torch_abs
rvbench validate baseline-results examples/baseline/baseline-results.json --spec examples/baseline/spec.json
```

基准 digest 命令输出 implementation_sha256 与所覆盖 case 的 case_contract_sha256。原 `rvbench digest FILE` 仍输出整个文件 canonical 摘要。`validate baseline-results` 必须提供 spec，同时核对代码和证据文件；验证不依赖命令行当前目录。未知 baseline ID 失败退出。

spec 2.0.0 的字段约束保持原样，不能偷偷添加 baseline。spec 2.1.0 允许可选 baseline，内容 version 独立更新。典型 8 算子定义接入 PyTorch baseline 后升至 2.1.0 / 内容 0.3.1，suite 仍是 draft，FlagGems 候选的执行与数值合同状态保留。

eval_results 新增 1.1.0，允许绑定 spec 2.0.0 或 2.1.0；仍接受 1.0.0，但该旧版本仍只能绑定 spec 2.0.0。隐藏格式和 legacy suite-v1 没有结构变化。

Abs 教学示例入口需现有 torch 环境。把 argv 占位符换成绝对 spec、case ID 与输出路径，并应用 run.env，即可手工运行。其 probe_only 输出不能直接登记为 baseline-results；对应教学结果继续保持 synthetic。典型 8 算子的 PyTorch runner 可直接生成 measured 记录与证据文件，通用自动调度尚未接入。
