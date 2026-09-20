#!/usr/bin/env python
"""兼容垫片 —— 正本已迁 riscv-flagos-kernel-eval-server/clients/archive/run_recall.py（2026-09-20 收拢，spec-20260920）。

母本管理于 riscv-flagos-kernel-eval-server/shims/（随仓版本化）；本文件为部署副本，勿手改。
仅保证旧命令行 `python bin/run_recall.py ...` 继续可用；被当作模块 import 时为空操作。
"""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    _d = Path(__file__).resolve().parents[1] / "riscv-flagos-kernel-eval-server" / "clients/archive"
    sys.path.insert(0, str(_d))
    runpy.run_path(str(_d / "run_recall.py"), run_name="__main__")
