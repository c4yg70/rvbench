#!/usr/bin/env python3
"""paramiko 版 SSH 通道 —— Windows OpenSSH (ssh.exe) 失效时的替代。

背景：2026-09-09 起 /c/Windows/System32/OpenSSH/ssh.exe 连 `-V` 都退出 255 且零输出，
Git Bash 自带 ssh 读 ed25519 私钥报 `error in libcrypto: unsupported`。两条路都断，
故改走 paramiko（隔离 venv: C:/Users/c4y/.workbuddy/binaries/python/envs/default）。

用法：
    python bin/pssh.py <alias|host> "<cmd>" [--timeout 120] [--no-config]
    python bin/pssh.py SpacemitK1-241 "uptime"
    python bin/pssh.py Home-SpacemitK3 "nproc" --timeout 30

主机别名/用户/密钥从 ~/.ssh/config 解析（与 bin/k3ssh 同一份配置）。
输出：先 stdout，再 stderr（带 [stderr] 前缀），末行 `EXIT=<rc>`。
"""
import argparse
import os
import sys

import paramiko


def resolve(target, use_config=True):
    host, user, keys, port = target, None, None, 22
    if use_config:
        cfg_path = os.path.expanduser("~/.ssh/config")
        if os.path.exists(cfg_path):
            cfg = paramiko.SSHConfig()
            with open(cfg_path, encoding="utf-8") as f:
                cfg.parse(f)
            c = cfg.lookup(target)
            host = c.get("hostname", target)
            user = c.get("user")
            port = int(c.get("port", 22))
            idf = c.get("identityfile")
            if idf:
                keys = [os.path.expanduser(k) for k in idf if os.path.exists(os.path.expanduser(k))]
    return host, user, keys, port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("command", nargs="?", default="")
    ap.add_argument("--timeout", type=int, default=120, help="命令超时（秒），默认 120")
    ap.add_argument("--connect-timeout", type=int, default=15)
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--user")
    ap.add_argument("--put", metavar="LOCAL:REMOTE",
                    help="上传文件（走 SFTP），指定后 command 被忽略")
    ap.add_argument("--get", metavar="REMOTE:LOCAL",
                    help="下载文件（走 SFTP），指定后 command 被忽略")
    args = ap.parse_args()

    host, user, keys, port = resolve(args.target, not args.no_config)
    user = args.user or user
    if not user:
        sys.exit("no user resolved; pass --user or add User to ~/.ssh/config")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=user, key_filename=keys,
            timeout=args.connect_timeout, banner_timeout=args.connect_timeout + 10,
            auth_timeout=args.connect_timeout + 10,
        )
    except Exception as e:  # noqa: BLE001
        sys.exit(f"CONNECT_FAILED {host}:{port} user={user} err={type(e).__name__}: {e}")

    if args.put:
        local, remote = args.put.split(":", 1)
        sftp = client.open_sftp()
        sftp.put(os.path.abspath(local), remote)
        sftp.close()
        print(f"PUT_OK {local} -> {remote} ({os.path.getsize(local)} bytes)")
        client.close()
        return
    if args.get:
        remote, local = args.get.split(":", 1)
        sftp = client.open_sftp()
        sftp.get(remote, os.path.abspath(local))
        sftp.close()
        print(f"GET_OK {remote} -> {local}")
        client.close()
        return

    try:
        _, stdout, stderr = client.exec_command(args.command, timeout=args.timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"EXEC_FAILED: {type(e).__name__}: {e}")
    finally:
        client.close()

    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if err:
        sys.stdout.write("[stderr] " + (err if err.endswith("\n") else err + "\n"))
    sys.stdout.write(f"EXIT={rc}\n")


if __name__ == "__main__":
    main()
