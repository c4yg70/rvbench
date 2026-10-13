# CHANGELOG — riscv-flagos-kernel-eval-server (evald)

版本痕迹的单一查阅点（此前散在 README/docstring/注释里）。
HTTP 响应头 `server_version` 现值 `evald/1.0`；`/metrics` exposition `version=0.0.4` 是文本格式版本号，与本文件的项目版本无关。

## 2026-10-07 — K3 板恢复回归门全绿 ＋ 多评测后端三件套落地（B3/B2/B1/A1+A2）

板重新上线（10.18.60.100，HUMAN-K3），「板恢复后回归门」0-5 步全部执行完毕：

- **门 0 安装树未污染**：567 条缺 `restored_sha256` 的 manifest 全为历史中间态——逐路径按
  「最后条目」判定，无任何路径处于"未恢复且当前=部署值"；34 个文件为战役定稿＋W0 补丁态
  （`bench_op.py` W0 输入域补丁、`*_best.diff` 定稿等），属预期非污染；`outer.py`
  existed=False 已按恢复语义回收；无 `.tmp` 暂存残留。
- **门 1 md5 对账**：`evald.py` 一致（2966864b）；**`warmd.py` 板上比正本多 W0-2
  `PR_SET_PDEATHSIG`（0915 上板未回仓）→ 正本吸收板端字节**，冻结清单 md5 重登记
  `023ad2de0fe49f93be9c84c69425bc06`；板上 `evaldctl`（无 `.sh`）与仓 `evaldctl.sh`
  **双向分歧**（板有 W0-2 孤儿 warmd 清理、仓有 PY 解释器覆盖）——未合并，动它须过板上
  selftest 门禁；dashboard/monitor 本就是 Windows 侧文档、不在 k3 板部署单元。
- **门 2 金丝雀**：旧板上 ev.py 与新 Windows ev.py 各跑 add 64,512,512，
  speedup 0.4909/0.4851/0.4855（三跑一致，可复现）。
- **门 3 selftest**：**最小 config 口径 ALL PASS**（README 234 基线之后 selftest 又扩了
  [7b]/[7c]/[15]-[20] 等节）。口径事实：SSH 会话亲和 0-7，带生产 config 跑会在 [4] 假 bench
  的 `taskset -c 8-15` 处 EINVAL——计时核 8-15 只有 evald 服务进程（`/proc/set_ai_thread`）
  可达；生产 config 还有 5 项"缺省语义"断言错配（OMP/taskset/output_selfcheck/推导），
  均非代码回退。
- **门 4 ev.py 重推上板**（`~/autoke/evald/ev.py`，备份 `.bak-pre-multi-backend-20261007`；
  同推 backends.py/result_schema.py）金丝雀通过。
- **门 5 dry 工单半轮**：沙箱工作区（`EV_WORKSPACE` 临时根、`baseline_paused`/relay 全关）
  3 票全链路（timing×2 → 8788 @k3，correctness×1 → 8791 @k3-acc），ROOT/registry/结果
  落点无漂移，真实战役工作区零接触（registry mtime 复核不变）。

多后端升级项落地（**全部客户端侧，七件冻结物零改动**；决策清单 2026-09-28 的 B3→B2→B1→A1 定案顺序）：

- **B1+B3 `clients/backends.py`**：`BackendProfile` 四档案（k3 在役、k1/sg2042/rvv_general
  规划占位）、`registry_key`（k3 保持旧格式——424 个既有键零漂移；非 k3 一律 `b:<backend>/`
  前缀，物理杜绝跨后端共用参照）、`comparable`（仅同 host 标签可比，failover 纪律）、
  `base_label` 统一正本（k3/k3-acc 逐字不变；未知 URL 由旧管线侧 "k1-proxy" 兜底改为原样串，
  与历史 host 值不等必触发重定基——宁可重定基不许假可比）。
- **B2 `clients/result_schema.py`**：result-v2 统一视图——一切指标允许 None（正确性车道
  speedup 无统计意义、跨后端无同簇基线、多 shape 顶层无标量），backend/host_tag/lane 三轴
  入 schema；`from_raw_v1`/`load_result_v2` 读侧双 schema 兼容，历史文件不迁移。
- **A1+A2 `clients/suite.py`**：suite-v1 校验正本（条目三键与 baseline_manifest 同构，
  `from_baseline` 直升格）＋ `expand` 落盘 ticket 后完全复用 scan/submit/finalize 链路
  （`_synth_relay_ticket` 先例）；STOCHASTIC 集自 eval_pipeline 源码 AST 吸入（单一正本，
  规避其 import 副作用）；幂等重跑按 raw.json 存在性 skip。
- **接线 `clients/ev.py` ＋ `clients/eval_pipeline.py`**：`EV_BACKEND` 环境变量 / `--backend`
  参数选档案；默认 k3 行为零变化，无 backends.py 的独立部署（板上）自动回落旧常量；
  `judge()` registry 键按 backend 升格（k3 路径走本地 `cond_key` 保证零漂移）；raw 与
  result.json 之外随写 `{stem}.result-v2.json`；registry 值非 k3 追加 `backend` 溯源字段。
- **测试与文档**：`tests/` 新建 45 用例全绿（`python -m unittest discover -s tests`）；
  ADR-0016/0017/0018 入仓、ADR 索引更新（0016 起跳号避开工作区 0015）；CONTEXT.md 增
  「评测后端 (Backend Profile)」「评测集 (Suite)」两术语（backend 三义消歧）；本地
  config.json（gitignore 件）同步板端现行字节。
- **冻结字节清单更新**：`warmd.py` `023ad2de0fe49f93be9c84c69425bc06`（吸收板端 W0-2，
  与板逐字节一致）；其余六件 md5 不变。

## 2026-09-20（三次） — 监控改造 spec 改址重写（服务化拓扑 · 角色化宿主，待审批）

- **监控改造计划迁址并重写**：`docs/spec.md` → [docs/specs/2026-09-20-监控栈改造.md](specs/2026-09-20-监控栈改造.md)，
  按规格说明书结构重写（问题陈述／方案概述／接缝／已定决策／用户故事／阶段计划／契约／测试决策／
  风险／明确不做／补充说明）。旧文件删除，内容见 `c30ab8a`。
- **拓扑前提变更（用户决定）**：第二块 K3 板即将到位——一块专责 perf 计时测试，另一块承担 acc、func
  与 Nightingale 这类监控/面板组件。监控枢纽（VictoriaMetrics ＋ Nightingale）由「开发机容器／WSL2」
  改为**落在 acc/func 宿主本体**，全案不再依赖 WSL2／端口转发／容器宿主；宿主角色与实例改由**板清单**
  （`boards.json`）声明，spec 不写死宿主数量与身份。
- **随之调整的三处硬约束**：① riscv64 自建范围由 Categraf 一件扩到三件（＋VM ＋n9e），前移为 P0
  硬门禁（R1）；② 告警表达式按 `board` 分组生成——两宿主的同名核簇是两台机器，跨宿主 `avg` 会把读数
  混成一个；③ 两宿主 `clusters`/`workers` 须同串（冻结面板对 `up{job="boards"}` 取末条命中），
  且监控钉核与 evald 整簇注册不重叠（计时节点钉 0-7、枢纽节点钉 8-15，见 ADR-0012/0014）。
- **目标架构图改服务化视角**（用户反馈"先写成软件框架，不要写板 A / 板 P"）：
  [docs/architecture-dev.html](architecture-dev.html) 的主图改为**服务分层**——被观测对象 →
  采集器 → 写入代理 → 时序库 → 告警与大盘 → 消费端，组件以服务为单元、不绑宿主；宿主落位与钉核挪到
  图外的「部署落位」角色表（计时节点 / 枢纽节点 / 消费端），由板清单声明，**新增或替换宿主不改本图**。
  页内链接改指新 spec；README 的文档索引与 `docs/` 分区行同步。
- **字节冻结不变**：七件冻结物本次**零改动**，md5 逐一复核与下方 2026-09-20 节清单一致；
  `monitor/*` 未碰。

## 2026-09-20 — 配套代码收拢（spec-20260920）

**仓库成为 evald 服务端＋全部配套代码的单一正本**；工作区 `bin/` 同名入口改为兼容垫片
（母本 `shims/`，旧命令行逐字不变）。详见 [docs/spec-20260920-reorg.md](spec-20260920-reorg.md)。

- 新分区：`clients/`（ev.py、eval_pipeline.py、op_status.py、recover_inflight.py）、
  `clients/archive/`（run_recall 等 4 个历史基线工具＋baseline 数据文件）、
  `board/`（bench_op/bench_gemm/torch_reference/fallback_refs）、
  `ops/`（pssh/evald_selftest_local/precompile*）、`tools/`（trace_check＋fixtures）、
  `shims/`（14 个 bin/ 垫片母本）。
- 路径适配（常量级，非逻辑改动）：ev.py / eval_pipeline.py 引入 `SELF_DIR/REPO_DIR/ROOT`；
  op_status.py `parents[2]`；recover_inflight.py / archive×4 token 改读 `REPO_DIR/config.json`；
  evald_selftest_local.py 的 evald.py 默认定位；precompile_candidate.sh 的 `cd ../..`。
- `bin/k3ssh`、`bin/k3scp` 删除（2026-09-09 起废弃）；6 份 `eval_pipeline.py.bak-*`
  移出 bin/（工作区 `tmp/archive-20260920/`）；`ops/precompile_candidate.sh` 传输层仍为
  k3ssh——**已标注⚠，复用前须迁 pssh**。
- 新增 `pyproject.toml`（纯元数据，零运行时依赖，不建包——ADR-0003 不变）。
- ADR-0014（双计时车道）自工作区 `docs/adr/` 双宿主一份入仓，修掉 README 死链。
- **冻结字节清单（本次零改动，md5 前后一致）**：
  `evald.py` `2966864b31e956524707b089a43af8e0`、`warmd.py` `3cf28f96daeecbcd4371f4d9e725dbfb`、
  `evaldctl.sh` `8c0b9e8d078a6d1c52e75f0e1ff08eee`、`config.example.json` `328b2e351210c0269039e9f9aac8e1f7`、
  `dashboard.html` `da364ef19201cb2f1ba58ad560e4daef`、`monitor/README.md` `6a58bd3909da40c8491ee5e544592035`、
  `monitor/prometheus.yml` `778614b51f311a53b40feeeb481283c5`。
  ⚠ md5 校验**对工作副本**（＝板端部署字节）：`evald.py` 与 `dashboard.html` 的工作副本/板端为
  **CRLF** 行尾，git 索引按 `.gitattributes`（eol=lf）存 LF——`git show HEAD:evald.py` 的 md5
  是 LF 形态（`324baede…`/`96266bd2…`），**不是**漂移；上板 push 一律用工作副本。
- 验证口径：板不可达期间仅静态（AST 全量、垫片转发目标存在性、md5 前后比对）；
  **板恢复后回归门**见 README「板恢复后回归门」节（含重推 `clients/ev.py` 上板）。

## 2026-09-20（二次整理）— bin/ 垫片退场 ＋ 收拢遗留死引用清理

承同日收拢（上文）：检出目录已改名 `rvbench-kernel` 并移出工作区，既不在工作区内、
也不再是 `bin/` 的兄弟目录 → 垫片的解析前提与"仓外工作区"假设一并作废。按**bin/ 入口作废**处置。

- **垫片退场**：删除 `shims/`（12 py + 1 sh 母本 ＋ README）；工作区 `bin/` 下同名副本不再
  维护。入口一律走仓内：`python clients/ev.py …`。spec §7／§14-D1 的垫片决策随之作废。
- **死引用清理**（收拢时的依赖扫描未覆盖到的一批）：
  - `clients/recover_inflight.py` FIX_NOTE 源：`bin/run_recall.py` → `clients/archive/run_recall.py`
    （沿用旧路径必 `IndexError`）。
  - `clients/eval_pipeline.py`：`PSSH = ROOT/"bin"/"pssh.py"` → `REPO_DIR/"ops"/"pssh.py"`。
  - 用法/路径文本改指新分区：`clients/ev.py`、`clients/op_status.py`、
    `ops/{pssh,precompile,evald_selftest_local}.py`、`tools/trace_check.py` ＋其 README、
    `board/{bench_op,fallback_refs}.py` ＋`fallback_refs.README.md`、`docs/relay-chain-design.md`；
    `baseline_shapes.yaml` 改指实际位置 `clients/archive/`（板上部署名 `~/autoke/scripts/baseline_shapes.yaml`
    不变）。**未改**：`clients/archive/**`、`docs/adr/**`、`docs/evald-优化设计-20260909.md`、
    `ops/precompile_candidate.sh`（待隔离）、`evald.py` 内注释（冻结文件）。
- **仓库卫生**：`.gitignore` 补 `evald.pid`／`evald.env`／`*.tmp`；删除 `__pycache__/`
  （含 spec §117 点名要删的孤儿 `_static_check.pyc`）。
- **冻结物 md5 双形态登记 ＋ 禁令（补强上文口径）**：冻结物只认**工作副本**（＝板端字节）；
  对冻结物**禁止** `git checkout -- <file>`／`git restore`——`.gitattributes`（`eol=lf`）会把
  CRLF 工作副本换成 LF，md5 立刻漂移，且 fresh clone 无法自证冻结。

  | 冻结文件 | 工作副本（板端字节） | git 索引 blob |
  |---|---|---|
  | `evald.py` | `2966864b31e956524707b089a43af8e0`（CRLF） | `324baedeb2c1c475e8bd8ececd82e641`（LF） |
  | `dashboard.html` | `da364ef19201cb2f1ba58ad560e4daef`（CRLF） | `96266bd265cef9aa8834362dc46bf5da`（LF） |
  | `warmd.py` | `3cf28f96daeecbcd4371f4d9e725dbfb` | 同左 |
  | `evaldctl.sh` | `8c0b9e8d078a6d1c52e75f0e1ff08eee` | 同左 |
  | `config.example.json` | `328b2e351210c0269039e9f9aac8e1f7` | 同左 |
  | `monitor/README.md` | `6a58bd3909da40c8491ee5e544592035` | 同左 |
  | `monitor/prometheus.yml` | `778614b51f311a53b40feeeb481283c5` | 同左 |

- **客户端工作区根改为显式**：`clients/{ev,eval_pipeline,op_status,recover_inflight}.py` 的 `ROOT`
  支持 `EV_WORKSPACE` 覆盖（缺省仍取仓的父目录），解析不到工作区时**具名报错**而非
  `FileNotFoundError`；`recover_inflight.py` 的 `OUT` 由 CWD 相对改为工作区根相对（该文件
  此前就因 CWD 相对路径出过问题）。`clients/archive/**` 未改（冻结历史）。
- **文档对账（活文档）**：README 的 `perf` 车道锁语义由"`max_concurrency>1` 仍被单锁串行化"
  更正为按 worker 分区（原文停留在 ADR-0014 之前，与同页 §计时互斥 自相矛盾）；ADR-0002 文末
  补修订注记并反链 0014；新增 [docs/adr/README.md](adr/README.md) 索引（标出 0005-0013
  未入本仓、ADR-0012 悬空、0004 与工作区撞号、编号 0011 两处皆缺）；`CONTEXT.md` 修正
  `EvalJob`→`Job`（以代码实体名为准）与计时互斥口径，并补 计时车道／字节冻结／战役工作区
  三条术语；`pyproject.toml` 增 `[project.optional-dependencies] ops = ["paramiko>=3"]`
  （pssh 的真实依赖此前只在注释里）。
- **新增 [docs/architecture.html](architecture.html)**：自包含架构总览（部署拓扑／作业生命周期
  与持锁区间／车道与并发／不变量与门禁／分区与板端契约／可观测性／本次审计发现），内联 CSS+SVG、
  零外部依赖、`file://` 直开；内容以代码事实为准（含 `run_job` 的锁顺序：path_lock 先于计时锁、
  计时锁覆盖部署到恢复整段）。
- 验证：全量 `py_compile` 绿；`rg` 确认活文档/代码无 `bin/`／`shims` 残留引用（`clients/archive`、
  ADR、设计稿与 CHANGELOG 历史条目除外）；7 件冻结物 md5 与上表逐一相符（本次未碰其字节）。
- **遗留（不在本次范围）**：`ops/precompile_candidate.sh` 的传输层仍为已废弃的 k3ssh，待隔离；
  `clients/archive/**` 的 `ROOT = REPO_DIR.parent` 未接 `EV_WORKSPACE`；`monitor/README.md:55`
  的 docker 挂载路径仍指向收拢前已空的目录——该文件在冻结清单内，改动前需先把"monitor/ 是
  Windows 侧文档、不属板端部署单元"这一例外写进冻结清单，故本次未改。

## 2026-09-15 — W0 基建天窗 ＋ `778defa`

多计时车道（A100 分核 8-11/12-15）＋ path_lock 部署互斥 ＋ 0911 加固 ＋ dashboard/monitor 入库。
板上 evald.py/warmd.py 与本提交逐字节一致（md5 见上）。W0 八项竣工记录：
工作区 `results/infra/infra-20260915-w0-hardening.md`。

## 2026-09-11 — 加固上板（P0/P1/P2）

依据 0910/0911 两夜事故：恢复后回读 sha 校验（L631）、槽位投毒加固（L801）、
`path_lock_dir` 双实例共用、`require_verified_base`、`warmd_output_selfcheck`（仅 8788）。
提案/报告/验收单：工作区 `results/infra/evald-{improvement-proposal-20260911,hardening-report,acceptance-checklist}.md`。

## 2026-09-09 ~ 09-10 — feature flags ＋ 8791 旁路 ＋ ev.py 直连通道

- perf/acc/func 三评估器 feature flags（`evaluators` 节＋`perf` 资源覆盖层）。
- K3 双实例定型：8788 纯计时（A100 8-15）/ 8791 acc+func 旁路（X100 0-7，
  systemd 瞬态单元 `evald-acc.service`，`CPUAffinity=0-7`）。
- `ev.py` 一次性直连客户端上线（交互单评免工单免常驻驱动；板上副本 `~/autoke/evald/ev.py`）。

## 2026-09-07 — 设计定稿（grilling）

`docs/eval-server-design.md`：HTTP 队列服务取代 ssh+flock；ADR-0001（部署-恢复）、
ADR-0002（共享计时互斥）、ADR-0003（stdlib-only）、ADR-0004（WarmFork 不变量预热）。
首个可用版 M1 起（仓库首提交 `2534f35`）。
