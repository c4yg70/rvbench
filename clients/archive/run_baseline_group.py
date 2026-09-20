#!/usr/bin/env python
"""evald baseline 收集器：一组算子逐个提交 baseline 作业（无 files=评估当前部署），
轮询到终态，结果追加 jsonl。支持断点续跑（跳过已记录 op）。

用法（cwd = rv-k3-kernel-opti）:
  kope-mem/.venv/Scripts/python.exe bin/run_baseline_group.py 1   # 读 bin/baseline_ops_part1.txt -> results/ledgers/baseline.jsonl#part1
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parents[1]        # = riscv-flagos-kernel-eval-server/
ROOT = REPO_DIR.parent                # 工作区根（原 bin/ 位置时 ROOT=parents[1]）
BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(REPO_DIR / "config.json"))["token"]

GEMM_OPS = {"mm", "addmm", "bmm"}


def _req(method, path, body=None, timeout_s=90):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def dtype_of(op: str) -> str:
    if op.startswith("bitwise"):
        return "int32"
    if op.startswith("logical"):
        return "bool"
    return "float32"


def make_job(op: str) -> dict:
    if op in GEMM_OPS:
        job = {"bench": "gemm", "op": op, "shape": "512,512", "dtype": "float32",
               "batch": 8}
    else:
        job = {"op": op, "shape": "64,512,512", "dtype": dtype_of(op)}
    job.update({"warmup": 5, "rep": 20, "timeout_min": 20})
    return job


def run_op(op: str) -> dict:
    rec = {"op": op, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        sub = _req("POST", "/jobs", make_job(op))
        job_id = sub.get("job_id")
        rec["job_id"] = job_id
        state = sub.get("state")
        for _ in range(60):  # 最多 ~60 分钟
            if state in ("done", "failed", "timeout", "canceled"):
                break
            time.sleep(55)
            j = _req("GET", f"/jobs/{job_id}?wait=60")
            state = j.get("state")
        rec["state"] = state
        if state == "done":
            r = j.get("result") or {}
            rec.update({
                "shape": r.get("shape"), "dtype": r.get("dtype"),
                "torch_ms": r.get("torch_ms"), "gems_ms": r.get("gems_ms"),
                "speedup": r.get("speedup"), "correct": r.get("correct"),
                "framework": isinstance(r.get("shape"), str) and r.get("shape") == "framework",
                "gems_gflops": r.get("gems_gflops"),
            })
        else:
            rec["error"] = (j.get("error") or "")[:300]
    except Exception as e:  # noqa: BLE001
        rec["state"] = "submit_error"
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    return rec


def main() -> None:
    gid = sys.argv[1]
    ops_file = SELF_DIR / f"baseline_ops_part{gid}.txt"   # 数据文件随迁 archive/
    out_file = ROOT / "results" / f"ledgers/baseline_part{gid}.jsonl"
    ops = [l.strip() for l in ops_file.read_text().splitlines() if l.strip()]
    done = set()
    if out_file.exists():
        for line in out_file.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    if r.get("state") in ("done", "failed", "timeout", "canceled"):
                        done.add(r.get("op"))
                except json.JSONDecodeError:
                    pass
    todo = [o for o in ops if o not in done]
    print(f"group {gid}: {len(ops)} ops, {len(done)} done, {len(todo)} todo", flush=True)
    for i, op in enumerate(todo):
        rec = run_op(op)
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{i+1}/{len(todo)}] {op}: {rec['state']} "
              f"speedup={rec.get('speedup')}", flush=True)
    with open(out_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({"DONE": True, "group": gid}) + "\n")
    print(f"group {gid} ALL DONE", flush=True)


if __name__ == "__main__":
    main()
