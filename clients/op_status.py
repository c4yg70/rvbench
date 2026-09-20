#!/usr/bin/env python
"""One-shot status for optimizer groups: per op, last judged round / pending
ticket / in-flight (ticket without raw). Non-blocking; poll as often as you
like instead of waiting. Usage:
  kope-mem/.venv/Scripts/python.exe clients/op_status.py            # all KOPE ops
  kope-mem/.venv/Scripts/python.exe clients/op_status.py div exp2   # specific ops
"""
import json
import os
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parent
ROOT = REPO_DIR.parent            # 战役工作区根（含 runs/）
if os.environ.get("EV_WORKSPACE"):   # 仓已独立于工作区，无法从仓位置推导 → 显式覆盖
    ROOT = Path(os.environ["EV_WORKSPACE"]).expanduser().resolve()
RUN = ROOT / "runs" / "E1" / "KOPE"
ORDER = "0123456789"


def rnum(stem: str) -> int:
    digits = "".join(c for c in stem.split(".")[0] if c in ORDER)
    return int(digits) if digits else -1


def main() -> None:
    if not RUN.is_dir():
        raise SystemExit(f"[op_status] 找不到 {RUN}；"
                         f"用 EV_WORKSPACE=<含 runs/ 与 results/ 的工作区根> 指定")
    want = set(sys.argv[1:])
    rows = []
    for d in sorted(RUN.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if want and d.name not in want:
            continue
        results = sorted(d.glob("results/*.result.json"),
                         key=lambda p: rnum(p.name[:-len(".result.json")]))
        tickets = sorted(d.glob("tickets/*.ticket.json"),
                         key=lambda p: rnum(p.name[:-len(".ticket.json")]))
        last_res = last_out = None
        if results:
            r = json.loads(results[-1].read_text(encoding="utf-8"))
            last_res = results[-1].name[:-len(".result.json")]
            last_out = r.get("outcome")
        pend = [t.name[:-len(".ticket.json")] for t in tickets
                if not (t.parent.parent / "results"
                        / f"{t.name[:-len('.ticket.json')]}.raw.json").exists()]
        state = "inflight" if pend else ("done" if results else "cold")
        rows.append((d.name, last_res or "-", last_out or "-", state,
                     ",".join(pend) if pend else ""))
    print(f"{'op':28} {'last':6} {'outcome':34} {'state':8} pending")
    for r in rows:
        print(f"{r[0]:28} {r[1]:6} {str(r[2]):34} {r[3]:8} {r[4]}")


if __name__ == "__main__":
    main()
