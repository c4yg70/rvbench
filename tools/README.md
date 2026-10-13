# rvbench-tools

原生 Rust 维护程序，无 Python 控制包装。

- `check`：Cargo fmt/test/clippy 和公开合同检查。
- `bundle --output DIR [--binary FILE]`：RISC-V K3 部署包及摘要清单。
- `trace --root DIR --json`：harness trace 的 D2/D3/D4 与跨根污染检查。
- `pssh HOST 'COMMAND'`：原生 SSH；`--put LOCAL REMOTE` / `--get REMOTE LOCAL` 使用 SFTP。
- `precompile HOST CANDIDATE --op OP --shape SHAPE`：上传候选和 Python 算子 JIT 桥接、执行并清理临时文件。

SSH 使用默认 `~/.ssh/config` 与 known_hosts；`--key` 指定私钥，`--accept-new` 显式接受未知主机。私钥口令与密码通过 `RVBENCH_SSH_KEY_PASSPHRASE` / `RVBENCH_SSH_PASSWORD` 提供，不写入命令行。当前不支持 agent 和 ProxyJump。调用 SSH/预编译会操作指定远程主机；本次迁移未执行这些远程操作。
