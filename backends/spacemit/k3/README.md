# SpacemiT K3 后端

`rvbench-evald` 为 Rust 服务。`src/` 负责 K3 配置、资源/车道、请求校验、候选路径白名单、执行器和旧 registry 判定；通用服务机制由 `rvbench-runtime` 提供。

`adapters/board/` 保留算子调用、torch 参照、fallback 与计时脚本。`adapters/python/warmd.py` 保留不变量预加载及每作业 fork 协议；`precompile_driver.py` 仅用于 Triton JIT。板端 Python 环境由已有 `run_env.sh` 激活。

## 本地检查

```sh
cargo test -p rvbench-k3 --locked
cargo run -p rvbench-k3 -- --config backends/spacemit/k3/evald/config.example.json --check-config
```

配置检查不会连接板端或加载 torch。模板中的 token 必须替换；生成 token 使用 `rvbench-evald --gen-token`。真实配置 `evald/config.json` 和 `evald.env` 忽略入库。

## RISC-V Linux 构建

板上原生编译可执行 `cargo build -p rvbench-k3 --release --locked`。从主机交叉编译：

```sh
rustup target add riscv64gc-unknown-linux-gnu
cargo install cargo-zigbuild --locked --version 0.23.4
# 将 Zig 0.14.0 放入 PATH；x-cmd 可用 x env use zig 安装
cargo zigbuild -p rvbench-k3 --release --target riscv64gc-unknown-linux-gnu --locked
cargo run -p rvbench-tools -- bundle --output artifacts/k3-rust-bundle
```

包内布局：

```text
bundle/
├── evald/
│   ├── rvbench-evald
│   ├── evaldctl.sh
│   ├── config.example.json
│   ├── warmd.py
│   ├── dashboard.html
│   └── monitor/
├── scripts/                    bench_op/bench_gemm/torch_reference/fallback_refs
└── manifest.json               文件 SHA-256、字节数及目标平台
```

打包器检查 ELF64 little-endian RISC-V 标识，拒绝 Windows/其他架构程序，拒绝覆盖已存在的输出目录。不包含真实配置、token 或隐藏定义。它只生成本地包。

部署时将 evald 和 scripts 的内容放到对应板端目录，保留既有私有配置与 run_env；核对 `data_dir`、`scripts_root`、`flag_gems_root`、`warmd_path` 与 CPU 亲和。跨 Windows 复制后设置执行权限：

```sh
chmod +x /home/c4y/autoke/evald/rvbench-evald /home/c4y/autoke/evald/evaldctl.sh
cd /home/c4y/autoke/evald
./rvbench-evald --config config.json --check-config
./evaldctl.sh start
./evaldctl.sh status
```

`evaldctl.sh` 可通过 `EVALD_BIN`、`EVALD_CONFIG` 覆盖位置；`start-ai` 保留 SpacemiT AI 线程启动方式。`stop` 发送 TERM 并等待清理，超时保留 PID 文件。

本次已完成 Windows 本地测试和 RISC-V release 编译，未部署或运行 K3 真机。warmfork 与真实 torch/Triton 的性能、数值及 ABI 兼容需板上验证。

## API 与维护

公开健康/指标端点 `/healthz`、`/metrics`；作业端点 `/jobs` 使用 `X-K3-Token`。客户端为 `rvbench ev ...`。SSH/SFTP 为 `rvbench-tools pssh`，预编译为 `rvbench-tools precompile`。SSH 支持配置别名、私钥或环境密码、known_hosts 校验；当前不支持 SSH agent 及 ProxyJump。

旧 Python clients/ops/evald/tests 在本地 `artifacts/python-legacy-2026-10-14/` 保存。原技术文档见仓库 `docs/backends/spacemit-k3/`，其中旧入口只作为历史参考。
