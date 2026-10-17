# rvbench Web UI

评测服务器前端页面设计与实现的统一目录。

- `console.html`：当前 manager / 独立 worker 共用的四标签页控制台。
  包含系统状态、任务提交、任务结果、排行榜，支持明暗主题，所有内容内联。
  K1/K3 与 native C/Triton 可独立选择，按各实现探测能力展示算子、dtype 和匹配节点。
  接口与部署见 [observability.md](../docs/observability.md)。

- `dashboard.html`：现有 K3 监控页面，单文件、零构建依赖。
- `design/k3-evald-specs.html`：原服务规格设计稿，作为历史设计参考。

页面可直接在浏览器打开。Prometheus 地址通过 `?prom=<url>` 覆盖，沿用既有 CORS 和 localStorage 配置。平台 API 与队列属于 Rust runtime/backends。

后续评测集选择、提交、状态、结果与证据页面，以及共用资源和构建配置，统一归入本目录；当前 dashboard 的功能是监控。

`rvbench-tools bundle` 复制当前控制台、两端契约、监控配置与部署 / 验收脚本。
当前 VictoriaMetrics 配置位于 `deploy/observability/`；旧 Prometheus 配置位于 `backends/spacemit/k3/monitor/`。
