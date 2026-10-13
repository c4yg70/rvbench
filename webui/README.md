# rvbench Web UI

评测服务器前端页面设计与实现的统一目录。

- `dashboard.html`：现有 K3 监控页面，单文件、零构建依赖。
- `design/k3-evald-specs.html`：原服务规格设计稿，作为历史设计参考。

页面可直接在浏览器打开。Prometheus 地址通过 `?prom=<url>` 覆盖，沿用既有 CORS 和 localStorage 配置。平台 API 与队列属于 Rust runtime/backends。

后续评测集选择、提交、状态、结果与证据页面，以及共用资源和构建配置，统一归入本目录；当前 dashboard 的功能是监控。

`rvbench-tools bundle` 将页面复制为部署包的 `evald/dashboard.html`。前端源码只在这里维护，复制文件是部署产物。Prometheus 配置位于 `backends/spacemit/k3/monitor/`。
