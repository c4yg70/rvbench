# K3 评估服务器监控（面板 prometheus.yml 正本）

K3 评估服务器（10.18.60.100）＋双 evald 实例的网页监控。面板零依赖单文件
[`webui/dashboard.html`](https://github.com/rvbench/rvbench-official-manager/blob/main/webui/dashboard.html)（manager 仓，file:// 双击直开），本目录存放 Prometheus 配置与说明。

> **2026-09-10 深夜迁址**：原 `work/k1-monitor/`（README、prometheus.yml、eval-probe/、attic/）
> 在邻会话文件架构重建中被删除且无搬迁映射，本目录为按上下文重建的正本。
> **损失清单**：`eval-probe/`（evald-probe.sh、k1eval-probe.service/timer、deploy 记录）与
> `attic/`（退役链路存档）内容未能恢复；板上 recovery 源：K3 `~/ne/evald-probe.sh` 仍在
> （README 曾记录"脚本本体保留回滚用"），K1 板已交还无法取回。

## 面板

- 从 manager 仓根打开 `webui/dashboard.html`（file:// 直开，Prometheus CORS 对 null origin 放行）；
  `?prom=<url>` 可覆盖数据源（记忆于 localStorage）。
- 刷新节奏（2026-09-10 起）：**即时值 5s**；重查询（query_range 火花线＋5m/1m 窗口统计）
  每 9 轮＝45s 一次（`dashboard.html` 顶部 `TICK_MS` / `RANGE_EVERY`）。
- 明暗主题 Alt+T；区块「图表 / 折叠」、主机「卡片 / 表格」均记忆于 localStorage。

## 面板结构（四层）

1. **KPI 指挥台**（6 格）：evald 实例在线 x/2（exporter 数入副标题）、吞吐（双实例合计）、
   队列积压、24h 成功率、平均 CPU（16 核均值）、最高温度。吞吐/队列满格基准
   `TP_FULL=12` / `Q_FULL=50` 在 `updateSummary()` 顶部。
2. **告警收敛**：主机失联、evald 实例离线（K3-ACC 详情提示 evald-acc/MemoryMax）、
   温度 ≥70/85、簇均核占用 ≥80/92、swap ≥60/80、内存/硬盘 ≥80/92、风扇 fault、队列 ≥20/50。
3. **evald 作业线**：车道拓扑条＋ K3 / K3-ACC 双大卡（计数、1/5/15 分钟交付、最近 3 作业、
   绑定核/worker 瓦片、60min 队列趋势）＋ **流水线漏斗桑基**（func→acc→perf→完赛，
   数据＝`evald_jobs_completed_total{lane,state}`，实例重启清零）。
4. **主机资源**：X100/A100 每核堆叠柱（核段 tooltip＝worker 名）、温度（尾标最热区）、
   内存、swap、硬盘、风扇（RPM＋PWM）、IP。卡片/表格双视图。

## 架构（数据面）

Prometheus 容器 `k1-prom`（Windows Docker，127.0.0.1:9090），挂载本目录：

- **job `boards`**：K3 node_exporter :9100（用户态，`~/ne/start.sh` @reboot 兜底）。
  静态标签 `clusters`（每核堆叠柱）＋ `workers`（worker↔核命名，与 8791 config 的
  lane_resources 分核对账）。
- **job `evald`**：K3 双实例原生 `/metrics`——:8788 board=K3（纯计时，A100 簇 cpu8-15）、
  :8791 board=K3-ACC（acc+func 正确性正本，acc=cpu0-5×6 / func=cpu6-7×2，
  systemd 瞬态单元 `evald-acc.service`：锁 0-7、MemoryHigh 10G / MemoryMax 12G、
  Restart=no 未 enable，板重启后需手动拉起，见 `work/board-acc-repartition/RUNBOOK.md` §7）。

textfile 探针已于 2026-09-09 退役（原生 /metrics 全量覆盖）；K1 四板同日下线，
抓取目标已撤。面板对 evald_* 指标做 board＋语义标签去重、原生优先（探针回退逻辑保留）。

## 运行 / 停止

```bash
# 启动（已启动则跳过）。目录挂载（单文件 bind 在 Windows 会 inode 失效）；
# --config.file 必须 MSYS_NO_PATHCONV=1（否则 /monitor 被 Git Bash 改写成 C:/Program Files/Git/...）
# 在 rvbench 主仓根执行（Windows Git Bash）。
MONITOR_DIR="$(cygpath -m "$PWD/backends/spacemit/k3/monitor")"
MSYS_NO_PATHCONV=1 docker run -d --name k1-prom --restart unless-stopped \
  -p 127.0.0.1:9090:9090 \
  -v "$MONITOR_DIR:/k1mon:ro" \
  prom/prometheus:latest --config.file=/k1mon/prometheus.yml

# 改配置后生效
MSYS_NO_PATHCONV=1 docker exec k1-prom kill -HUP 1

# 停止并拆除
docker rm -f k1-prom
```

## 已知事项

- K3 板重启后 node_exporter 由 crontab `@reboot` 兜底（≤1min）；8791 的 evald-acc 单元
  **不会自起**，需按 RUNBOOK §7 手动拉起——面板会以「K3-ACC evald 离线」红牌提示。
- 计时敏感实验期间 30s 抓取的微小扰动已获用户确认接受。
- worker 级「哪张票在哪个 worker 上跑」暂无出口（evald 未暴露 worker 维度指标），
  面板目前以核段负载近似；如需真值需 evald 加 `evald_worker_*` 遥测＋天窗期上板。
