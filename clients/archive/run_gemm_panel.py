#!/usr/bin/env python
"""GEMM 面板补口径：mm/addmm/bmm x float32/float16，shape 384,512，batch 8。
结果追加 results/panel/baseline_gemm.jsonl（双口径：gems_gflops + speedup）。
在捞回跑批结束、队列空后运行（共享账号级并发上限）。
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parents[1]        # = riscv-flagos-kernel-eval-server/
ROOT = REPO_DIR.parent                # 工作区根（原 bin/ 位置时 ROOT=parents[1]）
BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(REPO_DIR / "config.json"))["token"]
OUT_FILE = ROOT / "results" / "panel" / "baseline_gemm.jsonl"


def _req(method, path, body=None, timeout_s=90):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def main():
    done = set()
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("state") == "done":
                    done.add((r["op"], r["dtype"]))
    for op in ("mm", "addmm", "bmm"):
        for dtype in ("float32", "float16"):
            if (op, dtype) in done:
                print(f"gemm {op}/{dtype}: 已有，跳过", flush=True)
                continue
            rec = {"op": op, "dtype": dtype,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            payload = {"bench": "gemm", "op": op, "shape": "384,512", "dtype": dtype,
                       "batch": 8, "warmup": 5, "rep": 20, "timeout_min": 20}
            try:
                while True:
                    try:
                        sub = _req("POST", "/jobs", payload)
                        break
                    except urllib.error.HTTPError as e:
                        if e.code == 429:
                            time.sleep(30)
                        else:
                            raise
                jid = sub["job_id"]
                rec["job_id"] = jid
                state = sub.get("state")
                for _ in range(40):
                    if state in ("done", "failed", "timeout", "canceled"):
                        break
                    time.sleep(20)
                    j = _req("GET", f"/jobs/{jid}?wait=60")
                    state = j.get("state")
                rec["state"] = state
                if state == "done":
                    r = j.get("result") or {}
                    rec.update({
                        "shape": f'{r.get("M")},{r.get("N")},{r.get("K")}',
                        "torch_ms": r.get("torch_ms"), "gems_ms": r.get("gems_ms"),
                        "speedup": r.get("speedup"), "gems_gflops": r.get("gems_gflops"),
                        "torch_gflops": r.get("torch_gflops"),
                        "correct": r.get("correct"),
                    })
                else:
                    rec["error"] = (j.get("error") or "")[:300]
            except Exception as e:  # noqa: BLE001
                rec["state"] = "submit_error"
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            with open(OUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"gemm {op}/{dtype}: {rec['state']} gems_gflops={rec.get('gems_gflops')}",
                  flush=True)
    print("gemm ALL DONE", flush=True)


if __name__ == "__main__":
    main()
