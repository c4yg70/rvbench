# 评估服务器用 stdlib Python 而非框架/Rust

板上 FlagGems venv 连 pip 都没有（装 FastAPI 须重建 venv，有污染环境风险），且 bench 执行与 v1.1 预热执行器必然运行在 venv python 里——选 stdlib `ThreadingHTTPServer` + `subprocess` 让服务与执行器同在一个代码块，演进路径最短。Rust（cross 交叉编译 riscv64 musl 静态单文件）被否：部署更干净，但 v1.1 需额外 IPC 协议层，且每次改动都要本地交叉编译。服务进程跑系统 python3（3.14.4，原生 `uuid.uuid7`；若迁 venv 3.12 需 ~10 行 fallback）。
