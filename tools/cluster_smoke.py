"""Real three-board acceptance checks. Saves unmodified HTTP evidence, never synthetic timings."""
import argparse
import concurrent.futures
import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid

MANAGER="http://10.18.60.100:18800"
WORKERS={"k3-func":"http://10.18.60.100:18801","k3-performance":"http://10.18.60.18:18801","k1":"http://10.18.60.167:18801"}

def call(base,path,body=None,method=None):
    data=None if body is None else json.dumps(body).encode()
    request=urllib.request.Request(base+path,data,{"Content-Type":"application/json"},method=method)
    with urllib.request.urlopen(request,timeout=35) as response:
        return json.load(response)

def request(backend,kind,op="add",**kw):
    return dict(backend=backend,kind=kind,op=op,shape=[4096],dtype="float32",seed=42,warmup=3,repetitions=10,timeout_secs=180,client_request_id=str(uuid.uuid4()),**kw)

def execute(label,base,r,manager,output,expected="done"):
    record=call(base,"/jobs",r if manager else dict(request=r))
    deadline=time.monotonic()+240
    while record["state"] in {"queued","running"}:
        if time.monotonic()>deadline: raise RuntimeError(f"{label}: wait exceeded")
        record=call(base,"/jobs/"+record["job_id"]+"?wait=25")
    (output/(label+".json")).write_text(json.dumps(record,indent=2),encoding="utf-8")
    print(label,record["state"],record["worker_id"],record.get("error"),flush=True)
    assert record["state"]==expected,(label,record)
    if expected=="done":
        assert record["compilation"]["status"]=="passed"
        if r["kind"]!="func": assert record["evaluation"]["status"]=="passed"
        if r["kind"]=="performance": assert len(record["evaluation"]["details"]["samples_ms"])==r["repetitions"]
        if manager:
            expected_worker="human-k1" if r["backend"]=="k1" else "k3-performance" if r["kind"]=="performance" else "human-k3-func"
            assert record["worker_id"]==expected_worker
    return record

def rejected(base,r,status):
    try: call(base,"/jobs",dict(request=r))
    except urllib.error.HTTPError as error:
        assert error.code==status,(error.code,error.read());return
    raise AssertionError("unexpected admission")

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="artifacts/cluster-smoke-2026-10-17");parser.add_argument("--operators",action="store_true",help="also run the supported K3 float32 operator matrix");args=parser.parse_args()
    output=pathlib.Path(args.output);output.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+180
    while True:
        nodes=call(MANAGER,"/cluster/workers")
        if sum(n["online"] and n["worker"]["ready"] for n in nodes)==3:
            try:
                current={call(url,"/capabilities")["instance_id"] for url in WORKERS.values()}
                if current=={n["worker"]["instance_id"] for n in nodes}:break
            except (OSError,TimeoutError):pass
        if time.monotonic()>deadline:raise RuntimeError("three workers did not become ready")
        time.sleep(2)
    (output/"workers.json").write_text(json.dumps(nodes,indent=2),encoding="utf-8")
    rejected(WORKERS["k3-func"],request("k3","performance"),400)
    rejected(WORKERS["k3-performance"],request("k3","correctness"),400)
    # Direct submissions first, so local occupancy cannot intentionally race a remote grant.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(execute,"direct-"+name+"-"+kind,WORKERS[name],request("k3",kind,"abs"),False,output) for name,kind in [("k3-func","func"),("k3-func","correctness"),("k3-performance","performance")]]
        for future in futures:future.result()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(execute,"manager-"+backend+"-"+kind,MANAGER,request(backend,kind),True,output) for backend in ["k3","k1"] for kind in ["func","correctness","performance"]]
        for future in futures:future.result()
    wrong="void candidate(const float*a,const float*b,float*out,int n){for(int i=0;i<n;i++)out[i]=0;}"
    execute("k1-numerical-failure",WORKERS["k1"],request("k1","correctness",source=wrong),False,output,"failed")
    execute("k1-compiler-failure",WORKERS["k1"],request("k1","func",source="this is not C"),False,output,"failed")
    r=request("k1","func",source="void candidate(const float*a,const float*b,float*out,int n){for(;;){}}")
    r["timeout_secs"]=2
    execute("k1-process-timeout",WORKERS["k1"],r,False,output,"timeout")
    if args.operators:
        operator_output=output/"operators";operator_output.mkdir(exist_ok=True)
        cases={op:[4096] for op in ["sub","mul","div","exp","relu","sum","aminmax"]}
        cases.update(softmax=[16,128],mm=[32,32],mv=[32,32],topk=[16,32],conv2d=[1,4,16,16])
        def matrix(kind):
            worker=WORKERS["k3-func" if kind=="correctness" else "k3-performance"]
            for op,shape in cases.items():
                r=request("k3",kind,op);r["shape"]=shape
                execute(kind+"-"+op,worker,r,False,operator_output)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(matrix,["correctness","performance"]))
    print("cluster acceptance PASS",flush=True)
