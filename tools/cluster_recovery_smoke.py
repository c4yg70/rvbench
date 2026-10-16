"""Fault acceptance: briefly stop the new manager, then restart the K1 worker.

Run only against the approved deployment when no other test clients are submitting work.
Legacy production evald is never stopped. All evidence is saved as original HTTP records.
"""
import argparse
import json
import pathlib
import shlex
import time
from cluster_smoke import MANAGER,WORKERS,call,request
from deploy_cluster import NODES,ssh

MASTER=next(n for n in NODES if n["manager"])
K1=next(n for n in NODES if n["backend"]=="k1")

def save(output,name,record):
    (output/(name+".json")).write_text(json.dumps(record,indent=2),encoding="utf-8")

def wait_running(base,job_id):
    until=time.monotonic()+45
    while True:
        j=call(base,"/jobs/"+job_id)
        if j["state"]=="running":return j
        assert j["state"]=="queued",j
        assert time.monotonic()<until,j
        time.sleep(.1)

def wait_terminal(base,job_id,seconds=90):
    until=time.monotonic()+seconds
    while True:
        try:
            j=call(base,"/jobs/"+job_id+"?wait=25")
            if j["state"] not in {"queued","running"}:return j
        except OSError:pass
        assert time.monotonic()<until,"terminal wait exceeded"
        time.sleep(.2)

def manager_outbox(output):
    source="#include <unistd.h>\nvoid candidate(const float*a,const float*b,float*out,int n){usleep(300000);for(int i=0;i<n;i++)out[i]=a[i]+b[i];}"
    r=request("k1","performance",source=source);r["warmup"]=0;r["repetitions"]=2
    j=call(MANAGER,"/jobs",r);wait_running(MANAGER,j["job_id"])
    try:
        ssh(MASTER,"systemctl --user stop rvbench-manager")
        local=wait_terminal(WORKERS["k1"],j["job_id"])
        assert local["state"]=="done",local
        save(output,"manager-offline-worker-result",local)
        code="import sqlite3,json;d=json.loads(sqlite3.connect('/home/c4y/.local/share/rvbench-v1/worker-data/worker.sqlite').execute('select body from control_state').fetchone()[0]);print(len(d['outbox']))"
        assert int(ssh(K1,"python3 -c "+shlex.quote(code)).strip())>=1
    finally:
        ssh(MASTER,"systemctl --user start rvbench-manager")
    result=wait_terminal(MANAGER,j["job_id"])
    assert result["state"]=="done" and result["attempt_id"]==local["attempt_id"]
    assert result["evaluation"]==local["evaluation"]
    save(output,"manager-recovered-outbox-result",result)
    print("manager outage / durable outbox replay PASS",flush=True)

def worker_restart(output):
    source="#include <unistd.h>\nvoid candidate(const float*a,const float*b,float*out,int n){sleep(60);for(int i=0;i<n;i++)out[i]=a[i]+b[i];}"
    j=call(MANAGER,"/jobs",request("k1","correctness",source=source))
    wait_running(MANAGER,j["job_id"]);wait_running(WORKERS["k1"],j["job_id"])
    until=time.monotonic()+10
    while True:
        pids=ssh(K1,"pgrep -u c4y -x candidate || true").split()
        if pids:break
        assert time.monotonic()<until,"candidate did not start"
        time.sleep(.1)
    assert all(p.isdigit() for p in pids)
    ssh(K1,"systemctl --user restart rvbench-worker")
    local=wait_terminal(WORKERS["k1"],j["job_id"])
    assert local["state"]=="failed" and "restarted" in local["error"],local
    for pid in pids:assert ssh(K1,f"if test -e /proc/{pid}; then echo alive; else echo gone; fi").strip()=="gone"
    result=wait_terminal(MANAGER,j["job_id"])
    assert result["state"]=="failed" and result["attempt_id"]==local["attempt_id"]
    save(output,"worker-restart-local-result",local);save(output,"worker-restart-manager-result",result)
    print("worker restart / child cleanup / interrupted attempt report PASS",flush=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="artifacts/cluster-smoke-2026-10-17/final");args=parser.parse_args()
    output=pathlib.Path(args.output);output.mkdir(parents=True,exist_ok=True)
    manager_outbox(output);worker_restart(output)
