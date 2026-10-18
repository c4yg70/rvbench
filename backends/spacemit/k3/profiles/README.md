# 平台档案

Rust 档案实现位于 `../src/profiles.rs`，使用 `rvbench_core::BackendProfile`。具体平台资源与 CPU 分区由 `../src/config.rs` 解析。

K3 为已实现后端；K1、SG2042、rvv_general 档案用于旧客户端身份兼容，不表示平台执行已经接入。registry 的同主机比较和 5% 改善规则位于 `../src/judge.rs`。
