"""Deploy the approved three-board topology over existing SSH keys (no service auth).

Build first: cargo zigbuild -p rvbench-worker -p rvbench-manager --release
  --target riscv64gc-unknown-linux-gnu.2.38
Run from repository root: python tools/deploy_cluster.py [--standalone-k1]
Existing legacy production evald ports/services are not touched.
"""
import argparse
import json
import pathlib
import shlex
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = pathlib.Path.home() / ".ssh/c4y-dev.key"
NODES = [
    dict(host="10.18.60.100", user="c4y", backend="k3", worker="human-k3-func", board="HUMAN-K3", kinds=["func", "correctness"], delivery="hybrid", root="/home/c4y/.local/share/rvbench-v1", manager=True),
    dict(host="10.18.60.18", user="root", backend="k3", worker="k3-performance", board="K3-018", kinds=["performance"], delivery="push", root="/opt/rvbench-v1", manager=False),
    dict(host="10.18.60.167", user="c4y", backend="k1", worker="human-k1", board="HUMAN-Musebook-K1", kinds=["func", "correctness", "performance"], delivery="pull", root="/home/c4y/.local/share/rvbench-v1", manager=False),
]

def ssh(node, command):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-i", str(KEY), f"{node['user']}@{node['host']}", command], check=True, text=True, encoding="utf-8", errors="replace", capture_output=True).stdout

def put(node, source, target):
    subprocess.run(["scp", "-q", "-i", str(KEY), str(source), f"{node['user']}@{node['host']}:{target}"], check=True)

def config(node, standalone=False):
    backend = dict(backend=node["backend"], cc="gcc", python="python3", cpus="0-7" if node["backend"] == "k3" and node["manager"] else "8-15" if node["backend"] == "k3" else "0-7", ai_thread=node["backend"] == "k3" and not node["manager"], environment={})
    if node["backend"] == "k3":
        backend["python"] = "/home/c4y/autoke/FlagGems/.venv/bin/python"
        backend["environment"] = dict(GEMS_VENDOR="spacemit", OMP_NUM_THREADS="1", LD_LIBRARY_PATH="/opt/rvbench/deps:/home/c4y/autoke/deps/sleef/usr/lib/riscv64-linux-gnu:/home/c4y/autoke/deps/libomp/usr/lib/riscv64-linux-gnu", TRITON_CACHE_DIR=node["root"]+"/cache")
    return dict(listen="0.0.0.0:18801", url=f"http://{node['host']}:18801", worker_id=node["worker"], board_id=node["board"], data_dir=node["root"]+"/worker-data", kinds=node["kinds"], delivery=node["delivery"], manager=None if standalone else "http://10.18.60.100:18800", backend=backend)

def deploy(node, standalone=False):
    root=node["root"]
    system=node["user"] == "root"
    ctl="systemctl" if system else "systemctl --user"
    unitdir="/etc/systemd/system" if system else "/home/c4y/.config/systemd/user"
    names=["rvbench-worker"] + (["rvbench-manager"] if node["manager"] else [])
    ssh(node, "mkdir -p "+" ".join(shlex.quote(p) for p in [root, root+"/bin", root+"/worker-data", root+"/cache", unitdir]))
    if system:
        # Private runtime mapping is unused before first launch. Never chmod a running mapping.
        ssh(node, "if fuser /dev/shm/tcm_sync_standalone >/dev/null 2>&1; then echo 'TCM is occupied' >&2; exit 1; fi; chown initer:initer /dev/shm/tcm_sync_standalone; chown initer:initer "+" ".join(shlex.quote(p) for p in [root,root+"/worker-data",root+"/cache"]))
    with tempfile.TemporaryDirectory() as td:
        td=pathlib.Path(td)
        configs={"rvbench-worker":config(node,standalone)}
        if node["manager"]: configs["rvbench-manager"]=dict(listen="0.0.0.0:18800",data_dir=root+"/manager-data")
        for name in names:
            filename=td/(name+".json");filename.write_text(json.dumps(configs[name],indent=2),encoding="utf-8")
            put(node,filename,root+"/"+filename.name)
            binary=ROOT/"target/riscv64gc-unknown-linux-gnu/release"/name
            put(node,binary,root+"/bin/"+name+".next")
            ssh(node, f"{ctl} stop {name}.service 2>/dev/null || true")
            ssh(node, "chmod 755 "+shlex.quote(root+"/bin/"+name+".next")+" && mv "+shlex.quote(root+"/bin/"+name+".next")+" "+shlex.quote(root+"/bin/"+name))
            unit=td/(name+".service")
            unit.write_text("[Unit]\nDescription="+name+"\nAfter=network.target\n[Service]\nType=simple\n"+("User=initer\nGroup=initer\nEnvironment=HOME=/home/initer\n" if system else "")+f"ExecStart={root}/bin/{name} --config {root}/{name}.json\nRestart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=10\n[Install]\nWantedBy="+("multi-user.target" if system else "default.target")+"\n",encoding="utf-8")
            put(node,unit,unitdir+"/"+unit.name)
        if not system: ssh(node,"loginctl enable-linger c4y")
        ssh(node,f"{ctl} daemon-reload")
        for name in reversed(names): ssh(node,f"{ctl} enable --now {name}.service")
    print(node["host"], "deployed", ", ".join(names), flush=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--standalone-k1",action="store_true");parser.add_argument("--host")
    args=parser.parse_args()
    for node in NODES:
        if not args.host or node["host"]==args.host: deploy(node, args.standalone_k1 and node["backend"]=="k1")
