# riscv-flagos-kernel-eval-server (evald)

部署在 SpacemiT K3（RISC-V, Bianbu Linux）上的**算子代码评估服务器**：本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业，异步等待并获取计时结果——取代逐次 `ssh + flock` 的工作流。

- **服务** `evald.py` + 预热执行守护 `warmd.py`，纯 Python stdlib（板上 FlagGems venv 无 pip，见 [ADR-0003](docs/adr/0003-stdlib-python-eval-server.md)）
- **WarmForkExecutor**（默认）：守护父进程只预热 torch+triton（不变量），每作业 fork 子进程全新加载 flag_gems——省 ~5s/作业且候选代码永远从磁盘部署文件读取（[ADR-0004](docs/adr/0004-warmfork-invariant-preload.md)）；作业可用 `"executor":"subprocess"` 显式回退
- **异步队列·多车道**：timing 车道（默认）缺省单 worker 串行（计时纯净），可用
  `perf.perf.max_concurrency>1` 开**多计时车道**（配 `cpu_ids` 按 worker 均分核段、`omp_threads`
  限线程）——K3 生产为 2 车道 × 4 核（8-11 / 12-15），实测 2.0× 吞吐且 gems_ms CV 从 119% 降到
  3.2%（[ADR-0014](docs/adr/0014-dual-timing-lanes-a100.md)）；correctness 车道
  3 worker 并行（`correctness_workers`，0=关闭）——不持计时锁、强制 warmup=0/rep=1、taskset
  到车道核（handoff §3-L2）。队列按 lane 分列，上限 64 两车道共享，超时杀进程
- **部署-恢复自包含**：每个作业评估前备份 site-packages 原文件、断硬链写入候选，评估后自动恢复——杜绝异步队列下"测到上一个作业残留代码"的静默错误（[ADR-0001](docs/adr/0001-eval-job-deploy-restore.md)）。
  **同一部署路径的作业跨车道/跨 worker 互斥**（`Store.path_lock`，deploy→run→restore 全程持锁）：
  多车道下两个作业写同一 `flag_gems/ops/<op>.py` 会错配候选，服务端强制串行化（ADR-0014）
- **计时互斥**：worker 持有分区计时锁（`timing_lock_path`：单 worker = `/tmp/k3_bench.lock`，
  多 worker = `/tmp/k3_bench.lock.<index>`），与手工 SSH 基准互不污染
  （[ADR-0002](docs/adr/0002-shared-timing-mutex.md)）。**多 worker 部署下，手工基准与任何
  抢锁的回退路径必须同时持有全部分区锁**（`flock <lock>.0 flock <lock>.1 …`）
- **kope-mem 兼容**：响应自带 `kope_mem.speedup / pass_rate`，可直接喂给 K3BenchmarkPort

设计规格：[docs/eval-server-design.md](docs/eval-server-design.md)；
**架构总览（自包含 HTML，含拓扑/生命周期/车道/不变量/审计发现）**：[docs/architecture.html](docs/architecture.html)；
监控改造规格（待审批，双板拓扑）：[docs/specs/2026-09-20-监控栈改造.md](docs/specs/2026-09-20-监控栈改造.md)
＋目标蓝图 [docs/architecture-dev.html](docs/architecture-dev.html)；
接力链设计：[docs/relay-chain-design.md](docs/relay-chain-design.md)；术语：[CONTEXT.md](CONTEXT.md)；
变更史：[CHANGELOG.md](CHANGELOG.md)。

## 仓库结构（2026-09-20 收拢，spec-20260920）

本仓库是 evald **服务端与全部配套代码的单一正本**，所有入口都在仓内直接运行
（如 `python clients/ev.py status`）。工作区 `bin/` 下的旧同名入口与兼容垫片已于
2026-09-20 二次整理中退役——仓库不依赖任何仓外目录名，也不假定检出目录叫什么。

| 分区 | 内容 | 板上对应 |
|---|---|---|
| 根目录 | `evald.py` / `warmd.py` / `evaldctl.sh` / `config*.json` / `dashboard.html` / `monitor/` —— 服务端部署单元与监控 | `~/autoke/evald/`（push 清单见下） |
| [clients/](clients/) | Windows 侧 evald 客户端与战役驱动：`ev.py`（交互单评）、`eval_pipeline.py`（批量管线，judge/registry 正本）、`op_status.py`、`recover_inflight.py` | `ev.py` 另有板上副本 `~/autoke/evald/ev.py` |
| [clients/archive/](clients/archive/) | 历史一次性基线工具（run_recall / run_baseline_group / run_verify43_rerun / run_gemm_panel ＋ baseline 数据文件），路径常量已随迁修指 | — |
| [board/](board/) | 板端配套脚本（evald 文件白名单契约的一部分）：`bench_op.py`（计时三层分发）、`bench_gemm.py`、`torch_reference.py`、`fallback_refs.py`（数值判定正本） | `~/autoke/scripts/` |
| [ops/](ops/) | 部署与运维：`pssh.py`（SSH 通道）、`evald_selftest_local.py`、`precompile.py` + `precompile_candidate.sh`（⚠ 传输仍为已废弃的 k3ssh，复用前须迁 pssh） | — |
| [tools/](tools/) | 本地校验：`trace_check.py` ＋ `trace_fixtures/`（T3 harness-trace 落盘校验） | — |
| [docs/](docs/) | ADR（索引在 [docs/adr/](docs/adr/)）、总体设计、优化设计稿、接力链设计、收拢 spec、[specs/](docs/specs/)（待审批规格：监控栈改造）、[architecture.html](docs/architecture.html)（现状总览）、[architecture-dev.html](docs/architecture-dev.html)（目标蓝图·双板监控） | — |

**运行前置（Windows 侧客户端）**：本仓已独立于战役工作区，`clients/*` 用环境变量
`EV_WORKSPACE=<战役工作区根>`（应含 `runs/`、`results/`）定位战役状态；未设时回退为仓的
父目录，解析不到即以具名错误退出（不静默跑错目录）。例：
`EV_WORKSPACE=<工作区根> python clients/op_status.py`。

**部署不变式**：`evald.py`/`warmd.py`/`evaldctl.sh`/`config.example.json`/`dashboard.html`/
`monitor/*` 与板上部署保持**逐字节一致**（md5 见 CHANGELOG），改动它们须走板上 selftest 门禁。

## 部署（板上一次性）

```bash
# 本地推送（在仓库根执行）——k3ssh / k3scp 已废弃，一律走 pssh.py
# （--put 按第一个冒号切分，Windows 盘符路径会被切碎 → 先 cd 到文件所在目录再传相对名）
cd <仓库根>
python ops/pssh.py Home-SpacemitK3 --put "evald.py:/home/c4y/autoke/evald/evald.py"
python ops/pssh.py Home-SpacemitK3 --put "warmd.py:/home/c4y/autoke/evald/warmd.py"
python ops/pssh.py Home-SpacemitK3 --put "evaldctl.sh:/home/c4y/autoke/evald/evaldctl.sh"
python ops/pssh.py Home-SpacemitK3 --put "clients/ev.py:/home/c4y/autoke/evald/ev.py"   # 直连客户端板上副本：探活/金丝雀；--judge 需仓库侧

# 板上
mkdir -p ~/autoke/evald && cd ~/autoke/evald
cp config.example.json config.json   # 或 scp 一份已生成的
python3 evald.py --gen-token         # 把输出填进 config.json 的 token
python3 evald.py --selftest --config config.json
bash evaldctl.sh start               # setsid+nohup 守护（无交互终端依赖，适合 @reboot）
# K3（A100 测量簇）必须用 start-ai：execve 前写 /proc/set_ai_thread 注册，整服务绑 cpu8-15；
# 用普通 start 会落 X100 → 计时作业 PSR<8（infra 失败）。@reboot 也要写 start-ai。
crontab -e                           # @reboot cd /home/c4y/autoke/evald && bash evaldctl.sh start-ai
```

## 板恢复后回归门（2026-09-20 收拢的欠账）

本次收拢为纯本地静态整理（板不可达期间），板恢复后按序补：

**0. 先验：安装树未被污染（未过此门前的任何跑分都不可信）**

服务重启路径只把被打断的作业标 `failed`（`Store._sweep_stale`），**不会**恢复
site-packages。SIGTERM 落在 deploy→restore 之间时，候选代码会留在安装树里——正是
ADR-0001 要消灭的"测到上一个作业残留代码"。板恢复后先做：

```bash
cd <板上 data_dir>          # config.json 的 data_dir

# a) 列出"部署过但未恢复"的条目：manifest 存在、条目缺 restored_sha256（单行 JSON，逐条判）
python3 - <<'PY'
import json, glob
for mp in sorted(glob.glob("jobs/*/manifest.json")):
    for e in json.load(open(mp)):
        if "restored_sha256" not in e:
            print(mp, "existed=%s" % e["existed"], e["path"])
PY

# b) 命中即说明该槽位可能仍是候选代码：比对 abs 当前 sha256 与 orig_sha256 是否相符
#    需还原时用同作业备份 jobs/<job_id>/backup/<相对路径> 手工拷回
# c) 清 deploy 中途被打断留下的暂存文件（<白名单路径>.<job_id>.tmp）
```

口径：`jobs/<job_id>/manifest.json` 每条含 `path/abs/backup/existed/orig_sha256/
deployed_sha256/restored_sha256`；`restored_sha256` 由 `restore_files` 恢复后回写，
**缺该键 = 该条目未恢复**。（根治＝让 `_sweep_stale` 启动时按 manifest 补 restore，
属 `evald.py` 改动，须走本门禁。）

1. 板上 `md5sum` evald.py / warmd.py 对仓库（应仍逐字节一致——本次未改它们）。
2. 金丝雀：板上 `ev.py run --op add --shape 64,512,512` ＋ Windows 侧 `python clients/ev.py status`（验 token 链）。
3. `evald.py --selftest`（板上最小 config 口径，234 PASS 基线）。
4. 重推 `clients/ev.py` → 板上 `~/autoke/evald/ev.py`（板侧现为收拢前旧副本）。
5. `eval_pipeline` 以一个 dry 工单跑半轮，确认 ROOT/registry 落点无漂移。

## API

端口 8788；除 `/healthz` 外都需请求头 `X-K3-Token`。

| 端点 | 说明 |
|---|---|
| `POST /jobs` | 提交作业 → `202 {job_id, state, position}`；队列满 429 |
| `GET /jobs/{id}?wait=N` | 查询；`wait≤60` 长轮询，完成即返回 |
| `GET /jobs?limit=N&since=<job_id>` | 历史（UUIDv7 时间序游标） |
| `DELETE /jobs/{id}` | 取消排队中的作业 |
| `GET /healthz` | 存活 + 队列长度（顶层为两车道总和）+ 每车道 `lanes` 计数 + loadavg（免 token） |

### 请求体

```json
{
  "op": "add", "shape": "64,512,512", "dtype": "float32",
  "warmup": 5, "rep": 5, "bench": "op",
  "restore": true, "timeout_min": 15,
  "lane": "correctness",
  "files": [{"path": "flag_gems/ops/add.py", "content": "..."}]
}
```

- `bench`: `"op"`（bench_op.py 微基准）| `"gemm"`（bench_gemm.py，op 为 mm/addmm/bmm，shape 为逗号分隔的方阵边长列表，可选 `batch`）
- `lane`: `"timing"`（默认）| `"correctness"`（L2 并行车道）。**不带 lane 字段 = timing，行为与旧版逐字节一致**。correctness 车道：不获取 `/tmp/k3_bench.lock`、强制 `warmup=0/rep=1`（用户值被覆盖并在 request 回显为 `requested_warmup/requested_rep`）、`K3_LANE=correctness`＋`OMP_NUM_THREADS=1` 注入 bench 环境、taskset 到 `correctness_cpus`（默认 4-6，0-7 内）。仅正确性筛选用——`result.correct` 有效，`speedup` 无统计意义（结果带 `lane` 标注）
- `files`: 可省略（评估当前已部署代码）；路径白名单：`flag_gems/ops/**`、`flag_gems/runtime/backend/_spacemit/**`、`scripts/bench_op.py`、`scripts/bench_gemm.py`
- `restore: false` = **定稿作业**：测完保留候选代码于 site-packages

### 响应（终态作业）

```json
{
  "job_id": "0190…", "state": "done",
  "submitted_at": "…", "started_at": "…", "finished_at": "…",
  "queue_wait_s": 3.2, "duration_s": 28.1,
  "request": {"op": "add", "shape": "64,512,512", "dtype": "float32",
               "warmup": 5, "rep": 5, "bench": "op", "restore": true,
               "files": [{"path": "flag_gems/ops/add.py", "sha256": "…", "bytes": 1234}]},
  "result": {"torch_ms": 5.1, "gems_ms": 5.4, "speedup": 0.94,
              "correct": true, "max_abs_err": 0.0, "metric": "speedup"},
  "kope_mem": {"speedup": 0.94, "pass_rate": 1.0},
  "logs": {"stdout_tail": "…", "stderr_tail": "…"}
}
```

全量 stdout/stderr 落盘在板上 `~/autoke/evald/jobs/<job_id>/`，API 只带尾部 4KB。gemm 多 shape 时 `result` 为 `{metric: "gflops", rows: [...]}`。

## 评估器 feature flags（perf/acc/func）

config.json 新增顶层 `evaluators` 节，把三个评估器做成独立开关（契约定稿，代码实现中）：

```json
"evaluators": {
  "perf": {"enabled": true, "warmup_times": 5, "repeat_times": 5},
  "acc":  {"enabled": true, "workers": 4, "cpus": "4-7", "omp_threads": 1},
  "func": {"enabled": true, "workers": 2, "cpus": "4-7", "omp_threads": 1}
}
```

### 三个评估器与车道映射

| 评估器 | 车道 | 执行模型 | 说明 |
|---|---|---|---|
| `perf` | timing | 单 worker 串行、持计时锁 | 计时评估器，语义与旧版不变 |
| `acc` | correctness | 并行池、不持锁 | 数值精度评估器 |
| `func` | func | 并行池、不持锁 | 功能冒烟评估器：执行成功——stdout 出现 JSON 结果行即 pass；结果带 `lane="func"`，`kope_mem` 只有 `pass_rate` 无 `speedup` |

### 开关与校验（启动即报错）

- 三者独立开关，**至少启用一个**；全停则启动报错（`SystemExit`）。
- 未知评估器名、或节内未知键，同样启动报错。
- `force_lane` 必须指向**启用中**的评估器，否则启动报错。

### 缺省推导（config 无 `evaluators` 键时保持旧版行为）

- `perf` 开；
- `acc` 随旧扁平键 `correctness_workers`（0 即关）；
- `func` 关。

### 计时轮数与强制覆盖

- `warmup_times` / `repeat_times` 是 **perf** 的计时轮数配置（缺省 25/100）；作业请求体 `warmup` / `rep` 仍可逐作业覆盖。
- acc / func 两个设置里**恒定强制 `warmup=0`、`rep=1`**：配置写别的值（如 `acc.warmup_times=5`）启动直接报错拒绝；请求体的 `warmup` / `rep` 被覆盖为 0/1，并在 sanitized request 里注记 `requested_warmup` / `requested_rep`。

### 请求车道门禁

- `lane ∈ timing|correctness|func` 分别对应 `perf|acc|func`；对应评估器停用时提交直接 **400**，错误信息注明 `config.evaluators.<name>.enabled=false`。
- **先门禁、后 `force_lane` 改写**。

### 资源键与 func 环境

- `acc` 的 `workers` / `cpus` / `omp_threads` 缺省回落旧扁平键 `correctness_workers` / `correctness_cpus` / `correctness_omp_threads`。
- `func` 的 `workers` 缺省 2；`cpus` / `omp_threads` 缺省随 acc 生效值。
- func 车道进程 env 注入 `K3_LANE=func`，taskset 钉 `func.cpus`。

### 可观测性

- `/healthz` 与 `/metrics`：车道计数新增 `lane="func"` 序列（`evald_queued` / `evald_running`）。
- 启动横幅打印启用的评估器与各车道 worker 数。

### 队列容量指标（调度器算「空位」用）

外部调度器（如 eval-proxy）可只抓 `/metrics` 完成存活 + 占用 + 容量三件事，无需再读 `/healthz`：

| 指标 | 类型 | 语义 |
|---|---|---|
| `evald_up` | gauge | 恒 1（进程活着才答）→ 存活判定 |
| `evald_queued{lane}` / `evald_running{lane}` | gauge | 每车道排队/运行数 → 占用 |
| `evald_queue_max` | gauge | **全局队列容量**（= `cfg.queue_max`，跨车道总和上限；达此值 submit 返回 429），恒输出 |
| `evald_lane_queue_max{lane}` | gauge | 车道级容量覆盖（= `perf.<lane>.max_queue`）；**只对真配了上限的车道输出**，未配的车道不出序列（调度端不要把缺失误读为 0 容量，应回退到 `evald_queue_max` 或自己的缺省值） |

占用建议取 `max(后端 queued+running, 调度器自身对该板在飞数)`，空位 = 容量 - 占用。

## 资源调度字段 `perf`（可选）

顶层 `perf` 是 perf/acc/func 三评估器的**资源与调度覆盖层**：分别配置每个评估器分配的核数、核 id、最大并发数、最大队列深度等。**字段本身与其中每一项都是可选的**——缺省时完全回落到 `evaluators` 与旧扁平键（`correctness_*`）的既有行为，逐字节兼容。

值可以是 JSON 对象，**也可以是其字符串形式（json 字符串）**，后者启动时自动 `json.loads`：

```json
"perf": {
  "perf": {"max_concurrency": 1, "max_queue": 32},
  "acc":  {"cores": 4, "cpu_ids": "4-7", "max_concurrency": 4, "max_queue": 32, "omp_threads": 1, "timeout_min": 10},
  "func": {"cores": 2, "cpu_ids": "4-5", "max_concurrency": 2, "max_queue": 16, "executor": "subprocess"}
}
```

### 子字段（每个评估器节内，全部可选）

| 键 | 类型 | 含义 | 归并去向 |
|---|---|---|---|
| `cpu_ids` | 字符串或 int 列表 | 精确核 id：taskset 规格（`"12-14"` / `"0,2,4"` / `"0-3,8"`）或列表（`[12,13,14]`） | `evaluators.<n>.cpus` |
| `cores` | int ≥1 | 分配核数。与 `cpu_ids` 同给时**数目必须一致**（否则启动报错）；单独给时在**既有核池**（`evaluators`/旧扁平键的 `cpus`）上取前 N 个裁剪，无核池则报错 | `evaluators.<n>.cpus` |
| `max_concurrency` | int ≥1 | 该车道最大并发 worker 数 | `evaluators.<n>.workers` |
| `max_queue` | int ≥1 | 该车道**额外**的排队深度上限；与全局 `queue_max` 双闸，任一触顶即 429。缺省（不设）= 无车道级上限，只受全局 `queue_max` | `lane_queue_max[<lane>]` |
| `omp_threads` | int ≥1 | 注入 bench 进程的 `OMP_NUM_THREADS` | `evaluators.<n>.omp_threads` |
| `executor` | `subprocess`｜`warmfork` | 该车道默认执行器（请求体 `executor` 仍可逐作业覆盖） | `evaluators.<n>.executor` |
| `timeout_min` | 0.1..`timeout_max_min` | 该车道默认墙钟超时（请求体 `timeout_min` 仍可逐作业覆盖） | `evaluators.<n>.timeout_min` |

### 优先级与语义

- **优先级**：`perf` 字段 > `evaluators.<n>` > 旧扁平键（`correctness_workers/cpus/omp_threads`）> 硬缺省。归并后下游（`lane_resources` / `Worker` / `Store` / `validate_request`）只读归并结果。
- **车道映射**：`perf`→`timing`、`acc`→`correctness`、`func`→`func`。
- **分核**：归并出的 `cpus` 仍按既有机制在各 worker 间均分（`lane_resources(cfg, lane, index, total)`，`total>1` 时每 worker 独占一段核；核数不足以切分时回落整段共享）。
- **timing（perf）车道**：默认不钉核、不注 OMP（与旧版逐字节一致）。仅当显式给 `perf.perf.cpu_ids` / `omp_threads` 时才 opt-in 钉核/注 OMP；`K3_LANE` 始终不注入 timing。`max_concurrency>1` 时计时锁**按 worker 分区**（`timing_lock_path` = `/tmp/k3_bench.lock.<index>`），各车道独立计时、互不排队（ADR-0014）；同一部署路径的作业另由 `Store.path_lock` 串行化。
- **校验**：未知顶层键、未知子字段键、非法 `cpu_ids`、`cores` 与 `cpu_ids` 数目不一致、`cores` 超核池、`max_concurrency/max_queue/omp_threads < 1`、非法 `executor`、`timeout_min` 越界——一律启动即 `SystemExit`。

### 与 `evaluators` 的分工

`evaluators.<n>.enabled` 与 `warmup_times/repeat_times` 仍是评估器开关与计时轮数的正本；`perf` 字段只覆盖**资源与调度**（核/并发/队列/omp/执行器/超时），不改开关与轮数。两者可并存，`perf` 字段就资源键覆盖 `evaluators`。

## curl 配方（Git Bash）

```bash
TOKEN=… BASE=http://10.18.60.100:8788
H="X-K3-Token: $TOKEN"

# 提交（无文件，评估当前代码）
curl -s -X POST $BASE/jobs -H "$H" -H "Content-Type: application/json" \
  -d '{"op":"add","shape":"64,512,512","dtype":"float32","warmup":5,"rep":5}'

# 提交（带候选文件；jq 把整文件内容装进 JSON）
jq -n --rawfile f add.py '{op:"add",shape:"64,512,512",dtype:"float32",
    warmup:5,rep:5,files:[{path:"flag_gems/ops/add.py",content:$f}]}' \
  | curl -s -X POST $BASE/jobs -H "$H" -H "Content-Type: application/json" -d @-

# 提交（correctness 车道：不持计时锁、warmup=0/rep=1 强制、3 worker 并行；只看 correct）
curl -s -X POST $BASE/jobs -H "$H" -H "Content-Type: application/json" \
  -d '{"op":"add","shape":"64,512,512","dtype":"float32","warmup":5,"rep":5,"lane":"correctness"}'

# 长轮询等结果（≤60s 一轮，未终态就再来一次）
curl -s "$BASE/jobs/<job_id>?wait=60" -H "$H"

# 历史 / 取消
curl -s "$BASE/jobs?limit=50" -H "$H"
curl -s -X DELETE "$BASE/jobs/<job_id>" -H "$H"
```

## Roadmap

- **M1** ✅ evald.py + 子进程执行器 + 部署-恢复 + 队列/超时/429
- **M2** ✅ runbook 配方 + crontab 自启
- **M3** ✅ WarmForkExecutor（warmd 预热守护 + fork 子进程全新加载；实测同作业 20.2s→15.1s，A/B 交叉验证正确性一致）
- **2026-09-20** ✅ 配套代码收拢（clients/board/ops/tools 分区，spec-20260920；见 CHANGELOG）
- **2026-09-20（二次）** ✅ bin/ 垫片退场＋遗留 bin/ 死引用清理＋冻结物双 md5 登记（见 CHANGELOG）
- **2026-09-20（三次）** 📋 监控改造 spec 改址重写为双板拓扑（`docs/specs/2026-09-20-监控栈改造.md`，待审批）
- **backlog**：预编译缓存包上传（qemu-riscv64 交叉编译路径，依据见设计文档 Backlog 节）；进一步压缩子进程 flag_gems 导入（~6.8s，弥散性开销）；uv 打包 + cnb CI（`.qoder` c36 规格，须先过板上 selftest 门禁）
