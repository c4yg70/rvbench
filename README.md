# riscv-flagos-kernel-eval-server (evald)

部署在 SpacemiT K3（RISC-V, Bianbu Linux）上的**算子代码评估服务器**：本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业，异步等待并获取计时结果——取代逐次 `ssh + flock` 的工作流。

- **单文件服务** `evald.py`，纯 Python stdlib（板上 FlagGems venv 无 pip，见 [ADR-0003](docs/adr/0003-stdlib-python-eval-server.md)）
- **异步队列**：单 worker 串行执行（计时纯净），队列上限 32，超时杀进程
- **部署-恢复自包含**：每个作业评估前备份 site-packages 原文件、断硬链写入候选，评估后自动恢复——杜绝异步队列下"测到上一个作业残留代码"的静默错误（[ADR-0001](docs/adr/0001-eval-job-deploy-restore.md)）
- **计时互斥**：worker 持有 `/tmp/k3_bench.lock`，与手工 SSH 基准互不污染（[ADR-0002](docs/adr/0002-shared-timing-mutex.md)）
- **kope-mem 兼容**：响应自带 `kope_mem.speedup / pass_rate`，可直接喂给 K3BenchmarkPort

设计规格：[docs/eval-server-design.md](docs/eval-server-design.md)；术语：[CONTEXT.md](CONTEXT.md)。

## 部署（板上一次性）

```bash
# 本地推送（Windows 工作区）
k3scp evald.py Home-SpacemitK3:~/autoke/evald/
k3scp evaldctl Home-SpacemitK3:~/autoke/evald/

# 板上
mkdir -p ~/autoke/evald && cd ~/autoke/evald
cp config.example.json config.json   # 或 scp 一份已生成的
python3 evald.py --gen-token         # 把输出填进 config.json 的 token
python3 evald.py --selftest --config config.json
bash evaldctl start                  # setsid+nohup 守护（无交互终端依赖，适合 @reboot）
crontab -e                           # @reboot cd /home/c4y/autoke/evald && bash evaldctl start
```

## API

端口 8788；除 `/healthz` 外都需请求头 `X-K3-Token`。

| 端点 | 说明 |
|---|---|
| `POST /jobs` | 提交作业 → `202 {job_id, state, position}`；队列满 429 |
| `GET /jobs/{id}?wait=N` | 查询；`wait≤60` 长轮询，完成即返回 |
| `GET /jobs?limit=N&since=<job_id>` | 历史（UUIDv7 时间序游标） |
| `DELETE /jobs/{id}` | 取消排队中的作业 |
| `GET /healthz` | 存活 + 队列长度 + loadavg（免 token） |

### 请求体

```json
{
  "op": "add", "shape": "64,512,512", "dtype": "float32",
  "warmup": 10, "rep": 50, "bench": "op",
  "restore": true, "timeout_min": 15,
  "files": [{"path": "flag_gems/ops/add.py", "content": "..."}]
}
```

- `bench`: `"op"`（bench_op.py 微基准）| `"gemm"`（bench_gemm.py，op 为 mm/addmm/bmm，shape 为逗号分隔的方阵边长列表，可选 `batch`）
- `files`: 可省略（评估当前已部署代码）；路径白名单：`flag_gems/ops/**`、`flag_gems/runtime/backend/_spacemit/**`、`scripts/bench_op.py`、`scripts/bench_gemm.py`
- `restore: false` = **定稿作业**：测完保留候选代码于 site-packages

### 响应（终态作业）

```json
{
  "job_id": "0190…", "state": "done",
  "submitted_at": "…", "started_at": "…", "finished_at": "…",
  "queue_wait_s": 3.2, "duration_s": 28.1,
  "request": {"op": "add", "shape": "64,512,512", "dtype": "float32",
               "warmup": 10, "rep": 50, "bench": "op", "restore": true,
               "files": [{"path": "flag_gems/ops/add.py", "sha256": "…", "bytes": 1234}]},
  "result": {"torch_ms": 5.1, "gems_ms": 5.4, "speedup": 0.94,
              "correct": true, "max_abs_err": 0.0, "metric": "speedup"},
  "kope_mem": {"speedup": 0.94, "pass_rate": 1.0},
  "logs": {"stdout_tail": "…", "stderr_tail": "…"}
}
```

全量 stdout/stderr 落盘在板上 `~/autoke/evald/jobs/<job_id>/`，API 只带尾部 4KB。gemm 多 shape 时 `result` 为 `{metric: "gflops", rows: [...]}`。

## curl 配方（Git Bash）

```bash
TOKEN=… BASE=http://10.18.60.100:8788
H="X-K3-Token: $TOKEN"

# 提交（无文件，评估当前代码）
curl -s -X POST $BASE/jobs -H "$H" -H "Content-Type: application/json" \
  -d '{"op":"add","shape":"64,512,512","dtype":"float32","warmup":10,"rep":50}'

# 提交（带候选文件；jq 把整文件内容装进 JSON）
jq -n --rawfile f add.py '{op:"add",shape:"64,512,512",dtype:"float32",
    warmup:10,rep:50,files:[{path:"flag_gems/ops/add.py",content:$f}]}' \
  | curl -s -X POST $BASE/jobs -H "$H" -H "Content-Type: application/json" -d @-

# 长轮询等结果（≤60s 一轮，未终态就再来一次）
curl -s "$BASE/jobs/<job_id>?wait=60" -H "$H"

# 历史 / 取消
curl -s "$BASE/jobs?limit=50" -H "$H"
curl -s -X DELETE "$BASE/jobs/<job_id>" -H "$H"
```

## Roadmap

- **M1** ✅ evald.py + 子进程执行器 + 部署-恢复 + 队列/超时/429
- **M2** ✅ runbook 配方 + crontab 自启
- **M3 (v1.1)**：WarmForkExecutor——常驻预热 import torch+flag_gems，fork 子进程执行（吃掉每作业 ~20s 的 python 启动，预期吞吐 ×2~3）
- **backlog**：预编译缓存包上传（qemu-riscv64 交叉编译路径，依据见设计文档 Backlog 节）
