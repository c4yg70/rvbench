# 评估服务器沿用 /tmp/k3_bench.lock

所有计时执行必须互斥，现有机制是手工 SSH 命令 `flock /tmp/k3_bench.lock`。评估服务器的 worker 用 `fcntl.flock` 获取**同一把锁文件**而非服务器私有锁，保证过渡期内服务器作业与手工 SSH 基准互不污染计时；代价是手工任务持锁时服务器作业排队等待。待手工 SSH 计时完全退役后，方可改为服务器私有锁。

---

**修订（2026-09-09，决策记录见 [ADR-0014](0014-dual-timing-lanes-a100.md)）**：上面的"同一把锁文件"已收窄为**按分区**互斥——`max_concurrency>1` 时 `timing_lock_path` 取 `/tmp/k3_bench.lock.<index>`，各计时 worker 独立持锁（单 worker 时仍为 `/tmp/k3_bench.lock`，与原文一致）。多 worker 部署下，手工 SSH 基准与任何抢锁的回退路径必须**同时持有全部分区锁**，本不变量才继续成立。
