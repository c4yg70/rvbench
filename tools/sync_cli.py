#!/usr/bin/env python3
"""把主仓的合同与样例单向同步到 rvbench-cli 检出。

用法：
    python tools/sync_cli.py [rvbench-cli 路径]

默认路径按 参数 → RVBENCH_CLI_ROOT → 同级 ../rvbench-cli 的顺序定位。
同步内容：contracts/schemas/、contracts/api/openapi.json、
examples/hello-world/、examples/baseline/。

正本在主仓，rvbench-cli 内的副本只读；同步后在该仓跑 cargo test 复核
（tests/contracts.rs 会把 openapi.json 与 rvbench_protocol::SPEC 逐字节比对）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = (
    "contracts/schemas/*.schema.json",
    "contracts/api/openapi.json",
    "examples/hello-world/*",
    "examples/baseline/**/*",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cli_root() -> Path | None:
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1])
        return candidate if candidate.is_dir() else None
    env = os.environ.get("RVBENCH_CLI_ROOT")
    if env:
        candidate = Path(env)
        return candidate if candidate.is_dir() else None
    for parent in ROOT.parents:
        candidate = parent / "rvbench-cli"
        if candidate.is_dir():
            return candidate
    return None


def files() -> list[Path]:
    """按 TREES 展开受管文件；README 等说明文档不参与同步。"""
    found: set[Path] = set()
    for pattern in TREES:
        for path in ROOT.glob(pattern):
            if path.is_file():
                found.add(path)
    return sorted(found)


def main() -> int:
    target_root = cli_root()
    if target_root is None:
        print(
            "找不到 rvbench-cli 检出；传入路径或设置 RVBENCH_CLI_ROOT，例如：\n"
            "  python tools/sync_cli.py ../rvbench-cli",
            file=sys.stderr,
        )
        return 1
    mismatched = 0
    for source in files():
        relative = source.relative_to(ROOT)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # 正本统一以 LF 存储，避免 Windows 工作区的 CRLF 混进副本。
        payload = source.read_bytes().replace(b"\r\n", b"\n")
        source.write_bytes(payload)
        target.write_bytes(payload)
        same = digest(source) == digest(target)
        mismatched += 0 if same else 1
        print(f"{'ok  ' if same else 'FAIL'}  {relative}")
    print(f"{len(files())} files, {mismatched} mismatch")
    return 0 if mismatched == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
