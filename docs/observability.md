# manager / worker 可观测性与四标签页控制台

2026-10-17 用户批准实施。人工权威见协议仓 `rvbench-core-rust` 的 `observability.md`、
本仓 `deploy/observability/metrics.json`、共享 OpenAPI 与两端 OpenAPI；wire 类型由生成器维护。

## 页面与任务语义

入口 `http://10.18.60.100:18800/ui`，独立 worker 使用 `http://板IP:18801/ui`。
控制台是 `rvbench-protocol` 内嵌的自包含源码（`CONSOLE_HTML`），无 CDN、前端构建或登录依赖。

- 系统状态：首页显示节点、逻辑队列、ACK、实际执行、回传、资源与历史趋势。
- 任务提交：按实际能力选择后端 / 类型 / 算子 / dtype，支持候选代码、参数预览，
  网络失败重试沿用幂等 ID。worker 自动使用直接提交 envelope。
- 任务结果：对完整账本分页筛选；详情展示编译 / 评估、日志、源请求和证据清单。
  JSON 导出范围明确为本页或当前完整任务；工件仍通过已有 SSH 机制获取。
- 排行榜：同板 / 同环境 / 同用例分组，最新有效实现按样本中位延迟排序。
  performance 的数值预检在计时前完成。缺少证据的旧记录保留展示，不入榜。
  相对原生基准仅取同次参考样本；K1 没有参考测速时不显示虚构 speedup。

默认跟随系统明暗主题，也可切换；支持窄屏、键盘标签导航与原生详情 dialog。
所有外部文本转义，页面无示例统计或模拟测速。状态 5s、历史 30s、排行榜 15s 更新。
缺失值为 —，曲线保留空洞；监控不可达不影响提交。独立 worker 的本地任务与
指标不依赖 manager；主机历史由 manager 汇总。

## 指标与查询

`GET /metrics` 返回 Prometheus text 0.0.4。事务成功后发布轻量缓存；抓取不访问 SQLite。
业务计数来自保留的持久任务账本，幂等重放和进程重启不增加计数。任务不得直接清理；
将来增加保留策略时必须迁移累计计数。新任务的阶段耗时持久化；旧缺失值不补零。
HTTP 计数按进程重置；route 使用模板，不包含任务 ID。

manager `/observability/summary` 区分注册在线、ready、drained、ACK、实际指标状态。
采集健康包含每个 endpoint 的 up/error/last_scrape，VM 在线但部分采集失败为 degraded。
`/observability/targets` 提供 HTTP SD，保留失联节点；主机按 board_id 去重，冲突声明报错。
`/observability/range` 只接受固定 metric_id，最多 24h/1800 点，步长30..3600s，
最大4个同时查询，超时3s、响应2MiB；不提供任意 PromQL 代理。
每条序列提供最后采样时间，90s 后 stale；缺失 / NaN / Inf 不变成零。

`/results` 使用 ResultsPage，支持 backend/kind/state/op/worker_id/search/offset/limit。
`/leaderboard` 使用 LeaderboardPage，支持 backend/op/group_id。
比较指纹绑定板卡、运行环境、CPU配置、harness、shape/dtype/seed、warmup/repetitions 与时钟。
编译 / 评估必须 passed、有工件、数值预检 correct=true、样本数匹配且全部有限正值。
原生基准与候选中位数均由样本重算，不信任任意预设 speedup 或历史 suite baseline。

## 构建与部署

VictoriaMetrics v1.154.0：`29c43cd2fee05409499a4d1b38e950e1b7f13bd4`。
node_exporter v1.10.2：`654f19dee6a0c41de78a8d6d870e8c742cdb43b9`。
两者使用 Go1.27.2、CGO_ENABLED=0、GORISCV64=rva20u64。K1 上复用 Debian 动态链接
exporter 会在 cgo sysconf 调用中 SIGILL；纯 Go 构建经过真实启动 / 抓取验证。

```bash
bash tools/build_monitoring.sh
cargo zigbuild -p rvbench-worker -p rvbench-manager --release --target riscv64gc-unknown-linux-gnu.2.38
python tools/deploy_observability.py --vm-binary artifacts/monitoring-binaries/victoria-metrics --exporter-binary artifacts/monitoring-binaries/node_exporter
python tools/cluster_smoke.py --output artifacts/observability-check/cluster
python tools/cluster_recovery_smoke.py --output artifacts/observability-check/recovery
python tools/observability_smoke.py --output artifacts/observability-check/monitoring
```

构建脚本先加载 x-cmd，源码位于 `~/.x-repo/github.com/`，校验 pinned commit，不覆盖其他分支。
SHA256SUMS 随构建输出。部署先检查空闲并通过 SQLite backup API 保存账本、配置和旧二进制。
先升级 manager，再更新 workers；旧的无观测声明注册仍被接受。旧 evald 和主板 exporter 保留。

manager 主板的 `rvbench-victoriametrics` 为 c4y user service，仅监听127.0.0.1:8428，
数据在 `~/.local/share/rvbench-v1/observability/data`。初始保留7d，30s抓取，
GOMAXPROCS2 / 缓存128MiB / MemoryMax768MiB / X100 CPU0-1 / CPUQuota100% / Nice10。
浏览器只访问 manager 的同源接口。接口继续无认证，任意合法 worker 可注册。

`.18` 的 exporter 为 initer system service，K1 为 c4y user service，名称均为
`rvbench-node-exporter`。分别限 CPU0-7 / CPU0-1，CPUQuota25%、Nice19、MemoryMax128MiB。
主板内核缺少 `/proc/<pid>/io`，进程磁盘 I/O 指标不可用；不影响 TSDB 实际读写。
30s 抓取 / 发现及入库延迟意味着新节点历史可能需约一分钟出现。

## 运维与回滚

每次升级备份在服务根的 `backups/observability-UTC时间/服务名/`。
配置和二进制可在服务停止后恢复。数据库 backup 保留用于有需要的整批回滚；
不要把旧快照直接覆盖到已有新任务的运行实例，以免丢失新任务和所有权。
VM 数据不混入任务 SQLite。停用 VM 只使历史指标不可用，任务流程继续工作。
采样短测不等同于长期 compaction、异常断电或满载性能承诺。
