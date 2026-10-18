# rvbench CLI

原生 Rust 客户端与离线校验库。`rvbench --help`、`rvbench control --help` 查看参数。

## 二进制

```sh
rvbench control workers                      # 节点
rvbench control capabilities                # 能力表
rvbench control submit job.json --wait      # 提交
rvbench control get JOB_UUID --wait         # 查询
rvbench control cancel JOB_UUID             # 取消未运行任务
rvbench control drain WORKER_ID             # 排空节点
```

`--base` 指向 manager（默认 `http://127.0.0.1:18800`）或独立 worker 的 18801；直连 worker 时加 `--direct`。

离线工具不访问网络：

```sh
rvbench validate spec examples/hello-world/spec.json
rvbench validate results examples/hello-world/eval_results.json --spec examples/hello-world/spec.json
rvbench validate baseline-results results.json --spec examples/baseline/spec.json
rvbench digest examples/hello-world/spec.json
rvbench digest examples/baseline/spec.json --baseline torch_abs
```

`validate hidden` 针对评测集的 `spec.hidden.json`；公开仓不含隐藏定义，完整集随 rvbench-official-suites 开源。

`validate spec|hidden` 的 `--require-ready` 在仍有 readiness blocker 时返回 2；合同合法与完成板上验证是两件事。

## 库

`rvbench_cli` 同时是 lib，供 `rvbench-tools` 复用同一套校验与 I/O：

- `spec` / `results` / `baseline` / `hidden`：schema 与语义校验、readiness、摘要
- `files`：原子写入
- `control`：HTTP 客户端

`json`（严格解析、canonical 序列化、SHA-256）来自 `rvbench-protocol`，与 manager / worker 共用同一实现。
