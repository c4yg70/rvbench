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
  请求  {"id": <job_id>, "timeout_s": <float>, "req": {bench, op, shape, dtype, warmup, rep, batch}}
  响应  {"id", "state": "done|failed|timeout", "rc", "result", "stdout", "stderr", "error"}
  启动就绪时先发 {"ready": true, "fake": bool}
自检/单测：WARMD_FAKE=1 跳过 torch（配合 WARMD_FAKE_SLEEP/WARMD_FAKE_ECHO 模拟 bench）。
"""

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
        try:
            _run_bench(breq, os.environ.get("WARMD_FAKE") == "1")
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        except SystemExit as e:  # bench 脚本 argparse 错误等
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(e.code if isinstance(e.code, int) else 1)
        except BaseException as exc:  # noqa: BLE001
            sys.stderr.write(f"warmd child error: {exc!r}\n")
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
    out = bufs.get("out", b"").decode("utf-8", "replace")
    err = bufs.get("err", b"").decode("utf-8", "replace")
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
    _send({"id": rid, "state": state, "rc": rc, "result": result,
           "stdout": out, "stderr": err, "error": error})


def main():
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
