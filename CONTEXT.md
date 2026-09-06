# evald — 术语表

评估服务器（evald）的领域语言。背景：在 SpacemiT K3（RISC-V）上远程评估本地优化好的 FlagGems/Triton 算子代码。

## Language

**评估作业 (EvalJob)**:
一次提交给评估服务器的原子评估请求：候选文件包 + 评估参数 + 部署语义。
_Avoid_: task、benchmark request

**候选文件包 (Candidate Files)**:
随作业提交的代码文件集合（相对 flag_gems 包的路径 + 文件内容），服务器按路径白名单校验后才部署。
_Avoid_: patch、diff

**部署-恢复 (Deploy-Restore)**:
评估作业的执行语义：评估前备份 site-packages 原文件并断硬链写入候选，评估后自动恢复原文件。
_Avoid_: 覆盖即留、last-writer-wins

**定稿作业 (Finalize Job)**:
`restore: false` 的评估作业：评估完成后候选代码保留在 site-packages，用于固化最优版本。

**计时互斥 (Timing Mutex)**:
一切计时执行必须独占 `/tmp/k3_bench.lock` 的不变量；评估服务器 worker 与手工 SSH 基准共用这把锁。

**作业号 (Job ID)**:
评估作业的 UUIDv7 标识，时间有序，贯穿异步跟踪与历史记录；历史查询以其为游标。

**执行器 (Executor)**:
评估服务器中实际运行基准的可替换执行组件；v1 为每作业子进程，v1.1 为常驻预热 + fork 隔离的暖执行器。

**微基准 (Microbench)**:
bench_op.py 的单算子快速计时（5~30s），单行 JSON 输出 torch_ms/gems_ms/speedup/correct/max_abs_err。

**全量验收 (Full Acceptance)**:
pytest benchmark 全量跑分（约 2 分钟/算子），算子优化的验收口径；不进评估队列。
