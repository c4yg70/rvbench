#!/usr/bin/env python3
"""evald — SpacemiT K3 (RISC-V) 算子代码评估服务器

本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业（UUIDv7），
每个作业自包含部署-恢复 site-packages 候选文件（防异步队列下的代码残留）。
作业按 "lane" 字段分两车道：timing（默认）＝单 worker 串行执行并持有
/tmp/k3_bench.lock（与手工 SSH 基准互斥，计时语义不变）；correctness＝
并行 worker 池、不持计时锁、强制 warmup=0/rep=1、taskset 分核（handoff
a100-x100 §3-L2），仅正确性筛选，speedup 无意义仅作返回兼容。

设计规格：docs/eval-server-design.md；决策：docs/adr/。
仅用 stdlib（板上 FlagGems venv 无 pip，见 ADR-0003）。

用法（板上系统 python3.14，原生 uuid7）::

  python3 evald.py --config /home/c4y/autoke/evald/config.json   # 前台运行
  python3 evald.py --selftest --config ...                       # 自检（不监听端口）
  python3 evald.py --gen-token                                   # 生成随机 token
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TERMINAL_STATES = {"done", "failed", "timeout", "canceled"}
TAIL_BYTES = 4096

# op 名：允许前导下划线（_reshape_alias），长度上限 48（torch 最长算子名）；
# 字符集仍限 [a-z0-9_]，天然拒绝点/斜杠等路径危险字符
RE_OP = re.compile(r"^[a-z_][a-z0-9_]{0,47}$")
RE_SHAPE = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
GEMM_OPS = {"mm", "addmm", "bmm"}
OP_DTYPES = {"float32", "float16", "int32", "int64", "bool"}


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t if t is not None else time.time()))


def uuid7() -> str:
    if hasattr(uuid, "uuid7"):  # Python >= 3.14
        return str(uuid.uuid7())
    b = bytearray(os.urandom(16))
    b[0:6] = (time.time_ns() // 1_000_000).to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("token", "flag_gems_root", "scripts_root", "run_env"):
        if not cfg.get(key):
            raise SystemExit(f"config 缺少必填项: {key}")
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8788)
    cfg.setdefault("data_dir", os.path.dirname(os.path.abspath(path)))
    cfg.setdefault("lock_file", "/tmp/k3_bench.lock")
    cfg.setdefault("queue_max", 64)  # baseline 批量捞回阶段减少客户端 429
    cfg.setdefault("timeout_default_min", 15)
    cfg.setdefault("timeout_max_min", 30)
    cfg.setdefault("body_max_bytes", 8 * 1024 * 1024)
    cfg.setdefault("history_mem", 1000)
    cfg.setdefault("longpoll_max_s", 60)
    cfg.setdefault("executor", "warmfork")  # M3 起默认预热执行器；可回退 "subprocess"
    cfg.setdefault("warmd_path", os.path.join(cfg["data_dir"], "warmd.py"))
    cfg.setdefault("warmd_env", {})
    # L2 correctness 车道（handoff §3-L2）：并行正确性筛选，不持计时锁、不进稳态计时。
    # 回退开关：correctness_workers=0 即回到纯计时车道（无 lane 字段行为与旧版一致）
    cfg.setdefault("correctness_workers", 3)      # correctness 并行 worker 数；0=关闭车道
    cfg.setdefault("correctness_cpus", "4-6")     # 车道 taskset 核（须为守护 allowed_list 子集：X100 部署 4-6；A100 注册 8-15 后用 12-14，disjoint 会 EINVAL）
    cfg.setdefault("correctness_omp_threads", 1)  # 注入 OMP_NUM_THREADS（与分核对齐，防 3 作业超订）
    cfg.setdefault("correctness_executor", None)  # 车道默认执行器；None=跟随请求/全局 cfg（warmfork）
    return cfg


# ---------------------------------------------------------------- 白名单解析

def resolve_path(cfg, relpath):
    """把作业里的相对路径解析为板上绝对路径；白名单外返回 None。

    允许：flag_gems/ops/**、flag_gems/runtime/backend/_spacemit/**、
    scripts/bench_op.py、scripts/bench_gemm.py（ADR-0001 的写入边界）。
    """
    if not isinstance(relpath, str) or "\\" in relpath or relpath.startswith("/"):
        return None
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] == "flag_gems" and len(parts) >= 3:
        rest = parts[1:]
        if rest[0] == "ops":
            root = cfg["flag_gems_root"]
        elif rest[:3] == ["runtime", "backend", "_spacemit"]:
            root = cfg["flag_gems_root"]
        else:
            return None
        abs_path = os.path.normpath(os.path.join(root, *rest))
    elif parts[0] == "scripts" and len(parts) == 2 and parts[1] in ("bench_op.py", "bench_gemm.py"):
        abs_path = os.path.normpath(os.path.join(cfg["scripts_root"], parts[1]))
    else:
        return None
    root_norm = os.path.normpath(root if parts[0] == "flag_gems" else cfg["scripts_root"])
    if os.path.commonpath([root_norm, abs_path]) != root_norm:
        return None
    return abs_path


# ---------------------------------------------------------------- 部署-恢复

def deploy_files(cfg, job_dir, files):
    """备份原文件 → rm 断硬链 → 写候选。每写一个文件就更新 manifest，
    部署中途失败时 restore_files 可按已有 manifest 回滚。"""
    backup_root = os.path.join(job_dir, "backup")
    manifest = []
    for ent in files:
        abs_path = resolve_path(cfg, ent["path"])
        if abs_path is None:
            raise ValueError(f"路径不在白名单内: {ent['path']}")
        existed = os.path.lexists(abs_path)
        backup_path = os.path.join(backup_root, ent["path"])
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        created_dirs = _missing_parents(abs_path)
        if existed:
            shutil.copy2(abs_path, backup_path)
            mode = os.stat(abs_path).st_mode & 0o777
            os.remove(abs_path)  # 断 site-packages 硬链
        else:
            mode = 0o644
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(ent["content"])
        os.chmod(abs_path, mode)
        manifest.append({
            "path": ent["path"], "abs": abs_path, "backup": backup_path,
            "existed": existed, "created_dirs": created_dirs,
            "sha256": sha256_hex(ent["content"]),
        })
        with open(os.path.join(job_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def _missing_parents(abs_path):
    """记录部署时新创建的父目录（restore 时清理空目录用）。"""
    missing = []
    d = os.path.dirname(abs_path)
    while d and not os.path.isdir(d):
        missing.append(d)
        d = os.path.dirname(d)
    return missing


def restore_files(job_dir):
    """按 manifest 恢复原文件（新文件则删除候选）；空目录回收。
    manifest 缺失（部署一个文件都没写成）时静默返回。"""
    mp = os.path.join(job_dir, "manifest.json")
    if not os.path.exists(mp):
        return
    with open(mp, encoding="utf-8") as f:
        manifest = json.load(f)
    for ent in manifest:
        abs_path, backup = ent["abs"], ent["backup"]
        if os.path.lexists(abs_path):
            os.remove(abs_path)
        if ent["existed"]:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            shutil.copy2(backup, abs_path)
        for d in sorted(ent["created_dirs"], key=len, reverse=True):
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass


# ---------------------------------------------------------------- 请求校验

def validate_request(cfg, body):
    """校验并清洗提交体。返回 (sanitized, files, err)。"""
    if not isinstance(body, dict):
        return None, None, "body 必须是 JSON 对象"
    bench = body.get("bench", "op")
    if bench not in ("op", "gemm"):
        return None, None, "bench 只能是 op | gemm"
    op = body.get("op")
    if not isinstance(op, str) or not RE_OP.match(op):
        return None, None, "op 不合法"
    if bench == "gemm" and op not in GEMM_OPS:
        return None, None, f"gemm 只支持 {sorted(GEMM_OPS)}"
    dtype = body.get("dtype", "float32")
    if dtype not in OP_DTYPES:
        return None, None, f"dtype 只支持 {sorted(OP_DTYPES)}"
    shape = body.get("shape")
    if not isinstance(shape, str) or not RE_SHAPE.match(shape):
        return None, None, "shape 不合法（如 64,512,512）"
    warmup = body.get("warmup", 25)
    rep = body.get("rep", 100)
    if not isinstance(warmup, int) or not isinstance(rep, int) or not (0 <= warmup <= 10000 and 1 <= rep <= 10000):
        return None, None, "warmup/rep 必须是 0..10000 的整数"
    batch = body.get("batch", 8)
    if not isinstance(batch, int) or not (1 <= batch <= 4096):
        return None, None, "batch 必须是 1..4096 的整数"
    restore = body.get("restore", True)
    if not isinstance(restore, bool):
        return None, None, "restore 必须是 bool"
    timeout_min = body.get("timeout_min", cfg["timeout_default_min"])
    if not isinstance(timeout_min, (int, float)) or not (0.1 <= timeout_min <= cfg["timeout_max_min"]):
        return None, None, f"timeout_min 必须在 0.1..{cfg['timeout_max_min']} 分钟之间"
    executor = body.get("executor")
    if executor is not None and executor not in ("subprocess", "warmfork"):
        return None, None, "executor 只能是 subprocess | warmfork"
    lane = body.get("lane", "timing")
    if lane not in ("timing", "correctness"):
        return None, None, "lane 只能是 timing | correctness"
    # K1 池（board-pool.json must_disable）：force_lane 服务端锁死车道，客户端提交
    # 任何 lane/缺省都被强制改写；K3 部署不配置此键，行为逐字节不变
    if cfg.get("force_lane"):
        lane = cfg["force_lane"]
    files = body.get("files", [])
    if not isinstance(files, list) or len(files) > 32:
        return None, None, "files 最多 32 个"
    seen = set()
    for ent in files:
        if not isinstance(ent, dict) or not isinstance(ent.get("path"), str) or not isinstance(ent.get("content"), str):
            return None, None, "files[].path/content 必须是字符串"
        if ent["path"] in seen:
            return None, None, f"files 内路径重复: {ent['path']}"
        seen.add(ent["path"])
        if resolve_path(cfg, ent["path"]) is None:
            return None, None, f"路径不在白名单内: {ent['path']}"
    sanitized = {
        "op": op, "shape": shape, "dtype": dtype, "warmup": warmup, "rep": rep,
        "bench": bench, "restore": restore, "timeout_min": timeout_min,
        "executor": executor,
        "files": [{"path": e["path"], "sha256": sha256_hex(e["content"]),
                   "bytes": len(e["content"].encode("utf-8"))} for e in files],
    }
    # 向后兼容：body 无 lane 字段时 sanitized 不带 lane 键（request 回显/落盘逐字节不变）；
    # force_lane 生效时必须显式带 lane 键，否则执行层按缺省 timing 处理
    if "lane" in body or cfg.get("force_lane"):
        sanitized["lane"] = lane
    if lane == "correctness":
        # 车道语义（handoff §3-L2）：只筛正确性、不做稳态计时——覆盖轮数并注记原值。
        # bench_op 直调层 do_bench 的 warmup/rep 是时间预算（n=max(1,...)），0/1 安全；
        # 正确性比较发生在 do_bench 之外，不受影响
        sanitized["warmup"], sanitized["rep"] = 0, 1
        sanitized["requested_warmup"], sanitized["requested_rep"] = warmup, rep
    if bench == "gemm":
        sanitized["batch"] = batch
    return sanitized, files, None


# ---------------------------------------------------------------- 作业存储

class Job:
    def __init__(self, job_id, seq, request, extra=None):
        self.job_id = job_id
        self.seq = seq
        self.state = "queued"
        self.request = request
        # result_extra（board-pool must_enable）：随响应与落盘记录附加的固定标记
        self.extra = dict(extra) if extra else None
        self.result = None
        self.kope_mem = None
        self.error = None
        self.stdout = ""
        self.stderr = ""
        self.submitted_at = time.time()
        self.started_at = None
        self.finished_at = None

    def to_json(self, full=True):
        o = {
            "job_id": self.job_id, "seq": self.seq, "state": self.state,
            "submitted_at": now_iso(self.submitted_at),
            "started_at": now_iso(self.started_at) if self.started_at else None,
            "finished_at": now_iso(self.finished_at) if self.finished_at else None,
            "queue_wait_s": round(self.started_at - self.submitted_at, 1) if self.started_at else None,
            "duration_s": round(self.finished_at - self.started_at, 1)
            if self.started_at and self.finished_at else None,
            "request": self.request, "result": self.result, "kope_mem": self.kope_mem,
            "error": self.error,
        }
        if getattr(self, "extra", None):
            o.update(self.extra)
        if full:
            o["logs"] = {
                "stdout_tail": self.stdout[-TAIL_BYTES:],
                "stderr_tail": self.stderr[-TAIL_BYTES:],
            }
        return o


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs_dir = os.path.join(cfg["data_dir"], "jobs")
        self.jsonl_path = os.path.join(cfg["data_dir"], "jobs.jsonl")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.jobs = {}
        self.order = deque()          # seq 升序的 job_id（含历史）
        # 按车道分列的队列；queue_max 对两车道总和生效（L2 提速不放大排队深度）
        self.queues = {"timing": deque(), "correctness": deque()}
        self.events = {}
        self.seq = 0
        # /metrics 聚合计数器（持久语义：初值由 _init_metrics 扫 spool 衔接历史，
        # 之后由 submit()/finish() 维护；与 textfile 探针的 spool 扫描口径一致）
        self.m_submissions = 0
        self.m_jobs = {"done": 0, "failed": 0, "timeout": 0}
        self.m_last_sub_ts = 0.0
        self.m_last_comp_ts = 0.0
        self.m_last_job = None          # (op, state) —— 只暴露最新一条
        self._load_history()
        self._sweep_stale()
        self._init_metrics()

    def _load_history(self):
        if not os.path.exists(self.jsonl_path):
            return
        with open(self.jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()[-self.cfg["history_mem"]:]
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            job = Job(rec["job_id"], rec["seq"], rec.get("request", {}))
            job.state = rec["state"]
            job.result = rec.get("result")
            job.kope_mem = rec.get("kope_mem")
            job.error = rec.get("error")
            job.submitted_at = rec.get("submitted_epoch", 0)
            job.started_at = rec.get("started_epoch")
            job.finished_at = rec.get("finished_epoch")
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            self.seq = max(self.seq, job.seq)

    def _sweep_stale(self):
        """上次服务器重启时被打断的作业（有目录无终态记录）标记 failed。"""
        for name in os.listdir(self.jobs_dir):
            jid = name
            d = os.path.join(self.jobs_dir, name)
            if not os.path.isdir(d) or jid in self.jobs:
                continue
            job = Job(jid, self.seq + 1, {})
            job.state = "failed"
            job.error = "server restarted during job"
            job.finished_at = time.time()
            job.started_at = job.submitted_at
            with self.lock:
                self.jobs[jid] = job
                self.order.append(jid)
                self.seq += 1
            self._persist(job)

    def _init_metrics(self):
        """/metrics 计数器初值：扫一遍 spool（jobs/ 永不清理），重启无缝衔接。

        目录数 → submissions_total；result.json 的 state → jobs_total；
        最新 result.json mtime → last_completion（并取其 op/state → last_job_info）；
        最新 request.json mtime → last_submission。与 textfile 探针的 spool
        扫描口径一致，两者可互相对账。
        """
        submissions = 0
        last_sub_ts = 0.0
        last_key, last_info = (0.0, ""), None
        try:
            names = os.listdir(self.jobs_dir)
        except OSError:
            names = []
        for name in names:
            d = os.path.join(self.jobs_dir, name)
            if not os.path.isdir(d):
                continue
            submissions += 1
            try:
                last_sub_ts = max(last_sub_ts,
                                  os.stat(os.path.join(d, "request.json")).st_mtime)
            except OSError:
                pass
            rp = os.path.join(d, "result.json")
            try:
                comp_ts = os.stat(rp).st_mtime
                with open(rp, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            state = rec.get("state")
            if state in self.m_jobs:
                self.m_jobs[state] += 1
            # 主序 mtime；旧文件系统秒级粒度下按 ISO finished_at tie-break
            key = (comp_ts, rec.get("finished_at") or "")
            if key > last_key:
                last_key = key
                req = rec.get("request") or {}
                last_info = (req.get("op") or "unknown", state or "unknown")
        with self.lock:
            self.m_submissions = submissions
            self.m_last_sub_ts = last_sub_ts
            self.m_last_comp_ts = last_key[0]
            self.m_last_job = last_info

    def submit(self, sanitized, payload):
        with self.cond:
            if sum(len(q) for q in self.queues.values()) >= self.cfg["queue_max"]:
                raise QueueFullError()
            self.seq += 1
            job = Job(uuid7(), self.seq, sanitized, extra=self.cfg.get("result_extra"))
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            # 完整 payload（含代码内容）先落作业目录再入队，避免与 worker 竞态
            d = self.job_dir(job.job_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            self.queues[sanitized.get("lane", "timing")].append(job.job_id)
            self.m_submissions += 1
            self.m_last_sub_ts = job.submitted_at
            # notify_all：waiter 分属两车道，单 notify 可能被错误车道的 waiter
            # 消费掉（其队列仍空 → 回去睡），作业滞留到下一次提交才被捡起
            self.cond.notify_all()
            return job

    def next_job(self, lane="timing"):
        """取本车道队头；其他车道提交会唤醒本等待（while 循环回守空队列）。"""
        with self.cond:
            while not self.queues[lane]:
                self.cond.wait()
            return self.jobs[self.queues[lane].popleft()]

    def position(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            q = self.queues[job.request.get("lane", "timing")]
            try:
                return list(q).index(job_id) + 1
            except ValueError:
                return None

    def cancel(self, job_id):
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None or job.state != "queued":
                return False
            self.queues[job.request.get("lane", "timing")].remove(job_id)
            job.state = "canceled"
            job.finished_at = time.time()
            self._persist(job)
            self._fire_event(job_id)
            return True

    def lane_stats(self):
        """healthz 用：每车道 queued/running 计数。"""
        with self.lock:
            return {lane: {"queued": len(self.queues[lane]),
                           "running": sum(1 for j in self.jobs.values()
                                          if j.state == "running" and
                                          j.request.get("lane", "timing") == lane)}
                    for lane in self.queues}

    def metrics_snapshot(self):
        """/metrics 用：聚合计数快照（lane 计数与 lane_stats 同源、同一把锁）。"""
        with self.lock:
            return {"lanes": self.lane_stats(),
                    "submissions": self.m_submissions,
                    "jobs": dict(self.m_jobs),
                    "last_sub_ts": self.m_last_sub_ts,
                    "last_comp_ts": self.m_last_comp_ts,
                    "last_job": self.m_last_job}

    def job_dir(self, job_id):
        return os.path.join(self.jobs_dir, job_id)

    def event_for(self, job_id):
        with self.lock:
            if job_id not in self.events:
                self.events[job_id] = threading.Event()
            return self.events[job_id]

    def _fire_event(self, job_id):
        with self.lock:
            evt = self.events.get(job_id)
        if evt:
            evt.set()

    def finish(self, job):
        with self.lock:
            job.finished_at = time.time()
            if job.state in self.m_jobs:    # 终态累计（canceled 不经此处）
                self.m_jobs[job.state] += 1
            self.m_last_comp_ts = job.finished_at
            self.m_last_job = (job.request.get("op") or "unknown", job.state)
            self._persist(job)
        self._fire_event(job.job_id)
        d = self.job_dir(job.job_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as f:
            json.dump(job.to_json(full=True), f, ensure_ascii=False, indent=1)
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(job.stdout)
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write(job.stderr)

    def _persist(self, job):
        rec = job.to_json(full=False)
        rec["submitted_epoch"] = job.submitted_at
        rec["started_epoch"] = job.started_at
        rec["finished_epoch"] = job.finished_at
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def history(self, limit=50, since=None):
        with self.lock:
            since_seq = -1
            if since:
                sj = self.jobs.get(since)
                if sj is None:
                    return None
                since_seq = sj.seq
            out = []
            for jid in self.order:
                j = self.jobs[jid]
                if j.seq > since_seq:
                    out.append(j)
                    if len(out) >= limit:
                        break
            return out


class QueueFullError(Exception):
    pass


# ---------------------------------------------------------------- 执行器（v1 子进程）

class SubprocessExecutor:
    def __init__(self, cfg, lane="timing"):
        self.cfg = cfg
        self.lane = lane

    def _taskset_argv(self):
        """correctness 车道把 bench 进程钉到车道核；timing 车道返回 []（不改变任何行为）。"""
        cpus = self.cfg.get("correctness_cpus")
        if self.lane == "correctness" and cpus and shutil.which("taskset"):
            return ["taskset", "-c", str(cpus)]
        return []

    def _correctness_prefix(self):
        if self.lane != "correctness":
            return ""
        exports = "K3_LANE=correctness"
        if self.cfg.get("correctness_omp_threads"):
            exports += f" OMP_NUM_THREADS={self.cfg['correctness_omp_threads']}"
        return f"export {exports} && "

    def build_command(self, req):
        # 所有参数均已通过 validate_request 的正则/枚举校验，无注入面
        if req["bench"] == "gemm":
            cmd = (f"python {self.cfg['scripts_root']}/bench_gemm.py --op {req['op']} "
                   f"--dtype {req['dtype']} --shapes {req['shape']}")
            if req["op"] == "bmm":
                cmd += f" --batch {req['batch']}"
        else:
            cmd = (f"python {self.cfg['scripts_root']}/bench_op.py --op {req['op']} "
                   f"--shape {req['shape']} --dtype {req['dtype']} "
                   f"--warmup {req['warmup']} --rep {req['rep']}")
        return f"source {self.cfg['run_env']} && {self._correctness_prefix()}{cmd}"

    def run(self, job):
        """返回 (state, result, kope_mem, error)。超时杀进程组。"""
        req = job.request
        timeout_s = req["timeout_min"] * 60
        bash_cmd = self.cfg.get("bench_command_override") or self.build_command(req)
        popen_kwargs = {}
        if self.lane == "correctness":
            # 覆盖 bench_command_override（selftest 钩子）时环境也必须到位；
            # bash 内 export 在 source 之后兜底，防 run_env 再覆盖
            env = dict(os.environ)
            env["K3_LANE"] = "correctness"
            if self.cfg.get("correctness_omp_threads"):
                env["OMP_NUM_THREADS"] = str(self.cfg["correctness_omp_threads"])
            popen_kwargs["env"] = env
        proc = subprocess.Popen(
            [*self._taskset_argv(), "bash", "-c", bash_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, **popen_kwargs)
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            job.stdout, job.stderr = out or "", err or ""
            return "timeout", None, None, f"exceeded {req['timeout_min']} min"
        job.stdout, job.stderr = out or "", err or ""
        if proc.returncode != 0:
            return "failed", None, None, f"bench rc={proc.returncode}"
        rows = _parse_json_lines(out)
        if not rows:
            return "failed", None, None, "no JSON line in stdout"
        result, kope_mem = _map_rows(req["bench"], rows)
        return "done", result, kope_mem, None


def _map_rows(bench, rows):
    """把 bench 输出的 JSON 行映射为 (result, kope_mem)。"""
    if bench == "gemm":
        ok = all(r.get("correct") for r in rows)
        spd = rows[0].get("speedup")
        result = rows[0] if len(rows) == 1 else {"metric": "gflops", "rows": rows}
        if isinstance(result, dict) and "metric" not in result:
            result = dict(result, metric="gflops")
        return result, {"speedup": spd, "pass_rate": 1.0 if ok else 0.0}
    row = rows[-1]
    return row, {"speedup": row.get("speedup"),
                 "pass_rate": 1.0 if row.get("correct") else 0.0}


def _parse_json_lines(text):
    rows = []
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
            if len(rows) >= 16:
                break
    rows.reverse()
    return rows


# ---------------------------------------------------------------- 执行器（M3 预热 fork）

class WarmForkExecutor:
    """warmd 常驻预热守护 + 每作业 fork 子进程（ADR-0004）。

    父进程只预热 torch+triton（与候选代码无关的不变量）；子进程全新
    import flag_gems/bench 脚本——候选代码永远从磁盘已部署文件加载。
    守护异常（起不来/EOF/协议错）时本作业自动回退 SubprocessExecutor。
    """

    def __init__(self, cfg, lane="timing"):
        self.cfg = cfg
        self.lane = lane
        self.proc = None
        self.stderr_tail = []
        self.lock = threading.Lock()

    def _drain_stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                self.stderr_tail.append(line)
                if len(self.stderr_tail) > 200:
                    del self.stderr_tail[:100]
        except (OSError, ValueError):
            pass

    def _spawn(self):
        env = dict(os.environ)
        env["WARMD_SCRIPTS_ROOT"] = self.cfg["scripts_root"]
        env.update(self.cfg.get("warmd_env", {}))
        if self.lane == "correctness":
            # 车道环境随守护 env 被 fork 子进程继承（warmd 不改语义）；
            # 守护整体 taskset → 每个作业子进程天然钉在车道核
            env["K3_LANE"] = "correctness"
            if self.cfg.get("correctness_omp_threads"):
                env["OMP_NUM_THREADS"] = str(self.cfg["correctness_omp_threads"])
        cmd = f"source {self.cfg['run_env']} && exec python {self.cfg['warmd_path']}"
        cpus = self.cfg.get("correctness_cpus")
        argv = ["bash", "-c", cmd]
        if self.lane == "correctness" and cpus and shutil.which("taskset"):
            argv = ["taskset", "-c", str(cpus)] + argv
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
            start_new_session=True)  # 独立进程组：卡死时可 killpg 整树
        self.stderr_tail = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        line = self._readline(120)  # 预热 torch+triton 上限
        try:
            if line != "" and line and json.loads(line).get("ready"):
                return
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"warmd not ready: {line!r} {''.join(self.stderr_tail[-5:])[-300:]}")

    def _readline(self, timeout_s):
        """限期读一行；None=超时，''=EOF，否则为行内容。"""
        deadline = time.time() + timeout_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self.proc.stdout], [], [], min(remaining, 1.0))
            if r:
                line = self.proc.stdout.readline()
                return line if line else ""

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
        self.proc = None

    def run(self, job):
        req = job.request
        timeout_s = req["timeout_min"] * 60
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._spawn()
                self.proc.stdin.write(json.dumps({
                    "id": job.job_id, "timeout_s": timeout_s,
                    "req": {k: req[k] for k in ("bench", "op", "shape", "dtype",
                                                "warmup", "rep", "batch") if k in req},
                }) + "\n")
                self.proc.stdin.flush()
                line = self._readline(timeout_s + 30)
                if not line:
                    self._kill()
                    raise RuntimeError(
                        f"warmd no response: {'EOF' if line == '' else 'deadline'}")
                resp = json.loads(line)
            except (RuntimeError, OSError, ValueError) as exc:
                self._kill()
                job.stderr = (job.stderr or "") + f"\n[warmd 故障，本作业回退 subprocess: {exc}]"
                return SubprocessExecutor(self.cfg, self.lane).run(job)
        job.stdout = resp.get("stdout") or ""
        job.stderr = (job.stderr or "") + (resp.get("stderr") or "")
        state = resp.get("state", "failed")
        result = kope_mem = None
        error = resp.get("error")
        if state == "done":
            rows = resp.get("result") or []
            if rows:
                result, kope_mem = _map_rows(req["bench"], rows)
            else:
                state, error = "failed", "warmd done but empty rows"
        return state, result, kope_mem, error


# ---------------------------------------------------------------- worker

class Worker(threading.Thread):
    def __init__(self, cfg, store, lane="timing", index=0):
        super().__init__(daemon=True, name="evald-worker" if lane == "timing"
                         else f"evald-worker-{lane}-{index}")
        self.cfg = cfg
        self.store = store
        self.lane = lane
        self.executors = {"subprocess": SubprocessExecutor(cfg, lane=lane),
                          "warmfork": WarmForkExecutor(cfg, lane=lane)}

    def _executor_for(self, request):
        name = request.get("executor")
        if not name and self.lane == "correctness":
            name = self.cfg.get("correctness_executor")  # 车道级默认（如内存紧可降 subprocess）
        name = name or self.cfg.get("executor", "subprocess")
        return self.executors.get(name) or self.executors["subprocess"]

    def run(self):
        while True:
            job = self.store.next_job(self.lane)
            try:
                self.run_job(job)
            except Exception as exc:  # noqa: BLE001 — worker 必须吞掉一切异常
                job.error = f"worker exception: {exc!r}"
                if job.state not in TERMINAL_STATES:
                    job.state = "failed"
                self.store.finish(job)

    def run_job(self, job):
        store, cfg = self.store, self.cfg
        lane = job.request.get("lane", "timing")
        job.started_at = time.time()
        job.state = "running"
        jid = job.job_id
        job_dir = store.job_dir(jid)
        os.makedirs(job_dir, exist_ok=True)
        deployed = False
        # timing 车道：持 /tmp/k3_bench.lock 与手工 SSH 基准互斥（ADR-0002，语义不变）；
        # correctness 车道：整段不碰锁——这是该车道的全部意义（handoff §3-L2）
        lock_fd = None if lane == "correctness" else \
            os.open(cfg["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if job.request.get("files"):
                with open(os.path.join(job_dir, "request.json"), encoding="utf-8") as f:
                    payload = json.load(f)
                try:
                    deploy_files(cfg, job_dir, payload["files"])
                    deployed = True
                except Exception:
                    try:
                        restore_files(job_dir)  # 部署中途失败，按已写 manifest 回滚
                    except Exception as exc:  # noqa: BLE001
                        job.error = f"rollback failed: {exc!r}"
                    raise
            state, result, kope_mem, error = self._executor_for(job.request).run(job)
            job.state, job.result, job.kope_mem, job.error = state, result, kope_mem, error
            if lane == "correctness" and isinstance(job.result, dict):
                # warmup=0/rep=1 下 speedup 无统计意义；lane 标注供下游甄别（correct 仍有效）
                job.result["lane"] = "correctness"
        finally:
            if deployed and job.request.get("restore", True):
                try:
                    restore_files(job_dir)  # 任何路径（含超时/异常）都恢复（ADR-0001）
                except Exception as exc:  # noqa: BLE001
                    job.error = (job.error or "") + f" | restore failed: {exc!r}"
            if lock_fd is not None:
                os.close(lock_fd)
            store.finish(job)


# ---------------------------------------------------------------- /metrics 渲染

def _render_metrics(m):
    """把 metrics_snapshot() 渲染为 Prometheus 文本格式（0.0.4）。

    指标名与 textfile 探针版（work/k1-monitor/eval-probe/evald-probe.sh）逐字
    一致，面板按此契约实现；evald_last_job_info 只输出最新一条序列。
    """
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    lines = [
        "# HELP evald_up evald process reachable (1 whenever this endpoint responds).",
        "# TYPE evald_up gauge",
        "evald_up 1",
        "# HELP evald_queued jobs waiting per lane (authoritative in-memory queue).",
        "# TYPE evald_queued gauge",
    ]
    for lane in ("timing", "correctness"):
        lines.append('evald_queued{lane="%s"} %d' % (lane, m["lanes"][lane]["queued"]))
    lines += [
        "# HELP evald_running jobs currently executing per lane.",
        "# TYPE evald_running gauge",
    ]
    for lane in ("timing", "correctness"):
        lines.append('evald_running{lane="%s"} %d' % (lane, m["lanes"][lane]["running"]))
    lines += [
        "# HELP evald_submissions_total jobs submitted since spool creation"
        " (persists across restarts via spool scan).",
        "# TYPE evald_submissions_total counter",
        "evald_submissions_total %d" % m["submissions"],
        "# HELP evald_jobs_total jobs reached terminal state, per state"
        " (persists across restarts via spool scan).",
        "# TYPE evald_jobs_total counter",
    ]
    for state in ("done", "failed", "timeout"):
        lines.append('evald_jobs_total{state="%s"} %d' % (state, m["jobs"].get(state, 0)))
    lines += [
        "# HELP evald_last_submission_timestamp_seconds"
        " epoch time of the most recent submission (0 if none).",
        "# TYPE evald_last_submission_timestamp_seconds gauge",
        "evald_last_submission_timestamp_seconds %.3f" % m["last_sub_ts"],
        "# HELP evald_last_completion_timestamp_seconds"
        " epoch time of the most recent completion (0 if none).",
        "# TYPE evald_last_completion_timestamp_seconds gauge",
        "evald_last_completion_timestamp_seconds %.3f" % m["last_comp_ts"],
        "# HELP evald_last_job_info most recently completed job"
        " (op/state labels, value always 1).",
        "# TYPE evald_last_job_info gauge",
    ]
    if m["last_job"]:
        op, state = m["last_job"]
        lines.append('evald_last_job_info{op="%s",state="%s"} 1' % (esc(op), esc(state)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "evald/1.0"
    store = None
    cfg = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    # -- helpers
    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("X-K3-Token") == self.cfg["token"]

    def _reply_metrics(self):
        # 免 token（与 /healthz 同级）：监控抓取端无法发自定义头；本端点只暴露
        # 聚合计数、无作业内容，LAN 内可接受
        body = _render_metrics(self.store.metrics_snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            self._reply(200, self._health())
            return
        if u.path == "/metrics":
            self._reply_metrics()
            return
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        if u.path == "/":
            self._reply(200, {"endpoints": ["/healthz", "/metrics", "/jobs",
                                            "/jobs/{id}?wait=N"]})
        elif u.path == "/jobs":
            self._list_jobs(parse_qs(u.query))
        else:
            parts = [p for p in u.path.split("/") if p]
            if len(parts) == 2 and parts[0] == "jobs":
                self._get_job(parts[1], parse_qs(u.query))
            else:
                self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        if urlparse(self.path).path != "/jobs":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > self.cfg["body_max_bytes"]:
            self._reply(413, {"error": f"body must be 1..{self.cfg['body_max_bytes']} bytes"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._reply(400, {"error": f"invalid JSON: {exc}"})
            return
        sanitized, files, err = validate_request(self.cfg, body)
        if err:
            self._reply(400, {"error": err})
            return
        try:
            job = self.store.submit(sanitized, body)
        except QueueFullError:
            self._reply(429, {"error": f"queue full (>{self.cfg['queue_max']})"})
            return
        self._reply(202, {"job_id": job.job_id, "state": "queued",
                          "position": self.store.position(job.job_id)})

    def do_DELETE(self):
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "jobs":
            if self.store.cancel(parts[1]):
                self._reply(200, {"job_id": parts[1], "state": "canceled"})
            else:
                self._reply(409, {"error": "not cancellable (unknown or not queued)"})
        else:
            self._reply(404, {"error": "not found"})

    # -- endpoints
    def _health(self):
        try:
            loadavg = open("/proc/loadavg").read().split()[:3]
        except OSError:
            loadavg = None
        lanes = self.store.lane_stats()
        return {"ok": True,
                "queued": sum(v["queued"] for v in lanes.values()),
                "running": sum(v["running"] for v in lanes.values()),
                "lanes": lanes, "loadavg": loadavg}

    def _list_jobs(self, q):
        try:
            limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
        except ValueError:
            limit = 50
        since = q.get("since", [None])[0]
        jobs = self.store.history(limit, since)
        if jobs is None:
            self._reply(404, {"error": f"unknown since id: {since}"})
            return
        self._reply(200, {"jobs": [j.to_json(full=False) for j in jobs]})

    def _get_job(self, job_id, q):
        job = self.store.get(job_id)
        if job is None:
            self._reply(404, {"error": "unknown job_id"})
            return
        wait = 0
        if q.get("wait"):
            try:
                wait = max(0, min(self.cfg["longpoll_max_s"], int(q["wait"][0])))
            except ValueError:
                pass
        if wait > 0 and job.state not in TERMINAL_STATES:
            self.store.event_for(job_id).wait(wait)
            job = self.store.get(job_id) or job
        out = job.to_json(full=True)
        if job.state == "queued":
            out["queue_position"] = self.store.position(job_id)
        self._reply(200, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--gen-token", action="store_true")
    args = ap.parse_args()
    if args.gen_token:
        import secrets
        print(secrets.token_hex(16))
        return
    cfg = load_config(args.config)
    if args.selftest:
        sys.exit(selftest(cfg))

    store = Store(cfg)
    Worker(cfg, store).start()  # 计时 worker：锁/串行/warmfork 语义一字不动
    n_correctness = max(0, int(cfg.get("correctness_workers", 3)))
    for i in range(n_correctness):
        Worker(cfg, store, lane="correctness", index=i).start()
    Handler.store = store
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True

    def _shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print(f"[evald] listening on {cfg['host']}:{cfg['port']}, data_dir={cfg['data_dir']}, "
          f"workers: 1 timing + {n_correctness} correctness (cpus={cfg.get('correctness_cpus')})",
          flush=True)
    httpd.serve_forever()


# ---------------------------------------------------------------- 自检

def selftest(cfg):
    """不监听端口、不碰真实 roots：临时目录里验证白名单/部署-恢复/队列/超时。"""
    failures = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("[1] uuid7")
    u = uuid7()
    check("uuid7 格式", bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", u)), u)
    time.sleep(0.002)
    check("uuid7 时间有序", uuid7() > u)

    print("[2] 白名单")
    ok_paths = ["flag_gems/ops/add.py", "flag_gems/runtime/backend/_spacemit/ops/argmax.py",
                "flag_gems/runtime/backend/_spacemit/tune_configs.yaml",
                "scripts/bench_op.py", "scripts/bench_gemm.py"]
    bad_paths = ["flag_gems/__init__.py", "flag_gems/ops/../core.py", "scripts/run_env.sh",
                 "/etc/passwd", "flag_gems/ops/../../x.py", "scripts/bench_op.py/x",
                 "flag_gems/triton/x.py", "", "flag_gems/ops"]
    tmp = tempfile.mkdtemp(prefix="evald_selftest_")
    cfg = dict(cfg)
    cfg["flag_gems_root"] = os.path.join(tmp, "fg")
    cfg["scripts_root"] = os.path.join(tmp, "scripts")
    os.makedirs(os.path.join(cfg["flag_gems_root"], "ops"), exist_ok=True)
    os.makedirs(os.path.join(cfg["scripts_root"]), exist_ok=True)
    check("白名单接受", all(resolve_path(cfg, p) for p in ok_paths))
    check("白名单拒绝", all(resolve_path(cfg, p) is None for p in bad_paths))

    print("[3] 部署-恢复（含硬链接断链）")
    ops_dir = os.path.join(cfg["flag_gems_root"], "ops")
    orig = os.path.join(ops_dir, "add.py")
    with open(orig, "w") as f:
        f.write("ORIG")
    link = os.path.join(ops_dir, "add_link.py")
    os.link(orig, link)  # 模拟 site-packages nlink=2
    jd = os.path.join(tmp, "job1")
    os.makedirs(jd)
    manifest = deploy_files(cfg, jd, [
        {"path": "flag_gems/ops/add.py", "content": "CANDIDATE"},
        {"path": "flag_gems/ops/new_dir/new.py", "content": "NEW"},
    ])
    check("候选已写入", open(orig).read() == "CANDIDATE")
    check("硬链接已断（原文件未被改写）", open(link).read() == "ORIG")
    check("manifest 记录", manifest[0]["existed"] is True and manifest[1]["existed"] is False)
    restore_files(jd)
    check("恢复原内容", open(orig).read() == "ORIG")
    check("新文件已回收", not os.path.exists(os.path.join(ops_dir, "new_dir", "new.py")))
    check("空目录已清理", not os.path.exists(os.path.join(ops_dir, "new_dir")))

    print("[4] 队列 + 假 bench")
    cfg["data_dir"] = os.path.join(tmp, "data")
    cfg["executor"] = "subprocess"  # 本节测子进程执行器路径（默认已是 warmfork）
    os.makedirs(cfg["data_dir"], exist_ok=True)
    store = Store(cfg)
    echo_row = json.dumps({"op": "fake", "torch_ms": 2.0, "gems_ms": 1.0,
                           "speedup": 2.0, "correct": True, "max_abs_err": 0.0})
    cfg["bench_command_override"] = f"echo '{echo_row}'"  # selftest 专用钩子
    worker = Worker(cfg, store)
    req1 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
            "rep": 1, "bench": "op", "restore": True, "timeout_min": 1, "files": []}
    job = store.submit(req1, req1)
    worker.run_job(store.get(job.job_id))
    j = store.get(job.job_id)
    check("假 bench done", j.state == "done", j.state)
    check("结果解析+speedup", j.result and abs(j.result["speedup"] - 2.0) < 1e-9)
    check("kope_mem 兼容", j.kope_mem == {"speedup": 2.0, "pass_rate": 1.0})
    check("jsonl 已落盘", "jobs.jsonl" in os.listdir(cfg["data_dir"]))

    print("[5] 超时路径")
    cfg2 = dict(cfg)
    cfg2["data_dir"] = os.path.join(tmp, "data2")
    os.makedirs(cfg2["data_dir"], exist_ok=True)
    cfg2["bench_command_override"] = "sleep 30"
    store2 = Store(cfg2)
    worker2 = Worker(cfg2, store2)
    req2 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
            "rep": 1, "bench": "op", "restore": True, "timeout_min": 0.1, "files": []}
    job2 = store2.submit(req2, req2)
    t0 = time.time()
    worker2.run_job(store2.get(job2.job_id))
    j2 = store2.get(job2.job_id)
    check("超时标记", j2.state == "timeout", j2.state)
    check("超时及时返回", time.time() - t0 < 15)

    if hasattr(os, "fork"):
        print("[6] warmd 协议（WARMD_FAKE，不依赖 torch）")
        warmd_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warmd.py")
        check("warmd.py 存在", os.path.exists(warmd_src))
        stub_bin = os.path.join(tmp, "stubbin")
        os.makedirs(stub_bin, exist_ok=True)
        try:
            os.symlink(sys.executable, os.path.join(stub_bin, "python"))
        except (OSError, NotImplementedError):
            shutil.copy(sys.executable, os.path.join(stub_bin, "python"))
        stub_env = os.path.join(tmp, "run_env_stub.sh")
        with open(stub_env, "w") as f:
            f.write("#!/bin/bash\nexport PATH=\"%s:$PATH\"\n" % stub_bin)
        cfg3 = dict(cfg)
        cfg3["data_dir"] = os.path.join(tmp, "data3")
        os.makedirs(cfg3["data_dir"], exist_ok=True)
        cfg3["run_env"] = stub_env
        cfg3["warmd_path"] = warmd_src
        cfg3["warmd_env"] = {"WARMD_FAKE": "1"}
        cfg3["executor"] = "warmfork"
        store3 = Store(cfg3)
        worker3 = Worker(cfg3, store3)
        req3 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
                "rep": 1, "bench": "op", "restore": True, "timeout_min": 1,
                "executor": "warmfork", "files": []}
        job3 = store3.submit(req3, req3)
        worker3.run_job(store3.get(job3.job_id))
        j3 = store3.get(job3.job_id)
        check("warmfork 假作业 done", j3.state == "done", j3.state)
        check("warmfork speedup=2.0", (j3.result or {}).get("speedup") == 2.0)
        check("warmfork kope_mem", j3.kope_mem == {"speedup": 2.0, "pass_rate": 1.0})

        print("[7] warmd 子进程超时")
        cfg4 = dict(cfg3)
        cfg4["data_dir"] = os.path.join(tmp, "data4")
        os.makedirs(cfg4["data_dir"], exist_ok=True)
        cfg4["warmd_env"] = {"WARMD_FAKE": "1", "WARMD_FAKE_SLEEP": "30"}
        store4 = Store(cfg4)
        worker4 = Worker(cfg4, store4)
        req4 = dict(req3, timeout_min=0.1)
        job4 = store4.submit(req4, req4)
        t0 = time.time()
        worker4.run_job(store4.get(job4.job_id))
        j4 = store4.get(job4.job_id)
        check("warmd 超时标记", j4.state == "timeout", j4.state)
        check("warmd 超时及时返回", time.time() - t0 < 15)
    else:
        print("[6]/[7] warmd 协议测试需要 POSIX fork，本平台跳过")

    print("[8] op 名校验（RE_OP）")
    ok_ops = ["add", "_reshape_alias",
              "functional_sym_constrain_range_for_size",
              "special_scaled_modified_bessel_k1",
              "special_shifted_chebyshev_polynomial_v", "a" * 48]
    bad_ops = ["Add", "1add", "add.py", "a-b", "a/b", "a b", "", "a" * 49]
    check("op 名放宽后接受（前导下划线/长名）", all(RE_OP.match(p) for p in ok_ops))
    check("op 名拒绝非法字符/超长", all(not RE_OP.match(p) for p in bad_ops))

    print("[9] lane 字段校验与 correctness 注入")
    good = {"op": "add", "shape": "64,512,512", "dtype": "float32",
            "warmup": 10, "rep": 50, "bench": "op", "restore": True}
    s0, _, e0 = validate_request(cfg, dict(good))
    check("无 lane 请求不带 lane 键（向后逐字节兼容）", e0 is None and "lane" not in s0)
    check("无 lane 请求 warmup/rep 原样", s0["warmup"] == 10 and s0["rep"] == 50)
    s1, _, e1 = validate_request(cfg, dict(good, lane="correctness"))
    check("correctness 车道接受", e1 is None and s1["lane"] == "correctness")
    check("correctness 覆盖 warmup=0/rep=1", s1["warmup"] == 0 and s1["rep"] == 1)
    check("correctness 注记原值", s1["requested_warmup"] == 10 and s1["requested_rep"] == 50)
    s2, _, e2 = validate_request(cfg, dict(good, lane="timing"))
    check("显式 timing 车道接受", e2 is None and s2["lane"] == "timing")
    _, _, e3 = validate_request(cfg, dict(good, lane="banana"))
    check("非法 lane 拒绝（HTTP 400 同源）", e3 is not None, e3)
    cmd_t = SubprocessExecutor(cfg).build_command(s0)
    check("timing 命令逐字节不变", cmd_t ==
          (f"source {cfg['run_env']} && python {cfg['scripts_root']}/bench_op.py "
           f"--op add --shape 64,512,512 --dtype float32 --warmup 10 --rep 50"), cmd_t)
    cmd_c = SubprocessExecutor(cfg, lane="correctness").build_command(s1)
    check("correctness 命令注入 K3_LANE/OMP/--warmup 0 --rep 1",
          "K3_LANE=correctness" in cmd_c and "OMP_NUM_THREADS=1" in cmd_c
          and "--warmup 0 --rep 1" in cmd_c, cmd_c)
    if shutil.which("taskset"):
        check("correctness 进程 taskset 核亲和（Popen argv）",
              SubprocessExecutor(cfg, lane="correctness")._taskset_argv()
              == ["taskset", "-c", str(cfg["correctness_cpus"])])
        check("timing 车道不加 taskset（语义不变）",
              SubprocessExecutor(cfg)._taskset_argv() == [])
    else:
        print("  （本平台无 taskset，核亲和断言由板上 selftest 覆盖）")

    print("[10] 车道队列路由与共享 queue_max")
    cfg_r = dict(cfg)
    cfg_r["data_dir"] = os.path.join(tmp, "dataR")
    os.makedirs(cfg_r["data_dir"], exist_ok=True)
    store_r = Store(cfg_r)
    jt = store_r.submit(dict(s0), dict(s0))
    jc = store_r.submit(dict(s1), dict(s1))
    check("timing 作业入 timing 队列", jt.job_id in store_r.queues["timing"])
    check("correctness 作业入 correctness 队列", jc.job_id in store_r.queues["correctness"])
    stats = store_r.lane_stats()
    check("lane_stats 每车道计数", stats == {"timing": {"queued": 1, "running": 0},
                                            "correctness": {"queued": 1, "running": 0}}, stats)
    check("position 按车道独立", store_r.position(jt.job_id) == 1
          and store_r.position(jc.job_id) == 1)
    check("next_job 车道分派", store_r.next_job("correctness").job_id == jc.job_id
          and store_r.next_job("timing").job_id == jt.job_id)
    jt2 = store_r.submit(dict(s0), dict(s0))
    check("cancel 从所属车道队列移除", store_r.cancel(jt2.job_id)
          and len(store_r.queues["timing"]) == 0)
    cfg_q = dict(cfg_r)
    cfg_q["data_dir"] = os.path.join(tmp, "dataQ")
    os.makedirs(cfg_q["data_dir"], exist_ok=True)
    cfg_q["queue_max"] = 2
    store_q = Store(cfg_q)
    store_q.submit(dict(s0), dict(s0))
    store_q.submit(dict(s1), dict(s1))
    try:
        store_q.submit(dict(s1), dict(s1))
        full = False
    except QueueFullError:
        full = True
    check("queue_max 跨车道共享", full)

    print("[11] 锁语义：correctness 不获取 bench 锁（flock 探针）")
    cfg_l = dict(cfg)
    cfg_l["data_dir"] = os.path.join(tmp, "dataL")
    os.makedirs(cfg_l["data_dir"], exist_ok=True)
    cfg_l["executor"] = "subprocess"
    cfg_l["lock_file"] = os.path.join(tmp, "probe_bench.lock")
    cfg_l["bench_command_override"] = f"echo '{echo_row}'"
    store_l = Store(cfg_l)
    probe = os.open(cfg_l["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
    real_lock = True  # 本地 fcntl shim（无真实互斥）时降级为仅测 correctness 不碰锁
    try:
        fcntl.flock(probe, fcntl.LOCK_EX)  # 人为持锁：模拟手工 SSH 基准进行中
        probe2 = os.open(cfg_l["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(probe2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe2, fcntl.LOCK_UN)
            real_lock = False  # 第二 fd 也能拿锁 ⇒ 非真实互斥语义（shim 降级）
        except OSError:
            pass
        os.close(probe2)
    except (OSError, AttributeError):
        real_lock = False
    jc2 = store_l.submit(dict(s1), dict(s1))
    Worker(cfg_l, store_l, lane="correctness").run_job(store_l.get(jc2.job_id))
    j = store_l.get(jc2.job_id)
    check("持锁状态下 correctness 作业照常完成", j.state == "done", (j.state, j.error))
    check("correctness 结果带 lane 标注", (j.result or {}).get("lane") == "correctness")
    if real_lock:
        t_worker = Worker(cfg_l, store_l)  # timing worker：应被探针锁挡住
        jt3 = store_l.submit(dict(s0), dict(s0))
        th = threading.Thread(target=t_worker.run_job,
                              args=(store_l.get(jt3.job_id),), daemon=True)
        th.start()
        time.sleep(1.5)
        blocked = store_l.get(jt3.job_id).finished_at is None
        fcntl.flock(probe, fcntl.LOCK_UN)  # 释放锁
        th.join(timeout=30)
        check("timing 作业持锁期间排队、释放后完成",
              blocked and store_l.get(jt3.job_id).state == "done")
    else:
        print("  （本平台 flock 无真实互斥语义，timing 排队断言由板上 selftest 覆盖）")
    os.close(probe)

    print("[12] correctness 3 worker 并行互不阻塞（真实 Worker 线程）")
    cfg_p = dict(cfg_l)
    cfg_p["data_dir"] = os.path.join(tmp, "dataP")
    os.makedirs(cfg_p["data_dir"], exist_ok=True)
    cfg_p["bench_command_override"] = (
        f"sleep 2; echo '{echo_row}'; echo \"K3_LANE=$K3_LANE\"; "
        "grep Cpus_allowed_list /proc/self/status || true")
    store_p = Store(cfg_p)
    pool = [Worker(cfg_p, store_p)]  # timing worker 空转占位（锁路径无人为持锁）
    pool += [Worker(cfg_p, store_p, lane="correctness", index=i) for i in range(3)]
    for w in pool:
        w.start()
    c_ids = [store_p.submit(dict(s1), dict(s1)) for _ in range(3)]
    tmix = store_p.submit(dict(s0), dict(s0))  # 混提 1 个 timing 作业
    t0 = time.time()
    all_ids = c_ids + [tmix]
    while time.time() - t0 < 60 and not all(
            store_p.get(x.job_id).state in TERMINAL_STATES for x in all_ids):
        time.sleep(0.1)
    wall = time.time() - t0
    check("3 correctness + 1 timing 混提全部 done",
          all(store_p.get(x.job_id).state == "done" for x in all_ids),
          [store_p.get(x.job_id).state for x in all_ids])
    check("3 correctness 并行墙钟远小于串行（互不阻塞）", wall < 5.5, f"wall={wall:.1f}s")
    check("K3_LANE 注入 correctness bench 进程",
          all("K3_LANE=correctness" in store_p.get(x.job_id).stdout for x in c_ids))
    check("timing bench 进程不被注入 K3_LANE",
          "K3_LANE=correctness" not in store_p.get(tmix.job_id).stdout)
    if shutil.which("taskset") and os.path.exists("/proc/self/status"):
        cpus = str(cfg_p["correctness_cpus"])
        check("correctness bench 进程 affinity=车道核（end-to-end）",
              all(f"Cpus_allowed_list:\t{cpus}" in store_p.get(x.job_id).stdout
                  for x in c_ids))
        check("timing bench 进程 affinity 不受车道影响",
              f"Cpus_allowed_list:\t{cpus}" not in store_p.get(tmix.job_id).stdout)

    print("[13] /metrics 计数器（spool 持久语义）")
    cfg_m = dict(cfg)
    cfg_m["data_dir"] = os.path.join(tmp, "dataM")
    os.makedirs(cfg_m["data_dir"], exist_ok=True)
    store_m = Store(cfg_m)
    snap0 = store_m.metrics_snapshot()
    check("空 spool 初值", snap0["submissions"] == 0
          and snap0["jobs"] == {"done": 0, "failed": 0, "timeout": 0}, snap0)
    jm1 = store_m.submit(dict(s0), dict(s0))
    jm2 = store_m.submit(dict(s1), dict(s1))
    store_m.get(jm1.job_id).state = "done"
    store_m.finish(store_m.get(jm1.job_id))
    time.sleep(0.01)  # 秒级 mtime 粒度的文件系统下保证先后可辨
    store_m.get(jm2.job_id).state = "failed"
    store_m.finish(store_m.get(jm2.job_id))
    snap = store_m.metrics_snapshot()
    check("submissions 累计 +2", snap["submissions"] == 2, snap)
    check("jobs_total done/failed 各 +1",
          snap["jobs"] == {"done": 1, "failed": 1, "timeout": 0}, snap["jobs"])
    check("last_job_info 指向最新完成", snap["last_job"] == ("add", "failed"),
          snap["last_job"])
    check("last_submission ≤ last_completion",
          0 < snap["last_sub_ts"] <= snap["last_comp_ts"])
    text = _render_metrics(snap)
    check("文本契约逐字（计数/info 行）",
          "evald_submissions_total 2" in text
          and 'evald_jobs_total{state="done"} 1' in text
          and 'evald_jobs_total{state="failed"} 1' in text
          and 'evald_last_job_info{op="add",state="failed"} 1' in text
          and text.startswith("# HELP evald_up"), text)
    store_m2 = Store(cfg_m)  # 模拟重启：同一 spool 重新扫描
    snap2 = store_m2.metrics_snapshot()
    check("重启后计数无缝衔接",
          snap2["submissions"] == 2
          and snap2["jobs"] == {"done": 1, "failed": 1, "timeout": 0}
          and snap2["last_job"] == ("add", "failed")
          and abs(snap2["last_comp_ts"] - snap["last_comp_ts"]) < 1)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nselftest: {'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    main()
