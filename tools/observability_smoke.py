"""Real monitoring acceptance; fault and ABBA checks require an idle private cluster."""
import argparse
import concurrent.futures
import json
import math
import pathlib
import shlex
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from cluster_smoke import MANAGER,WORKERS,call,request,execute
from deploy_cluster import NODES,ssh

MASTER=NODES[0]
PERF=next(n for n in NODES if n['host']=='10.18.60.18')
def save(folder,name,value):
    (folder/(name+'.json')).write_text(json.dumps(value,indent=2),encoding='utf-8')
def raw(base,path):
    with urllib.request.urlopen(base+path,timeout=15) as r:return r.read().decode()
def counters(base):
    return sorted(line for line in raw(base,'/metrics').splitlines() if line.startswith(('rvbench_manager_jobs_submitted_total{','rvbench_manager_jobs_completed_total{')))
def vm(path):
    code='import urllib.request;print(urllib.request.urlopen('+repr('http://127.0.0.1:8428'+path)+',timeout=10).read().decode())'
    return json.loads(ssh(MASTER,'python3 -c '+shlex.quote(code)))
def wait_until(fn,seconds=90):
    end=time.monotonic()+seconds
    while True:
        try:
            value=fn()
            if value:return value
        except (OSError,subprocess.CalledProcessError):pass
        if time.monotonic()>end:raise TimeoutError('acceptance condition not met')
        time.sleep(2)
def monitor_on():
    ssh(PERF,'systemctl start rvbench-node-exporter')
    ssh(MASTER,'systemctl --user start rvbench-victoriametrics')
def verify_exporter_outage(folder):
    try:
        ssh(PERF,'systemctl stop rvbench-node-exporter')
        def detected():
            s=call(MANAGER,'/observability/summary');h=s['collection']
            failed=any(t.get('job')=='rvbench_nodes' and t.get('board_id')=='K3-018' and not t['up'] and t.get('last_scrape_at',0)>0 and t['error'] for t in h.get('targets',[]))
            return s if h['status']=='degraded' and h.get('targets_up',0)>0 and failed else None
        degraded=wait_until(detected,75)
        save(folder,'exporter-down-summary',degraded)
    finally:monitor_on()
def monitoring(folder,profiles):
    summary=wait_until(lambda: (s if (s:=call(MANAGER,'/observability/summary'))['collection'].get('targets_up')==8 and s['collection']['status']=='ok' else None),120)
    save(folder,'summary',summary)
    targets=vm('/api/v1/targets');save(folder,'targets',targets)
    assert len(targets['data']['activeTargets'])==8
    assert all(t['health']=='up' and not t['lastError'] for t in targets['data']['activeTargets'])
    end=int(time.time()/30)*30
    ids=['cpu_percent','x100_cpu_percent','a100_cpu_percent','memory_percent','disk_percent','temperature_c','load1','queue_depth','running','throughput','outbox','collection_up','submissions','completed']
    def read_metric(metric):
        value=call(MANAGER,'/observability/range?'+urllib.parse.urlencode(dict(metric_id=metric,start=end-600,end=end,step_seconds=30)))
        assert not value['partial'],value['errors']
        assert value['series'],metric
        assert all(math.isfinite(p['value']) for s in value['series'] for p in s['points'] if 'value' in p)
        save(folder,'range-'+metric,value)
        return metric,len(value['series'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: ranges=dict(pool.map(read_metric,ids))
    for name,base in WORKERS.items():
        s=call(base,'/observability/summary');save(folder,'worker-'+name,s)
        assert s['service']=='worker'
        text=raw(base,'/metrics');assert 'rvbench_worker_jobs_submitted_total' in text
    wrong='void candidate(const float*a,const float*b,float*out,int n){for(int i=0;i<n;i++)out[i]=0;}'
    bad=execute('preflight-rejects-wrong-performance',MANAGER,request('k1','performance',source=wrong),True,folder,'failed')
    assert bad['evaluation']['details']['correct'] is False
    ranking=call(MANAGER,'/leaderboard');save(folder,'leaderboard',ranking)
    assert ranking['groups']
    assert all(e['job']['job_id']!=bad['job_id'] for g in ranking['groups'] for e in g['entries'])
    before=counters(MANAGER)
    ssh(MASTER,'systemctl --user restart rvbench-manager')
    wait_until(lambda:call(MANAGER,'/healthz'))
    assert before==counters(MANAGER),'restart changed persisted business counts'
    print('8 targets / fixed queries / numerical ranking gate / durable counters PASS',flush=True)
    # Graceful storage restart: compare a fully historical, already visible interval.
    end=int((time.time()-60)/30)*30
    path='/api/v1/query_range?'+urllib.parse.urlencode(dict(query='up{job="rvbench_manager"}',start=end-240,end=end,step=30,nocache=1))
    before=vm(path);assert before['data']['result']
    save(folder,'vm-history-before',before)
    ssh(MASTER,'systemctl --user restart rvbench-victoriametrics')
    after=wait_until(lambda:vm(path))
    assert before['data']==after['data'],'storage restart changed historical values'
    save(folder,'vm-history-before',before);save(folder,'vm-history-after',after)
    try:
        ssh(MASTER,'systemctl --user stop rvbench-victoriametrics')
        unavailable=wait_until(lambda: (s if (s:=call(MANAGER,'/observability/summary'))['collection']['status']=='unavailable' else None),20)
        save(folder,'vm-unavailable-summary',unavailable)
        execute('task-works-without-vm',MANAGER,request('k1','func'),True,folder)
    finally:monitor_on()
    # Per-target outage must be degraded even while the TSDB stays healthy.
    verify_exporter_outage(folder)
    results=[]
    if profiles:
        for i,on in enumerate([True,False,False,True]):
            try:
                if on:monitor_on()
                else:
                    ssh(PERF,'systemctl stop rvbench-node-exporter')
                    ssh(MASTER,'systemctl --user stop rvbench-victoriametrics')
                r=request('k3','performance','mm');r['shape']=[128,128];r['warmup']=5;r['repetitions']=50
                job=execute(f'profile-{i}-'+('on' if on else 'off'),MANAGER,r,True,folder)
                assert job['evaluation']['details']['correct'] is True
                samples=job['evaluation']['details']['samples_ms'];assert all(math.isfinite(x) and x>0 for x in samples)
                results.append(dict(monitoring=on,median_ms=statistics.median(samples),job_id=job['job_id']))
            finally:monitor_on()
        on=statistics.median(x['median_ms'] for x in results if x['monitoring'])
        off=statistics.median(x['median_ms'] for x in results if not x['monitoring'])
        save(folder,'sampling-impact',dict(runs=results,monitored_median_ms=on,unmonitored_median_ms=off,relative_change_percent=(on/off-1)*100,scope='ABBA short sample; collection on/off only; not a long-term performance guarantee'))
    wait_until(lambda:call(MANAGER,'/observability/summary')['collection']['status']=='ok',120)
    final=call(MANAGER,'/observability/summary');save(folder,'final-summary',final)
    save(folder,'acceptance',dict(passed=True,ranges=ranges,profiles=results))
    print('VM persistence / telemetry outage isolation / exporter degradation PASS',flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=pathlib.Path,default=pathlib.Path('artifacts/observability-acceptance'));p.add_argument('--profiles',action='store_true');args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    monitoring(args.output,args.profiles)
