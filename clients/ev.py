#!/usr/bin/env python
"""ev — 一次性直连 evald 客户端（agent 交互通道，2026-09-10）。

目标：agent 不依赖常驻工单驱动（clients/eval_pipeline.py）也能安全用 evald。
本工具**无状态、单进程跑完即退**：提交 → 长轮询 → （--judge 机械判定+落盘）→ stdout JSON。
无本地状态文件 → 不存在双实例互踩；同路径并发由服务端 Store.path_lock 串行化；
队列过载由服务端 429 + 内置退避消化；作业记录在板上 spool 跨重启存活，
中断后用 `get <job_id>` 续接，永不丢作业。

双部署形态（同一份代码）：
- Windows 仓库（正本 `clients/ev.py`）：全功能。`--judge` 惰性 import eval_pipeline，
  复用其 judge()/registry/落盘契约（ADR-0012 同簇比较、§11 >5% ratchet、
  stochastic/suspended 语义一致），registry 与管线共享 runs/E1/.incumbent-registry.json
  （原子写；并发判定的最坏情形 = 一次改进被晚写者覆盖，raw.json 恒有全量记录可审计）。
- 板上副本（K3 `/home/c4y/autoke/evald/ev.py`，token 就近读同目录 config.json）：
  status/run/submit/get/history/cancel 开箱即用（探活/金丝雀/恢复验证）；`--judge`
  与非法 shape 的 manifest 回退需要仓库上下文，板上不可用并明确报错。

批量战役（多算子排闸 / func→acc→perf 接力 / baseline 扫描）仍走工单+驱动管线，
见 并发评测经验.md；两者可共存（服务端队列与 path_lock 是仲裁者）。

PSR 附加门：timing 作业在 K3 8788 上 psr<8 → outcome=psr_infra_fail
（实验环境.md §4.6，不计候选 attempt）；correctness/func 车道只回显 psr 不设门
（X100 分核下 PSR 本就 <8）。

用法：
  ev.py status [--limit 8]                  # 双实例探活 + 车道计数 + 最近作业
  ev.py run --op add --shape 64,512,512     # 提交并同步等终态（推荐交互入口）
            [--lane timing|correctness|func] [--dtype float32]
            [--candidate FILE [--deploy-path flag_gems/ops/add.py]]
            [--shape 可重复=多 shape 一单] [--warmup 5 --rep 5] [--timeout-min 15]
            [--bench gemm --batch 8] [--keep-deployed(=定稿 restore:false)]
            [--judge [--cond KOPE] [--stem ev-HHMMSS] [--attempt 1]]
            [--base URL] [--budget-s 480] [--out FILE]
  ev.py submit …                            # 同 run 参数，只投不等，打印 job_id
  ev.py get <job_id> [--wait 60] [--base URL]   # 长轮询一次（≤60s），续接用
  ev.py judge <job_id> [--cond KOPE --stem ev-HHMMSS --attempt 1]
                                            # 对已终态作业补机械判定+落盘（需仓库）
  ev.py history [--limit 20] [--base URL]
  ev.py cancel <job_id> [--base URL]

退出码：0=终态；5=预算内未终态（job_id 已在 stdout，用 get 续接）；3=healthz 失败；
4=提交失败（含退避耗尽）；2=用法/参数/落盘冲突。
Bash 调用建议 timeout ≥ 600000ms（计时作业均值 ~85s + 排队余量）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parent        # = 仓库根（板上部署时 = ~/autoke）
ROOT = REPO_DIR.parent            # 战役工作区根：candidate 相对路径、runs/E1 读数语义
if os.environ.get("EV_WORKSPACE"):   # 仓已独立于工作区，无法从仓位置推导 → 显式覆盖
    ROOT = Path(os.environ["EV_WORKSPACE"]).expanduser().resolve()

BASE = "http://10.18.60.100:8788"
DEFAULT_CORR_BASE = "http://10.18.60.100:8791"
BOARD_EVALD_DIR = Path("/home/c4y/autoke/evald")   # 板上副本所在目录（token 就近）
BACKOFF_S = (30, 60, 120, 240)   # 429 退避序列，受 --budget-s 硬约束
TERMINAL = ("done", "failed", "timeout", "canceled")
RE_SHAPE_OK = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")


def now_local() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[ev {datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 2):
    print(f"[ev] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_token() -> str:
    """EV_TOKEN 环境变量 → 仓库 config.json → 脚本同目录 → 板上 evald 目录。"""
    env = os.environ.get("EV_TOKEN")
    if env:
        return env.strip()
    for p in (REPO_DIR / "config.json",
              SELF_DIR / "config.json",
              BOARD_EVALD_DIR / "config.json"):
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))["token"]
        except (OSError, ValueError, KeyError):
            continue
    die("找不到 token：设 EV_TOKEN 或在仓库/脚本目录放 config.json")


TOKEN = load_token()


def req(method: str, path: str, body=None, timeout_s=90, base=BASE):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def corr_base() -> str:
    return load_json(ROOT / "runs" / "E1" / ".pipeline-config.json", {}).get(
        "correctness_base", DEFAULT_CORR_BASE)


def base_for_lane(lane: str, override: str | None = None) -> str:
    if override:
        return override.rstrip("/")
    return BASE if lane == "timing" else corr_base()


def base_label(base: str) -> str:
    if base.rstrip("/") == BASE:
        return "k3"
    if base.rstrip("/").endswith(":8791"):
        return "k3-acc"
    return str(base)


def resolve_kind(lane: str) -> str:
    return {"correctness": "correctness", "func": "func"}.get(lane, "timing-final")


def compact(job: dict) -> dict:
    r = job.get("result") or {}
    req_ = job.get("request") or {}
    return {"job_id": job.get("job_id"), "state": job.get("state"),
            "op": req_.get("op"), "lane": req_.get("lane", "timing"),
            "duration_s": job.get("duration_s"), "queue_wait_s": job.get("queue_wait_s"),
            "speedup": r.get("speedup") if isinstance(r, dict) else None,
            "correct": r.get("correct") if isinstance(r, dict) else None}


# ----------------------------------------------------------------- submit ---

def _norm_shape(op: str, shapes: list[str]) -> tuple[str | list[str], str | None]:
    """先本地正则校验；非法时才借仓库 eval_pipeline.norm_shape 做 manifest 回退。"""
    if all(RE_SHAPE_OK.match(s.strip()) for s in shapes):
        return (shapes[0].strip() if len(shapes) == 1 else [s.strip() for s in shapes]), None
    try:
        from eval_pipeline import norm_shape  # 仓库上下文才可用（含 manifest 回退）
    except Exception:  # noqa: BLE001
        die(f"shape {shapes!r} 非法，且本环境无仓库上下文（eval_pipeline）可做 manifest 回退")
    return norm_shape(op, shapes[0] if len(shapes) == 1 else list(shapes))


def build_body(a: argparse.Namespace) -> tuple[dict, dict]:
    """构造作业体；返回 (body, meta)。meta 带 sha256/deploy_path/shape_warn 供展示与落盘。"""
    if a.bench == "gemm":
        if a.op not in ("mm", "addmm", "bmm"):
            die(f"gemm 基准 op 只支持 mm|addmm|bmm，得到 {a.op!r}")
        if not a.shape or len(a.shape) != 1:
            die("gemm 基准 --shape 恰好一个（逗号分隔方阵边长列表，如 384,512）")
        shape, warn = a.shape[0].strip(), None
        if not RE_SHAPE_OK.match(shape):
            die(f"gemm shape {shape!r} 非纯数字逗号串")
        body = {"bench": "gemm", "op": a.op, "shape": shape, "dtype": a.dtype,
                "warmup": a.warmup, "rep": a.rep, "timeout_min": a.timeout_min}
        if a.batch:
            body["batch"] = a.batch
    else:
        shape, warn = _norm_shape(a.op, [s for s in a.shape])
        body = {"op": a.op, "shape": shape, "dtype": a.dtype,
                "warmup": a.warmup, "rep": a.rep, "timeout_min": a.timeout_min}
    if a.lane != "timing":
        body["lane"] = a.lane
    if a.keep_deployed:
        body["restore"] = False
    meta: dict = {"sha256": None, "deploy_path": None, "shape_warn": warn,
                  "shape_sent": shape, "candidate_path": None}
    if a.candidate:
        cp = Path(a.candidate)
        if not cp.is_absolute():
            cp = ROOT / cp
        if not cp.exists():
            die(f"候选文件不存在（防计时空跑，见排坑清单）：{a.candidate}"
                f"（相对路径按工作区根 {ROOT} 解析；根不对时设 EV_WORKSPACE）")
        dep = a.deploy_path or f"flag_gems/ops/{a.op}.py"
        body["files"] = [{"path": dep, "content": cp.read_text(encoding="utf-8")}]
        meta["sha256"] = hashlib.sha256(cp.read_bytes()).hexdigest()
        meta["deploy_path"] = dep
        try:
            meta["candidate_path"] = str(cp.relative_to(ROOT))
        except ValueError:
            meta["candidate_path"] = str(cp)
    return body, meta


def submit_with_backoff(body: dict, base: str, deadline: float) -> dict:
    for i, wait in enumerate((*BACKOFF_S, None)):
        try:
            return req("POST", "/jobs", body, base=base)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 429 and wait is not None and time.monotonic() + wait < deadline:
                log(f"429 队列满，退避 {wait}s（第 {i + 1} 次）")
                time.sleep(wait)
                continue
            die(f"提交失败 HTTP {e.code}: {detail}", code=4)
        except Exception as e:  # noqa: BLE001
            die(f"提交失败：{e}", code=4)
    die("提交失败：429 退避耗尽（队列持续满）", code=4)


def healthz_gate(base: str) -> None:
    try:
        h = req("GET", "/healthz", timeout_s=15, base=base)
        if not h.get("ok"):
            raise RuntimeError(f"healthz body not ok: {h}")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        die(f"healthz 探活失败（{base}）：{e}；处置见 实验环境.md §4.6（evaldctl stop→start）", code=3)


# ------------------------------------------------------------------ judge ---

def finalize(job: dict, base: str, cond: str, op: str, stem: str, attempt: int,
             meta: dict) -> dict:
    """对终态作业做机械判定并落盘（契约与管线 finalize_ticket 同构）。

    需要仓库上下文（eval_pipeline 的 judge()/registry/cond_root）；板上副本
    没有 runs/ 与 manifest，明确报错而不是半途写坏。
    """
    try:
        from eval_pipeline import (REGISTRY_FILE, FLAGS, MECHANICAL_OK,
                                   cond_root, judge, _split_cases)
    except Exception as e:  # noqa: BLE001
        die(f"--judge 需要仓库上下文（eval_pipeline：{e}）；板上副本只支持 "
            f"status/run/submit/get/history/cancel，判定请在 Windows 仓库侧做")
    res_dir = cond_root(cond) / cond / op / "results"
    raw_path = res_dir / f"{stem}.raw.json"
    if raw_path.exists():
        die(f"refuse：{raw_path} 已存在，不覆盖；换 --stem 重跑")
    kind = meta.get("kind") or resolve_kind(
        (job.get("request") or {}).get("lane", "timing"))
    r, cases = _split_cases(job)
    tk = {"op": op, "kind": kind, "attempt": attempt, "cond": cond,
          "shape": (job.get("request") or {}).get("shape") or meta.get("shape_sent"),
          "dtype": (job.get("request") or {}).get("dtype"),
          "candidate_sha256": meta.get("sha256"),
          "candidate_path": meta.get("candidate_path")}
    job_j = dict(job, result=r) if cases is not None else job
    registry = load_json(REGISTRY_FILE, {})
    outcome, note, registry = judge(tk, job_j, registry, base)
    atomic_write(REGISTRY_FILE, registry)
    # PSR 附加门：只对 A100 计时正本生效（§4.6；X100 车道 PSR 本就 <8，不设门）
    psr = r.get("psr") if isinstance(r, dict) else None
    if kind == "timing-final" and base.rstrip("/") == BASE and psr is not None and float(psr) < 8:
        outcome = "psr_infra_fail"
        note = f"{note}；PSR={psr}<8 按 infra 失败处置（不计候选 attempt）"
    res_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "schema": "raw-v1", "writer": "ev-v1", "condition": cond, "kind": kind,
        "op": op, "shape": tk["shape"], "dtype": tk["dtype"], "attempt": attempt,
        "candidate_sha256": meta.get("sha256"), "candidate_path": meta.get("candidate_path"),
        "deploy_path": meta.get("deploy_path"),
        "job_id": job.get("job_id"), "state": job.get("state"),
        "evald_ts": job.get("ts") or now_local(), "evald_host": base_label(base),
        "queue_wait_s": job.get("queue_wait_s"), "duration_s": job.get("duration_s"),
        "shape_sent": meta.get("shape_sent"), "shape_warn": meta.get("shape_warn"),
        "result": r,
        "kope_mem": {"speedup": r.get("speedup") if isinstance(r, dict) else None,
                     "pass_rate": (1.0 if r.get("correct") is True else 0.0)
                                  if isinstance(r, dict) else 0.0},
        "stdout_tail": job.get("stdout_tail"), "stderr_tail": job.get("stderr_tail"),
        "error": job.get("error"),
    }
    if cases is not None:
        raw["result_cases"] = cases
    atomic_write(raw_path, raw)
    result = {"job_id": job.get("job_id"), "state": job.get("state"),
              "evald_ts": raw["evald_ts"], "evald_host": raw["evald_host"],
              "condition": cond, "op": op, "attempt": attempt, "kind": kind,
              "candidate_sha256": meta.get("sha256"), "candidate_path": meta.get("candidate_path"),
              "result": r, "stdout_tail": job.get("stdout_tail"),
              "stderr_tail": job.get("stderr_tail"), "error": job.get("error"),
              "queue_wait_s": job.get("queue_wait_s"), "duration_s": job.get("duration_s"),
              "psr": psr,
              "outcome": outcome, "retry_notes": note, "semantics_note": note,
              "judge": "ev-v1-mechanical（与管线共享 judge()）"}
    atomic_write(res_dir / f"{stem}.result.json", result)
    jobs_log = cond_root(cond) / "evald_jobs.jsonl"
    with open(jobs_log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": job.get("job_id"), "ts": now_local(),
                            "condition": cond, "op": op, "attempt": attempt,
                            "kind": kind, "state": job.get("state"),
                            "duration_s": job.get("duration_s"), "outcome": outcome,
                            "base": base, "writer": "ev-v1", "stem": stem}) + "\n")
    if outcome not in MECHANICAL_OK:
        with open(FLAGS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_local(), "condition": cond, "op": op,
                                "attempt": attempt, "outcome": outcome,
                                "job_id": job.get("job_id"),
                                "state": job.get("state"), "writer": "ev-v1"}) + "\n")
    return {"outcome": outcome, "note": note, "psr": psr, "raw": str(raw_path),
            "result": str(res_dir / f"{stem}.result.json")}


# -------------------------------------------------------------- commands ---

def cmd_run(a: argparse.Namespace) -> int:
    base = base_for_lane(a.lane, a.base)
    deadline = time.monotonic() + a.budget_s
    healthz_gate(base)
    body, meta = build_body(a)
    sub = submit_with_backoff(body, base, deadline)
    jid = sub["job_id"]
    log(f"submitted {jid} -> {base_label(base)} kind={resolve_kind(a.lane)}"
        + (f" sha256={meta['sha256'][:12]}" if meta["sha256"] else "")
        + (f" shape_warn: {meta['shape_warn']}" if meta["shape_warn"] else ""))
    job = None
    last_state = None
    while time.monotonic() < deadline:
        remain = deadline - time.monotonic()
        w = int(max(5, min(60, remain + 2)))
        try:
            job = req("GET", f"/jobs/{jid}?wait={w}", timeout_s=w + 30, base=base)
        except Exception as e:  # noqa: BLE001
            log(f"poll {jid} error: {e}，重试")
            time.sleep(5)
            continue
        st = job.get("state")
        if st != last_state:
            log(f"state={st}")
            last_state = st
        if st in TERMINAL:
            break
        job = None
    if job is None:  # 预算耗尽仍未终态：job 在板上 spool，永不丢
        print(json.dumps({"job_id": jid, "state": "inflight",
                          "resumable": f"python clients/ev.py get {jid} --wait 60",
                          "base": base}, ensure_ascii=False))
        return 5
    out = dict(job)
    if a.judge:
        stem = a.stem or f"ev-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        meta["kind"] = resolve_kind(a.lane)
        j = finalize(job, base, a.cond, a.op, stem, a.attempt, meta)
        out["outcome"], out["retry_notes"], out["recorded"] = j["outcome"], j["note"], j
        log(f"judged: {j['outcome']} @ {j['raw']}")
    if a.out:
        atomic_write(Path(a.out), out)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_submit(a: argparse.Namespace) -> int:
    base = base_for_lane(a.lane, a.base)
    healthz_gate(base)
    body, meta = build_body(a)
    sub = submit_with_backoff(body, base, time.monotonic() + 600)
    print(json.dumps({"job_id": sub["job_id"], "state": sub.get("state", "queued"),
                      "position": sub.get("position"), "base": base,
                      "kind": resolve_kind(a.lane), "meta": meta}, ensure_ascii=False))
    return 0


def cmd_get(a: argparse.Namespace) -> int:
    base = a.base.rstrip("/") if a.base else BASE
    job = req("GET", f"/jobs/{a.job_id}?wait={max(1, min(60, a.wait))}",
              timeout_s=a.wait + 30, base=base)
    print(json.dumps(job, ensure_ascii=False))
    return 0


def cmd_judge(a: argparse.Namespace) -> int:
    base = a.base.rstrip("/") if a.base else BASE
    job = req("GET", f"/jobs/{a.job_id}", timeout_s=30, base=base)
    if job.get("state") not in TERMINAL:
        die(f"作业未终态（state={job.get('state')}）；先 get 等终态再 judge")
    req_ = job.get("request") or {}
    op = req_.get("op") or die("作业缺 request.op，无法判定")
    lane = req_.get("lane", "timing")
    stem = a.stem or f"ev-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    meta = {"kind": resolve_kind(lane), "sha256": None, "candidate_path": None,
            "deploy_path": None, "shape_sent": req_.get("shape"), "shape_warn": None}
    j = finalize(job, base, a.cond, op, stem, a.attempt, meta)
    print(json.dumps({"job_id": a.job_id, "op": op, "stem": stem, **j},
                     ensure_ascii=False))
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    out: dict = {}
    for name, base in (("timing_8788", BASE), ("corr", corr_base())):
        try:
            h = req("GET", "/healthz", timeout_s=10, base=base)
            out[name] = {"base": base, "ok": h.get("ok"),
                         "queued": h.get("queued"), "running": h.get("running"),
                         "lanes": h.get("lanes"), "loadavg": h.get("loadavg")}
        except Exception as e:  # noqa: BLE001
            out[name] = {"base": base, "ok": False, "error": str(e)[:120]}
    try:
        jobs = req("GET", f"/jobs?limit={a.limit}", timeout_s=15)["jobs"]
        out["recent"] = [compact(j) for j in jobs]
    except Exception as e:  # noqa: BLE001
        out["recent_error"] = str(e)[:120]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_history(a: argparse.Namespace) -> int:
    base = a.base.rstrip("/") if a.base else BASE
    jobs = req("GET", f"/jobs?limit={a.limit}", timeout_s=15, base=base)["jobs"]
    print(json.dumps([compact(j) for j in jobs], ensure_ascii=False, indent=1))
    return 0


def cmd_cancel(a: argparse.Namespace) -> int:
    base = a.base.rstrip("/") if a.base else BASE
    out = req("DELETE", f"/jobs/{a.job_id}", timeout_s=30, base=base)
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------- CLI ---

def add_submit_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--op", required=True)
    sp.add_argument("--shape", action="append", default=None,
                    help="数字逗号串；可重复传 = 多 shape 一单（一单 N case）")
    sp.add_argument("--dtype", default="float32")
    sp.add_argument("--lane", choices=["timing", "correctness", "func"], default="timing")
    sp.add_argument("--bench", choices=["op", "gemm"], default="op")
    sp.add_argument("--batch", type=int, default=None, help="仅 gemm")
    sp.add_argument("--candidate", default=None, help="候选文件（仓库相对或绝对路径）")
    sp.add_argument("--deploy-path", default=None,
                    help="部署路径，默认 flag_gems/ops/<op>.py；白名单见 README")
    sp.add_argument("--warmup", type=int, default=5)
    sp.add_argument("--rep", type=int, default=5)
    sp.add_argument("--timeout-min", type=float, default=15)
    sp.add_argument("--keep-deployed", action="store_true",
                    help="定稿作业 restore:false——测完保留候选，仅波次收尾用")
    sp.add_argument("--base", default=None, help="覆盖 evald 地址（默认 K3 8788；"
                                                 "correctness/func 默认 8791）")


def add_base_arg(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--base", default=None, help="覆盖 evald 地址（默认 K3 8788）")


def main() -> None:
    p = argparse.ArgumentParser(prog="ev.py", description="一次性直连 evald 客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="提交并同步等终态")
    add_submit_args(run)
    run.add_argument("--judge", action="store_true", help="终态后机械判定+落盘（与管线同口径）")
    run.add_argument("--cond", default="KOPE", help="判定落盘条件目录")
    run.add_argument("--stem", default=None, help="结果文件名干，默认 ev-<时间戳>")
    run.add_argument("--attempt", type=int, default=1)
    run.add_argument("--budget-s", type=int, default=480,
                     help="总预算秒（含 429 退避）；耗尽 exit 5，job_id 在 stdout")
    run.add_argument("--out", default=None, help="把完整响应另存为 JSON 文件")
    run.set_defaults(fn=cmd_run)

    sm = sub.add_parser("submit", help="只提交不等待")
    add_submit_args(sm)
    sm.set_defaults(fn=cmd_submit)

    gt = sub.add_parser("get", help="长轮询一次（≤60s）")
    gt.add_argument("job_id")
    gt.add_argument("--wait", type=int, default=60)
    add_base_arg(gt)
    gt.set_defaults(fn=cmd_get)

    jd = sub.add_parser("judge", help="对已终态作业补机械判定+落盘（需仓库上下文）")
    jd.add_argument("job_id")
    jd.add_argument("--cond", default="KOPE")
    jd.add_argument("--stem", default=None)
    jd.add_argument("--attempt", type=int, default=1)
    add_base_arg(jd)
    jd.set_defaults(fn=cmd_judge)

    st = sub.add_parser("status", help="双实例探活 + 最近作业")
    st.add_argument("--limit", type=int, default=8)
    st.set_defaults(fn=cmd_status)

    hi = sub.add_parser("history", help="最近作业列表")
    hi.add_argument("--limit", type=int, default=20)
    add_base_arg(hi)
    hi.set_defaults(fn=cmd_history)

    ca = sub.add_parser("cancel", help="取消排队中的作业")
    ca.add_argument("job_id")
    add_base_arg(ca)
    ca.set_defaults(fn=cmd_cancel)

    args = p.parse_args()
    if args.cmd in ("run", "submit") and not args.shape:
        die("缺少 --shape（数字逗号串，如 64,512,512；gemm 用 384,512）")
    raise SystemExit(args.fn(args))


if __name__ == "__main__":
    main()
