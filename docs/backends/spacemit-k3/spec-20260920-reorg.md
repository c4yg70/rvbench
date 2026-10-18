# spec — evald 及配套代码收拢重构计划（2026-09-20，待审批）

> 本文件是重构计划的**审批件**。批准前不执行任何整理动作；批准后按 §12 步骤执行，
> 本文件届时作为 `docs/spec-20260920-reorg.md` 入库存档。
> 前提：K3 板当前不可达——本次整理**纯本地（Windows）+ 纯静态**，板端动作全部推迟到板恢复后（§11.2）。

---

## 1. 背景与目标

- evald 服务端正本已在 `riscv-flagos-kernel-eval-server/`（HEAD `778defa`，已推 cnb.cool），
  但**配套代码散落**：客户端在 `bin/`（ev.py、eval_pipeline.py、pssh.py…），板端脚本混在
  `bin/` 约 110 个脚本里，`bin/` 里还有已废弃入口、6 个 .bak、论文工具、历史候选 kernel。
- 目标：把"当前在用"的 evald + 配套代码收拢进 `riscv-flagos-kernel-eval-server/`，
  形成**单一正本、清晰分区、零行为变化**的工程形态；所有现行命令行用法保持不变（垫片兼容）。

## 2. 现状盘点（两轮只读调研结论，2026-09-20）

1. **目标仓库 = 服务端正本，且与板上部署逐字节一致**（最高优先级事实）：
   `evald.py` md5 `2966864b31e956524707b089a43af8e0`、`warmd.py` md5
   `3cf28f96daeecbcd4371f4d9e725dbfb`，与 09-15 上板的加固版完全一致。
   仓库 main 干净、up to date with origin，共 10 个 commit。
2. 仓库现有：`evald.py`（3555 行单文件巨石，纯 stdlib，selftest 内嵌 ~1200 行）、
   `warmd.py`（366 行）、`evaldctl.sh`、`dashboard.html`（2186 行零依赖单文件）、
   `monitor/`（README＋prometheus.yml）、`docs/`（4 份服务端 ADR＋2 份设计文档）、
   `config.example.json`；`config.json`（真实 token）被 gitignore 留在工作区。
   **无** pyproject/requirements/tests；`__pycache__/` 有孤儿 `_static_check.pyc`（源已删）。
3. **配套散落（工作区 `bin/` 与 `runs/`）**：
   - 客户端/驱动：`bin/ev.py`（525 行，交互单评，惰性 import eval_pipeline 复用 judge/registry）、
     `bin/eval_pipeline.py`（1134 行，战役唯一驱动，judge()/registry 正本）、
     `bin/op_status.py`、`bin/recover_inflight.py`、`bin/pssh.py`（SSH 通道，paramiko）。
   - 板端配套脚本（evald 白名单契约的一部分，板上是 `~/autoke/scripts/`）：
     `bin/bench_op.py`（605 行计时三层分发）、`bin/bench_gemm.py`、
     `bin/torch_reference.py`、`bin/fallback_refs.py`（1300 行，数值判定正本）＋README。
   - 运维：`bin/evald_selftest_local.py`（Windows selftest shim）、`bin/precompile.py`＋
     `bin/precompile_candidate.sh`（X100 预编译）。
   - 历史副本：`results/k1/` 下 K1 四板 evald 归档（9 个 md5 版本）——属归档数据，不动。
   - 波级工具（a5–a10、kope.py 等）在 `runs/*/_tools/`——属冻结实验记录，不动。
4. **跨仓依赖（迁移必须处理的两处路径逻辑）**：
   - `eval_pipeline.py`：`ROOT = parents[1]`（工作区根）；`TOKEN` 读
     `ROOT/"riscv-flagos-kernel-eval-server"/"config.json"`；manifest/registry/BASELINE 全挂 ROOT。
   - `ev.py`：`ROOT = parents[1]`、`SELF_DIR`；token 链 = EV_TOKEN →
     `ROOT/"riscv-flagos-kernel-eval-server"/config.json` → `SELF_DIR/config.json` → 板上目录；
   - ADR 双宿主：工作区 `docs/adr/` 与仓库 `docs/adr/` 的 0001–0003 同 md5（有意双宿主），
     0004 两边是**不同** ADR（编号撞号）——维持现状，不合并。
   - 已有先例规格 `.qoder/specs/evald_uv_打包与_cnb_发布_task-c36.md`（uv 打包＋cnb CI＋私有
     PyPI）：**本次不采纳其包结构改造**（见 §10），仅与其不变式清单保持兼容。

## 3. 设计原则

| # | 原则 | 理由 |
|---|---|---|
| P1 | **冻结字节**：`evald.py`、`warmd.py`、`evaldctl.sh`、`config.example.json`、`dashboard.html`、`monitor/*` 一个字节都不改，移动前后 md5 校验 | 与板上部署逐字节一致是硬资产（无板可验证，改了就无法确认没改坏）；部署模型＝单文件直接 push |
| P2 | 单一正本：同一代码只允许一个可编辑正本位置；其余是垫片或归档 | 消灭"9 个 md5 版本不知道哪个是真"的现状 |
| P3 | 零行为变化：所有现行命令行（`python bin/ev.py …` 等）经垫片保持可用 | AGENTS.md/实验环境.md/子代理任务书/muscle memory 全指向 bin/ |
| P4 | Windows 只做静态验证（py_compile/ast/md5/rg/git diff），运行时验证推迟到板恢复（§11.2） | AGENTS.md 铁律＋板不可达 |
| P5 | 冻结实验记录不搬：`runs/*/_tools/`、`results/**`、K1 归档、历史一次性脚本留原地 | 它们是实验状态与审计链的一部分 |

## 4. 目标结构

```
riscv-flagos-kernel-eval-server/
├── README.md                  # 改写：仓库总览＋四区导航（server/clients/board/ops）＋部署不变式
├── CONTEXT.md                 # 保留
├── CHANGELOG.md               # 新增：收口散落日期注记（0911 加固等）＋本次整理＋当前 md5
├── pyproject.toml             # 新增：纯元数据（name/version/requires-python>=3.12/dependencies=[]），
│                              #   不建包不装包，只为工程位与将来 c36 阶段铺垫；不破 ADR-0003
├── evald.py                   # ┐
├── warmd.py                   # │ 冻结字节（仓库根＝部署单元：pssh --put 这三个文件＋config）
├── evaldctl.sh                # ┘
├── config.example.json        #   冻结字节
├── config.json                #   留根、继续 gitignore（token 正本，eval_pipeline 读它）
├── dashboard.html             #   冻结字节（file:// 直开，与 monitor/ 的描述耦合，不挪）
├── monitor/                   #   冻结字节（README＋prometheus.yml）
├── clients/                   # 【迁入】Windows 侧 evald 客户端与战役驱动（对 8788/8791 API）
│   ├── ev.py                  #   含 §6 最小路径改动
│   ├── eval_pipeline.py       #   含 §6 最小路径改动（judge/registry 正本）
│   ├── op_status.py
│   └── recover_inflight.py
├── board/                     # 【迁入】板端配套脚本（部署到 ~/autoke/scripts/，evald 白名单契约）
│   ├── bench_op.py
│   ├── bench_gemm.py
│   ├── torch_reference.py
│   ├── fallback_refs.py
│   └── fallback_refs.README.md
├── ops/                       # 【迁入】部署与运维
│   ├── pssh.py                #   SSH 通道（部署/运维底座，非 evald API 客户端）
│   ├── evald_selftest_local.py#   Windows selftest shim（注：受铁律约束，本机默认不跑）
│   ├── precompile.py
│   └── precompile_candidate.sh
├── docs/
│   ├── adr/0001..0004         # 已在
│   ├── eval-server-design.md  # 已在
│   ├── evald-优化设计-20260909.md   # 已在
│   └── relay-chain-design.md  # 【迁入】自工作区 docs/（eval_pipeline v2.1 接力链设计）
├── .gitignore / .gitattributes# 已在（__pycache__ 已覆盖新目录）
└── spec.md → docs/spec-20260920-reorg.md  # 本计划入库存档
```

## 5. 迁移映射总表

| 现位置（工作区） | 去向 | 方式 | bin/ 残留 |
|---|---|---|---|
| `bin/ev.py` | `clients/ev.py` | 移动＋§6 改动 | 兼容垫片 |
| `bin/eval_pipeline.py` | `clients/eval_pipeline.py` | 移动＋§6 改动 | 兼容垫片 |
| `bin/op_status.py`、`bin/recover_inflight.py` | `clients/` | 纯移动 | 兼容垫片 |
| `bin/pssh.py` | `ops/pssh.py` | 纯移动 | 兼容垫片 |
| `bin/evald_selftest_local.py` | `ops/` | 纯移动 | 无（文档改指） |
| `bin/precompile.py`、`bin/precompile_candidate.sh` | `ops/` | 移动＋壳内路径自查 | `precompile_candidate.sh` 垫片（实验环境.md §4.1 引用） |
| `bin/bench_op.py`、`bench_gemm.py`、`torch_reference.py`、`fallback_refs.py`＋README | `board/` | 纯移动（四件同迁，保住同目录互引） | 无（引用者按 §7 扫描修补） |
| 工作区 `docs/relay-chain-design.md` | 仓库 `docs/` | 移动 | —（引用处改路径） |
| `bin/eval_pipeline.py.bak-*`（6 份） | `tmp/archive-20260920/` | 归档 | 删（见 D3） |
| `bin/k3ssh`、`bin/k3scp` | — | 删除（实验环境.md §0 已作废） | — |
| `bin/run_baseline_group.py`、`run_recall.py`、`run_verify43_rerun.py`、`run_gemm_panel.py`＋`baseline_*.yaml/txt` | **留 bin/** | 不动（历史一次性基线工具，见 D5） | — |
| `bin/trace_check.py`＋fixtures、`k3mem.py`、论文工具、诊断/候选脚本 | **留 bin/** | 不动（非 evald 契约，见 D6/P5） | — |
| `runs/*/_tools/`、`results/**`（含 K1 归档、infra 文档） | 原地 | 不动（P5） | — |
| 仓库 `__pycache__/`（含孤儿 `_static_check.pyc`） | — | 删除（未跟踪，纯磁盘卫生） | — |

## 6. 唯二的代码改动（除垫片外，全部迁移文件零逻辑改动）

**改动 A：`clients/eval_pipeline.py` 头部路径常量**（原 L39–42）：

```python
# 原：
ROOT = Path(__file__).resolve().parent.parent
BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(ROOT / "riscv-flagos-kernel-eval-server" / "config.json"))["token"]

# 新：
SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parent        # = riscv-flagos-kernel-eval-server/
ROOT = REPO_DIR.parent            # 工作区根：runs/ results/ 语义不变
BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(REPO_DIR / "config.json"))["token"]
```

**改动 B：`clients/ev.py` 根定位＋token 链**（原 L63–64、L92–94）：

```python
# 原：
ROOT = Path(__file__).resolve().parent.parent
SELF_DIR = Path(__file__).resolve().parent
# 新：
SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parent        # = riscv-flagos-kernel-eval-server/（板上部署时 = ~/autoke）
ROOT = REPO_DIR.parent            # 工作区根（candidate 相对路径、runs/E1 读数语义不变）

# token 链（load_token 内）：
# 原：(ROOT / "riscv-flagos-kernel-eval-server" / "config.json", SELF_DIR / "config.json", BOARD_EVALD_DIR / "config.json")
# 新：(REPO_DIR / "config.json", SELF_DIR / "config.json", BOARD_EVALD_DIR / "config.json")
```

双部署形态自检（静态推演，写进 CHANGELOG）：
- Windows 仓内/垫片运行：`REPO_DIR/config.json` 命中（token 正本仍在仓库根，gitignore 不变）。
- 未来重新上板 `~/autoke/evald/ev.py`：`REPO_DIR=~/autoke`（无 config）→ `SELF_DIR/config.json`
  命中——与今日板上行为一致；板上 ROOT 变为 `~/`，仅影响 fail-soft 的 `.pipeline-config`
  读取与 `--judge`（板上本就明确报错不可用），金丝雀命令 `run --op add --shape 64,512,512` 不受影响。

## 7. bin/ 垫片与依赖扫描

**垫片（6 个，python 用 runpy、shell 用 exec）**：`ev.py`、`eval_pipeline.py`、`op_status.py`、
`recover_inflight.py`、`pssh.py`、`precompile_candidate.sh`。python 垫片模板：

```python
#!/usr/bin/env python
"""兼容垫片：正本已迁至 ../riscv-flagos-kernel-eval-server/clients/ev.py（2026-09-20 整理，spec §7）。"""
import runpy, sys
from pathlib import Path
d = Path(__file__).resolve().parents[1] / "riscv-flagos-kernel-eval-server" / "clients"
sys.path.insert(0, str(d))          # ev.py 惰性 `from eval_pipeline import ...` 依赖此路径
runpy.run_path(str(d / "ev.py"), run_name="__main__")
```

效果：`python bin/ev.py run …` 等所有现行命令逐字不变（argv/CWD/相对路径语义不变）。

**依赖扫描（执行期硬步骤）**：`rg -l "fallback_refs|bench_op|torch_reference|bench_gemm|evald_selftest_local|precompile\.py"` 
横扫 `bin/*.py` 与 `runs/**/_tools/*.py`，对每个引用 moved 文件路径/import 的脚本：
- 按**路径常量**引用的（如 run_recall 携带修补版 bench_op、preflight_ops 引 fallback_refs）→
  改指 `board/` 新路径（一行级改动，逐个列进执行日志）；
- 按**模块名** import 的 → 在该脚本头部加两行 sys.path 引导指向 `board/`。
已知确定命中：`bin/preflight_ops.py`（fallback_refs 覆盖检查）；其余以扫描结果为准，扫出几个改几个。

## 8. 文档更新清单（只改"活文档"，历史记录一律不动）

| 文档 | 改动 |
|---|---|
| 仓库 `README.md` | 改写：四区导航（server/clients/board/ops）、部署不变式（md5 冻结、pssh --put 文件清单）、把指向 `../bin/*` 的引用改指 `clients/`/`ops/`、补"bin/ 垫片"说明与板恢复回归门（§11.2）链接 |
| 仓库 `CHANGELOG.md` | 新增：时间线收口（09-07 设计定稿→09-09 feature flags→09-11 加固上板→09-15 W0+多计时车道 `778defa`→09-20 本次收拢）＋当前冻结文件 md5 表 |
| 仓库 `pyproject.toml` | 新增（§4） |
| `AGENTS.md` | §评估/§并发评测里的 `bin/ev.py`、`bin/eval_pipeline.py` 表述改为"正本在 eval-server 仓，bin/ 留垫片"（命令行写法不变） |
| `实验环境.md` | §0 pssh 正本位置、§4.2 `PY`/token/ev.py 正本路径、§4.4 bench_op 路径 → 改指新位置（命令不变） |
| `并发评测经验.md` | 工具链分工段落补一句正本位置 |
| `runbook.md` | §3.5 补正本位置；顺手修 §1 仍写 k3ssh/k3scp 的过期表述（与实验环境.md §0 对齐） |
| 工作区 `docs/` | `relay-chain-design.md` 移走后，扫引用处改指仓库内路径 |

## 9. 工程化新增的理由与边界

- `pyproject.toml`：**只有元数据**（name=evald-server、version=1.0.0 对齐 `server_version "evald/1.0"`、
  requires-python>=3.12、dependencies=[]）。不建 src 包、不加 entry points、不配 CI——
  因为 P1 冻结与 ADR-0003（stdlib 单文件直部署）不允许现在拆包；pyproject 是工程占位，
  让"零运行时依赖、目标解释器≥3.12"成为仓库内可机读的事实。
- `CHANGELOG.md`：现状的版本痕迹散在 docstring/注释/README 三处（server_version、metrics
  version=0.0.4、两处 0911 加固标记），收口成单一查阅点。
- 不建 `tests/`：测试形态就是 `evald.py --selftest`（内嵌 20 段）＋板上最小 config；在 Windows
  建 pytest 目录是空仪式（铁律禁跑）。

## 10. 非目标（本次不做）

1. **不拆 evald.py 巨石、不做 uv 打包/CI/私有 PyPI**：即不执行 `.qoder/specs/…c36` 的阶段 1——
   它要求改 warmd_path 解析、--config 默认值并重跑板上 selftest 做门禁，与 P1 冻结和"板不可达"
   冲突。其不变式清单（A100 start-ai 同 PID、warmd 走 venv python、config 外置）已在 README
   继续成文，留作板恢复后的可选阶段 2。
2. 不动 `runs/**`、`results/**`（含 `results/infra/*.md` 各加固记录——它们是带日期的审计记录，
   仓库 README 以相对路径引用、不搬运）。
3. 不动 K1 归档（`results/k1/**` 的 9 个 evald md5 版本＝历史演进链快照）。
4. 不迁移 a5–a10/kope.py 波级工具（冻结实验记录）。
5. 不做板端任何写操作（板不可达；恢复后的动作见 §11.2 且需另行天窗确认）。

## 11. 验证方案

### 11.1 本次可做（Windows，全部静态）
1. 冻结校验：移动/整理前后 `md5sum` 六件冻结物逐一比对（§3 P1 表）。
2. 语法校验：`py_compile` 覆盖仓库全部 `.py`（含垫片）。
3. 路径推演：§6 双部署形态推演表写入 CHANGELOG；`rg` 确认 `clients/` 内无残留
   `"riscv-flagos-kernel-eval-server"` 自引用路径。
4. 依赖扫描闭环：§7 的 rg 扫描结果清单＝修补清单，逐条打勾。
5. git：仓库单 commit，`git status` 干净、`config.json` 确认未被跟踪；`git show --stat` 人工复核。

### 11.2 板恢复后（回归门，另择天窗执行，非本次范围）
1. 板上 `md5sum` evald.py/warmd.py 对仓库（应仍逐字节一致——本次没改它们）。
2. 金丝雀：板上 `ev.py run --op add --shape 64,512,512` ＋ Windows 侧 `python bin/ev.py status`。
3. `evald.py --selftest`（板上最小 config 口径，234 PASS 基线）。
4. 重新上板 `clients/ev.py` → `~/autoke/evald/ev.py`（板侧现仍为旧副本）。
5. `eval_pipeline` 以一个 dry 工单跑半轮确认 ROOT/registry 落点无漂移。

## 12. 执行步骤（批准后，预计单会话完成）

| # | 步骤 | 产出/校验 |
|---|---|---|
| S0 | 快照基线：冻结物 md5、bin/ 文件清单、`git rev-parse HEAD` | 回滚依据落 `tmp/archive-20260920/manifest.txt` |
| S1 | 建 `clients/ board/ ops/`，按 §5 移动文件（冻结物不动） | 文件树对照 §4 |
| S2 | §6 两处最小改动＋`py_compile` 全绿 | diff 粘贴进执行日志 |
| S3 | bin/ 6 垫片＋删 k3ssh/k3scp＋.bak 归档＋依赖扫描修补（§7） | 扫描清单逐条打勾 |
| S4 | README 重写、CHANGELOG/pyproject 新增、活文档更新（§8）、`relay-chain-design.md` 迁移 | rg 无活文档残留旧路径 |
| S5 | 冻结物 md5 复验＋仓库 commit（单 commit，main） | `git show --stat` 复核 |
| S6 | （D4 批准则）`git push origin main` | cnb.cool 与本地一致 |

## 13. 风险与回滚

| 风险 | 缓解 |
|---|---|
| ev/eval_pipeline 路径改动引入回归，且无板可跑运行时验证 | 改动收敛为 §6 两处常量级 diff；垫片保证入口不变；§11.2 回归门兜底；出问题 revert 单 commit 即回滚 |
| bin/ 内未知的路径引用者被移动破坏 | §7 扫描是硬步骤，扫出即修；垫片覆盖 6 个高频入口 |
| 删除 .bak/k3ssh 丢历史（工作区无 git） | .bak 先归档 `tmp/archive-20260920/`；k3ssh/k3scp 内容仅数十字节且实验环境.md 有作废记载 |
| push 泄漏敏感信息 | `config.json`/`evald.env` 在 gitignore；S5 的 `git status`/`git show --stat` 人工确认无 token 文件 |
| 与并行改稿冲突 | 本次整理期间不派实验子代理（板也不可达，无在跑作业） |

## 14. 待批决策点（默认值＝推荐项，审批时可逐条改）

| # | 决策 | 推荐 | 备选 |
|---|---|---|---|
| D1 | bin/ 垫片范围 | 6 个高频入口（ev/eval_pipeline/pssh/op_status/recover_inflight/precompile_candidate.sh） | 全部迁入物都留垫片（+3）/ 不留垫片只改文档（命令行全变，不推荐） |
| D2 | 板端脚本（bench_op 等 5 件）迁入 `board/` | 迁入（evald 白名单契约应与服务器同仓） | 留 bin/（少一次依赖扫描，但契约继续散落） |
| D3 | `bin/eval_pipeline.py.bak-*` 6 份处置 | 移 `tmp/archive-20260920/` 后从 bin/ 删除 | 直接删除 / 原地保留（不推荐，正本已入 git） |
| D4 | 整理完成后是否 push cnb.cool | 是（单 commit，远端是私有仓，板恢复后同步有据） | 只本地 commit，push 另行批准 |
| D5 | 历史 run_*（baseline/recall/verify43/gemm_panel）与 baseline 数据文件 | 留 bin/（一次性历史工具，属实验记录） | 迁 `clients/archive/` 冻结 |
| D6 | `trace_check.py`＋fixtures | 留 bin/（T3 实验工具，非 evald 契约） | 迁 board/ |

---

## 附：审批与执行记录（2026-09-20）

**审批**：作者批复「同意计划，所有垫片/脚本都移入库中」——即 §14 决策按全量入库执行：
- D1 → **全量垫片**：所有被迁移的 bin/ 入口（12 py + 1 sh）均留垫片；**垫片母本入库 `shims/`**（随仓版本化），bin/ 下为部署副本（部署方式见 `shims/README.md`）。
- D5 → run_*×4 ＋ baseline 数据文件迁入 `clients/archive/`（路径常量随迁修指，逻辑零改动）。
- D6 → `trace_check.py` ＋ README ＋ `trace_fixtures/` 迁入 `tools/`。
- D3/D4 → 按推荐执行（.bak 归档至工作区 `tmp/archive-20260920/` 后删；完成后 push cnb.cool）。

**执行实况**（与 §12 的差异点）：
- 扫描额外发现并修复：`recover_inflight.py`（token CWD 相对路径）、`op_status.py`（`parents[1]`→`parents[2]`）、
  `ops/evald_selftest_local.py`（evald.py 定位深一级）、`ops/precompile_candidate.sh`（`cd ..`→`cd ../..`）。
  `runs/E1/KOPE/_tools/diag-probes/diag_b_warmfork.py` 的 `import bench_op` 走板上绝对路径，无需修。
- `ops/precompile_candidate.sh` 传输层仍为已废弃的 `bin/k3ssh`（脚本本就自 09-09 起不可用）——
  已在脚本头与 CHANGELOG 标注⚠复用前须迁 pssh，未做逻辑改写。
- ADR-0014 自工作区双宿主一份入仓（md5 `3cfb1255…` 两处一致），修掉 README 死链。
- **EOL 事实（执行中发现并已定规）**：`evald.py`/`dashboard.html` 的**工作副本＝板端字节＝CRLF**，
  git 索引历来是 LF（.gitattributes 归一）——两者 md5 不同不是漂移。md5 校验一律对工作副本；
  收拢后新迁入文件已统一 LF 工作副本。
- 验证：AST 全量 30 文件零语法错误；13 垫片转发目标逐一存在且可解析；冻结物 md5 前后比对（见 CHANGELOG）。

*调研出处：两轮只读子代理报告（2026-09-20，目标仓库盘点＋工作区生态盘点）＋主会话对
ev.py/eval_pipeline.py 路径逻辑、ADR md5、`.qoder` c36 规格的复核。*
