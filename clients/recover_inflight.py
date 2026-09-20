#!/usr/bin/env python
"""收掉被打断的在飞作业：对 evald 上 queued/running 的本任务作业轮询到终态，
追加进 results/panel/baseline_recall.jsonl（attempt 从命令行给，默认 1）。
用法: python clients/recover_inflight.py [attempt]
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_DIR = Path(__file__).resolve().parent.parent   # = 仓库根
ROOT = REPO_DIR.parent                              # 战役工作区根（含 results/）
if os.environ.get("EV_WORKSPACE"):   # 仓已独立于工作区，无法从仓位置推导 → 显式覆盖
    ROOT = Path(os.environ["EV_WORKSPACE"]).expanduser().resolve()
BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(REPO_DIR / "config.json"))["token"]
OUT = ROOT / "results" / "panel" / "baseline_recall.jsonl"
if not OUT.parent.is_dir():     # 工作区根不对就别去轮询板上作业（拿不回落点）
    raise SystemExit(f"[recover_inflight] 找不到 {OUT.parent}；"
                     f"用 EV_WORKSPACE=<含 runs/ 与 results/ 的工作区根> 指定")


def req(method, path, body=None, timeout_s=90):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def main():
    attempt = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    src = open(REPO_DIR / "clients" / "archive" / "run_recall.py", encoding='utf-8').read()
    fix_note = dict(re.findall(r'"([\w]+)": "([^"]+)"', src.split('FIX_NOTE = {')[1].split('\n}')[0]))
    js = req("GET", "/jobs?limit=3000")
    jobs = js if isinstance(js, list) else js.get("jobs", js.get("items", []))
    inflight = {}
    for j in jobs:
        if j.get("state") in ("queued", "running"):
            rq = j.get("request") or {}
            if rq.get("bench") == "gemm" or rq.get("op"):
                inflight[j["job_id"]] = (rq.get("op"), rq.get("dtype") or "float32",
                                         rq.get("shape") or "64,512,512")
    if not inflight:
        print("no inflight")
        return
    out = open(OUT, "a", encoding="utf-8")
    while inflight:
        for jid, (op, dt, shape) in list(inflight.items()):
            j = req("GET", f"/jobs/{jid}?wait=55")
            state = j.get("state")
            if state in ("done", "failed", "timeout", "canceled"):
                rec = {"op": op, "dtype": dt, "job_shape": shape, "attempt": attempt,
                       "fix_note": fix_note.get(op, "bench_op 分发修复"), "job_id": jid,
                       "state": state}
                if state == "done":
                    r = j.get("result") or {}
                    rec.update({"shape": r.get("shape"), "torch_ms": r.get("torch_ms"),
                                "gems_ms": r.get("gems_ms"), "speedup": r.get("speedup"),
                                "correct": r.get("correct"),
                                "framework": r.get("shape") == "framework",
                                "gems_gflops": r.get("gems_gflops")})
                else:
                    rec["error"] = (j.get("error") or "")[:300]
                rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                print(op, state, rec.get("speedup"), flush=True)
                del inflight[jid]
    print("recovery done")


if __name__ == "__main__":
    main()
