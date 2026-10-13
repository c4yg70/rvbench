# rvbench CLI

原生 Rust 客户端。使用 `rvbench --help`、`rvbench ev --help`、`rvbench suite --help` 查看参数。

`validate spec|hidden|results`、`digest` 为离线合同工具。`ev run|submit|get|judge|status|history|cancel` 使用 HTTP 作业服务。`suite validate|expand|from-baseline|run` 处理历史 suite-v1；`pipeline` 处理工作区 ticket，`op-status` 展示本地状态。

全局参数：`--root` 主仓，`--workspace` 工作区，`--backend` 档案，`--base` 显式服务地址，`--token` 认证。推荐通过 `EV_TOKEN` 提供 token，避免命令行历史；支持 `EV_WORKSPACE` 和 `EV_BACKEND`。

管线持久化为 `runs/.rvbench-pipeline.json`，与旧 Python state 不互读。ticket 布局为 `runs/<wave>/<condition>/<op>/tickets/<stem>.ticket.json`；同一 ticket 路径与内容生成稳定重试 ID，不同 wave 独立。非 baseline ticket 必须提供候选文件；声明摘要时必须匹配。

结果沿用 raw-v1/result-v2 并更新旧 registry。它们不代表新的 eval_results 合同中已采齐输入、清理与计时证据。
