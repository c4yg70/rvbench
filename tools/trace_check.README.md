# trace_check.py — T3 harness-trace 落盘管线校验脚本

当前版本 **v1.1**（2026-09-09：runs/PILOT 端到端演练缺陷 D2/D3/D4 协调者裁决回写；
变更见下文「v1.1 条件感知规则」）。T3 spec 执行内容 3 的本地校验工具。
校验 `runs/<task>/<condition>/<op>/trace.jsonl`
（每候选一行 JSON）的必填字段、枚举、hash 格式、跨根污染与候选序完整性。
纯 Python stdlib，Windows Git Bash 可跑；不修改 `data/harness-trace.template.json`。

依据：`2026-kemos-cgo27/experiments/specs/T3-orchestration.md`（执行内容 2/3/5、隔离纪律）、
`2026-kemos-cgo27/data/harness-trace.template.json`（字段模板）、
`results/panel/feedback-states.md`（V06 七态）、
`docs/adr/0005-zcode-channel-runner-manual-cost-checkpoints.md`（成本断点）、
`docs/adr/0008-single-pass-execution.md`（单遍执行，行号即尝试序）。

## 用法

```bash
python bin/trace_check.py --dir results/conditions/full/add   # 单目录：目录内直接含 trace.jsonl，或递归其子树
python bin/trace_check.py --root runs/          # 从 runs 根递归收集并按 task/condition 汇总
python bin/trace_check.py --root runs/ --json   # stdout 输出机器可读 JSON（人读摘要转 stderr）
```

`--dir` / `--root` 二选一（必填）。`--json` 的 JSON 结构：
`{tool, version, generated_at, mode, base, ok, summary{files,lines,errors,warnings},
by_task, by_condition, files[{path,task,condition,op,lines,errors,warnings,issues[]}]}`
，issue 为 `{severity, line, code, field, message}`。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 无 ERROR（WARN 不影响结果） |
| 1 | 至少一个 ERROR |
| 2 | 用法/IO 错误（缺参数、路径不存在、找不到任何 trace.jsonl） |

## 每行候选的必填字段（16 个，存在且非空）

模板原生字段（照 `harness-trace.template.json`）：

| 字段 | 语义 |
|---|---|
| `schema_version` | 必须等于 `kelso.harness-trace/1.0` |
| `run_id` | run 标识 |
| `fixed_model_revision` | 模型身份（ADR-0005：仅记录 ZCode 暴露的版本名） |
| `kernel_parent_sha256` | 起点（incumbent）hash |
| `candidate_sha256` | 候选 hash；`repaired` 必须绑定新 hash，与失败候选区分 |
| `injected_evidence_ids` | 注入证据引用（非空列表；**v1.1 起 BARE 条件允许空列表**，其余条件引用条件卡或实值，如 `conditions-card:bare`） |
| `selected_strategy_id` / `strategy_reason` | 选取策略与理由 |
| 其余 `*_sha256` 字段 | 若出现且非 null，做 hash 格式检查（不强制非空） |

编排扩展字段（模板不动，trace 行在模板字段之上新增；语义来自 T3 spec 执行内容 2/5 与 ADR-0005）：

| 字段 | 语义 |
|---|---|
| `task` | 任务标识 ∈ {E1, E3, E9, E10_PASS2, E4, E7, PILOT}，须与目录一致 |
| `condition` | 条件 ∈ {BARE, KOPE, E_ONLY, R_ONLY, G, LOOSE, FROZEN_MEMORY, FROZEN_STRATEGY, FLAT_G, M}，须与目录一致 |
| `op` | 算子标识，须与目录 `<op>` 一致 |
| `result_state` | V06 七态单值：`accepted_correctness_and_faster` / `rejected_correctness` / `rejected_slower` / `repaired` / `rolled_back` / `timeout` / `crash` |
| `evald_job_id` | evald 作业号（断点清单取数指针） |
| `cost_checkpoint_id` | ADR-0005 成本断点指针（session/时间窗断点，作者手动归档） |
| `state_update_ref` | 记忆/策略更新记录指针（指向本 run 自己目录内的落盘位置） |
| `memory_root` | 本 run 记忆根，必须以 `runs/` 开头（v1.1 `E_MEMORY_ROOT`）且落在 `runs/<task>/<condition>/` 前缀下（`E_CROSS_ROOT`） |

## v1.1 条件感知规则（2026-09-09，runs/PILOT D2/D3/D4 裁决）

1. **D2**：`injected_evidence_ids` 允许为空列表，当且仅当 `condition=BARE`；其余条件仍必须非空（`E_EMPTY_FIELD`）。
2. **D3**：`strategy_before_sha256`/`strategy_after_sha256` 合法值扩为 {64-hex, `"pending"`, `"none"`}，其中 `"none"` 仅限无策略语义条件 {BARE, KOPE, E_ONLY, R_ONLY, LOOSE}；FROZEN_MEMORY / FROZEN_STRATEGY 的策略 hash 必须是**真实 64-hex**（恒等断言语义，不接受 `"pending"`/`"none"`）；其余条件（G/FLAT_G/M 等）为 {64-hex, `"pending"`}。违规报 `E_BAD_HASH`。其余 `*_sha256` 字段规则不变。
3. **D4**：`memory_root` 必须是以 `runs/` 开头的仓库相对路径（禁止裸 `mem/` 或其他形态），违规报 **`E_MEMORY_ROOT`**（新增错误码）；`runs/<task>/<condition>/` 前缀检查（`E_CROSS_ROOT`）保持不变。

## hash 规则

所有以 `_sha256` 结尾的字段：64 位十六进制，或显式字符串 `"pending"`（大小写不敏感）；
`null` 允许（模板口径），但必填两个字段（`kernel_parent_sha256`、`candidate_sha256`）不得为 null/空。

## 跨根污染审计（E_CROSS_ROOT，ERROR）

递归扫描行内**所有字符串值**（含嵌套 outcome/state_update/理由文本等）：

1. 出现 `kope-mem`（大小写不敏感）→ 违规（共享 kope-mem 根）。
2. 出现独立 `mem/` 根引用（如 `mem/relu`，即不在 `runs/<task>/<condition>/` 下的记忆根）→ 违规。
3. 出现任何 `runs/<a>/<b>/...` 路径引用，若不落在本行自己的 `runs/<task>/<condition>/` 前缀下
   （如 G 行引用 `runs/E4/KOPE/...`）→ 违规。

Windows 反斜杠路径会先归一化为 `/` 再比对；前缀比对大小写不敏感。
文本理由里提及他条件路径同样算违规（T3 隔离纪律：禁止跨 run 通信）。

## 同 op 候选序（WARN 级，不影响退出码）

- `attempt` 字段（可选，整数）：若行内出现，则整文件都须有；序须为 1..N 连续、不重复、按文件顺序
  （行号即尝试序，ADR-0008 单遍执行）。缺号 `W_ATTEMPT_GAP`、重复 `W_ATTEMPT_DUP`、
  乱序 `W_ATTEMPT_ORDER`、混用 `W_ATTEMPT_MISSING`、非整数 `W_ATTEMPT_TYPE`。
- 同文件内 `evald_job_id` 重复 → `W_DUP_JOB`。

## 错误码

`E_MISSING_FIELD`、`E_EMPTY_FIELD`、`E_BAD_TYPE`、`E_SCHEMA_VERSION`、`E_BAD_ENUM`、
`E_BAD_HASH`、`E_MEMORY_ROOT`（v1.1：memory_root 非 runs/ 相对路径）、`E_CROSS_ROOT`、
`E_PATH_MISMATCH`（行内 task/condition/op 与目录不一致）、
`E_LINE_JSON`、`E_LINE_NOT_OBJECT`、`E_EMPTY_FILE`、`E_IO`。

## Fixtures 自测

`bin/trace_fixtures/good/E1/G/add/trace.jsonl`：3 行合法候选
（accepted_correctness_and_faster / rejected_slower / repaired）。
`bin/trace_fixtures/bad/E4/R_ONLY/relu/trace.jsonl`：2 行违规
（非法 hash、null 理由、枚举外结果态、kope-mem 与 mem/ 引用、空注入证据、跨条件路径、重复 job_id）。

```bash
python bin/trace_check.py --dir bin/trace_fixtures/good   # 必须 PASS，exit 0
python bin/trace_check.py --dir bin/trace_fixtures/bad    # 必须 FAIL，exit 1
```
