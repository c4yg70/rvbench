"""Verify saved HTTP artifact manifests against the real board files over SSH."""
import argparse
import base64
import json
import pathlib
import shlex
import uuid
from deploy_cluster import NODES,ssh

def verify(folder):
    manifests={n["worker"]:{} for n in NODES}
    for file in pathlib.Path(folder).glob("*.json"):
        r=json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(r,dict) or "job_id" not in r:continue
        uuid.UUID(r["job_id"])
        for phase in [r.get("compilation"),r.get("evaluation")]:
            for a in (phase or {}).get("artifacts",[]):
                key=(r["job_id"],a["path"])
                previous=manifests[r["worker_id"]].get(key)
                assert previous is None or previous==a,"manifest changed across snapshots"
                manifests[r["worker_id"]][key]=a
    summary=[]
    for node in NODES:
        entries=[dict(job_id=job,path=path,sha256=a["sha256"],size_bytes=a["size_bytes"]) for (job,path),a in manifests[node["worker"]].items()]
        encoded=base64.b64encode(json.dumps(entries).encode()).decode()
        code=f"""import base64,json,pathlib,hashlib
root=pathlib.Path({node['root']!r})/'worker-data'/'jobs'
entries=json.loads(base64.b64decode({encoded!r}))
errors=[]
for a in entries:
 base=(root/a['job_id']).resolve(); path=(base/a['path']).resolve()
 if not path.is_relative_to(base):errors.append(a['path']+': escaped job directory');continue
 try:
  data=path.read_bytes()
  if len(data)!=a['size_bytes'] or hashlib.sha256(data).hexdigest()!=a['sha256']:errors.append(a['job_id']+'/'+a['path'])
 except OSError as e:errors.append(str(e))
print(json.dumps(dict(host={node['host']!r},checked=len(entries),errors=errors)))
"""
        result=json.loads(ssh(node,"python3 -c "+shlex.quote(code)))
        print(json.dumps(result),flush=True);assert not result["errors"],result
        summary.append(result)
    pathlib.Path(folder,"artifact-verification.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("folder");args=parser.parse_args();verify(args.folder)
