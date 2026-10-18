# 预热执行器只预热不变量，候选代码子进程全新加载

板上实测：每作业 ~20s 固定开销中，python+torch+triton 导入约 3.9s，flag_gems 导入约 6.8s（弥散于 runtime 硬件探测、sqlalchemy、上百个 op 模块，无单一可缓存子树），首跑编译约 5s（triton 磁盘缓存命中后归零）。全量常驻 + 精准 reload 虽可再省 ~6.8s，但需要维护"谁还引用着被换模块"的依赖图（fused 模块直接 from-import 算子函数、_spacemit 运行时持有配置对象），残留风险正是 ADR-0001 要杜绝的静默错误源。

故 WarmForkExecutor（warmd 守护）只在父进程预热 torch+triton——与候选代码无关的不变量；每个作业 fork 子进程全新 import flag_gems，候选代码永远从磁盘已部署文件加载，正确性由构造保证。守护故障（起不来/EOF/协议错）时该作业自动回退 SubprocessExecutor。实测同作业 20.2s→15.1s（-25%）；正确性由 A/B 交叉验证（同作业两种执行器 speedup 一致）。
