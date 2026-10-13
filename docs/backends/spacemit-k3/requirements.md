# rvbench-kernel-server 下一代架构需求

版本：0.3 · 日期：2026-10-08（Asia/Shanghai） · 状态：设计草案，尚未实现。

本文件是需求正本；`specs.html` 是据此编写的设计方案。现有源码基线为 `ee2135f905ab77f8dba1c478f666066c615b53e8`。本次仅新增设计文档与目标架构图，不修改板端冻结文件，不部署服务。

## 1. 已确认的用户需求

1. 同局域网多机使用 manager-worker 架构，由单一 manager 接受用户提交并分配 worker 测试。
2. manager 所在主机可以是 amd64、arm64 或 rv64；所有 worker 都是 rv64。
3. 还要支持单二进制直接运行最简形式，加载 `spec.json` 测试集（例如 micro）执行测试。
4. 先整理 requirements，再编写包含目标架构图的 specs.html。
5. 新控制与调度程序使用 **Rust**；测试执行使用已安装的 Python/torch/Triton/FlagGems。
6. `spec.json` 与 `spec.hidden.json` 均必须有顶层 `schema_version`（匹配各自 Schema）和 `version`（文件自身内容版本）；定义独立 `eval_results.json` 结果 Schema。

## 2. 本版假设与待决事项

- **A1 输入已确认**：用户提供 `spec.json`，可以是 micro 等任意符合 rvbench spec 合同的集合。格式权威来自配套 rvbench-kernel 仓的 Schema 与语义校验工具，不新造一份 suite-v2 输入格式。micro 的现有设计文档不等于已有 ready 且可执行的 spec；不得把当前私有战役基线或 230 个算子自动标成公开集。
  配套仓当前 micro 文档已标记被典型 8 集计划替代；首份 `definitions/typical8/spec.json` 是 draft 的 8 个 C01，不能宣称已完成 80 个可执行用例。服务按文件内容和版本加载，不把 micro/typical8 名称硬编码成执行规则。
- **A2 单二进制边界已确认**：Rust 控制程序、调度、CLI、Schema 与执行适配脚本包含在一个可执行文件中；实际算子运行使用 RV64 上已安装、版本固定的 Python/torch/Triton/FlagGems。不捆绑这些科学计算运行时。`spec.json` 及其引用合同属于用户提供的测试输入，可以是多文件输入包。
- **A3 平台建议**：第一版 manager 和 worker 都以 Linux 为验收平台，manager 覆盖三种架构，worker 仅 linux/riscv64。Windows/macOS 客户端可通过 HTTP 使用 manager；manager 的其他 OS 支持未列为已确认需求。
- **A4 语言已确认**：采用 Rust；拟用 bundled SQLite 内嵌状态库、Rust TLS 与嵌入资源。每目标平台发布一个程序；不要求同一文件跨 ISA，不把“单二进制”误写成“完全静态无 libc”。具体依赖版本、交叉编译工具链和最低 libc 版本在构建验证后锁定。
- **A5 规模建议**：先按一个 manager、1–16 台 worker、1000 个待调度 case 的实验室规模设计；这些是验收基线建议，不是已实测容量或用户给定硬限制。
- **A6 信任边界建议**：面向受控实验室账号及受信任候选代码。任意候选代码会在 worker 执行；无 root 身份不构成恶意代码沙箱。开放匿名公网或运行不受信任代码需独立威胁模型与 OS 隔离工作。

## 3. 范围与术语

Run 是一次用户提交的 Suite 或单 case 运行；Case 是具有稳定 case_id 的输入条目；Job 是该 case 和候选包的一次逻辑评估；Attempt 是在一台 worker 上执行该 Job 的一次尝试；Lease 是 manager 授予 Attempt 的有限执行授权。重试新增 Attempt，不能覆盖旧 Attempt 的原始证据。

Manager 是唯一用户提交入口、逻辑状态权威和调度决策者；Worker Agent 是 RV64 上唯一资源与进程管理者；Runner 是两种部署模式共享的执行核心；Benchmark Adapter 是 Python 等具体测试运行时的边界；Environment Fingerprint 是硬件、核簇、软件与计时协议的可比性身份。

第一版包含分布式模式和 RV64 本地 `run` 模式。高可用多 manager、跨数据中心调度、硬件仿真计时、自动安装厂商运行时、通用公网多租户沙箱、远程任意 shell、Redis/Kafka/Kubernetes 依赖均不在首版范围。保留后续扩展接口。

## 4. 功能与约束

表中 **MUST** 表示首版必须满足；**SHOULD** 表示建议目标；验收编号在 §5。

| ID | 级别 | 需求与可观察行为 | 验收 |
|---|---|---|---|
| R-001 | MUST | 分布式部署只有一个活动 manager，所有用户提交、查询与取消由它接受；worker 不提供用户作业提交接口。 | AT-01 |
| R-002 | MUST | manager 发布 linux/amd64、linux/arm64、linux/riscv64 构建；无需本地 torch、Triton 或 RVV 就可调度、存储和查询。 | AT-02 |
| R-003 | MUST | worker 与本地测试运行只能在 linux/riscv64 启动；实测能力必须满足所选 Suite 的 ISA/运行时要求，单凭架构标签不足以接单。 | AT-02, AT-03 |
| R-004 | MUST | 同一产品的单可执行文件提供 manager、worker、run、doctor 子命令；按目标架构分别发布，不承诺一个文件在三种 ISA 上通用。 | AT-02, AT-04 |
| R-005 | MUST | RV64 上 `run --spec <spec.json>` 无 manager、无网络、无额外控制服务、无战役目录即可运行；默认测试当前已安装的 candidate 实现并使用 spec 指定 reference，候选文件包是可选输入。 | AT-04 |
| R-006 | MUST | 两种模式共享 Suite 校验、Runner、资源互斥、超时、恢复、结果格式；差异仅在传输与调度范围。 | AT-05 |
| R-007 | MUST | 加载用户提供 spec.json，固定其版本/digest、case 清单、reference/oracle、评价协议及引用合同摘要；可执行文件内嵌 Schema 和适配脚本，测试集不强制内嵌。测试输入包的公开分发仍遵守来源许可证。 | AT-04, AT-06 |
| R-008 | MUST | Suite 支持同 op 的多个 shape/dtype；case_id 全集唯一、规范化内容可摘要；公开集无需 baseline_manifest、EV_WORKSPACE 或私有 incumbent registry。 | AT-06 |
| R-009 | MUST | manager 服务端校验并展开 Suite，持久化规范化 Run 快照；同时接受单 case；非法输入在任何部署或执行前失败。 | AT-06 |
| R-010 | MUST | 用户请求有主体隔离的幂等键，键和规范化请求摘要一起存储；相同键不同内容返回冲突，相同内容返回既有 Run。 | AT-07 |
| R-011 | MUST | manager 在持久提交后返回接受成功；Run/Job/Attempt、调度租约、事件、幂等记录与结果索引可重启恢复，另有按摘要保存的不可变候选/套件/日志对象。 | AT-08 |
| R-012 | MUST | worker 由管理员纳入并鉴权，使用稳定 worker_id 与每次启动的 boot_id 上报架构、ISA、核簇、CPU 池、内存、运行时摘要、适配器/协议版本与可用状态。 | AT-03, AT-09 |
| R-013 | MUST | 调度只选择能力匹配且 ready 的 worker；预留 lane/CPU/内存与安装槽位资源；正式 timing 默认绑定具体 worker 与 environment，禁止静默跨机器沿用基线。 | AT-03, AT-10 |
| R-014 | MUST | manager 维护全局与主体队列限额及每 worker lane 容量；满载返回明确背压；同优先级主体轮转、主体内 FIFO，不能让大 Suite 独占队列。 | AT-10 |
| R-015 | MUST | worker 主动建立经认证的 LAN 通道并请求工作；manager 在租约响应中选择并下发 Job，而不是 worker 自行争抢任意用户任务。无需可入站的 worker HTTP 服务或 SSH。 | AT-01, AT-09 |
| R-016 | MUST | 每个 Attempt 绑定 worker_id、boot_id、manager epoch、递增 generation 和 lease_id；心跳、续约、进度、完成报告均校验有效令牌，过期或重复报告不能覆盖权威状态。 | AT-11 |
| R-017 | MUST | 不承诺全局执行恰好一次。安全可重试任务采用至少一次尝试、唯一有效完成提交；所有旧尝试保留审计。只自动重试明确的基础设施故障，并受重试预算约束。 | AT-11, AT-12 |
| R-018 | MUST | 网络分区或 manager 重启时，worker 在本地单调时钟的执行授权期限前续租；到期停止整个进程树、进入 cleanup，不接新任务；manager 不将未知清理状态当作已空闲。 | AT-11 |
| R-019 | MUST | worker 本地持久写 Attempt journal 与结果 outbox；重启先 reconcile、清理残留进程与未恢复安装槽位，再进入 ready；恢复失败进入 quarantined 并报告。 | AT-08, AT-12 |
| R-020 | MUST | 默认每 Attempt 使用独立作业目录与候选 overlay；适配器不能支持 overlay 时可用受控 shared-install 模式，须跨进程按安装根和路径排序加锁、先 journal 再改写、验证摘要并恢复。 | AT-13 |
| R-021 | MUST | Runner 将候选加载摘要、进程退出、清理状态和结果有效性一起记录；只有已完成需要的清理且当前租约有效的结果可成为可信完成，失败清理不能伪装成功。 | AT-12, AT-13 |
| R-022 | MUST | perf/timing、acc/correctness、func 三种 lane 可独立启停；timing 持测量资源锁并钉核，acc/func 不给出正式 speedup；timing 默认独占 worker，可显式证明隔离后允许并行。 | AT-10, AT-14 |
| R-023 | MUST | CPU 核簇选择、taskset/厂商调度入口由 worker 的 Host Driver 封装，manager 不直接写 /proc；CPU 池不满足、路径锁不可用或绑核失败时 fail closed。 | AT-03, AT-14 |
| R-024 | MUST | Runner 首版支持 Subprocess；WarmFork 保留为能力声明后的可选适配器。候选变更不得进入常驻父进程；任何预热优化不改变 case 评价协议。 | AT-05, AT-14 |
| R-025 | MUST | 安装树永久变更与普通评估分开；分布式普通 Job 拒绝 restore=false，独立 worker pin 的 promote 管理操作后续实现，首版不自动重试任何定稿写入。 | AT-13 |
| R-026 | MUST | 单 case 超时、租约过期、用户取消均经 stopping→cleanup；整 Run 取消阻止新 Attempt、通知在飞任务，完成清理前不能声称 canceled。 | AT-15 |
| R-027 | MUST | 结果符合 rvbench-kernel 的 eval_results.schema.json 与跨字段语义合同；保存每 case/历史 Attempt、输入、候选与公开/隐藏 spec 版本及摘要、seed、实际 warmup/rep、worker/软件 fingerprint、检查、采样与清理证据；缺值显式 null，错误/skip 具原因码，公开隐藏记录脱敏。 | AT-05, AT-16 |
| R-028 | MUST | correctness 使用公开集 oracle/tolerance；随机算子使用声明的统计验证，否则 unverifiable；func 只表示执行成功。全套分母固定，不能用跳过/失败项制造全通过。 | AT-06, AT-16 |
| R-029 | MUST | 正式计时比较仅在同测量环境和协议内进行；reference 与候选按固定协议成对测量，跨 worker/核簇/版本的数值分组展示，禁止混合平均 speedup 或自动更新 incumbent。 | AT-16 |
| R-030 | MUST | 本地以 eval_results.json 为结果快照正本，原子写入；逐 case JSONL、summary、HTML 从其派生；分布式查询 Run 与 Attempt 的状态、原因和授权产物下载使用同一合同；本地 `--resume` 只复用摘要及环境一致的可信完成。 | AT-04, AT-16 |
| R-031 | MUST | 数据面使用摘要校验、大小上限、流式传输、临时写入后原子发布；失败报告留 worker outbox 重传；manager ACK 之前不得丢失本地终态证据。 | AT-08, AT-17 |
| R-032 | MUST | 用户 API 与 worker 协议分别鉴权与授权；worker 身份与证书绑定，LAN 分布式默认 TLS，禁止明文默认密钥；用户不能提交任意 shell 或白名单外部署路径。 | AT-09, AT-17 |
| R-033 | MUST | manager/worker 暴露作业、租约、资源、清理及队列指标；健康分存活与就绪，指标默认不泄漏候选代码、token 或原始日志；Prometheus 是可选消费端。 | AT-18 |
| R-034 | SHOULD | 为 ev.py/eval_pipeline 提供受控 v1 HTTP 兼容入口，worker URL 改为 manager；未知字段、未支持语义与无 worker 情况明确报错。历史 result-v2/raw-v1 读侧兼容，不重写旧文件。 | AT-19 |
| R-035 | MUST | 新系统先旁路构建，不覆盖冻结 evald/warmd/监控部署物；共享安装树不得新旧服务并行写；上线经过回归与可恢复迁移，保留旧系统回退路径。 | AT-19 |
| R-036 | MUST | 配置、协议、Suite、结果都显式版本化；spec.json/spec.hidden.json 必填顶层 schema_version（各自 Schema 的 const）与 version（自身版本），eval_results.json 也区分两者并记录所用 spec 的两个版本与摘要；未知格式、缺版本或错误 Schema 拒绝；doctor 离线报告平台/依赖/路径/锁/磁盘/计时核问题。 | AT-03, AT-04, AT-16 |
| R-037 | SHOULD | 初期规模 16 worker、1000 个 ready case 时，控制面请求接受 p95 <200ms、空闲 worker 正常接单 p95 <2s；不计候选上传、长轮询和实际基准耗时；必须实测再宣称。 | AT-20 |
| R-038 | MUST | 离线最简运行不强制数据库服务、消息队列、容器或后台常驻进程；测试时间从 worker 本地单调时钟计算，manager 时间只用于事件排序和排队时间。 | AT-04, AT-11 |
| R-039 | MUST | 原生保留 spec 的 operators/call/returns/cases/inputs/attrs/outputs/verification/execution/policies 合同，支持多输入、显式 None、零维、strides 与多返回项；禁止压平成旧 op/shape/dtype 请求或静默替代 callable/generator。 | AT-21 |
| R-040 | MUST | 分开报告定义有效、可验收与实际通过；正式运行遵守 ready 门禁与完整合同。draft 仅允许显式 probe 模式，pending/blocked/skip 不包装为正式通过；未实现的 adapter/generator/check 明确报告 unsupported。 | AT-06, AT-21 |
| R-041 | MUST | Rust spec loader 与 rvbench-kernel 权威校验和 digest 有一致性样本；拒绝重复键/非有限数、越界/符号链接逃逸及未绑定引用；manager 提交打包 spec 相对合同文件，worker 不依赖客户端绝对路径或仓库目录。 | AT-17, AT-21 |
| R-042 | MUST | 默认仅运行 public spec；不自动读取同目录 spec.hidden.json。隐藏集是独立、显式授权的后续输入扩展，私有 case/输入不经公开下载、报告或指标泄漏。 | AT-22 |

## 5. 验收场景

| ID | 场景与通过条件 |
|---|---|
| AT-01 | amd64 manager 接收一个 Suite，两台 RV64 worker 并发执行不同 case；用户只有一个入口；worker 无用户提交接口。 |
| AT-02 | Linux 三种架构 manager 构建并启动，不安装科学计算库；RV64 worker/run 真机启动；amd64/arm64 worker/run 返回平台不支持，不产生部署副作用。 |
| AT-03 | 非 RV64、缺 RVV/版本不匹配、无空闲核/内存、未知协议、厂商核驱动失败均不可接单；公开/隐藏文件缺任一版本、schema_version 与所用 Schema 不符、旧 suite.version 或非法自身版本均拒绝；两文件可使用不同 Schema 版本；doctor 给出明确原因。 |
| AT-04 | 在准备好 runtime 的 RV64 真机断网执行 `run --spec <ready spec.json>`；只提供程序、测试输入包和已确认前置运行时，没有 config/token/战役目录；生成全部报告与正确退出码。micro 先具备 ready spec 和适配器才可成为验收 fixture。 |
| AT-05 | 同 case、seed、候选与环境在本地/分布式产生等价规范化输入与语义结果；时间戳/任务 ID 可不同；计时只检查协议与容差，不断言数字逐字相等。 |
| AT-06 | 同 op 多 shape/dtype 不覆盖；digest 改变产生新版本；oracle 缺失/随机无统计规则为 unverifiable；非法 Suite、资源篡改及许可证未核准不发布。 |
| AT-07 | 100 次并发重复提交只产生一个 Run；同键不同摘要为 409；不同主体不共用幂等记录。 |
| AT-08 | manager 在返回 202 后被杀再启动，作业不丢；worker 在产出结果后断网重启，outbox 继续回传；对象与索引写入故障可恢复且不会引用半文件。 |
| AT-09 | 未登记 worker、被撤销证书、错 worker 身份、错 boot_id、用户尝试 worker API 被拒；旧 session 不能重新获取租约。 |
| AT-10 | 大 Suite 与小 Suite 共存有公平机会；容量预留防止过量分派；不匹配 worker 不接单；timing 独占或经过已配置隔离，队列满为背压。 |
| AT-11 | 网络分区、续租 ACK 丢失、旧完成延迟、manager epoch 改变及时钟偏移注入；执行本地到期停机、完成提交单一有效、旧 worker 隔离至清理证明。 |
| AT-12 | 杀进程/断电覆盖 journal、deploy、run、restore、finish 各断点；重启先恢复再 ready；恢复失败 quarantined，Job 没有可信成功，基础设施重试预算有限。 |
| AT-13 | 两个 Attempt 写同槽位不碰撞；symlink/越界路径、加载 SHA 不符、锁不可用拒绝；取消/超时恢复；普通分布式 restore=false 拒绝。 |
| AT-14 | K3 双核簇、perf 核锁、acc/func 非计时、Subprocess/WarmFork 候选加载均按协议；核选错拒绝，常驻父进程不缓存候选。 |
| AT-15 | queued/run 中取消与完成竞争用 CAS 决定终态；收到取消的运行任务杀整个进程树再恢复；Run 显示部分成功及其余 canceled。 |
| AT-16 | eval_results.json 同时通过 Schema 与语义校验；所有 case 均有 outcome，历史失败重试不重复计数；缺返回/检查、选择不可信或未清理 attempt、spec 版本/摘要不符、伪造 median/speedup/summary 均拒绝；null 不变 0，func 不冒充数值通过；按环境分组，公开隐藏记录去掉参数/原始证据；同环境 resume 只复用可信完成。 |
| AT-17 | 候选/日志上传损坏或超限失败；临时对象不被读取；重复完成幂等，摘要冲突拒绝；worker 仅在持久 ACK 后清除 outbox。 |
| AT-18 | manager/worker readiness、lease expiry、cleanup failure、queue/resource 指标可查询；本地 run 不需要 Prometheus；无候选/token 泄漏。 |
| AT-19 | 代表性 v1 客户端路径与 result-v2/null/多 case 兼容回归；迁移停旧写者后才启新写者；回退前清理新 Attempt，核验安装树原摘要。 |
| AT-20 | 16 模拟 worker/1000 case 控制面压力测量，同时在至少两台 RV64 真机验证调度；输出 p50/p95 与测试配置，达标后才能宣称目标满足。 |
| AT-21 | 与 rvbench-kernel 校验正负样本及 digest 对照；多输入 Conv2D/TopK 多返回、None/零维/非连续 layout 均保真；draft/pending 不正式通过；不存在的 adapter 不回落别的 callable。 |
| AT-22 | public 模式在目录存在隐藏文件时依然只处理公开 spec；公开 API、报告、缓存清单与日志不得包含私有参数。 |

## 6. 公开集运行结果与退出约定

每 case 区分 `passed`、`failed`（数值/功能不通过）、`skipped`（声明能力不适用）、`unverifiable`（无验证规则）、`infra_error`、`canceled`。性能指标不是通过状态。摘要以原始选择的 case 数为分母，公开报告列出所有 outcome；可另给 applicable 子集指标但不得替代全集口径。

本地命令建议退出码：0=所选测试满足 Suite 声明的验收策略；1=测试不通过或存在未允许的 skip/unverifiable；2=参数、平台、配置或运行时前置错误；3=基础设施/清理失败；130=用户中断且已安全清理。混合错误按 infra_error > 参数错误 > 测试不通过裁决；清理失败优先返回 3。退出策略写入 Suite/run 快照。

## 7. 现状依据与新需求差异

| 现有证据 | 现状 | 新需求对应变化 |
|---|---|---|
| `evald.py:2143–2223, 2287–2327` | 每板 HTTP + Store + lane worker | manager 成为入口与权威，worker 独立 Agent；R-001/009/015。 |
| `evald.py:1223–1249, 1788–1881` | 两实例共享路径 flock，失败可退进程内锁，finally 恢复 | 新系统锁错误 fail closed，持久恢复 journal；R-019/020/023。 |
| `evald.py:1029–1045` | 重启只将残留 Job 标记 failed，没有启动恢复安装树 | reconcile 先于 ready；R-019/021。 |
| `warmd.py:151–239, 348–355` | Python warm parent + per-job fork | Runner 保留可选适配器；R-024。 |
| `clients/suite.py:82–137, 165–201` | 一个 op 只能一项，展开依赖战役 ticket | manager/local 共享 Suite 模型，稳定 case_id；R-007/008/009。 |
| `clients/backends.py:10–45, 111–145` | URL 与 host 标签用于后端/可比性 | worker 动态能力、测量环境 fingerprint；R-012/013/029。 |
| `clients/result_schema.py:21–43` | result-v2 统一 nullable 指标 | 扩展分布式 provenance，保留历史读接口；R-027/034。 |
| `docs/adr/0018-suite-v1-ticket-expansion.md` | suite 不进服务端是为守住 Byte-Freeze 的旧决策 | v2 明确变更此决策，仅对新实现生效；R-035。 |
| rvbench-kernel 的 `docs/specs/2026-10-08-spec-json格式.md`、`schemas/spec.schema.json`、`tools/validate_spec.py` | spec v2/2.0.0 是完整合同，顶层 version 为内容版本；隐藏 Schema 独立为 1.0.0；当前工作副本有未提交新增文件 | 原生加载 spec，固定格式资源快照并做跨语言一致性验证；R-036/039–041。工作副本事实不是 server HEAD 的已提交证据。 |
| rvbench-kernel 的 `schemas/eval_results.schema.json`、`tools/validate_eval_results.py`、`examples/eval_results.json` | 结果格式 1.0.0、跨字段校验与明确 synthetic 夹具，尚未接入 runner | Rust/Python 写入与读取复用同一合同，R-027/030/036；结果本身不能证明租约和测量真实性。 |

## 8. 设计交付与下一步

`specs.html` 必须为 R-001–R-042 给出可追溯的设计章节，包含两种运行方式、Rust 模块职责、持久化、状态机/租约、API/CLI、spec/结果契约、调度/隔离、迁移、风险与验收。命令和数据结构示例标为拟定接口，不能被当成已实现功能。已确认输入、语言及依赖边界；仍需验证最低系统版本、具体 Rust 依赖、micro 的 ready spec 与执行适配器。
