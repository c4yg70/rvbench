# Python 执行桥接

`warmd.py` 保留 torch/Triton 不变量预加载和逐作业 fork 的 JSONL 协议，由 Rust K3 执行器管理进程、校验响应并负责失败回退。部署打包时复制为 evald/warmd.py。

`precompile_driver.py` 接收候选、op、dtype 和 shapes-json，导入候选并触发 Triton JIT；由 Rust rvbench-tools precompile 上传和调用。它不处理 HTTP、队列、SSH 或控制面状态。
