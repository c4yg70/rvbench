# riscv-flagos-kernel-eval-server (evald)

部署在 SpacemiT K3（RISC-V, Bianbu Linux）上的**算子代码评估服务器**：本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业，异步等待并获取计时结果——取代逐次 `ssh + flock` 的工作流。

- **服务** `evald.py` + 预热执行守护 `warmd.py`，纯 Python stdlib（板上 FlagGems venv 无 pip，见 [ADR-0003](docs/adr/0003-stdlib-python-eval-server.md)）
- **WarmForkExecutor**（默认）：守护父进程只预热 torch+triton（不变量），每作业 fork 子进程全新加载 flag_gems——省 ~5s/作业且候选代码永远从磁盘部署文件读取（[ADR-0004](docs/adr/0004-warmfork-invariant-preload.md)）；作业可用 `"executor":"subprocess"` 显式回退
- **异步队列·双车道**：timing 车道（默认）单 worker 串行执行（计时纯净）；correctness 车道
  3 worker 并行（`correctness_workers`，0=关闭）——不持计时锁、强制 warmup=0/rep=1、taskset
  到车道核（handoff §3-L2）。队列按 lane 分列，上限 64 两车道共享，超时杀进程
- **部署-恢复自包含**：每个作业评估前备份 site-packages 原文件、断硬链写入候选，评估后自动恢复——杜绝异步队列下"测到上一个作业残留代码"的静默错误（[ADR-0001](docs/adr/0001-eval-job-deploy-restore.md)）
- **计时互斥**：worker 持有 `/tmp/k3_bench.lock`，与手工 SSH 基准互不污染（[ADR-0002](docs/adr/0002-shared-timing-mutex.md)）
- **kope-mem 兼容**：响应自带 `kope_mem.speedup / pass_rate`，可直接喂给 K3BenchmarkPort

设计规格：[docs/eval-server-design.md](docs/eval-server-design.md)；术语：[CONTEXT.md](CONTEXT.md)。

## 部署（板上一次性）

```bash
# 本地推送（Windows 工作区）
k3scp evald.py warmd.py Home-SpacemitK3:~/autoke/evald/
k3scp evaldctl.sh Home-SpacemitK3:~/autoke/evald/

# 板上
mkdir -p ~/autoke/evald && cd ~/autoke/evald
cp config.example.json config.json   # 或 scp 一份已生成的
python3 evald.py --gen-token         # 把输出填进 config.json 的 token
python3 evald.py --selftest --config config.json
bash evaldctl.sh start               # setsid+nohup 守护（无交互终端依赖，适合 @reboot）
crontab -e                           # @reboot cd /home/c4y/autoke/evald && bash evaldctl.sh start
```

> **更名注记（2026-09-09）**：本仓库与 K1-230 板上 `/home/c4y/autoke/evald/evaldctl` 已更名为 `evaldctl.sh`（230 的 c4y crontab `@reboot` 行已同步为 `evaldctl.sh start`）。K3（Home-SpacemitK3）与 233/241/243 板上现存副本**仍叫旧名 `evaldctl`**；同步新名时须同时修改各板 crontab `@reboot` 引用。

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
- **backlog**：预编译缓存包上传（qemu-riscv64 交叉编译路径，依据见设计文档 Backlog 节）；进一步压缩子进程 flag_gems 导入（~6.8s，弥散性开销）
