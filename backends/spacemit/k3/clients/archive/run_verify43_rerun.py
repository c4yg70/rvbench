#!/usr/bin/env python
"""fallback-verify43 复验提交器（2026-09-08 五类缺陷修复后）。

从 results/panel/fallback-verify43.json 读 canary 结构，payload.files 换成新版
bin/bench_op.py（旧 payload 内嵌的是修复前内容），过滤到复验集合（上次 null/False
且不在排除表的 22 op），生成 results/panel/fallback-verify43-rerun.json 后按
how-to-eval-on-server.md 标准循环提交（并发 <=4、429 退避 30s、长轮询 wait=60）。
结果追加 results/panel/fallback-verify43-rerun.jsonl（与 verify43-results.jsonl 同构），
断点续跑：已有记录的 op 跳过（done/failed 均不重提）。

用法（cwd = rv-k3-kernel-opti）::
  kope-mem/.venv/Scripts/python.exe bin/run_verify43_rerun.py --gen-only   # 只生成 payload
  kope-mem/.venv/Scripts/python.exe bin/run_verify43_rerun.py              # 生成(如缺)+提交+轮询
"""

import argparse
import json
import threading
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
SRC = ROOT / "results" / "panel" / "fallback-verify43.json"
GEN_OUT = ROOT / "results" / "panel" / "fallback-verify43-rerun.json"
OUT_FILE = ROOT / "results" / "panel" / "fallback-verify43-rerun.jsonl"
BENCH_OP = REPO_DIR / "board" / "bench_op.py"
MAX_INFLIGHT = 4

# 复验集合 = 上次 null/False - tensor_split（真 bug 维持，不重跑）。
# full_like/unsqueeze 上次已 True（走直调层，README 覆盖缺口说明）不在集合。
RERUN_OPS = [
    "assert_async",          # E：_b_assert_async 改单元素 bool（0-dim）
    "add_relu",              # A：use_gems 重放
    "batch_norm_no_update",  # A
    "bitwise_and",           # A（板上已验证精确）
    "chunk_cat",             # A
    "dropout",               # C：gems_fn + out_adapter 取 g[0]
    "full",                  # B：gems_fn keyword-only
    "functional_sym_constrain_range",  # A（预期仍 null：板上无 dep_token 私有 API）
    "fused_adam",            # A 四级 resolver（板上在 flag_gems.ops._fused_adam）
    "group_norm",            # B：use_gems 天然覆盖（F.group_norm 经 dispatcher）
    "greater_equal",         # A
    "jagged_to_padded_dense_forward",  # A
    "less",                  # A
    "linspace",              # B：gems_fn keyword-only
    "logspace",              # B：gems_fn keyword-only
    "prelu_kernel",          # A
    "resize_output",         # A
    "safe_softmax",          # A（板上已验证 1.5e-8）
    "sparse_semi_structured_mm",       # minor：unjudgeable 短路（预期恒 null）
    "unfold_view",           # A
    "unsafe_masked_index",   # A
    "unsafe_view",           # A
]


def _req(method, path, body=None, timeout_s=90):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def gen_payloads():
    """从 verify43 结构取 payload，files 内容替换为当前 bin/bench_op.py，过滤复验集合。"""
    src = json.load(open(SRC, encoding="utf-8"))
    content = BENCH_OP.read_text(encoding="utf-8")
    canaries = []
    for c in src["canaries"]:
        if c["op"] not in RERUN_OPS:
            continue
        c = dict(c)
        p = dict(c["payload"])
        p["files"] = [{"path": "scripts/bench_op.py", "content": content}]
        c["payload"] = p
        canaries.append(c)
    got = [c["op"] for c in canaries]
    missing = [op for op in RERUN_OPS if op not in got]
    if missing:
        raise SystemExit(f"verify43.json 缺复验集合算子: {missing}")
    out = {
        "name": "fallback-verify43-rerun",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "purpose": "五类缺陷修复（A use_gems 重放+四级 importlib resolver / B full,linspace,"
                   "logspace keyword-only gems_fn（group_norm 走 use_gems）/ C dropout "
                   "out_adapter 取 g[0] / E assert_async 单元素 bool / minor unjudgeable "
                   "短路）后的 22 op 复验；payload 结构同 fallback-verify43.json，files "
                   "已替换为修复后 bin/bench_op.py",
        "bench_op_sha256_source": "bin/bench_op.py (live)",
        "source": "fallback-verify43.json",
        "rerun_ops": RERUN_OPS,
        "canaries": canaries,
    }
    GEN_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"gen: {len(canaries)} payloads -> {GEN_OUT.name} "
          f"(bench_op sha {__import__('hashlib').sha256(content.encode()).hexdigest()[:12]})",
          flush=True)


def load_done_ops():
    done = set()
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["op"])
    return done


def record(rec):
    with OUT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def submit_and_poll():
    data = json.load(open(GEN_OUT, encoding="utf-8"))
    done_ops = load_done_ops()
    todo = [c for c in data["canaries"] if c["op"] not in done_ops]
    print(f"rerun: {len(todo)} jobs todo ({len(done_ops)} already recorded)", flush=True)

    out_lock = threading.Lock()
    inflight = {}

    def finish(rec, j):
        state = j.get("state")
        rec["state"] = state
        r = j.get("result") or {}
        rec.update({
            "correct": r.get("correct"),
            "correct_mode": r.get("correct_mode"),
            "max_abs_err": r.get("max_abs_err"),
            "note": r.get("note") or "",
            "speedup": r.get("speedup"),
        })
        if state != "done":
            rec["error"] = (j.get("error") or "")[:300]
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with out_lock:
            record(rec)
        print(f"[rerun] {rec['op']}: {state} correct={rec['correct']} "
              f"mode={rec['correct_mode']} note={rec['note'][:60]}", flush=True)

    i = 0
    while i < len(todo) or inflight:
        while i < len(todo) and len(inflight) < MAX_INFLIGHT:
            c = todo[i]
            i += 1
            rec = {"op": c["op"]}
            while True:
                try:
                    sub = _req("POST", "/jobs", c["payload"])
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        print(f"429 队列满，等 30s 重提 {c['op']}", flush=True)
                        time.sleep(30)
                    else:
                        raise
            rec["job_id"] = sub.get("job_id")
            state = sub.get("state")
            if state in ("done", "failed", "timeout", "canceled"):
                finish(rec, sub)
            else:
                inflight[rec["job_id"]] = rec
        if inflight:
            jid, rec = next(iter(inflight.items()))
            j = _req("GET", f"/jobs/{jid}?wait=60")
            if j.get("state") in ("done", "failed", "timeout", "canceled"):
                finish(rec, j)
                del inflight[jid]
        else:
            time.sleep(5)
    print("rerun ALL DONE", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-only", action="store_true", help="只生成 payload 不提交")
    args = ap.parse_args()
    if args.gen_only or not GEN_OUT.exists():
        gen_payloads()
    if not args.gen_only:
        submit_and_poll()


if __name__ == "__main__":
    main()
