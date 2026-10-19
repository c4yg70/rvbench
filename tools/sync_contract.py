#!/usr/bin/env python3
"""把 contracts/api/openapi.json 同步到协议仓副本，并打印两侧 sha256。

用法：
    python tools/sync_contract.py [协议仓路径]

默认协议仓路径为 ../rvbench-core-rust。协议仓的 openapi.json 是随 crate 发布的副本，
正本在主仓 contracts/api/openapi.json；本脚本只做单向同步，不反向写入。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "contracts" / "api" / "openapi.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protocol_root() -> Path | None:
    """按参数 → 环境变量 → 常见同级布局的顺序定位协议仓。"""
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1])
        return candidate if candidate.is_dir() else None
    env = os.environ.get("RVBENCH_PROTOCOL_ROOT")
    if env:
        candidate = Path(env)
        return candidate if candidate.is_dir() else None
    for candidate in (parent / "rvbench-core-rust" for parent in ROOT.parents):
        if candidate.is_dir():
            return candidate
    return None


def main() -> int:
    root = protocol_root()
    if root is None:
        print(
            "找不到协议仓；传入路径或设置 RVBENCH_PROTOCOL_ROOT，例如：\n"
            "  python tools/sync_contract.py ../rvbench-core-rust",
            file=sys.stderr,
        )
        return 1
    target = root / "openapi.json"
    if not SOURCE.is_file():
        print(f"正本缺失：{SOURCE}", file=sys.stderr)
        return 1

    # 正本以 LF 存储；写入前归一化，避免 Windows 工作区 CRLF 混进副本。
    payload = SOURCE.read_bytes().replace(b"\r\n", b"\n")
    SOURCE.write_bytes(payload)
    target.write_bytes(payload)

    source_digest = digest(SOURCE)
    target_digest = digest(target)
    print(f"source  {SOURCE}  {source_digest}")
    print(f"target  {target}  {target_digest}")
    print("ok" if source_digest == target_digest else "mismatch")
    return 0 if source_digest == target_digest else 1


if __name__ == "__main__":
    raise SystemExit(main())
