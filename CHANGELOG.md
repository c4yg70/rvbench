# CHANGELOG — riscv-flagos-kernel-eval-server (evald)

版本痕迹的单一查阅点（此前散在 README/docstring/注释里）。
HTTP 响应头 `server_version` 现值 `evald/1.0`；`/metrics` exposition `version=0.0.4` 是文本格式版本号，与本文件的项目版本无关。

## 2026-09-20 — 配套代码收拢（spec-20260920）

**仓库成为 evald 服务端＋全部配套代码的单一正本**；工作区 `bin/` 同名入口改为兼容垫片
（母本 `shims/`，旧命令行逐字不变）。详见 [docs/spec-20260920-reorg.md](docs/spec-20260920-reorg.md)。

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
  补修订注记并反链 0014；新增 [docs/adr/README.md](docs/adr/README.md) 索引（标出 0005-0013
  未入本仓、ADR-0012 悬空、0004 与工作区撞号、编号 0011 两处皆缺）；`CONTEXT.md` 修正
  `EvalJob`→`Job`（以代码实体名为准）与计时互斥口径，并补 计时车道／字节冻结／战役工作区
  三条术语；`pyproject.toml` 增 `[project.optional-dependencies] ops = ["paramiko>=3"]`
  （pssh 的真实依赖此前只在注释里）。
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
