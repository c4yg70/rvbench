#!/usr/bin/env bash
# 兼容垫片 —— 正本已迁 riscv-flagos-kernel-eval-server/ops/precompile_candidate.sh（2026-09-20 收拢，spec-20260920）。
# 母本管理于 riscv-flagos-kernel-eval-server/shims/；本文件为部署副本，勿手改。
exec bash "$(dirname "${BASH_SOURCE[0]}")/../riscv-flagos-kernel-eval-server/ops/precompile_candidate.sh" "$@"
