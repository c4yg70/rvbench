# rvbench-runtime

通用 Rust 运行时：Axum API、限流队列、worker、持久化、路径读写锁、计时分区锁、候选部署及恢复、进程树超时清理和 Prometheus 指标。

后端实现 `Backend` 与 `Executor`。运行时不导入 K3 crate；平台路径白名单、CPU/线程配置与 Python 执行桥接属于后端。

提交先持久化请求与候选内容，再入队；内存只保留身份和日志尾部。部署记录两阶段 manifest 和备份，终态写入前完成恢复；启动时恢复旧 journal 与未写 journal 的孤立目录。冲突槽位不被覆盖。单 data_dir 锁阻止多个服务使用同一 spool。

Unix 使用独立进程组，Windows 使用 Job Object；超时清理包括继承输出管道的子孙进程。SIGTERM/Ctrl-C 停止接收任务并等待执行中作业清理；未执行的队列在下一次启动时按恢复路径处理。

这是嵌入 evald 的调度运行时；新 spec 中 Run/Case/Attempt 的完整评测执行与证据生产尚需后续实现。
