# spec — 监控技术栈改造：VictoriaMetrics × Categraf × Nightingale（2026-09-20，待审批）

> 本文件是改造计划的**审批件**。批准前不部署任何组件、不改任何文件；批准后按 §7 阶段推进。
> 目标架构图见 [architecture-dev.html](architecture-dev.html)（自包含，file:// 直开）；
> 现状契约见 [architecture.html](architecture.html) §06 与 [monitor/README.md](../monitor/README.md)。
> **本计划不修改任何业务代码与冻结物**：`evald.py`、`warmd.py`、`evaldctl.sh`、
> `config.example.json`、`dashboard.html`、`monitor/*` 一个字节不动（md5 基线见 CHANGELOG）。

---

## 1. 背景与目标

### 1.1 现状链路

```
K3 板 (10.18.60.100)                        Windows Docker
├─ node_exporter :9100 (~/ne, @reboot)      k1-prom 容器 (Prometheus :9090, 绑 127.0.0.1)
├─ evald :8788  /metrics  ──(30s 拉)──────▶ │  job=boards / job=evald
└─ evald :8791  /metrics                    └──▶ dashboard.html (浏览器 file:// 直查, 前端算告警)
```

### 1.2 痛点

| # | 痛点 | 后果 |
|---|---|---|
| 1 | 告警全部由 dashboard.html **前端计算** | 浏览器不开＝无告警；无通知渠道、无恢复通知、无屏蔽窗口、无告警历史 |
| 2 | Prometheus 无长期保留（缺省 15d） | 排查历史趋势（如温度劣化、队列积压周期）无数据 |
| 3 | 面板是手工单文件，图表不可组合、无变量、无权限 | 只有一人一屏一种视图 |
| 4 | 板上 node_exporter 单指标面（textfile 探针已退役） | 业务与主机指标无统一采集器框架，扩展靠改 prometheus.yml |
| 5 | 告警阈值散落在 dashboard.html JS 里 | 阈值即代码，改动需动冻结物（改面板须过板上 selftest 门禁） |

### 1.3 目标

- **记录**：VictoriaMetrics 单机版承接全部时序数据（retention 90d），API 与 Prometheus 同构。
- **采集**：Categraf 统一板端采集——主机指标（`input.node_exporter`，node_\* 命名 100% 兼容）＋
  业务指标（`input.prometheus` 抓 evald 双实例，evald 零改动）。
- **可视化**：Nightingale（夜莺）**单二进制部署**（WSL2，轻量模式内置 SQLite＋miniredis，零外部
  MySQL/Redis），承担大盘（四层重建）＋告警引擎（9 族规则、通知媒介、屏蔽天窗）＋用户/业务组。
- **不变量**：evald / warmd / dashboard.html / monitor\* 冻结字节零改动；既有 PromQL 查询资产
  （`job` 标签选择器、node_\* / evald_\* 序列名）保持原义。

## 2. 现状盘点（调研结论，2026-09-20）

1. **指标面**：evald 双实例原生 `/metrics`（exposition 0.0.4，免 token）产出
   `evald_up / evald_queued{lane} / evald_running{lane} / evald_jobs_total /
   evald_jobs_completed_total{lane,state}` 等；主机侧 node_exporter（:9100）产出
   `node_cpu_seconds_total / node_memory_* / node_filesystem_* / node_hwmon_fan_rpm·pwm·fault /
   node_hwmon_temp_celsius / node_uname_info / node_network_*`。
2. **面板依赖**（dashboard.html 反查）：
   - 只调 `GET /api/v1/query` 与 `/api/v1/query_range`（dashboard.html:921 一处 fetch 封装）→
     **VM API 同构，面板理论零改动可切数据源**；
   - 查询选择器锚定 `{job="boards"}` / `{job="evald"}`，并有 `{job!="evald"}` 回退分支；
   - 从 `up` 序列读 `clusters` / `workers` 静态标签驱动核簇堆叠柱（dashboard.html:1280-1293）；
   - 即时值 5s（`TICK_MS`）、重查询每 45s（`RANGE_EVERY`）。
3. **告警口径**（monitor/README「面板结构」§2，9 族）：主机失联、evald 实例离线、温度 ≥70/85、
   簇均核占用 ≥80/92、swap ≥60/80、内存/硬盘 ≥80/92、风扇 fault、队列 ≥20/50。
4. **冻结物**（零改动承诺，md5 见 CHANGELOG 2026-09-20 节）：`evald.py`、`warmd.py`、`evaldctl.sh`、
   `config.example.json`、`dashboard.html`、`monitor/README.md`、`monitor/prometheus.yml`。
5. **网络现状**：Windows → 板（拉取 :9100/:8788/:8791、SSH）通；**板 → Windows 方向从未使用**——
   推送链路（:17000）是新增前提，P0 必须先验证。
6. **外部组件事实**（2026-09-20 调研）：
   - Nightingale 最新 v9.1.1（2026-08-18）；发布物仅 **linux amd64/arm64 tarball，无 Windows 原生包**。
     官方二进制部署：解压即启（`./n9e`，:17000，缺省账号 root/root.2020），**单机轻量模式缺省内置
     SQLite＋miniredis，免外部 MySQL/Redis**；生产模式改 etc 配置 `[DB]/[Redis]` 外接（HA＝多 n9e
     共用一套 MySQL/Redis）；官方文档自带 systemd 单元示例。官方将轻量模式定位为测试/体验
     （本仓单板单用户场景接受该取舍，见 R6）；AI 助手功能需真 Redis Streams（不用，不受影响）。
     官方 compose-bridge 五件套（vm＋n9e＋mysql＋redis＋categraf）中 mysql/redis 部分**本方案不采纳**。
   - VictoriaMetrics 单机版：默认 :8428；`-promscrape.config` 可直接吃 Prometheus 语义配置；
     `/api/v1/query(_range)`、remote write `/api/v1/write` 兼容；`-retentionPeriod` 缺省 31d。
   - Categraf v0.5.18：`conf/config.toml` 有 `[[writers]]`（remote write，支持 basic_auth）、
     `[heartbeat]`（10s 报 n9e `/v1/n9e/heartbeat`）、`[global.labels]`；
     `input.node_exporter` 内嵌官方 node_exporter 核心逻辑、**node_\* 指标 100% 兼容**（与原生
     cpu/mem 套件二选一）；`input.prometheus` 对目标 /metrics 指标与标签 100% 保真透传，
     默认以 URL 作 `instance` 标签（README 未记载实例级 labels，见 R4）。
   - **Categraf 无官方 riscv64 发布物**（amd64/arm64/windows）——K3（Bianbu riscv64）需 Go 交叉编译
     自建（R1，全案最大风险）。
   - Nightingale v6 起不再生成 `target_up` 指标，机器失联以**心跳超时**（Targets 在线态）判定。

## 3. 设计原则

| # | 原则 | 理由 |
|---|---|---|
| P1 | **冻结字节零改动**：§2.4 七件一个字节不动 | 部署不变式（板上 md5 对账）是硬资产；监控改造不构成动它的理由 |
| P2 | **新增物全部落 `monitor-next/`**：现有 `monitor/` 冻结原样，作为 k1-prom 挂载正本直至 P6 | 退路清晰：旧链路的正本与运行物在退役前始终完整 |
| P3 | **存储替换与采集替换解耦**：先 VM 直抓并行双跑（P2，板零改动），后 Categraf 推送（P3） | 两件事各自可独立回退；任一失败不牵连另一件 |
| P4 | **板端动作集中一次**（P3），且以「停服务」为回退单元（systemctl stop categraf 即回 P2 形态） | 减少对计时簇的扰动窗口；回退不需要改任何文件 |
| P5 | **指标语义不变**：node_\*/evald_\* 序列名、`job`/`board`/`instance`/`clusters`/`workers` 标签语义保持 | dashboard.html 与告警 PromQL 资产原样复用；对账才有基准 |
| P6 | **能力升格项（告警/权限/值班）与迁移项分开验收** | 告警是新增能力而非平移，验收口径是「演练触发」而非「与旧面板一致」 |

## 4. 目标架构

（拓扑图与逐组件说明见 [architecture-dev.html](architecture-dev.html)；此处只列落地参数。）

| 组件 | 版本 / 形态 | 端口 / 绑定 | 数据卷 / 持久化 |
|---|---|---|---|
| VictoriaMetrics | 单机版容器（Windows Docker），镜像 tag P1 时固定当周 stable | 0.0.0.0:8428（本机 dashboard 过渡读＋n9e/WSL2 数据源；k1-prom 的 127.0.0.1 绑定不可复用） | `vmdata/` 卷（retention 90d） |
| Nightingale | v9.1.1 官方 **linux-amd64 tarball 单二进制**，WSL2 systemd 常驻（换任意 Linux 宿主通用） | 0.0.0.0:17000（板端推送目标；LAN 放通经 WSL2 mirrored 或 netsh portproxy，见 R2） | `n9e.db`（SQLite 单文件）＋规则/大盘 JSON 导出入仓 |
| Categraf（板） | v0.5.x 源码自建 riscv64（R1） | 无监听端口 | 无 |
| Categraf（Win 主机，可选） | 官方 amd64 包，原生 cpu/mem/disk 套件 | — | n9e 内置主机大盘即用 |

> **不引入 MySQL/Redis**：n9e 单机轻量模式内置 SQLite（元数据）＋miniredis（缓存/登录态/心跳态），
> 解压即启、零外部状态件——监控枢纽的运维重量与现栈（k1-prom 零状态）保持同量级。
> 生产化升级路径已留：etc 配置 `[DB]/[Redis]` 改外接即可（多实例 HA 同路），**部署形态仍是单二进制**。

**数据面**：Categraf → n9e `/prometheus/v1/write`（写入代理）→ VM `/api/v1/write`；
n9e 大盘/告警以 PromQL 查 VM。心跳 10s 独立通道（Targets 在线态）。
n9e 落 WSL2、VM 在 Windows Docker：n9e↔VM 走 mirrored localhost（或宿主 IP，P1 验证）；
板→n9e 的 :17000 放通同样依赖该网络模式（R2）。
**备选写径**（P1 记录、默认不用）：Categraf 直写 VM `:8428/api/v1/write`，n9e 只读——
少一跳，但代理写路径是官方组合形态且 agent 配置稳定（换后端不动板端），维持默认。

## 5. 数据与标签契约

### 5.1 指标命名决策

| 决策 | 选择 | 备选（否决理由） |
|---|---|---|
| 板端主机指标 | `input.node_exporter`（node_\* 命名） | 原生 cpu/mem 套件：指标名变为 `cpu_usage_*` 系，面板/告警全量重写，且 n9e 内置大盘用的正是原生命名——但复用既有 PromQL 资产价值更高（原则 P5） |
| 业务指标 | `input.prometheus` 保真透传 evald_\* | 无实质备选（不改 evald） |
| Windows 主机自监控（可选） | 原生套件（n9e 内置大盘即用） | 不承担兼容义务，选官方默认体验 |

### 5.2 标签映射（prometheus.yml 静态标签 → Categraf 注入点）

| 现状标签 | 注入点 | 值 |
|---|---|---|
| `board` / `role` / `cores` / `accent`（evald job） | `input.prometheus` 实例级 labels（README 未记档，P3 验证；不支持则见 R4 兜底） | K3 / K3-ACC；纯计时·A100 簇 / acc 0-5·func 6-7；8-15 / 0-7 |
| `clusters` / `workers` / `fleet`（boards job） | `[global.labels]` | `X100:0-7,A100:8-15`；`acc:0-5,func:6-7`；`k3` |
| `job="boards"` | `[global.labels]`（主机序列）＋ up 兼容流 | 面板/告警选择器锚点，必须保持 |
| `job="evald"` | `input.prometheus` 实例 labels（同上待验证）＋ up 兼容流 | 同上 |
| `ident`（新增） | Categraf 自动（板主机名） | n9e Targets 身份键 |

### 5.3 up 兼容流（推送链路的固有缺口）

推送链路没有抓取器，天然不产 `up`；而 dashboard.html 的在线判定、`clusters`/`workers` 标签读取
都挂在 `up` 上。P3 以 **exec 插件脚本**每 30s 输出三条序列补齐：

```
up{job="boards",board="K3",clusters="X100:0-7,A100:8-15",workers="acc:0-5,func:6-7",fleet="k3"} 1
up{job="evald",board="K3",role="纯计时 · A100 簇",cores="8-15",instance="127.0.0.1:8788"} 1
up{job="evald",board="K3-ACC",role="acc 0-5 / func 6-7",cores="0-7",instance="127.0.0.1:8791"} 1
```

脚本自带可达性检测（curl 两个 /metrics 端点，失败则该序列发 0 或停发）→ `up` 语义比现状
（进程活着就答）更接近真实。备选：VM 侧加 up-only 极简抓取任务（metric_relabel 丢弃全部目标样本，
仅留抓取器自身 up）；exec 方案板端闭环、无回程拉取，优先。

## 6. 新增部署物（monitor-next/，全部新文件）

```
monitor-next/
├── README.md                # 运行手册（本 spec 的落地版：起停、改配、备份、回退）
├── vm/
│   ├── run-vm.md            # VM 容器启动命令正本（0.0.0.0:8428、vmdata 卷、镜像 tag 锁定）
│   └── promscrape.yml       # P2 过渡用直抓配置（复制 monitor/prometheus.yml 语义，原文件不动；P6 删抓取节）
├── categraf/
│   ├── BUILD-riscv64.md     # 交叉编译记录（Go 版本、build tags 裁剪清单、产物 md5）
│   ├── config.toml          # writers/heartbeat/global.labels（interval=30）
│   ├── input.node_exporter.toml
│   ├── input.prometheus.toml # 两 instance（:8788/:8791）＋ labels
│   ├── input.exec.toml      # up 兼容流脚本引用
│   ├── up_compat.sh         # §5.3 脚本
│   └── categraf.service     # systemd 单元（CPUAffinity 钉 X100 用户域 cpu0-7）
├── n9e/
│   ├── INSTALL-WSL2.md      # 单二进制安装：tarball 解压 /opt/n9e、n9e.service、任务计划自启链、升级/备份
│   ├── n9e.conf.diff        # 相对官方缺省的最小改动（监听、数据源、写入代理；DB/Redis 保持内置）
│   ├── n9e.service          # systemd 单元（官方二进制部署文档示例为底）
│   ├── lan-expose.md        # :17000 放通双方案：WSL2 mirrored / netsh portproxy 脚本（WSL IP 自适应）
│   ├── datasource.md        # VM 数据源（默认查询＋写入代理）配置记录
│   ├── alert-rules.json     # 9 族告警规则导出（§8）
│   └── dashboards/          # 大盘 JSON 导出（§9，P5 产出）
└── windows-categraf/        # （可选）Windows 主机自监控配置
```

> 仓库根 README 的「仓库结构」表与 CHANGELOG 在 P1 落地 commit 时补一行索引（纯文档改动，不触冻结物）。

## 7. 阶段计划

| 阶段 | 动作 | 涉及面 | 验证门（不过门不进下一阶段） | 回退 |
|---|---|---|---|---|
| **P0 前置确认** | 板 → 枢纽 :17000 端到端可达（先定 WSL2 网络模式：mirrored 优先、portproxy 兜底）；防火墙窄放行；WSL2 就绪（systemd）；Docker 资源余量；冻结物 md5 复核 | 零改动 | 板上 `curl http://<win>:17000` 有响应（经 mirrored/portproxy 落到 WSL2 n9e） | — |
| **P1 枢纽骨架** | WSL2 装 n9e tarball 至 /opt/n9e（解压即启：SQLite＋miniredis 内置，零外部状态件）＋ n9e.service ＋ Windows 任务计划自启链；VM 容器改绑 0.0.0.0:8428；n9e 建 admin/业务组（首登改默认口令）；VM 设为默认数据源＋开启写入代理；镜像 tag 固定 | monitor-next/（vm/＋n9e/） | ① 板 curl :17000 端到端（P0 网络模式生效）；② n9e UI「数据源」探测通过、可查 VM 样本；③ Windows 重启后 n9e/VM 自启 | `systemctl stop n9e` / `wsl --shutdown`（n9e.db 保留）＋停 VM 容器 |
| **P2 存储并行** | VM `-promscrape.config` 挂 promscrape.yml 直抓三目标（30s）；k1-prom 照跑；dashboard.html 加 `?prom=http://127.0.0.1:8428` 书签只读并行 | monitor-next/vm/ | ① 双跑 ≥7d 且抽查 `evald_queued` / `node_cpu_seconds_total` / `node_hwmon_fan_rpm` 序列值一致；② file:// null origin CORS 实测（R3）；③ VM 磁盘增长符合 <10MB/天 | 停 VM 容器即回 k1-prom 单跑 |
| **P3 Categraf 上板** | ① riscv64 编译 spike（BUILD-riscv64.md）；② systemd 部署＋三输入插件＋up 兼容流；③ `~/ne` node_exporter 停推？**否——P3 期间 ne 继续跑**（VM 直抓仍开着，三方数据并存） | 板（唯一板端阶段）＋ monitor-next/categraf/ | ① 编译产物上板自启动；② `node_*` 序列与 ne 版 diff（面板在用清单 §2.1 全命中）；③ evald 序列标签注入符合 §5.2（labels 不支持→R4 兜底生效）；④ 心跳在 n9e Targets 显示在线；⑤ vmstat/负载对比无恶化（A100 簇零接触抽查） | `systemctl stop categraf`（回到 P2 形态；ne 未动） |
| **P4 告警上线** | n9e 录入 9 族规则（§8）＋通知媒介（渠道待用户定）＋计时天窗屏蔽规则；先全部「仅记录」模式观察 ≥3d 再启用通知 | monitor-next/n9e/ | ① 演练：停 evald-acc 单元 → 离线告警触发＋恢复通知；② 阈值族用历史数据手动 PromQL 回放验证表达式；③ 屏蔽窗口内演练静默生效 | 规则停用（n9e UI 一键，无基础设施回退） |
| **P5 大盘重建** | 四层大盘在 n9e 落地（§9）；KPI/作业线/主机资源逐层与 dashboard.html 并排对数 | monitor-next/n9e/dashboards/ | ① 六格 KPI 与 dashboard.html 同时刻数值一致；② 漏斗堆叠柱各层数与桑基口径一致；③ 变量（board/lane）切换正确 | 不影响旧面板（仍活着） |
| **P6 退役收尾** | ① VM promscrape 抓取节撤（转纯推送）；② k1-prom 停（卷封存 30d）；③ 板上 ne 停用（crontab @reboot 注释、文件保留）；④ dashboard.html 归档只读（README 挂「已退役」注记——**改 README 即破冻，故注记只写在 monitor-next/README，dashboard.html 本身不动**）；⑤ monitor-next/README 记录接替关系 | 各处 | 每项退役后 24h 观察无序列缺失（n9e 缺图检查＋`count(node_*)`/`count(evald_*)` 对比退役前） | 逐项独立：docker start k1-prom / 取消 crontab 注释 / promscrape 配置回滚 |

> 时序依赖：P3 依赖 P2（对账基准）；P4/P5 依赖 P3（数据全走新链）；P6 依赖 P4＋P5 验收。
> P1–P2 可在板不可达期间先行（纯 Windows 侧，直抓除外——直抓需板可达；
> 板不可达时 P1 的「板→:17000 端到端」验证项顺延至板恢复后补）。

## 8. 告警规则映射（9 族）

级别统一映射：warn（面板黄）→ n9e Sev2；crit（面板红）/失联 → Sev1。
持续时长缺省 3m（`for`）；队列族 2m。通知渠道 P4 时由用户指定（钉钉/飞书/企微/邮件/webhook 任选）。

| 族 | 级别 | PromQL 草案（迁后口径） | 备注 |
|---|---|---|---|
| 主机失联 | Sev1 | n9e 心跳超时（Targets 离线事件）；PromQL 兜底 `absent(node_uname_info{job="boards"})` | 心跳断≠数据断，双口径并存 |
| evald 实例离线 | Sev1 | `absent(evald_up{instance=~"127.0.0.1:8788"})` 与 `:8791` 两条规则 | 规则描述里附 K3-ACC 疑触 MemoryMax 的排障指引（RUNBOOK §7） |
| 温度 | Sev2 / Sev1 | `max(node_hwmon_temp_celsius{job="boards"}) ≥ 70 / 85` | |
| 簇均核占用 | Sev2 / Sev1 | `100*(1-avg(rate(node_cpu_seconds_total{job="boards",cpu=~"0|1|2|3|4|5|6|7",mode="idle"}[2m]))) ≥ 80/92`；A100 簇同式 cpu=~"8…15" | X100/A100 两条规则（clusters 标签是面板展示用字符串，不做选择器） |
| swap | Sev2 / Sev1 | `100*(1-node_memory_SwapFree_bytes/node_memory_SwapTotal_bytes) ≥ 60/80` | 分母为 0（无 swap）时不触发：`node_memory_SwapTotal_bytes > 0` 前置 |
| 内存 | Sev2 / Sev1 | `100*(1-node_memory_MemAvailable_bytes{job="boards"}/node_memory_MemTotal_bytes) ≥ 80/92` | |
| 硬盘 | Sev2 / Sev1 | `100*(1-node_filesystem_avail_bytes/node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}) ≥ 80/92` | 按 mountpoint 分组 |
| 风扇 fault | Sev1 | `max(node_hwmon_fan_fault{job="boards"}) ≥ 1` | |
| 队列 | Sev2 / Sev1 | `sum by (board) (evald_queued{job="evald"}) ≥ 20/50` | board 标签依赖 §5.2 注入；未注入前以 instance 分组 |

**屏蔽规则**：① 计时敏感天窗（手工基准/全量验收时段，按通知渠道声明）；② 板端维护窗口。
这是现状没有的能力，也是本次改造最直接的日常收益之一。

## 9. 大盘映射（四层）

| 现状层 | n9e 落位 | 迁移要点 |
|---|---|---|
| KPI 指挥台（6 格） | 大盘首行 6 图 ＋ 变量 `board` | 在线 x/2 用 up 兼容流；吞吐/成功率用 `increase(evald_jobs_total{state="done"}[...])`；TP_FULL=12 / Q_FULL=50 基准线写入图表配置 |
| 告警收敛 | n9e「活跃告警」页 | 不在大盘内重建——服务端告警后此层职责消失 |
| evald 作业线 | 车道拓扑条（表格瓦片）＋双实例卡＋**漏斗堆叠柱** | 桑基 n9e 无原生等价（R9）；`evald_jobs_completed_total{lane,state}` 口径不变，func→acc→perf→完赛以堆叠柱/表格表达；60min 队列趋势用 `sum by (board)(evald_queued)` 时序图 |
| 主机资源 | 每核堆叠柱（cpu 维度）＋温度/内存/swap/盘/风扇行 | node_\* 序列名不变；workers 核段命名沿用标签；卡片/表格双视图由 n9e 变量承担 |

## 10. 风险与未决

| # | 风险 | 等级 | 处置 |
|---|---|---|---|
| R1 | Categraf 无 riscv64 官方发布物；自建可能因个别插件 CGO 依赖受阻 | **高** | P3① 硬门禁：`GOOS=linux GOARCH=riscv64 CGO_ENABLED=0 go build` ＋ build tags 裁剪（oracle 等）；不过门 → **VM 直抓长期化**（架构退一档：categraf 仅 Windows 侧自监控，n9e/VM 收益不受影响），重开本 spec 修订 |
| R2 | 板→枢纽推送方向未验证；终点是 WSL2 内的 n9e（:17000），另叠加 n9e(WSL2)→VM(Win Docker) 跨界访问 | 高 | P0 定网络模式：WSL2 mirrored 优先（Win11 22H2+，本机 build 26200 满足），netsh portproxy＋任务计划兜底（WSL IP 动态，脚本自适应）；n9e 绑 0.0.0.0:17000、VM 绑 0.0.0.0:8428；不做「拉平」妥协（如 SSH 隧道）除非用户明示 |
| R3 | VM 对 file:// null origin 的 CORS 未实测 | 中 | P2② 验证；不放行 → dashboard.html 过渡期以本地静态服务器或反代承载，或接受其提前退役（P5 提速） |
| R4 | `input.prometheus` 实例级 labels 文档未记载 | 中 | P3③ 验证；不支持 → board/role 区分退回 `instance` 标签（:8788/:8791 端口可解析），并在 n9e 大盘/告警里用 `label_replace` 或直接 instance 匹配 |
| R5 | hwmon 风扇序列名 parity（fan_rpm/pwm/fault） | 中 | P3② 与 ne 版逐序列 diff，以面板在用清单为准 |
| R6 | n9e 引入状态件（SQLite 单文件＋miniredis 内存态）＋WSL2 运维链（开机不自启、内存需上限） | 低 | n9e.db 周备（计划任务拷文件）＋规则/大盘 JSON 导出入仓（本 spec §6 文件即灾备正本）；重启丢登录态（重登）与心跳态（分钟级重建），单用户场景可接受；官方定位轻量模式为测试/体验，扩容路径＝etc 配 `[DB]/[Redis]` 外接 MySQL/Redis（部署形态不变）；`.wslconfig` 限内存；任务计划 `wsl -d …` 拉起 systemd |
| R7 | :17000（n9e/WSL2）与 :8428（VM 写口）双双暴露 LAN | 低 | 防火墙按源窄放行（板 IP＋本网段）；categraf writers basic_auth（n9e 侧强制鉴权能力 P1 验证）；n9e UI 自带用户体系（首登改默认口令 root/root.2020） |
| R8 | 计时簇扰动 | 低 | Categraf systemd `CPUAffinity=0-7`（X100 用户域）；interval 30s 与现状一致；P3⑤ vmstat 对比；A100 簇 cpu8-15 零接触 |
| R9 | 桑基/5s 即时刷新等面板特性无等价物 | 低 | 漏斗→堆叠柱；即时值接受 30s 档；dashboard.html 归档前与 n9e 并行 2 周 |
| U1 | **未决：通知渠道**（钉钉/飞书/企微/邮件/webhook/短信） | — | P4 前用户指定 |
| U2 | **未决：Windows 主机自监控是否纳入**（可选件） | — | P1 时决定，不影响主线 |
| U3 | **未决：n9e 版本策略**——v9 为最新大版，是否固定 v9.1.x 小版本跟进 | — | 建议：固定小版本，季度评估升级 |

## 11. 验收清单（DoD）

- [ ] P6 完成：k1-prom / 板上 node_exporter 停用 24h 后，n9e 大盘无序列缺失
      （`count(node_*)`、`count(evald_*)` 与退役前一致）
- [ ] 9 族告警全部演练触发过一次（或历史回放验证表达式），恢复通知可用
- [ ] 计时天窗屏蔽规则演练静默生效
- [ ] 四层大盘与 dashboard.html 并排对数一致（P5 门），桑基→堆叠柱口径核对
- [ ] n9e.db 周备任务在 Windows 计划任务就位，且 9 族告警规则＋大盘 JSON 导出入 monitor-next/n9e/
      （灾备正本），恢复演练一次
- [ ] Windows 重启后 n9e（WSL2 任务计划→systemd）与 VM 容器自启演练通过
- [ ] monitor-next/README 完整记录起停/改配/回退/备份手册；BUILD-riscv64.md 记录编译参数与产物 md5
- [ ] 冻结物七件 md5 与 CHANGELOG 2026-09-20 节基线一致（全程零改动的最终证明）
- [ ] 仓库 README/CHANGELOG 补 monitor-next 索引（纯文档 commit）

## 12. 明确不做的事

- 不改 `evald.py` / `warmd.py` / `evaldctl.sh` / `config.example.json` / `dashboard.html` / `monitor/*`（冻结）
- 不引入 vmagent / vmalert / vmcluster / vmauth（单机规模；告警由 n9e，采集由 categraf/VM 自带抓取器过渡）
- 不引入外部 MySQL/Redis（n9e 轻量模式内置 SQLite＋miniredis；生产化升级路径已留，见 §4 注）
- 不做 HA / 双活 / 远程备份（单板实验场景，n9e.db＋vmdata 本机卷即可）
- 不接入 K1 板（已下线交还；恢复接入=加 categraf 目标或 VM 抓取目标，另开 spec）
- 不做日志/链路追踪（n9e 生态支持，但本次范围仅指标）
- 不在 P3 之外安排任何板端动作；P3 不碰 A100 簇 cpu8-15 与 `/tmp/k3_bench.lock*`

## 13. 板端部署通道选型（讨论稿，待决策）

> P3（categraf 上板）与板恢复后的回归门（README「板恢复后回归门」）都需要一条**幂等、可验证、
> 可回退**的部署通道。当前结论倾向：**repo 原生清单式部署器**（基于 `ops/pssh.py`）先行，
> ansible 作多板化升级路径，opentofu 不适用。用户拍板后本节转正并落 `ops/deploy.py`。

| 方案 | 适用性判断 | 结论 |
|---|---|---|
| OpenTofu / Terraform | 面向云厂商 API 的状态机（provision＋state）；无裸板 SSH 一等支持（社区 ssh provider 停维护、把 SSH 当 CRUD 属反模式） | **不适用**：K3 是已存在资产，需要的是配置管理（configure），不是资源供给（provision） |
| Ansible（WSL2 控制节点） | 控制节点不支持 Windows 原生（须落 WSL2——n9e 已使其存在）；单板用不上其核心收益（并行 fleet／inventory／模块生态）；板上 python3 已具备（evald 依赖） | **升级路径**：K1 四板回归或多板 fleet 时启用；届时把部署清单翻译成 playbook 成本低 |
| **repo 原生清单式部署器** | `ops/deploy.py`＋声明式 manifest，复用 `ops/pssh.py`（paramiko）通道 | **先做**：零新依赖，与仓库哲学（纯 stdlib／md5 对账／selftest 门禁）同构 |
| pull 式自部署（板上 cron/systemd timer `git pull`＋bootstrap） | 板主动拉正本执行 | **不作常态通道**：板上须持 git 凭据，且变更时序不受控（可能撞计时互斥窗口）；仅可作只读文档同步 |

`ops/deploy.py` 设计要点（相当于单板 mini-ansible，量级 ~200 行）：

1. **manifest 即目标状态**：`files[]`（仓内源→板上路径＋期望 md5，与 CHANGELOG 冻结 md5 清单同源）、
   `units[]`（systemd 单元）、`cron[]`（crontab 行）、`dirs[]`（目录/权限）；
2. **幂等执行**：读板端现状→只推差异→推送前打时间戳备份→写后 md5 复验；
3. **门禁钩子**：hooks 串（`evald.py --selftest`、categraf config 自检、`ev.py run --op add` 金丝雀），
   任一失败自动回滚到本次备份目录——把 README 回归门从手工 runbook 变成部署流水线的硬步骤；
4. **首个用户 = P3**：categraf riscv64 二进制＋conf＋`categraf.service`（CPUAffinity=X100 用户域）进清单，
   部署即验证、不过即回退——与 §7 P3 的回退语义（`systemctl stop categraf`）互补。

---

## 附：调研来源（2026-09-20）

- VictoriaMetrics 单机版（端口 8428 / `-promscrape.config` / API 兼容 / retention）：
  <https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/>
- Nightingale v9.1.1 发布物（linux amd64/arm64，无 Windows 原生包）；二进制部署＝解压即启 /
  轻量模式 SQLite＋miniredis / 生产模式 `[DB]/[Redis]` 外接 / systemd 示例 / HA＝多实例共用存储：
  <https://github.com/ccfos/nightingale> ·
  <https://n9e.github.io/docs/install/binary/>；
  官方 compose-bridge 五件套（vm＋n9e＋mysql＋redis＋categraf，其中 mysql/redis 本方案不采纳）：
  <https://github.com/ccfos/nightingale/tree/main/docker>
- Nightingale v6+ 心跳/Targets、无 target_up：
  <https://flashcat.cloud/docs/content/flashcat-monitor/nightingale-v6/agent/categraf/>
- Categraf 发布物（无 riscv64）、config.toml（writers/heartbeat/global.labels）、
  `input.node_exporter`（node_\* 100% 兼容、与原生套件二选一）、`input.prometheus`
  （100% 保真透传、url_label_key）、inputs 全清单：
  <https://github.com/flashcatcloud/categraf> ·
  <https://flashcat.cloud/docs/content/flashcat-monitor/nightingale-v6/agent/categraf/>
- 本仓现状：`monitor/README.md`、`monitor/prometheus.yml`、`dashboard.html`（API 面/标签依赖反查）、
  `CHANGELOG.md`（冻结物 md5 基线）
