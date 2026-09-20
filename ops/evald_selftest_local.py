#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evald selftest 本地 shim 运行器（Windows / 无 fork 平台）。

本地 Python 没有 fcntl：运行前在 sys.modules 预注册 fcntl shim——
msvcrt.locking 实现（Windows 文件锁按句柄互斥，同进程第二个 fd 冲突，
与 flock 语义一致），selftest [11] 的 flock 探针据此可跑全量断言。
板上真机直接 `python3 evald.py --selftest`，不经本 runner。
照旧例：无 os.fork 时 selftest 自行跳过 [6]/[7]。

用法（工作区根）::
  python ops/evald_selftest_local.py [evald.py 路径]
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types


def install_fcntl_shim():
    """缺 fcntl 时注入 msvcrt 版 shim；返回是否注入（注入则 LOCK_NB 探针仍为真实语义）。"""
    try:
        import fcntl  # noqa: F401
        return False
    except ImportError:
        pass
    import msvcrt

    mod = types.ModuleType("fcntl")
    mod.LOCK_SH = 1
    mod.LOCK_EX = 2
    mod.LOCK_NB = 4
    mod.LOCK_UN = 8

    def flock(fd, op):
        # 整锁模拟为 0 号字节 1 byte；Windows 锁按句柄互斥（同进程第二 fd 亦冲突）
        os.lseek(fd, 0, os.SEEK_SET)
        if op & mod.LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif op & mod.LOCK_NB:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # 阻塞加锁（LK_LOCK 最多重试 ~10s，selftest 1.5s 内释放，够用）
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    mod.flock = flock
    sys.modules["fcntl"] = mod
    return True


def install_posix_min_shims():
    """超时杀进程路径用 os.killpg/os.getpgid/signal.SIGKILL（POSIX）。本地仅保证
    selftest 可走通：killpg≈taskkill /T（杀整棵进程树，等价板上进程组语义；
    若只 TerminateProcess 直接子进程，孙进程 sleep 持管道句柄会让 communicate
    再等到自然结束，[5] 超时断言即失败）。"""
    if not hasattr(os, "getpgid"):
        os.getpgid = lambda pid: pid
    if not hasattr(os, "killpg"):
        def _killpg(pid, sig):
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            if r.returncode != 0:
                os.kill(pid, sig)
        os.killpg = _killpg
    if not hasattr(signal, "SIGKILL"):
        signal.SIGKILL = 9


def install_bash_cwd_shim():
    """System32 的 WSL bash 桩在 CreateProcess 搜索序里先于 PATH，会让 evald 的
    ["bash","-c",…] 作业 rc=1。把 Git Bash 的 bash.exe(+msys-2.0.dll) 复制到临时
    目录并 chdir 过去——CreateProcess 搜索序中父进程当前目录排在 System32 前。
    返回 shim 目录（失败返回 None，selftest 的 bench 类用例会相应失败）。"""
    git_bash = shutil.which("bash")
    if not git_bash or not os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                       "System32", "bash.exe")):
        return None
    shim = tempfile.mkdtemp(prefix="bash_shim_")
    try:
        shutil.copy(git_bash, os.path.join(shim, "bash.exe"))
        dll = os.path.join(os.path.dirname(git_bash), "msys-2.0.dll")
        if os.path.exists(dll):
            shutil.copy(dll, os.path.join(shim, "msys-2.0.dll"))
    except OSError:
        return None
    os.chdir(shim)
    return shim


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_evald = os.path.join(os.path.dirname(here), "evald.py")   # ops/ 的上级＝仓库根
    evald_path = sys.argv[1] if len(sys.argv) > 1 else default_evald
    shimmed = install_fcntl_shim()
    install_posix_min_shims()
    bash_shim = install_bash_cwd_shim()
    print(f"[local] evald: {evald_path}")
    print(f"[local] fcntl shim 注入: {shimmed}（板上为真实 flock，无需 shim）")
    print(f"[local] bash cwd shim: {bash_shim or '不需要/不可用'}")

    spec = importlib.util.spec_from_file_location("evald", evald_path)
    evald = importlib.util.module_from_spec(spec)
    sys.modules["evald"] = evald
    spec.loader.exec_module(evald)

    tmp = tempfile.mkdtemp(prefix="evald_selftest_local_")
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"token": "local-selftest", "host": "127.0.0.1", "port": 0,
                   "flag_gems_root": "fg", "scripts_root": "scripts",
                   "run_env": "run_env.sh", "data_dir": os.path.join(tmp, "data")}, f)
    cfg = evald.load_config(cfg_path)
    rc = evald.selftest(cfg)
    print(f"[local] 临时目录: {tmp}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
