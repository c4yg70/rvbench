# func→acc→perf 自动接力设计（eval_pipeline v2.1，2026-09-10）

> 实现正本：`clients/eval_pipeline.py`（relay 章节 + main() 两处挂点）。
> 状态：**已实现、默认全关**，未激活（上板/激活等用户通知天窗期后另行进行）。
> 依据：实验环境.md §3/§4（口径红线、计时口径）、并发评测经验.md §3（ADR-0012/0014）、AGENTS.md 铁律。

## 1. 链路

```
优化组工单（kind=func，8791 X100 侧）
        │ 终态 func_pass（机械判定）
        │ [开关 chain_func_acc]
        ▼
合成 acc 票 <stem>-acc（kind=correctness）──→ scan_tickets → submit 现有路径
        │                                        → corr_base() 车道（8791）
        │ 终态 done 且 result.correct==true
        │ （= judge 判定 accepted_correctness_and_faster：
        │   X100 rep=1 粗计时 快于同簇现任 >5%）
        │ [开关 relay_acc_perf]
        ▼
内存闸（relay_mem_gate_mb，pssh 只读查 MemAvailable）+ 上限（relay_max_per_run）+ 去重
        │ 全部通过
        ▼
合成计时票 <stem>-acc-perf（kind=timing-final，与现行计时票同构、带相同 files）
        └──→ submit 现有路径 → BASE（8788 A100，warmup=5/rep=5 正本计时）
             两簇同 ISA、共享 ~/.triton/cache → acc 在 X100 编译的缓存条目
             A100 计时作业直接命中，省作业内首编 60–90s（热缓存）
```

要点：

- **合成票复用 build_body/submit 全部现有路径**：车道路由（kind 决定 corr/计时）、
  `deploy_key`/`k3_busy_paths` 同路径避让、服务端 `path_lock` 串行化全继承，不另起 HTTP 调用。
- **粗计时红线**：acc 车道返回的 rep=1 粗计时只许做「触发/排序」比较，绝不写入任何
  result/报告/入库路径；接力判断**委托 judge 的 `accepted_correctness_and_faster` 结论**
  （judge 在 finalize 时已用同簇 registry 做过 `g < ref*0.95` 比较，host 键区分
  k3-acc/k3，钩子不读数值、不重复比较——judge 切换后 `ref:=g`，重算永远不满足）。
  计时正本只在 8788（A100，warmup=5/rep=5）产出。

## 2. 配置键（RUN/.pipeline-config.json，逐 tick 动态读，默认关闭）

| 键 | 默认 | 语义 |
|---|---|---|
| `chain_func_acc` | `false` | func 票终态 func_pass → 为同一 (cond/op/stem/shape/dtype/candidate_path) 合成 acc 票（kind=correctness），走 corr_base() 车道 |
| `relay_acc_perf` | `false` | correctness 票 done 且 correct==true（即 judge 同簇粗计时 >5% 快于现任）→ 合成计时票投 BASE；**同时控制挂起队列冲刷**（关=冻结队列） |
| `relay_mem_gate_mb` | `3072` | relay 投计时票前经 `python ops/pssh.py Home-SpacemitK3 'grep MemAvailable /proc/meminfo'` 只读检查，低于阈值本 tick 挂起、下 tick 重试并记 flags；SSH 异常 fail-closed（挂起），连续 3 次失败降级放行并注记 flags（探测恢复成功自动收回降级）。只约束 relay 计时票，不约束 chain 的 acc 票 |
| `relay_max_per_run` | `10` | 本次驱动运行 relay 自动投计时票上限；新触发超限→注记 flags 停投（不排队）；挂起队列超限→保留队列待下次运行 |

读取姿势与 `correctness_paused` 相同（`load_json(CONFIG_FILE, ...)` 每 tick 重读）：
**开关切换即时生效，不需要重启驱动**；重启只在加载新代码时需要（见 §5）。

## 3. 去重（三层）

| 层 | 机制 |
|---|---|
| acc 接力去重 | 合成 stem 固定为 `<原 stem>-acc`；工单文件已存在（含已完结）或在飞 `ticket_path` 命中即跳过（等价于 scan_tickets + infl_tickets 按 lost_key 查重） |
| 计时接力去重 | `RUN/.relay-timed.json`：`<op>/<candidate_sha256>` → `{ts, src, src_job, ticket}`；**同候选只自动投一次**（key 取候选 sha256 而非 stem，同候选换轮次重提不会重复投）。sha256 缺失时回退 `<op>/stem:<stem>` |
| 挂起队列去重 | key 相同的票已在 `state.relay_pending` 中则不再入队 |

`.relay-timed.json` 与 `.pipeline-state.json` 同目录（runs/E1/）同生命周期：
驱动启动加载、原子写落盘、单实例驱动持有（沿用 state 文件单实例互斥口径）。
挂起队列存 `.pipeline-state.json` 的 `relay_pending` 键，跨重启存活。

## 4. 内存闸细节

- 探测：`ops/pssh.py`（paramiko），一 tick 至多一次（`CYCLE_S=20` 内缓存复用，多条挂起票共享）。
- 解释器：默认当前解释器，可 env `KOPE_PSSH_PYTHON` 覆盖（同 `ops/precompile.py`）。
  驱动 venv 若缺 paramiko → 探测按失败计 → fail-closed 挂起 → 3 个 tick（~60s）后自动降级放行，接力不卡死。
- 失败计数仅进程内（重启清零）；`MemAvailable` 低于阈值属正常挂起，不计入失败连击。

## 5. 激活规程（波次边界执行）

**原则（并发评测经验.md §3，ADR-0014）**：客户端逻辑改动，**在飞 pipeline 进程要重启才吃到新代码**；
开关本身逐 tick 读，重启一次之后开/关都不再需要重启。在飞作业与既有结果不受影响。

1. **波次边界**：确认当前无在飞优化批次依赖（或在当前批次收尾后）。停旧驱动
   （写 `runs/E1/HALT` 让其优雅退出 exit 2，或直接结束进程）；确保同一时刻只有一个驱动实例（state 文件单实例）。
2. **部署代码**：确认驱动从本仓库 `clients/eval_pipeline.py` 启动（新代码已在本仓库）。
3. **开开关**：编辑 `runs/E1/.pipeline-config.json`：
   ```json
   {
     "correctness_base": "http://10.18.60.100:8791",
     "correctness_paused": false,
     "baseline_paused": false,
     "timing_slots": 4,
     "chain_func_acc": true,
     "relay_acc_perf": true
   }
   ```
   （`relay_mem_gate_mb` / `relay_max_per_run` 可省略，默认 3072 / 10。）
4. **重启驱动**，确认启动日志第二行 `relay chain: chain_func_acc=True relay_acc_perf=True ...`。
5. 观察首张接力票：flags 里 `relay_acc_synth` / `relay_perf_synth`，日志 `RELAY ... 合成工单`。

## 6. 回滚

- **软回滚（首选）**：`.pipeline-config.json` 把 `chain_func_acc` / `relay_acc_perf` 置 false
  （逐 tick 生效，无需重启）。挂起队列原样保留，去重表保留——**之后重新打开不会重复投递**。
- **硬回滚**：恢复旧版 `clients/eval_pipeline.py` 并重启驱动（波次边界）。两个状态文件
  （`.relay-timed.json`、state 的 `relay_pending` 键）可保留不清理，旧代码不读它们，无副作用。
- 已投出的接力票就是普通工单，走既有 finalize/判定路径，无需特殊清理。

## 7. 现场确认点（激活时留意）

1. **acc 票 rep 语义**：合成 acc 票复用 build_body（warmup=5/rep=5 + `lane=correctness`），
   实际 rep=1 粗计时由 8791 服务端 lane 语义决定——与现行 correctness 票行为完全一致，
   首张 acc 票终态后核对 result.gems_ms 精度口径无异常即可。
2. **relay-timed 粒度**：key 用 `<op>/<candidate_sha256>`（按"同候选只投一次"的目的句取
   sha256 粒度，stem 记在 value 里备查）；若现场想要"每 stem 一次"需改 key 拼法。
3. **内存闸覆盖面**：按任务书字面只闸 relay 计时票；chain 的 acc 票走 corr 车道
   （CORR_SLOTS 自约束）不经内存闸，若 X100 侧也需内存保护需另行扩。
4. **编排侧计数**：接力票会为同一 op 增加结果文件（stem 带 `-acc` / `-acc-perf` 后缀），
   `kope.py wait <op> <N>` / `op_status.py` 等按结果数消费的工具会把它们计入，
   编排口径如按"每轮 1 结果"计数需知悉。
5. **judge 委托语义**：relay 触发条件 = judge 的 `accepted_correctness_and_faster`
   （同簇、有现任参照、粗计时快 >5% 三个条件同时成立）；registry 冷启动/跨簇重定基
   （`accepted_correctness`）不触发接力——这是"必须快于现任"的字面语义。
