#!/usr/bin/env python3
"""warmd — evald 的常驻预热执行守护（venv python 运行，M3 WarmForkExecutor 后端）

父进程一次性 import torch+triton（实测 ~3.9s，最贵且与候选代码无关）；
每个作业 fork 子进程，子进程全新 import flag_gems 与 bench 脚本——
候选代码永远从磁盘已部署文件加载，从构造上杜绝模块残留（ADR-0001/0004）。

**禁止在父进程预热 triton 编译/launcher**（2026-09-09 实测负向结果，详见
`results/panel/job-cost-anatomy-20260909.md`）：首次 kernel 编译会 dlopen+init
756MB 的 libtriton.so（X100 ~8s），但此后父进程带 9 线程且持有 spine native
launcher/编译管线状态，fork 出的子进程在自己第一次 launch 上**必定死锁**
（卡在 spine_triton/driver.py:485 `CPULauncher.__call__`）；只编译不 launch
（`JITFunction.warmup`）同样毒化子进程。那 ~8s（A100 约 16s）是每作业进程
必付的固定成本，只能靠“每进程多干活”或“多车道并行”摊薄。

协议（stdin/stdout，每作业各一行 JSON）：
  请求  {"id": <job_id>, "timeout_s": <float>,
         "req": {bench, op, shape, dtype, warmup, rep, batch, skip_torch, seed}}
  响应  {"id", "state": "done|failed|timeout", "rc", "result", "stdout", "stderr", "error"}
  启动就绪时先发 {"ready": true, "fake": bool}
自检/单测：WARMD_FAKE=1 跳过 torch（配合 WARMD_FAKE_SLEEP/WARMD_FAKE_ECHO 模拟 bench）。
输出写后自检（grp-DIAG 模式 A）：WARMD_OUTPUT_SELFCHECK=1 时子进程对 stdout 全量字节
做 sha256、收尾追加一行 {"warmd_selfcheck": {bytes, sha256}} trailer（不计入摘要），
父进程复算字节前缀核对——不一致/trailer 缺失 → 作业显式 failed。开关链见 evald
config["warmd_output_selfcheck"]（默认 False，行为与改前逐字节一致）。
"""

import hashlib
import json
import os
import signal
import sys
import threading
import time


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg):
    sys.stderr.write(f"[warmd] {msg}\n")
    sys.stderr.flush()


def _parse_json_lines(text, cap=16):
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
            if len(rows) >= cap:
                break
    rows.reverse()
    return rows


# ---------------------------------------------------------------- 输出写后自检
# （grp-DIAG 模式 A 建议）：timing 车道曾出现 stdout JSON 行数值异常、疑似部分写/
# 脏内存的作业。自检给 warmd 子进程的 stdout 全量字节做 sha256，收尾写一行 trailer
#（trailer 本身不计入摘要），父进程按字节前缀复算核对：不一致或 trailer 缺失 →
# 作业显式 failed（行能解析 ≠ 行没被改）。文本层计数：只覆盖经 sys.stdout 的写
#（bench 脚本全走 print，够用）；直接写 fd 1 的路径不在 coverage 内。
_SELFCHECK_ENV = "WARMD_OUTPUT_SELFCHECK"
_SELFCHECK_KEY = "warmd_selfcheck"


def _selfcheck_enabled():
    return os.environ.get(_SELFCHECK_ENV) == "1"


class _SelfCheckStream:
    """包一层 sys.stdout：写穿透 + 累计字节/sha256；trailer 经 raw 写出、不进摘要。"""

    def __init__(self, raw):
        self._raw = raw
        self._n = 0
        self._h = hashlib.sha256()

    def write(self, s):
        b = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
        self._n += len(b)
        self._h.update(b)
        return self._raw.write(s)

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def flush(self):
        return self._raw.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._raw, "encoding", "utf-8")

    def emit_trailer(self):
        """收尾：把 trailer 写到真 stdout（不经过计数层，避免自引用）。"""
        trailer = json.dumps({_SELFCHECK_KEY: {"bytes": self._n,
                                               "sha256": self._h.hexdigest()}})
        self._raw.write(trailer + "\n")
        self._raw.flush()


def _selfcheck_emit(sc):
    """尽力补 trailer；失败静默——trailer 缺失由父进程按完整性失败处理，不在
    子进程退出路径上二次抛异常。"""
    if sc is None:
        return
    try:
        sc.emit_trailer()
    except Exception:  # noqa: BLE001 — 退出路径上的兜底，异常吞掉
        pass


def _selfcheck_verify(out_b):
    """父进程侧核对：剥 trailer、复算字节前缀 sha256/长度。

    返回 (剥 trailer 后的字节, error)；error 非 None 即完整性失败（结果行照常
    解析带回，但 state 由调用方压成 failed，供排障看坏在哪个字节）。
    """
    stripped = out_b.rstrip(b"\n")
    nl = stripped.rfind(b"\n")
    line_b = stripped[nl + 1:]
    try:
        trailer = json.loads(line_b.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return out_b, "selfcheck: 末行不是 JSON，trailer 缺失"
    if not (isinstance(trailer, dict) and isinstance(trailer.get(_SELFCHECK_KEY), dict)):
        return out_b, "selfcheck: trailer 缺失"
    meta = trailer[_SELFCHECK_KEY]
    prefix = stripped[:nl + 1] if nl >= 0 else b""
    want = hashlib.sha256(prefix).hexdigest()
    if meta.get("bytes") != len(prefix) or meta.get("sha256") != want:
        return out_b, (f"selfcheck: 输出完整性不一致（child 报 {meta.get('bytes')}B/"
                       f"{str(meta.get('sha256'))[:12]}…，parent 复算 {len(prefix)}B/"
                       f"{want[:12]}…）")
    return prefix, None


def _run_bench(breq, fake):
    """在 fork 子进程里执行；父进程保证此刻 sys.modules 里没有 flag_gems/bench_*。"""
    if fake:
        time.sleep(float(os.environ.get("WARMD_FAKE_SLEEP", "0")))
        line = os.environ.get(
            "WARMD_FAKE_ECHO",
            json.dumps({"op": "fake", "torch_ms": 2.0, "gems_ms": 1.0,
                        "speedup": 2.0, "correct": True, "max_abs_err": 0.0}))
        sys.stdout.write(line + "\n")
        return
    scripts = os.environ["WARMD_SCRIPTS_ROOT"]
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    shapes = breq["shape"]
    if not isinstance(shapes, list):
        shapes = [shapes]
    if breq["bench"] == "gemm":
        import bench_gemm as mod
        sys.argv = ["bench_gemm.py", "--op", breq["op"], "--dtype", breq["dtype"],
                    "--shapes", breq["shape"]]
        if breq["op"] == "bmm":
            sys.argv += ["--batch", str(breq.get("batch", 8))]
        _call_main(mod)
        return
    # 多 shape 在**同一个子进程**里循环：torch/flag_gems/bench 只 import 一次，
    # 摊掉最贵的那段固定成本（实测 import torch 4.5s + flag_gems 11.6s）
    import bench_op as mod
    for shape in shapes:
        sys.argv = ["bench_op.py", "--op", breq["op"], "--shape", shape,
                    "--dtype", breq["dtype"],
                    "--warmup", str(breq.get("warmup", 25)),
                    "--rep", str(breq.get("rep", 100))]
        # torch 臂外提/输入播种（handoff-torch-arm §3.2.2）：与 subprocess 降级
        # 路径（evald SubprocessExecutor.build_command）保持两条执行器路径等价；
        # seed 在循环内每 shape 重播一次（bench_op.main 内部 manual_seed），
        # 同请求多 shape 取同一 seed 的确定性采样
        if breq.get("skip_torch"):
            sys.argv += ["--skip-torch"]
        if breq.get("seed") is not None:
            sys.argv += ["--seed", str(breq["seed"])]
        _call_main(mod)
        sys.stdout.flush()


def _call_main(mod):
    """调用 bench 脚本的 main()；容忍正常退出，把真失败抛给外层。

    main() 的正常路径可能 sys.exit(0)（argparse 分支等），循环里必须吞掉它
    才能继续跑下一个 shape；非 0 退出码是真失败，原样抛出。
    """
    try:
        mod.main()
    except SystemExit as e:
        if e.code not in (0, None):
            raise


def handle(req):
    rid = req.get("id")
    timeout_s = float(req.get("timeout_s", 600))
    breq = req["req"]
    # 多 shape：一行 JSON 一个 case，cap 必须 ≥ case 数，否则结果被截断
    _shapes = breq.get("shape")
    n_expected = len(_shapes) if isinstance(_shapes, list) else 1
    cap = max(16, n_expected + 2)
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # ---- 子进程：一次性执行，异常也无所谓，跑完即弃 ----
        os.close(out_r)
        os.close(err_r)
        try:
            os.close(0)  # 不消费守护的请求流
        except OSError:
            pass
        os.dup2(out_w, 1)
        os.dup2(err_w, 2)
        os.close(out_w)
        os.close(err_w)
        # 输出写后自检（默认关）：包住 sys.stdout 计数/摘要，trailer 不进摘要
        sc = _SelfCheckStream(sys.stdout) if _selfcheck_enabled() else None
        if sc is not None:
            sys.stdout = sc
        try:
            _run_bench(breq, os.environ.get("WARMD_FAKE") == "1")
            _selfcheck_emit(sc)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        except SystemExit as e:  # bench 脚本 argparse 错误等
            _selfcheck_emit(sc)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(e.code if isinstance(e.code, int) else 1)
        except BaseException as exc:  # noqa: BLE001
            # DIAG 模式 B 修复：原单行 f"warmd child error: {exc!r}" 吞掉调用链，
            # 板上排障拿不到栈——改打完整 traceback（与 diag_b_warmfork.py 探针
            # 同语义），再尽力补自检 trailer 后退出
            import traceback
            traceback.print_exc()
            _selfcheck_emit(sc)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)
    # ---- 父进程：双管道读线程（防 64KB 管道写满死锁）+ 限期 waitpid ----
    os.close(out_w)
    os.close(err_w)
    bufs = {}

    def _reader(fd, key):
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        bufs[key] = b"".join(chunks)
        os.close(fd)

    ths = [threading.Thread(target=_reader, args=(out_r, "out"), daemon=True),
           threading.Thread(target=_reader, args=(err_r, "err"), daemon=True)]
    for t in ths:
        t.start()
    deadline = time.time() + timeout_s
    timed_out = False
    status = 0
    while True:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid:
            break
        if time.time() > deadline:
            timed_out = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            break
        time.sleep(0.05)
    for t in ths:
        t.join(timeout=5)
    out_b = bufs.get("out", b"")
    err = bufs.get("err", b"").decode("utf-8", "replace")
    # 输出写后自检（父进程侧）：先核对完整性并剥掉 trailer，再按既有语义判态；
    # 完整性失败压过 done（行能解析≠行没被改），failed/timeout 只追加注记
    integ_err = None
    if _selfcheck_enabled():
        out_b, integ_err = _selfcheck_verify(out_b)
    out = out_b.decode("utf-8", "replace")
    rc = os.waitstatus_to_exitcode(status) if not timed_out else -9
    if timed_out:
        state, result, error = "timeout", None, f"exceeded {timeout_s:.0f}s"
    elif rc != 0:
        # 多 shape 下最后一个 case 退出码非 0 不代表前几个无效：结果齐就用
        rows = _parse_json_lines(out, cap=cap)
        if rows and len(rows) >= n_expected:
            state, result, error = "done", rows, f"bench rc={rc}（case 结果已完整）"
        elif rows:
            state, result, error = "failed", rows, (
                f"bench rc={rc}，仅 {len(rows)}/{n_expected} 个 case 出结果")
        else:
            state, result, error = "failed", None, f"bench rc={rc}"
    else:
        rows = _parse_json_lines(out, cap=cap)
        if rows:
            state, result, error = "done", rows, None
        else:
            state, result, error = "failed", None, "no JSON line in stdout"
    if integ_err:
        if state == "done":
            state, error = "failed", integ_err
        else:
            error = f"{error}; {integ_err}" if error else integ_err
    # P1-1 失败可诊断：rc<0（POSIX wait 语义 = 被信号 -rc 杀死）时把信号名一并
    # 写进响应（SIGSEGV/SIGABRT 直接可读）；timeout 的 -9 是本进程合成值，不标
    resp = {"id": rid, "state": state, "rc": rc, "result": result,
            "stdout": out, "stderr": err, "error": error}
    if not timed_out and isinstance(rc, int) and rc < 0:
        try:
            resp["signal_name"] = signal.Signals(-rc).name
        except ValueError:
            resp["signal_name"] = "SIG%d" % (-rc)
    _send(resp)


def _pdeathsig():
    # W0-2 (2026-09-15): evald 退出后 warmd 必须随葬——0911 事故根因是历次
    # 重启累积的孤儿 warmd 各吃满核。PR_SET_PDEATHSIG=SIGTERM。
    try:
        import ctypes
        import signal as _sig
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, _sig.SIGTERM, 0, 0, 0)  # 1 = PR_SET_PDEATHSIG
    except Exception:
        pass  # 非 Linux/无 prctl 时静默跳过，不阻塞启动


def main():
    _pdeathsig()
    fake = os.environ.get("WARMD_FAKE") == "1"
    if not fake:
        import torch  # noqa: F401 — 预热（~3.6s）
        import triton  # noqa: F401 — 预热（~0.2s）
        # 不变量：父进程绝不 import flag_gems/bench_*，子进程才能全新加载候选代码；
        # 也绝不在父进程编译/launch 任何 triton kernel（fork 后子进程必死锁，见模块头）
    _send({"ready": True, "fake": fake})
    _log(f"warmd ready (fake={fake}, pid={os.getpid()})")
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _send({"id": None, "state": "failed", "rc": 0, "result": None,
                   "stdout": "", "stderr": "", "error": f"bad request line: {exc}"})
            continue
        try:
            handle(req)
        except Exception as exc:  # noqa: BLE001 — 单作业异常不能带倒守护
            _send({"id": req.get("id"), "state": "failed", "rc": 0, "result": None,
                   "stdout": "", "stderr": "", "error": f"warmd handle: {exc!r}"})


if __name__ == "__main__":
    main()
