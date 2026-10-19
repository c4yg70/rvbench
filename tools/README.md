# rvbench-tools

原生 Rust 维护程序，无 Python 控制包装。

- `check`：Cargo fmt/test/clippy 和公开合同检查。
- `bundle --output DIR --worker-root DIR [--manager-root DIR] [--binary FILE]`：RISC-V K3 部署包及摘要清单。
  `--manager-root` 指向独立 manager 仓检出，省略时打包 worker-only；给出时缺 RISC-V manager 二进制即报错。
- `trace --root DIR --json`：harness trace 的 D2/D3/D4 与跨根污染检查。
- `pssh HOST 'COMMAND'`：原生 SSH；`--put LOCAL REMOTE` / `--get REMOTE LOCAL` 使用 SFTP。
- `precompile HOST CANDIDATE --op OP --shape SHAPE`：上传候选和 Python 算子 JIT 桥接、执行并清理临时文件。

SSH 使用默认 `~/.ssh/config` 与 known_hosts；`--key` 指定私钥、`--accept-new` 显式接受未知主机。私钥口令与密码通过 `RVBENCH_SSH_KEY_PASSPHRASE` / `RVBENCH_SSH_PASSWORD` 提供，不写入命令行。当前不支持 agent 和 ProxyJump。调用 SSH/预编译会操作指定远程主机；本次迁移未执行这些远程操作。

## 跨仓同步脚本

- `sync_contract.py [协议仓路径]`：把 `contracts/api/openapi.json` 单向同步到 `rvbench-core-rust`，并打印两侧 sha256。
- `sync_cli.py [rvbench-cli 路径]`：把 `contracts/` 与 `examples/` 单向同步到 `rvbench-cli` 检出；该仓的 `tests/contracts.rs` 与命令级回归依赖这些副本。
  路径按 参数 → `RVBENCH_PROTOCOL_ROOT` / `RVBENCH_CLI_ROOT` → 同级 `../` 目录 的顺序定位。
