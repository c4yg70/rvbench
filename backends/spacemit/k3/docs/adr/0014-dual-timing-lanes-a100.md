# 双计时车道：A100 分核并行 + 计时锁分区 + 部署路径互斥

日期：2026-09-09。状态：**已在 K3 生产启用**（`perf.perf.max_concurrency=2, cpu_ids="8-15", omp_threads=4`）。

## Context

K3 evald 的计时吞吐卡在 ~90 作业/h（单作业 34~45s，队列等待均值 154s），而今日 KOPE 目标是
237 算子 × 5 轮 ≈ 1185 个 timing 作业。作业成本解剖（`results/panel/job-cost-anatomy-20260909.md`）
表明单作业墙钟几乎全是**每进程固定成本**，真正怕噪声的测量窗口只有 0.86s：

| 成本项 | X100 实测 | A100 推算 | 可消除性 |
|---|---|---|---|
| `libtriton.so`（**756MB**）首次编译期 dlopen+init | 8.0~8.3s | ~16s（L1 探针编译比 1.9~2.1×） | **不可**：不能 fork 继承（子进程必死锁）、无磁盘缓存可复用、无编译子进程可省 |
| `import flag_gems` | 6.5~7.0s | ~8s | 结构上不可惰性（`_FULL_CONFIG` 829 个符号硬绑定全部 ops 模块） |
| do_bench 双臂（warmup5+rep5） | **0.86s** | ~1.05s | 这是唯一真正的“测量” |
| X100 预编译能省的部分 | — | **~1.5s（4%）** | 已实现但收益微小 |

已排除的三条路（都有实测负向证据，详见成本解剖文档）：

1. **warmd 父进程预热 triton**：父进程一旦编译/launch 任何 kernel（哪怕 `JITFunction.warmup`
   只编译不 launch），fork 出的子进程在自己第一次 launch 上**必定死锁**
   （栈：`spine_triton/driver.py:485 CPULauncher.__call__`；CPython 自己警告
   “multi-threaded…fork() may lead to deadlocks”）。作业实测 timeout 625s。
2. **flag_gems 惰性导入**：`flag_gems.testing` 的 1.31s 是 importtime **累计值**，其中 1.305s
   是它顺带引入的 `flag_gems.runtime`（`__init__` 本来就要 import）；惰性化实测收益 **0.00s**
   （10.68 vs 10.72，n=3）。已回滚，不留无收益的风险。
3. **X100 编译农场（2 worker 各 4 核）**：编译不在关键路径上（预编译与 A100 计时天然重叠、
   只值 1.5s），且评估侧不能落 X100——evald 整簇注册后子进程 taskset 只能是 `8-15` 的子集
   （disjoint 会 EINVAL），ADR-0012 也禁止跨簇绝对延迟对照。

结论：固定成本既不能消除也不能移出进程，**唯一出路是让两份固定成本并行发生**。

## Decision

在 A100（cpu8-15）上开 **2 条计时车道**，每条独占 4 核，配套三项改动：

1. **计时锁分区**（`evald.py: timing_lock_path`）：`max_concurrency=1` 时锁路径就是
   `cfg["lock_file"]`（与旧版逐字节一致）；`>1` 时按 worker index 分区（`<lock_file>.<index>`）。
   不分区则多 worker 仍被同一把锁串行化（旧版注释原话：“并发只影响 pickup 顺序”）。
   ADR-0002 的互斥不变量由“全局串行”收窄为“**同分区串行**”。
2. **部署路径互斥**（`evald.py: Store.path_lock`）：同一 site-packages 目标文件的
   deploy→run→restore 全程跨车道/跨 worker 互斥，**先于计时锁获取**（不为等 per-path 锁而占着
   整条车道），多文件作业按路径排序获取避免死锁。客户端 `bin/eval_pipeline.py` 同步加
   **主机感知**的 `k3_busy_paths` 门禁（correctness 走 k1-proxy、另一批机器，其 deploy_key
   不占 K3 额度）与在飞工单去重（否则 `TIMING_SLOTS` 越大越会重投同一张工单）。
3. **分核 + 限线程**：`cpu_ids="8-15"` 由 `lane_resources` 均分为 8-11 / 12-15，
   `omp_threads=4` 注入 `OMP_NUM_THREADS=4`（与 4 核对齐，防超订）。

## 实测证据（K3 板上，2026-09-09，n=5/3，作业不带 files）

| 臂 | 配置 | abs gems_ms CV | add gems_ms CV | abs speedup 中位 | 吞吐 |
|---|---|---|---|---|---|
| S（嘈杂） | 1 车道不钉核 | 23.1% | 137% | 2.45 | ~94/h |
| S-quiet | 1 车道不钉核 | 15.2% | 119% | 2.44 | ~94/h |
| P2 | 2 车道不钉核 | 19.5% | 113% | 2.56 | ~1.9× |
| **P / P3（采纳）** | **2 车道钉核 8-11/12-15 + OMP=4** | **2.05% / 1.64%** | **41.4% / 3.24%** | 3.42 / 3.45 | **~180/h** |

**2× 吞吐，同时小算子 gems_ms 的 CV 从 119% 降到 3.24%（约 37×）**：每条车道有专属核段、
torch 臂线程数被限死，不再闯进 0.9s 的测量窗口。“不钉核的双车道”（P2）没有这个收益——
两条车道 floating 在 8 核上互相撞，CV 与单车道同级。

部署路径互斥验收（`results/tools/validate_pathlock.py`，并发投同一 op 的两个不同候选，
其中一个是故意算错的 `tl.abs(x)*2.0`）：

- r1.py → `correct=True`（max_abs_err 0.0）；abs_broken_x2.py → `correct=False`（max_abs_err 5.298）
  → **correct 标志各归各位，无候选错配**
- 安装树 `flag_gems/ops/abs.py` md5 前后一致（`d03201e0…`）→ 无残留
- duration 39.1s / 78.8s → 后一个作业等了前一个的整段 deploy→restore 窗口（串行化生效）

`evald.py --selftest` 在 K3 上 **ALL PASS**，含 7 条新断言（path_lock 同路径同锁/异路径异锁、
同部署路径两作业 enter-exit 不交错、恢复后安装树无残留、单 worker 锁路径逐字节兼容、
多 worker 锁路径分区互异、跨分区可并行、同分区仍互斥）。

## Consequences

**必须遵守**

1. **speedup 不可跨本次配置变更比较**：钉核 4 核 + OMP=4 把多线程的 torch 参照臂拖慢 ~35%
   （abs torch_ms ~34ms → ~46ms），speedup 中位从 2.44 抬到 3.45（+40%），而候选代码没变快。
   **判定与搜索一律用 gems_ms**（本就是 A/B 判据口径），speedup 只在同配置内解读。
2. **gems_ms 有 ~3-5% 的系统性下移**（abs 13.86/14.17 不钉核 → 13.39~13.47 钉核），
   小于原单车道噪声（CV 15~119%）但大于新噪声（CV 1.6~3.2%）。跨 15:0x 前后比较 timing
   数字时必须标注配置轴；`Fast@1.1`（10%）阈值不受影响，但接近 3-5% 的结论要复测。
3. **手工 SSH 基准与任何抢锁的回退路径必须同时持有全部分区锁**
   （`flock <lock>.0 flock <lock>.1 …`），否则会与某条车道重叠。
   `bin/precompile.py --fallback`（默认关）持的是单锁，多 worker 部署下不要开。
4. 客户端 `TIMING_SLOTS` 需按车道数放大（`.pipeline-config.json` 的 `timing_slots`，双车道建议 4
   = 2 running + 2 queued）；已在飞的 pipeline 进程要重启才吃到新代码。

**不受影响（更正先前判断）**

5. `.incumbent-registry.json` **不需要重置**：其 106 条 `ref_ms` 全部 `host="k1-proxy"`
   （K1 池 X60 上的 correctness 车道数字），K1 池配置未变；`judge()` 只在同 host 内比较。
6. correctness 车道（acc）在 K3 上停用（`correctness_workers=0`），与分核无冲突；
   K1 池 correctness 流量不变。

**回滚**

7. `perf.perf.max_concurrency=1` → `timing_lock_path` 返回原路径，锁语义与旧版逐字节一致，
   **零代码回滚**；分核/OMP 也随 `cpu_ids`/`omp_threads` 移除而失效。
8. 板上备份：`evald.py.bak-pre-pathlock-20260909`（= `047365c8…`）、
   `evald.py.bak-pre-duallane-20260909`（= `aeed0769…`，升级前生产版）、
   `config.json.bak-pre-duallane-20260909`。

## 待办（本 ADR 未覆盖）

- **torch 臂外提**：每作业都测 torch 参照，但判定只用 gems_ms；小算子只值 ~1%，
  慢算子（框架兜底类 torch_ms 100~400ms）值 1.5~6s/作业。改为“一次性 torch 参照表 +
  每作业只测 gems”需要动 `bench_op.py`（加 `--skip-torch`）、evald 结果契约
  （`torch_ms`/`speedup` 变 null）与下游读法，属口径变更，另议。
- **跨 op 批量作业**：摊薄 16s 固定成本的另一条路；baseline 类作业（无候选文件）最安全，
  候选作业需评估 deploy 语义与失败连坐。
- **PSR 字段缺失**：`evald_psr` histogram 与 PSR≥8 验收字段依赖 bench 侧输出 `result.psr`，
  当前 `bench_op.py` 不产出（实测 `psr: null`），验收证据链有缺口。
