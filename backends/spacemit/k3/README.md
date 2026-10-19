# SpacemiT K3 板端资产

服务端已由 `rvbench-worker` / `rvbench-manager` 承担，见 [部署说明](../../../docs/manager-worker.md)。本目录只剩板端脚本与平台资料，不参与 Rust workspace 构建。

## 内容

```text
k3/
├── adapters/board/     算子调用、torch 参照、fallback 与计时脚本
├── adapters/python/    warmd 预加载与 Triton JIT 预编译驱动
├── evald/              run_env 启动脚本与配置模板
├── monitor/            Prometheus 抓取配置
├── profiles/           平台档案说明
└── clients/            旧客户端归档
```

真实的 `evald/config.json` 与环境文件按 `.gitignore` 不入库。

## 板端环境

Python 侧需要匹配的 torch / Triton / FlagGems，由板端 `run_env.sh` 激活。`rvbench-worker` 启动时会实际编译并 smoke 调用所选实现，环境缺失只会让该实现 `ready=false`，不影响另一种实现。

跨 Windows 复制脚本后需恢复执行权限：

```sh
chmod +x ~/autoke/evald/*.sh ~/autoke/evald/*.py
```

## 维护工具

SSH/SFTP 与预编译由 `rvbench-tools` 提供：

```sh
rvbench-tools pssh <host> "<command>"
rvbench-tools precompile --help
```

## 历史文档

原 evald 技术文档已迁至 `rvbench-spacemit-worker` 仓的 `docs/`（ADR、总体设计、架构图、规格、变更史），仅作设计参考。
