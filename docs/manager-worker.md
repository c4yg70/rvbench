# Manager / worker v1

`rvbench-evald` 的服务入口已更名为 `rvbench-worker`，新增独立 `rvbench-manager`。
使用 Axum / Tokio。接口没有认证、token 配置或节点名单，任意符合 contract 的 worker 可注册。
配置拒绝未知字段。新服务与旧 Python evald 使用不同端口；旧生产进程暂留供迁移。

## Contract-first

人工权威为 `contract/openapi.json`（共享 schema）、`worker/contract/openapi.yaml`、
`manager/contract/openapi.yaml`；后两份是 JSON 语法的合法 YAML，引用共享 schema。
先编写 contract 和正反例，再使用锁定的 openapi-to-rust 0.15.0 生成类型。
运行时校验原始 JSON 后反序列化生成类型。CLI、两端服务共用该类型和校验器。
变更 wire 接口必须先改 contract，然后生成、校验与实现。

```sh
openapi-to-rust generate -c contract/codegen.toml
openapi-to-rust generate -c contract/codegen.toml --check
cargo test -p rvbench-protocol -p rvbench-manager -p rvbench-worker
```

## 组件和执行

worker 可在 `manager: null` 时独立接收、排队、编译、评估、返回结果。
`Compiler::prepare` 创建不可变请求快照和源文件、实际编译/JIT 并 smoke 调用。
`Evaluator::evaluate` 分 correctness、performance。`func` 只做编译验证。
正确性结果仅证明提交的 shape / dtype / seed，不代表整个 suite 已通过。
当前烟测容差：K3 float32 的 atol/rtol 均为 0.005，float16 均为 0.05；
K1 原生 C 的 atol=0.000001、rtol=0.00001。这些值不代替 suite 待裁决的正式容差。
性能结果保存真实样本、warmup 和重复次数；编译和首次调用不计入样本。
K3 另测原生 PyTorch 参照，每个结果保留 worker/board/instance、工件大小和 SHA-256。

K3 后端使用已安装 Torch / Triton / FlagGems，用户源文件导出
`candidate(*inputs)`。源文件在独立作业目录导入，不覆盖安装树。
支持 add/sub/mul/div/abs/exp/relu/sum/softmax/mm/mv/aminmax/topk/conv2d，
float32/float16；mm/mv 要求二维，conv2d 使用 NCHW。
其余输入构造和参数见 `worker/adapters/k3.py`。
Flash attention 不列入可用能力：完整辅助输出合同仍 pending，SDPA 与完整 Flash 接口不等价，
且 SDPA 适配探测的 float32/float16 输入触发 SpacemiT 编译失败。注册环境明确保存 unavailable_ops。

K1 v1 使用本机 gcc 的原生 C 后端，支持 add/abs 和 float32。
候选签名为 `void candidate(const float *a, const float *b, float *out, int n)`。
没有声称 K1 已装好 PyTorch / Triton；后续增加该运行环境需要单独验证。

一个 worker 广告一个执行槽；直连、Push、Pull 使用同一持久接收索引与执行循环。
直连队列上限 32，另保留一个已授予 manager 作业的排队位置，以处理容量快照与直连提交的竞争。
各来源仍共用唯一执行槽。manager 全局未完成任务上限 1000。源码上限 1 MiB UTF-8，
规范化请求上限 2 MiB；传输 envelope 上限 8 MiB，容纳重复绑定的请求和结果证据。张量最多 16777216 元素。
外部进程隔离进程组，超时杀死并回收整组。systemd `KillMode=control-group` 在服务重启时清理子进程。
重启后未完成作业标记中断失败并保留证据，不自动重新执行。
若重启发生在 claim 已提交、本机尚未接收的窗口，先注册新 instance，再重放旧 receipt；
将原授予保存为中断失败和 outbox，释放原任务归属供人工重新提交。旧 instance 不会领取新工作。

## 调度和一致性

manager 只接收统一请求、保存队列、匹配 backend/kind/op/dtype/readiness，按剩余槽位分配。
Push、Pull 共用 SQLite 原子授予入口。不同 worker 竞争时，最早提交且兼容的任务优先。
注册心跳与执行续租独立；心跳间隔约 2–3 秒、在线 TTL 30 秒、执行 lease 30 秒。
分配与网络交付分离：先提交分配事务，再派发；丢失响应重发相同 assignment。
领取请求持久化 request_id，相同请求返回同一回执；改变请求内容返回 409。
已授予 assignment 在超时后显示 uncertain，保持原执行归属，绝不自动跨板重跑。
旧 lease/instance/job/attempt 不匹配时拒绝确认与回报；迟到的原始结果可对账。

SQLite WAL / synchronous FULL 原子保存控制状态。v1 采用单快照事务，适合当前小型板卡集群；
事务不含网络/子进程或工件 IO。未来规模增大时可迁移为按行表，而不改协议。
worker 终态与 outbox 在同一事务保存，至少一次重传；manager 保存事件摘要去重。
确认后删除对应 outbox；本地 job/attempt 和 manager 的有效授予回执/事件墓碑在 v1 不自动清理。
没有领取到任务的空回执保留至少 300 秒，再清理，以免空轮询长期累积。
manager 重启继续读取原 epoch 和授予；无需把既有在途执行重新派发。
节点 drain 持久化，停止新分配并保留在途任务。
取消仅支持未分配的 manager 队列和 worker 直连未运行任务；运行中取消返回 409。
未交付而状态不明的分配保留用于人工对账，不因心跳离线释放所有权。

## 三板部署

| 板 | 服务 | 执行角色 | 通道 |
|---|---|---|---|
| HUMAN-K3 10.18.60.100 | manager :18800 + worker :18801 | func、correctness | hybrid |
| K3 10.18.60.18 | worker :18801 | performance | push |
| K1 10.18.60.167 | worker :18801 | func、correctness、performance | pull |

```sh
cargo zigbuild -p rvbench-worker -p rvbench-manager --release --target riscv64gc-unknown-linux-gnu.2.38
python tools/deploy_cluster.py
python tools/cluster_smoke.py
python tools/cluster_recovery_smoke.py
python tools/verify_cluster_artifacts.py artifacts/cluster-smoke-2026-10-17/final
```

部署脚本只用已有 SSH 密钥，不保存登录口令。`.100` 和 K1 使用 c4y 的 systemd user 服务并启用 linger；
`.18` 使用 system service 的 initer 用户。首次启动先完成真实编译探测后才开放 HTTP 并广告 ready。
性能 K3 必须先注册 `/proc/set_ai_thread` 再设置 CPU8–15 亲和性。
K3 func/correctness 使用 CPU0–7，K1 使用 CPU0–7。
新服务端口 18800/18801；旧 `.100` 的 8788/8791 不受这次部署替换。
恢复验收脚本会短暂停止新 manager 并重启 K1 worker；在没有其他客户端提交任务时运行。
`cluster_smoke.py --operators` 可额外执行 K3 支持算子的 float32 矩阵，逐项保存原始记录。

## 使用

manager 页面 `http://10.18.60.100:18800/ui`，每块板 worker 页面 `http://板IP:18801/ui`。
两端共用 `webui/console.html`，内联样式和脚本，无 CDN。
四个标签页为系统状态、任务提交、任务结果、排行榜。观测、指标与部署见 [observability.md](observability.md)。

```sh
rvbench --base http://10.18.60.100:18800 control workers
rvbench --base http://10.18.60.100:18800 control submit contract/examples/job.json --wait
rvbench --base http://10.18.60.167:18801 control submit request-k1.json --direct --wait
rvbench --base http://10.18.60.100:18800 control get JOB_UUID --wait
rvbench --base http://10.18.60.100:18800 control drain WORKER_ID
```

直连 HTTP 的 POST `/jobs` 使用 `{ "request": JobRequest }` 包装，manager 直接提交 JobRequest。
查询均为 GET `/jobs/{id}?wait=25`；相同 client_request_id + 同摘要复用作业，异摘要返回409。
所有端点见 OpenAPI。工件路径相对 worker 的 `data_dir/jobs/JOB_UUID`，可用 SSH 取回并核对摘要；
v1 API 返回 manifest，不提供任意文件路径下载。

旧 `rvbench ev` / pipeline 与 `rvbench-k3` 库继续支持原 evald 的协议和历史结果；
新 API 使用 `rvbench control`。旧 token 配置只属于保留的旧客户端/兼容库，新服务不会读取它。
旧 lane 映射：func→func（compiler），correctness→correctness（evaluator），timing→performance（evaluator）。
旧部署路径、warmfork、registry judge 不能直接作为新源文件或 wire 请求传入。
公开 typical8 suite 的 draft/readiness 状态保持原有证据要求；服务上线并不自动使 suite ready。
