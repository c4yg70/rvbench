#!/usr/bin/env python3
"""evald — SpacemiT K3 (RISC-V) 算子代码评估服务器

本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业（UUIDv7），
单 worker 串行执行并持有 /tmp/k3_bench.lock（与手工 SSH 基准互斥），
每个作业自包含部署-恢复 site-packages 候选文件（防异步队列下的代码残留）。

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

RE_OP = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RE_SHAPE = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
GEMM_OPS = {"mm", "addmm", "bmm"}
OP_DTYPES = {"float32", "float16"}


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
    cfg.setdefault("queue_max", 32)
    cfg.setdefault("timeout_default_min", 15)
    cfg.setdefault("timeout_max_min", 30)
    cfg.setdefault("body_max_bytes", 8 * 1024 * 1024)
    cfg.setdefault("history_mem", 1000)
    cfg.setdefault("longpoll_max_s", 60)
    cfg.setdefault("executor", "warmfork")  # M3 起默认预热执行器；可回退 "subprocess"
    cfg.setdefault("warmd_path", os.path.join(cfg["data_dir"], "warmd.py"))
    cfg.setdefault("warmd_env", {})
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
    if bench == "gemm":
        sanitized["batch"] = batch
    return sanitized, files, None


# ---------------------------------------------------------------- 作业存储

class Job:
    def __init__(self, job_id, seq, request):
        self.job_id = job_id
        self.seq = seq
        self.state = "queued"
        self.request = request
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
        self.queue = deque()          # 排队中 job_id
        self.events = {}
        self.seq = 0
        self._load_history()
        self._sweep_stale()

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

    def submit(self, sanitized, payload):
        with self.cond:
            if len(self.queue) >= self.cfg["queue_max"]:
                raise QueueFullError()
            self.seq += 1
            job = Job(uuid7(), self.seq, sanitized)
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            # 完整 payload（含代码内容）先落作业目录再入队，避免与 worker 竞态
            d = self.job_dir(job.job_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            self.queue.append(job.job_id)
            self.cond.notify()
            return job

    def next_job(self):
        with self.cond:
            while not self.queue:
                self.cond.wait()
            return self.jobs[self.queue.popleft()]

    def position(self, job_id):
        with self.lock:
            try:
                return list(self.queue).index(job_id) + 1
            except ValueError:
                return None

    def cancel(self, job_id):
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None or job.state != "queued":
                return False
            self.queue.remove(job_id)
            job.state = "canceled"
            job.finished_at = time.time()
            self._persist(job)
            self._fire_event(job_id)
            return True

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
    def __init__(self, cfg):
        self.cfg = cfg

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
        return f"source {self.cfg['run_env']} && {cmd}"

    def run(self, job):
        """返回 (state, result, kope_mem, error)。超时杀进程组。"""
        req = job.request
        timeout_s = req["timeout_min"] * 60
        bash_cmd = self.cfg.get("bench_command_override") or self.build_command(req)
        proc = subprocess.Popen(
            ["bash", "-c", bash_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
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

    def __init__(self, cfg):
        self.cfg = cfg
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
        cmd = f"source {self.cfg['run_env']} && exec python {self.cfg['warmd_path']}"
        self.proc = subprocess.Popen(
            ["bash", "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
                return SubprocessExecutor(self.cfg).run(job)
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
    def __init__(self, cfg, store):
        super().__init__(daemon=True, name="evald-worker")
        self.cfg = cfg
        self.store = store
        self.executors = {"subprocess": SubprocessExecutor(cfg),
                          "warmfork": WarmForkExecutor(cfg)}

    def _executor_for(self, request):
        name = request.get("executor") or self.cfg.get("executor", "subprocess")
        return self.executors.get(name) or self.executors["subprocess"]

    def run(self):
        while True:
            job = self.store.next_job()
            try:
                self.run_job(job)
            except Exception as exc:  # noqa: BLE001 — worker 必须吞掉一切异常
                job.error = f"worker exception: {exc!r}"
                if job.state not in TERMINAL_STATES:
                    job.state = "failed"
                self.store.finish(job)

    def run_job(self, job):
        store, cfg = self.store, self.cfg
        job.started_at = time.time()
        job.state = "running"
        jid = job.job_id
        job_dir = store.job_dir(jid)
        os.makedirs(job_dir, exist_ok=True)
        deployed = False
        lock_fd = os.open(cfg["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # 计时互斥：与手工 SSH 基准共用（ADR-0002）
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
        finally:
            if deployed and job.request.get("restore", True):
                try:
                    restore_files(job_dir)  # 任何路径（含超时/异常）都恢复（ADR-0001）
                except Exception as exc:  # noqa: BLE001
                    job.error = (job.error or "") + f" | restore failed: {exc!r}"
            os.close(lock_fd)
            store.finish(job)


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

    # -- routing
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            self._reply(200, self._health())
            return
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        if u.path == "/":
            self._reply(200, {"endpoints": ["/healthz", "/jobs", "/jobs/{id}?wait=N"]})
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
        with self.store.lock:
            queued = len(self.store.queue)
            running = sum(1 for j in self.store.jobs.values() if j.state == "running")
        return {"ok": True, "queued": queued, "running": running, "loadavg": loadavg}

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
    Worker(cfg, store).start()
    Handler.store = store
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True

    def _shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print(f"[evald] listening on {cfg['host']}:{cfg['port']}, data_dir={cfg['data_dir']}", flush=True)
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

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nselftest: {'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    main()
