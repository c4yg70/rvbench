#!/usr/bin/env python
"""evald baseline 捞回提交器：failed 算子逐个重提（files 携带修补版 scripts/bench_op.py），
账号级并发 <=4（429 等 30s），结果追加 results/panel/baseline_recall.jsonl。

断点/重试语义：跳过终态为 done 的 (op, shape)；failed/timeout 且 attempt<2 的下轮自动重试。
用法（cwd = rv-k3-kernel-opti）:
  kope-mem/.venv/Scripts/python.exe bin/run_recall.py            # 首轮/续跑
  kope-mem/.venv/Scripts/python.exe bin/run_recall.py --retry    # 只重试 failed (attempt<2)
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
OUT_FILE = ROOT / "results" / "panel" / "baseline_recall.jsonl"
MAX_INFLIGHT = 4
MAX_ATTEMPT = 2

FIX_NOTE = {
    "addmv": "特判直调(2D x 1D vec)+小shape",
    "addr": "特判直调+FORCE_FRAMEWORK(SIGABRT直调崩溃)",
    "affine_grid_generator": "框架兜底(测试体自带theta构造)",
    "alias": "框架兜底修复(torch参考缺失不再中断)",
    "allclose": "bool输出比较修复",
    "apply_rotary_pos_emb": "框架兜底修复",
    "arange": "直调预热探测失败自动转框架",
    "bernoulli": "特判直调(概率输入)+correct=None",
    "broadcast_tensors": "tuple输出比较修复",
    "cdist_forward": "框架兜底(SIGABRT重试)",
    "clip": "特判直调(min/max)",
    "col2im": "框架兜底(SIGABRT重试)",
    "constant_pad_nd": "框架兜底+timeout_min=30(干跑300s超时)",
    "contiguous": "框架兜底修复",
    "diag_embed": "特判直调+小shape(34GB分配规避)",
    "diagonal_scatter": "框架兜底",
    "dot": "特判直调+1D shape",
    "elu": "框架兜底修复",
    "embedding": "框架兜底+op级shape覆盖[128,64]",
    "empty": "工厂类走框架(kwargs构造)",
    "equal": "bool输出比较修复",
    "euclidean_dist": "框架兜底(SIGABRT重试)",
    "expand": "框架兜底修复",
    "expand_as": "框架兜底修复",
    "floor_divide": "直调(干跑rc=0,首轮llc崩溃为并发伪象)",
    "gather_block_quantized": "框架兜底修复",
    "glu": "框架兜底修复",
    "hardsigmoid": "框架兜底修复",
    "ilshift": "int32 dtype路由+框架兜底",
    "is_all_true": "bool dtype路由+框架兜底",
    "is_nonzero": "特判直调+单元素shape",
    "kron": "特判直调+小shape(OOM规避)",
    "leaky_relu": "框架兜底修复",
    "lift": "框架兜底修复",
    "lift_fresh_copy": "框架兜底修复",
    "log_sigmoid": "框架兜底修复",
    "log_sigmoid_forward": "框架兜底修复",
    "log_softmax": "框架兜底(SIGABRT重试)",
    "lshift": "int32 dtype路由+框架兜底",
    "max_unpool2d": "框架兜底(测试体自带indices构造)",
    "max_unpool3d": "框架兜底(测试体自带indices构造)",
    "median": "特判直调+1D shape(attempt2 降至1024:后端选择表fp32大宽度无实现)",
    "randn": "工厂类走框架(kwargs构造,attempt2加超时:偶发挂起)",
    "mish": "框架兜底修复",
    "mv": "特判直调(2D x 1D vec)+小shape",
    "new_ones": "工厂类走框架(kwargs构造)",
    "norm": "框架兜底(dim=)none探测修复",
    "ones": "工厂类走框架(kwargs构造)",
    "outer": "特判直调+1D shape",
    "pad": "框架兜底",
    "pdist": "特判直调+2D shape",
    "pixel_unshuffle": "框架兜底(4D shape)",
    "prelu": "框架兜底(weight构造)",
    "rand": "工厂类走框架(kwargs构造)",
    "randn": "工厂类走框架(kwargs构造)",
    "relu6": "框架兜底修复",
    "remainder": "直调(干跑rc=0,首轮llc崩溃为并发伪象)",
    "repeat": "框架兜底修复",
    "resize": "框架兜底修复",
    "resize_as": "框架兜底修复",
    "rrelu_with_noise_functional": "框架兜底+correct=None",
    "rwkv_ka_fusion": "框架兜底修复",
    "rwkv_mm_sparsity": "框架兜底修复",
    "scalar_tensor": "工厂类走框架(kwargs构造)",
    "smooth_l1_loss": "框架兜底修复",
    "softplus": "框架兜底修复",
    "softshrink": "框架兜底修复",
    "special_airy_ai": "框架兜底修复(torch.special.*参考在test内)",
    "special_chebyshev_polynomial_u": "框架兜底修复",
    "special_digamma": "框架兜底修复",
    "special_erfc": "框架兜底修复",
    "special_erfinv": "框架兜底修复",
    "special_i0e": "框架兜底修复",
    "special_i1": "框架兜底修复",
    "special_legendre_polynomial_p": "框架兜底修复",
    "special_log1p": "框架兜底修复",
    "special_logsumexp": "框架兜底修复",
    "special_modified_bessel_k1": "框架兜底修复",
    "special_sinc": "框架兜底修复",
    "special_scaled_modified_bessel_k1": "框架兜底修复(submitfix转入)",
    "special_shifted_chebyshev_polynomial_v": "框架兜底修复(submitfix转入)",
    "_reshape_alias": "框架兜底修复(submitfix转入)",
    "sym_stride": "框架兜底修复",
    "t_copy": "特判直调+2D shape",
    "trace": "特判直调+2D shape",
    "unbind": "tuple输出比较修复",
    "unbind_copy": "框架兜底(SIGABRT重试)",
    "vector_norm": "框架兜底(dim=)none探测修复",
    "view_copy": "框架兜底",
    "zero": "框架兜底修复",
    "zeros": "工厂类走框架(kwargs构造)",
    "where": "spacemit7_probe 转入",
    "where_self": "spacemit7_probe 转入",
    "pow": "spacemit7_probe 转入",
}

# 超时敏感（框架层慢/偶发挂起）
TIMEOUT_MIN = {"constant_pad_nd": 30, "randn": 30}


def _req(method, path, body=None, timeout_s=90):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def load_ops():
    """90 个捞回算子 + spacemit7 转入 where/pow（各 2 个 shape 口径）。"""
    jobs = []
    for line in (ROOT / "results" / "panel" / "_recall_ops.txt").read_text().splitlines():
        if not line.strip():
            continue
        op, dtype = line.split("|")
        jobs.append({"op": op, "dtype": dtype, "shape": "64,512,512"})
    for op in ("where", "pow"):
        for shape in ("64,512,512", "4194304"):
            jobs.append({"op": op, "dtype": "float32", "shape": shape})
    return jobs


def make_payload(job):
    content = (REPO_DIR / "board" / "bench_op.py").read_text(encoding="utf-8")
    p = {"op": job["op"], "shape": job["shape"], "dtype": job["dtype"],
         "warmup": 5, "rep": 20,
         "timeout_min": TIMEOUT_MIN.get(job["op"], 20),
         "files": [{"path": "scripts/bench_op.py", "content": content}]}
    return p


class Pool:
    def __init__(self, out_lock):
        self.inflight = {}  # job_id -> rec
        self.out_lock = out_lock

    def submit(self, job, rec):
        while True:
            try:
                sub = _req("POST", "/jobs", make_payload(job))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print(f"429 队列满，等 30s 重提 {job['op']}", flush=True)
                    time.sleep(30)
                else:
                    raise
        rec["job_id"] = sub.get("job_id")
        state = sub.get("state")
        if state in ("done", "failed", "timeout", "canceled"):
            self.finish(rec, sub)
        else:
            self.inflight[rec["job_id"]] = (rec, job)

    def poll_once(self):
        """轮询一个在飞作业；有终态则落盘并返回 True。"""
        if not self.inflight:
            return False
        jid, (rec, job) = next(iter(self.inflight.items()))
        j = _req("GET", f"/jobs/{jid}?wait=60")
        state = j.get("state")
        if state in ("done", "failed", "timeout", "canceled"):
            self.finish(rec, j)
            del self.inflight[jid]
            return True
        return False

    def finish(self, rec, j):
        state = j.get("state")
        rec["state"] = state
        if state == "done":
            r = j.get("result") or {}
            rec.update({
                "shape": r.get("shape"), "dtype": r.get("dtype"),
                "torch_ms": r.get("torch_ms"), "gems_ms": r.get("gems_ms"),
                "speedup": r.get("speedup"), "correct": r.get("correct"),
                "framework": r.get("shape") == "framework",
                "gems_gflops": r.get("gems_gflops"),
            })
        else:
            rec["error"] = (j.get("error") or "")[:300]
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.out_lock:
            with open(OUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[recall] {rec['op']} attempt{rec['attempt']}: {state} "
              f"speedup={rec.get('speedup')}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry", action="store_true",
                    help="只重试 failed/timeout 且 attempt<2 的记录")
    ap.add_argument("--max-seconds", type=int, default=0,
                    help=">0 时限时内跑：到期停新提交，收完在飞退出（供前台分块）")
    args = ap.parse_args()

    jobs = load_ops()
    seen_keys, retry_jobs = set(), []
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["op"], r.get("job_shape") or "64,512,512")
            seen_keys.add(key)
            if r.get("state") in ("failed", "timeout", "submit_error") and r.get("attempt", 1) < MAX_ATTEMPT:
                retry_jobs.append({**r, "_prev_attempt": r.get("attempt", 1)})
    todo = []
    for job in jobs:
        if args.retry:
            continue
        if (job["op"], job["shape"]) in seen_keys:
            continue  # 任何已有记录的 key 都不重提；failed/timeout 归 --retry 轮
        todo.append({"job": job, "attempt": 1})
    if args.retry:
        seen = set()
        for r in retry_jobs:
            key = (r["op"], r.get("job_shape") or "64,512,512")
            if key in seen:
                continue
            seen.add(key)
            todo.append({"job": {"op": r["op"], "dtype": r.get("dtype", "float32"),
                                 "shape": key[1]},
                         "attempt": r["_prev_attempt"] + 1})
    total = len(todo)
    print(f"recall: {total} jobs todo (retry={args.retry})", flush=True)

    out_lock = threading.Lock()
    pool = Pool(out_lock)
    t0 = time.time()
    i = 0
    while i < total or pool.inflight:
        if args.max_seconds and time.time() - t0 > args.max_seconds and not pool.inflight:
            print(f"time budget exhausted, {total - i} jobs left unsubmitted", flush=True)
            break
        while (i < total and len(pool.inflight) < MAX_INFLIGHT
               and not (args.max_seconds and time.time() - t0 > args.max_seconds)):
            item = todo[i]
            i += 1
            job, attempt = item["job"], item["attempt"]
            rec = {"op": job["op"], "dtype": job["dtype"], "job_shape": job["shape"],
                   "attempt": attempt,
                   "fix_note": FIX_NOTE.get(job["op"], "bench_op 分发修复")}
            try:
                pool.submit(job, rec)
            except Exception as e:  # noqa: BLE001
                rec.update({"state": "submit_error", "error": f"{type(e).__name__}: {e}"[:300],
                            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                with out_lock:
                    with open(OUT_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[recall] {job['op']}: submit_error {e}", flush=True)
        if pool.inflight:
            pool.poll_once()
        else:
            time.sleep(5)
    print("recall ALL DONE", flush=True)


if __name__ == "__main__":
    main()
