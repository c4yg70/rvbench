# K3 评估服务器（evald）设计定稿

状态：2026-09-07 grilling session 定稿，待实现。术语见 [CONTEXT.md](../CONTEXT.md)，决策见 [docs/adr/](./adr/)。

## 目标

- **主**：异步作业队列取代手工 flock 约定——排队由机器保证、状态可见（queued/running/done/failed/timeout/canceled）
- **副**：HTTP 直达（免 k3ssh 往返）、结果落盘可查、响应字段兼容 kope-mem K3BenchmarkPort（P4 铺路）

## 架构

- 板上常驻服务 `evald`：stdlib `ThreadingHTTPServer`，跑系统 python3（3.14.4，原生 uuid7）
- **单 worker 串行执行**（计时纯净性）；worker 用 `fcntl.flock` 持有 `/tmp/k3_bench.lock`，与手工 SSH 基准互斥（ADR-0002）
- **执行器可替换**：v1 `SubprocessExecutor`（venv python 跑 bench_op.py / bench_gemm.py，每作业 ~20s 启动，与现状等速）；v1.1 `WarmForkExecutor`（常驻预热 import torch+flag_gems，fork 子进程执行，子进程内 reload 候选模块，崩溃隔离、无模块残留）
- 板上目录 `~/autoke/evald/`：`evald.py`、`config.json`（token 等）、`jobs.jsonl`（追加式历史）、`jobs/<job_id>/{request.json, stdout.log, stderr.log, result.json, backup/}`

## API（端口 8788，绑定 0.0.0.0，请求头 `X-K3-Token`）

| 端点 | 说明 |
|---|---|
| `POST /jobs` | 提交作业 → `202 {job_id, state:"queued"}`；队列 >32 返回 429 |
| `GET /jobs/{id}?wait=N` | 查询作业；`wait≤60s` 长轮询，完成即返回 |
| `GET /jobs?limit=N&since=<uuid7>` | 历史列表，UUIDv7 时间序游标 |
| `DELETE /jobs/{id}` | 仅排队中可取消；运行中不提供强杀 |

**POST /jobs 请求体**：

```json
{
  "op": "add", "shape": "64,512,512", "dtype": "float32",
  "warmup": 10, "rep": 50, "bench": "op | gemm",
  "restore": true,
  "files": [{"path": "flag_gems/ops/add.py", "content": "..."}]
}
```

**结果契约**：

```json
{
  "job_id": "<uuid7>",
  "state": "queued | running | done | failed | timeout | canceled",
  "submitted_at": "...", "started_at": "...", "finished_at": "...", "queue_wait_s": 0.0,
  "request": {"op": "...", "shape": "...", "dtype": "...", "warmup": 10, "rep": 50,
               "restore": true, "files": [{"path": "...", "sha256": "..."}]},
  "result": {"torch_ms": 0.0, "gems_ms": 0.0, "speedup": 0.0, "correct": true,
              "max_abs_err": 0.0, "metric": "speedup | gflops"},
  "kope_mem": {"speedup": 0.0, "pass_rate": 1.0},
  "logs": {"stdout_tail": "...4KB...", "stderr_tail": "...4KB..."}
}
```

stdout/stderr 全量落盘每作业目录，API 响应只带尾部 4KB。

## 部署-恢复（ADR-0001）

- **路径白名单**：`flag_gems` 包内 `ops/`、`runtime/backend/_spacemit/**`、配置（`tune_configs.yaml`、`heuristics_config_utils.py`），及 `~/autoke/scripts/` 下 `bench_op.py`、`bench_gemm.py`；白名单外一律 400 拒收
- 写入流程：`rm` 断硬链 → 原文件备份到 `jobs/<id>/backup/` → 写候选 → 评估 → `restore:true` 则恢复（恢复同样先 rm 断链）
- `restore: false` = 定稿作业，候选代码留在 site-packages

## 队列与超时

- 队列上限 32，超出 429 拒收
- 单作业硬超时默认 15 分钟（提交可调，绝对上限 30 分钟）：超时杀子进程、标 `timeout`、照常恢复文件

## 评估能力

- v1：bench_op.py 微基准 + bench_gemm.py（GFLOPS 口径）
- pytest 全量验收仍走 SSH（低频长任务，不占队列），接口留扩展位

## 运维

- 守护：setsid+nohup+pidfile（无交互终端依赖，适合 @reboot）+ 用户级 crontab `@reboot` 自启
- 日志按大小截断

## 里程碑

- **M1**：evald.py + SubprocessExecutor + 部署-恢复 + 队列/超时/429；用已归档 4 算子（argmax/sum/abs/add）复测对齐既有结果
- **M2**：runbook curl 配方（提交/长轮询/历史）+ 子智能体任务书模板改用 API + crontab 自启
- **M3** ✅ WarmForkExecutor（warmd 预热守护；实测 20.2s→15.1s；只预热 torch+triton、子进程全新加载 flag_gems，见 ADR-0004）

## Backlog（有意不做）

- **预编译缓存包上传 / 编译-评估隔离**：板上 triton wheel 为 `cp312-cp312-linux_riscv64` 专属（x86 原生不可行，仅 qemu-riscv64 仿真一条路）；实测单 kernel 编译仅 ~0.4s、整 bench 编译开销 ~5s，而 python+torch 启动固定 ~20s 才是大头——缓存上传解决不了启动。仅当出现"数百 kernel × 多 shape 大批量首跑"场景再重启调研。files 契约保持通用列表，天然可承载未来 `cache_bundle` 类型。
