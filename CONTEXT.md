# evald — 术语表

评估服务器（evald）的领域语言。背景：在 SpacemiT K3（RISC-V）上远程评估本地优化好的 FlagGems/Triton 算子代码。

## Language

**评估作业 (Job)**:
一次提交给评估服务器的原子评估请求：候选文件包 + 评估参数 + 部署语义。
代码实体为 `evald.py` 的 `class Job`。
_Avoid_: task、benchmark request、EvalJob

**候选文件包 (Candidate Files)**:
随作业提交的代码文件集合（相对 flag_gems 包的路径 + 文件内容），服务器按路径白名单校验后才部署。
_Avoid_: patch、diff

**部署-恢复 (Deploy-Restore)**:
评估作业的执行语义：评估前备份 site-packages 原文件并断硬链写入候选，评估后自动恢复原文件。
_Avoid_: 覆盖即留、last-writer-wins

**定稿作业 (Finalize Job)**:
`restore: false` 的评估作业：评估完成后候选代码保留在 site-packages，用于固化最优版本。

**计时互斥 (Timing Mutex)**:
一切计时执行必须独占计时锁的不变量；评估服务器 worker 与手工 SSH 基准共用同一族锁文件。
单 worker 用 `/tmp/k3_bench.lock`；多计时车道按 worker 分区为 `/tmp/k3_bench.lock.<index>`
（ADR-0014）——此时手工基准与任何抢锁的回退路径必须**同时持有全部分区锁**。

**作业号 (Job ID)**:
评估作业的 UUIDv7 标识，时间有序，贯穿异步跟踪与历史记录；历史查询以其为游标。

**执行器 (Executor)**:
评估服务器中实际运行基准的可替换执行组件；v1 为每作业子进程，v1.1 为常驻预热 + fork 隔离的暖执行器。

**微基准 (Microbench)**:
bench_op.py 的单算子快速计时（5~30s），单行 JSON 输出 torch_ms/gems_ms/speedup/correct/max_abs_err。

**全量验收 (Full Acceptance)**:
pytest benchmark 全量跑分（约 2 分钟/算子），算子优化的验收口径；不进评估队列。

**计时车道 (Lane)**:
作业按用途分列的调度单位：`timing`（计时，持分区计时锁）/ `correctness`（正确性，多 worker、
不持计时锁）/ `func`。feature flag 名 `perf`/`acc`/`func` 分别映射到三者。

**评测后端 (Backend Profile)**:
clients/backends.py 中 BackendProfile 描述的一台/一族评测靶机的客户端侧档案（k3/k1/sg2042/rvv_general）
——base URL、host 标签与缺省参数；registry 键与结果 schema 的 backend 轴由此承载。
_Avoid_: backend（三义：flag_gems runtime/backend 目录、k1-proxy healthz 的 backends 数组、本义——
仓内说「后端」须带 Profile 或写全）、target 混用。

**评测集 (Suite)**:
suite-v1 JSON 文档定义的一次成批评测的算子清单（op/shape/dtype 条目＋缺省参数＋backend 档案名）；
clients/suite.py 展开为 ticket 后完全复用 scan/submit/finalize 链路，suite 不进服务端。
_Avoid_: benchmark set、manifest（manifest 特指 baseline_manifest 基线清单）。

**字节冻结 (Byte-Freeze)**:
`evald.py`、`warmd.py`、`evaldctl.sh`、`config.example.json`、`dashboard.html`、`monitor/*`
与板端部署**逐字节一致**的部署不变量（md5 记录于 CHANGELOG）；改这些文件须过板上 selftest 门禁。

**战役工作区 (Campaign Workspace)**:
存放 `runs/`、`results/` 与实验文档的工作区根。本仓是其代码正本但已独立于它——客户端用
`EV_WORKSPACE` 指向它，不用目录相对关系推导。
