"""Upgrade the approved cluster and install isolated monitoring services.

Build the Rust services first. Supply pinned, validated riscv64 binaries.
Existing .100 node_exporter and legacy evald are reused without modifications.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import shlex
import subprocess
import tempfile
import time
from deploy_cluster import NODES,ROOT,KEY,ssh,put,component_root
from cluster_smoke import call

STAMP=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
def quote(s):return shlex.quote(str(s))
def control(n):return 'systemctl' if n['user']=='root' else 'systemctl --user'
def idle(n):
    cap=call('http://'+n['host']+':18801','/capabilities')
    assert cap['direct_active']==0,'direct jobs unfinished on '+n['host']
    nodes=call('http://10.18.60.100:18800','/cluster/workers')
    assert all(not x['assignments'] for x in nodes if x['worker']['worker_id']==n['worker']),'managed jobs unfinished on '+n['host']
def install_unit(n,name,body):
    folder='/etc/systemd/system' if n['user']=='root' else '/home/c4y/.config/systemd/user'
    ssh(n,'mkdir -p '+quote(folder))
    with tempfile.TemporaryDirectory() as td:
        p=pathlib.Path(td)/(name+'.service');p.write_text(body,encoding='utf-8');put(n,p,folder+'/'+p.name)
    ssh(n,control(n)+' daemon-reload')
    ssh(n,control(n)+' enable --now '+name)
def upgrade(n,name):
    idle(n);root=n['root'];ctl=control(n);backup=root+'/backups/observability-'+STAMP+'/'+name
    ssh(n,'mkdir -p '+quote(backup))
    code="import sqlite3,shutil,pathlib;root=pathlib.Path("+repr(root)+");backup=pathlib.Path("+repr(backup)+");src=root/"+repr('manager-data/manager.sqlite' if name.endswith('manager') else 'worker-data/worker.sqlite')+";a=sqlite3.connect(str(src));b=sqlite3.connect(str(backup/'state.sqlite'));a.backup(b);b.close();a.close();shutil.copy2(root/"+repr(name+'.json')+",backup/'config.json');shutil.copy2(root/'bin'/"+repr(name)+",backup/'binary')"
    ssh(n,'python3 -c '+quote(code))
    cfg=json.loads(ssh(n,'cat '+quote(root+'/'+name+'.json')))
    if name.endswith('manager'):cfg['victoriametrics']='http://127.0.0.1:8428'
    else:cfg['host_metrics_url']='http://'+n['host']+':9100/metrics'
    with tempfile.TemporaryDirectory() as td:
        p=pathlib.Path(td)/(name+'.json.next');p.write_text(json.dumps(cfg,indent=2),encoding='utf-8');put(n,p,root+'/'+p.name)
    put(n,component_root(name)/'target/riscv64gc-unknown-linux-gnu/release'/name,root+'/bin/'+name+'.next')
    # Recheck just before stopping; never deliberately interrupt known active work.
    idle(n);ssh(n,ctl+' stop '+name)
    try:
        ssh(n,'chmod 755 '+quote(root+'/bin/'+name+'.next')+' && mv '+quote(root+'/bin/'+name+'.next')+' '+quote(root+'/bin/'+name)+' && mv '+quote(root+'/'+name+'.json.next')+' '+quote(root+'/'+name+'.json'))
        ssh(n,ctl+' start '+name)
        port='18800' if name.endswith('manager') else '18801'
        until=time.monotonic()+150
        while True:
            try:call('http://'+n['host']+':'+port,'/healthz');break
            except OSError:
                if time.monotonic()>until:raise
                time.sleep(2)
    except Exception:
        ssh(n,ctl+' stop '+name+'; cp '+quote(backup+'/binary')+' '+quote(root+'/bin/'+name)+'; cp '+quote(backup+'/config.json')+' '+quote(root+'/'+name+'.json')+'; '+ctl+' start '+name)
        raise
    print(n['host'],name,'upgraded; backup',backup,flush=True)
    return backup
def exporter(n,binary):
    folder=n['root']+'/observability';ssh(n,'mkdir -p '+quote(folder+'/bin'))
    put(n,binary,folder+'/bin/node_exporter.next')
    ssh(n,control(n)+' stop rvbench-node-exporter 2>/dev/null || true')
    ssh(n,'chmod 755 '+quote(folder+'/bin/node_exporter.next')+' && mv '+quote(folder+'/bin/node_exporter.next')+' '+quote(folder+'/bin/node_exporter'))
    print(n['host'],ssh(n,quote(folder+'/bin/node_exporter')+' --version 2>&1').splitlines()[0],flush=True)
    user='User=initer\nGroup=initer\n' if n['user']=='root' else ''
    unit=f'''[Unit]
Description=rvbench host metrics
After=network.target
[Service]
{user}ExecStart={folder}/bin/node_exporter --web.listen-address=:9100 --collector.disable-defaults --collector.cpu --collector.loadavg --collector.meminfo --collector.filesystem --collector.uname --collector.hwmon --collector.netdev --collector.filesystem.mount-points-exclude=^/(dev|proc|sys|run|snap)($|/) --collector.netdev.device-exclude=^(veth|docker|br-|lo)
Restart=on-failure
RestartSec=5
CPUAffinity={'0-7' if n['backend']=='k3' else '0-1'}
CPUQuota=25%
Nice=19
MemoryMax=128M
Environment=GOMAXPROCS=2
Environment=GOMEMLIMIT=96MiB
[Install]
WantedBy={'multi-user.target' if n['user']=='root' else 'default.target'}
'''
    install_unit(n,'rvbench-node-exporter',unit)
    import urllib.request
    until=time.monotonic()+20
    while True:
        try:
            with urllib.request.urlopen('http://'+n['host']+':9100/metrics',timeout=5) as r:r.read(64)
            break
        except OSError:
            if time.monotonic()>until:raise
            time.sleep(1)
def vm(n,binary):
    folder=n['root']+'/observability';ssh(n,'mkdir -p '+quote(folder+'/bin')+' '+quote(folder+'/data'))
    put(n,binary,folder+'/bin/victoria-metrics.next');put(n,ROOT/'deploy/observability/scrape.yml',folder+'/scrape.yml');ssh(n,'chmod 755 '+quote(folder+'/bin/victoria-metrics.next'))
    ssh(n,quote(folder+'/bin/victoria-metrics.next')+' -dryRun -promscrape.config='+quote(folder+'/scrape.yml'))
    ssh(n,control(n)+' stop rvbench-victoriametrics 2>/dev/null || true')
    ssh(n,'mv '+quote(folder+'/bin/victoria-metrics.next')+' '+quote(folder+'/bin/victoria-metrics'))
    install_unit(n,'rvbench-victoriametrics',f'''[Unit]
Description=rvbench VictoriaMetrics single-node
After=network.target rvbench-manager.service
[Service]
ExecStart={folder}/bin/victoria-metrics -httpListenAddr=127.0.0.1:8428 -storageDataPath={folder}/data -retentionPeriod=7d -memory.allowedBytes=128MiB -promscrape.config={folder}/scrape.yml -promscrape.httpSDCheckInterval=30s -promscrape.seriesLimitPerTarget=10000
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
CPUAffinity=0-1
CPUQuota=100%
Nice=10
MemoryMax=768M
Environment=GOMAXPROCS=2
Environment=GOMEMLIMIT=384MiB
[Install]
WantedBy=default.target
''')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--vm-binary',type=pathlib.Path,required=True);p.add_argument('--exporter-binary',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,default=ROOT/'artifacts/observability-deploy-2026-10-17');args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    evidence={'stamp':STAMP,'binaries':{str(b):hashlib.sha256(b.read_bytes()).hexdigest() for b in [args.vm_binary,args.exporter_binary]},'backups':[]}
    for n in NODES:idle(n)
    master=NODES[0];evidence['backups'].append(upgrade(master,'rvbench-manager'))
    for n in NODES:
        if not n['manager']:exporter(n,args.exporter_binary)
        evidence['backups'].append(upgrade(n,'rvbench-worker'))
    vm(master,args.vm_binary)
    (args.output/'deployment.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print('Deployment complete. Registration TTL and HTTP discovery may take 30 seconds.',flush=True)
