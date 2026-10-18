# ADR-0001：统一主仓、平台后端与 Web UI

日期：2026-10-14。状态：用户已确认；本文件记录合仓时的 Python 目录与验证快照。随后 Rust 改写以 [ADR-0002](0002-rust-control-plane.md) 和当前 README 为准。

## 决策

主仓名为 `rvbench`。远程主仓使用用户将从 `c4yg70/rvbench-kernel-server`
更名而来的 `c4yg70/rvbench`。本地 origin 指向新地址，远程更名由用户执行。

`core/` 承载通用合同、校验与可复用类型；`suites/kernels/` 表示算子评测领域；
平台实现采用 `backends/spacemit/k3/`。worker 是执行角色，当前不建立插件发现机制。
新增 `webui/`，统一存放评测服务器前端页面、资源和设计稿。

现有 evald 保持独立部署单元。队列、超时、恢复与执行器机制暂留在 K3 后端，
`runtime/` 说明后续通用运行时抽取边界，不宣称已经实现通用 manager/worker。

## 当前迁移

| 原内容 | 当前位置 |
|---|---|
| kernel/tools 校验器 | core/ |
| kernel/definitions | suites/kernels/ |
| kernel/schemas、examples、tests | 主仓同名目录 |
| server/evald.py、warmd.py、evaldctl.sh、config | backends/spacemit/k3/evald/ |
| server/board | backends/spacemit/k3/adapters/board/ |
| server/clients、ops、monitor、tools、tests | backends/spacemit/k3/ 下同名目录 |
| server/clients/result_schema.py 实现 | core/compat/result_schema.py；原导入位置保留桥接 |
| BackendProfile 类型 | core/backend_profile.py；具体档案仍由后端维护 |
| server/dashboard.html | webui/dashboard.html |
| server/specs.html | webui/design/k3-evald-specs.html |
| server/docs、CHANGELOG、CONTEXT、requirements | docs/backends/spacemit-k3/ |
| 原 README 与项目元数据 | docs/history/ |
| 原报告、归档与构建产物 | artifacts/ 下保留本地文件 |

更新客户端的配置位置与工作区推导。`EV_WORKSPACE` 仍可覆盖工作区，
未设置时延续原来的“仓库父目录”语义，不误用新的 K3 目录作为战役工作区。
仓库级 `cli/ev.py` 提供原客户端入口。

前端只有 `webui/dashboard.html` 一份源码。部署工具按原板上目录复制，
并生成 SHA-256 清单；真实 config.json、隐藏用例和本地构建产物均不入库。

spec 的相对 Schema 路径因目录层级变化而更新。公开内容版本从 0.2.0 升至 0.2.1，
格式版本仍为 2.0.0；本地隐藏集核验旧 public pin 后重新绑定新摘要并递增内容版本。
synthetic 结果示例同步引用并递增内容版本。原副本保留在源仓备份。

## 历史与本地内容

合仓基础提交使用原 server HEAD 为第一父提交、原 kernel HEAD 为第二父提交，
保留两套可追溯历史。该提交仅包含两个仓原有已提交的树。
合仓阶段的原本地修改、未跟踪文件与结构整理先保留为新仓未提交变更；后续 Rust 迁移的提交和发布以 ADR-0002 与 Git 历史为准。

源仓在完整复制校验后归档到工作区 `.repo-backups/2026-10-14/`，保留完整 .git、
配置、私有用例与原工作树。迁移脚本和原文件摘要清单位于工作区 `.rvbench-migration/`。
主仓仅包含一个 .git，不包含嵌套 Git 仓或子模块。

## 未改变的服务合同与后续工作

服务 evald.py、warmd.py、evaldctl.sh、config.example.json、dashboard.html
和 Prometheus 抓取配置按原字节迁入。本次没有连接或部署到板端。

旧 suite-v1、result-v2 与新版 spec/eval_results 仍是不同合同。
兼容层保留旧结果，不能补造新合同要求的输入摘要、逐项检查、原始计时样本或清理证明。
新版合同实际执行接入与通用运行时抽取作为后续实现。

现有 Web UI 仍是 K3 监控页。作业提交、评测集选择、结果与证据浏览等新页面
由 webui 统一承载，当前没有宣称这些交互已经实现。

## 验证

迁移前：核心 79 个测试通过；原平台 45 个测试通过，其中 1 个因缺历史工作区跳过。
迁移后检查入口为 `python tools/check.py`，覆盖原两套测试、合仓集成检查与公开合同校验。
另执行本地 evald selftest；Windows 的 fork/亲和能力边界按原 selftest 行为报告。

迁移后核心与合仓集成检查共 83 项通过；平台 45 项通过，其中 1 项沿用原跳过原因。
公开定义、synthetic 结果与本地隐藏引用校验均通过。spec 仍有准备状态阻塞项，未升为 ready。
6 个服务与监控部署源文件逐字节核对一致；全部 Python 源码语法与现行文档链接检查通过。

本地 evald selftest 完成 220 个 PASS 断言并报告 ALL PASS。修正本地 Windows runner 的
Git Bash 定位与工具 PATH，避免 PowerShell 默认解析到 WSL 启动器或丢失 sleep；
这些调整只影响本地测试入口，没有改动板端服务源码。fork 和 taskset 专项仍由板端覆盖。
原仓共 512 个文件在归档前按 SHA-256 复核未改变；两套历史祖先检查与 Git fsck 均通过。
