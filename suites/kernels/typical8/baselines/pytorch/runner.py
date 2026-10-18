"""CPU PyTorch baselines for typical8; no FlagGems or Triton execution.

Each operator owns a native baseline() in its own module (abs.py, mm.py, etc.).
Run that module using the entry's argv with {spec}, {case_id}, {output}.
The output is baseline-results 1.0, with a companion JSON evidence artifact.
SDPA's out-only measurement is evidence, not a full five-output Flash baseline.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import traceback
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                    encoding='utf-8', newline='\n')


def tensor_hash(tensor):
    contiguous = tensor.detach().contiguous()
    if contiguous.device.type != 'cpu' or sys.byteorder != 'little':
        raise ValueError('tensor hash v1 requires little-endian CPU storage')
    return hashlib.sha256(ctypes.string_at(contiguous.data_ptr(),
                         contiguous.numel() * contiguous.element_size())).hexdigest()


def materialize(torch, item):
    if item['kind'] == 'none':
        return None
    dtype = getattr(torch, item['dtype'])
    shape = item['shape']
    layout = item['layout']
    if layout['kind'] == 'contiguous':
        strides, stride = [], 1
        for dim in reversed(shape):
            strides.insert(0, stride)
            stride *= max(dim, 1)
        offset = 0
    elif layout['kind'] == 'strided':
        strides, offset = layout['strides'], layout.get('storage_offset', 0)
    else:
        raise ValueError('unsupported layout')
    size = offset + (1 + sum((d - 1) * s for d, s in zip(shape, strides))
                     if all(shape) else 0)
    gen = item['generator']
    if gen['kind'] == 'torch_cpu_uniform_v1':
        generator = torch.Generator(device='cpu').manual_seed(gen['seed'])
        data = torch.rand(size, generator=generator, dtype=torch.float32, device='cpu')
        data = (gen['low'] + (gen['high'] - gen['low']) * data).to(dtype)
    elif gen['kind'] == 'constant_v1':
        data = torch.full((size,), gen['value'], dtype=dtype, device='cpu')
    else:
        raise ValueError('unsupported input generator')
    return torch.as_strided(data, shape, strides, offset)


def describe_input(item, tensor):
    result = {'name': item['name'], 'kind': item['kind']}
    if tensor is None:
        return {**result, **dict.fromkeys(['shape', 'dtype', 'strides', 'storage_offset',
                                          'content_sha256', 'generator'])}
    return {**result, 'shape': list(tensor.shape), 'dtype': str(tensor.dtype).removeprefix('torch.'),
            'strides': list(tensor.stride()), 'storage_offset': tensor.storage_offset(),
            'content_sha256': tensor_hash(tensor), 'generator': item['generator']}


def build_call(op, case, tensors, baseline, reference_id, partial=False):
    """Bind suite inputs to the callable supplied by the per-operator module."""
    kind = 'adapter' if partial else 'python_symbol'
    if op['call']['implementations']['reference'] != {'kind':kind,'id':reference_id}:
        raise ValueError('reference callable differs from the per-operator PyTorch binding')
    def resolve(binding):
        return tensors[binding['name']] if binding['kind'] == 'input' else case['attrs'][binding['name']]
    args = [resolve(v) for v in op['call']['args']]
    kwargs = {k:resolve(v) for k,v in op['call']['kwargs'].items()}
    return lambda: baseline(*args, **kwargs), partial


def check_record(name, kind, outputs, passed, method, reason=None, metrics=None):
    return {'id':name, 'kind':kind, 'outputs':outputs,
            'status':'passed' if passed else ('unverifiable' if passed is None else 'failed'),
            'reason':reason, 'metrics':metrics or {}, 'method':method,
            'contract_sha256':None, 'parameters':{}}


def verify(torch, op, case, tensors, outputs, partial):
    reason = lambda code, message: {'code':code, 'message':message}
    expected = case['outputs'][:1] if partial else case['outputs']
    metadata_ok = len(outputs) == len(expected) and all(
        e['kind'] == 'tensor' and list(t.shape) == e['shape']
        and str(t.dtype).removeprefix('torch.') == e['dtype']
        and bool(torch.isfinite(t).all()) for t, e in zip(outputs, expected))
    checks = [check_record('native-call', 'execution', [e['name'] for e in expected],
                          metadata_ok, 'pytorch_native_call_v1',
                          None if metadata_ok else reason('OUTPUT_MISMATCH', 'Output metadata or finiteness mismatch'))]
    if not metadata_ok:
        return 'failed', reason('OUTPUT_MISMATCH', 'Output metadata or finiteness mismatch'), checks
    if partial:
        return 'unverifiable', reason('SDPA_OUT_ONLY', 'PyTorch SDPA returns out only; lse/philox_seed/philox_offset/p remain unimplemented'), checks
    if op['id'] == 'abs':
        ok = bool(torch.equal(outputs[0], torch.where(tensors['x'] < 0, -tensors['x'], tensors['x'])))
        checks.append(check_record('abs-values', 'numeric', ['out'], ok, 'exact_values',
                                   None if ok else reason('EXACT_MISMATCH', 'Abs values differ')))
    elif op['id'] == 'aminmax':
        attrs = case['attrs']
        refs = [torch.amin(tensors['x'], dim=attrs['dim'], keepdim=attrs['keepdim']),
                torch.amax(tensors['x'], dim=attrs['dim'], keepdim=attrs['keepdim'])]
        for name, actual, ref in zip(['min','max'], outputs, refs):
            ok = bool(torch.equal(actual, ref))
            checks.append(check_record(name+'-values','numeric',[name],ok,'exact_values',
                                       None if ok else reason('EXACT_MISMATCH','Reduction differs')))
    elif op['id'] == 'topk':
        attrs = case['attrs']; x = tensors['x']; values, indices = outputs
        sorted_values = torch.sort(x, dim=attrs['dim'], descending=attrs['largest']).values
        ref = sorted_values.narrow(attrs['dim'], 0, attrs['k'])
        ok_values = bool(torch.equal(values if attrs['sorted'] else torch.sort(values).values,
                                     ref if attrs['sorted'] else torch.sort(ref).values))
        in_range = bool(((indices >= 0) & (indices < x.shape[attrs['dim']])).all())
        ordered_indices = torch.sort(indices, dim=attrs['dim']).values
        unique = bool((torch.diff(ordered_indices, dim=attrs['dim']) != 0).all())
        ok_indices = in_range and unique and bool(torch.equal(torch.gather(x, attrs['dim'], indices), values))
        for name, ok in [('values',ok_values),('indices',ok_indices)]:
            checks.append(check_record('topk-'+name,'numeric' if name=='values' else 'semantic',
                [name],ok,'topk_'+name,None if ok else reason('TOPK_MISMATCH','TopK selection invalid')))
    else:
        pending = reason('NUMERIC_CONTRACT_PENDING', 'Native call completed; suite numerical tolerances are not yet resolved')
        for output in case['verification']['output_checks']:
            checks.append(check_record('numeric-'+output['output'],'numeric',[output['output']],
                                       None,output['method'],pending))
        return 'unverifiable', pending, checks
    if any(c['status'] != 'passed' for c in checks):
        return 'failed', reason('CHECK_FAILED','A native baseline check failed'), checks
    return 'passed', None, checks


def environment(torch, threads, affinity):
    def version(package):
        try: return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: return 'not-installed'
    cpu = Path('/proc/cpuinfo').read_text()
    fields = dict(line.split(':',1) for line in cpu.splitlines() if ':' in line)
    fields = {k.strip():v.strip() for k,v in fields.items()}
    env = {'id':platform.node()+'-pytorch-cpu', 'architecture':'rv64', 'worker_id':None,
           'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
           'cpu':{'model':fields.get('model name',fields.get('uarch',platform.processor() or platform.machine())),
                  'isa':fields.get('isa',platform.machine()),'logical_cpus':os.cpu_count(), 'affinity':affinity},
           'runtime':{'python':platform.python_version(),'torch':torch.__version__,
                      'triton':version('triton'), 'flag_gems':version('flag_gems'),
                      'runner':'rvbench-pytorch-baseline/1.1.0','adapter':'torch-native8-per-op/1.1.0',
                      'generator':'torch_cpu_uniform_v1'},
           'isolation':{'kind':'host_lock','description':'Exclusive k3_bench.lock, .0 and .1; AI CPU affinity; no FlagGems/Triton imports'},
           'backend':'spacemit','host_tag':platform.node(),'omp_threads':threads}
    env['fingerprint_sha256'] = digest({k:v for k,v in env.items() if k!='id'})
    return env


def main(operator_id, baseline, reference_id, partial=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--case-id',required=True)
    parser.add_argument('--baseline-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if platform.machine() != 'riscv64':
        raise RuntimeError('This recorded environment format currently requires a RISC-V Linux worker')
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    op, case = next((op,c) for op in spec['operators'] for c in op['cases'] if c['id']==args.case_id)
    entry = next(e for e in spec['baseline']['entries'] if e['id']==args.baseline_id)
    if args.case_id not in entry['case_ids'] or op['id'] != entry['operator_id'] or op['id'] != operator_id:
        raise ValueError('case does not belong to selected baseline')
    code_root = args.spec.parent / entry['code']['root']
    for file in entry['code']['files']:
        if file_hash(code_root/file['path']) != file['sha256']:
            raise ValueError('baseline source identity mismatch')
    contract = spec['policies']['timing']['contract']
    if contract['status'] != 'resolved' or file_hash(args.spec.parent/contract['path']) != contract['sha256']:
        raise ValueError('baseline requires a resolved timing contract')
    timing_policy = json.loads((args.spec.parent/contract['path']).read_text())
    locks = []
    for path in ['/tmp/k3_bench.lock','/tmp/k3_bench.lock.0','/tmp/k3_bench.lock.1']:
        lock = open(path,'a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); locks.append(lock)
    health = {}
    for port in [8788,8791]:
        health[str(port)] = json.load(urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/healthz',timeout=5))
        if health[str(port)]['running'] or health[str(port)]['queued']:
            raise RuntimeError('production work is active; postpone baseline capture')
    import torch
    from torch.utils.benchmark import Timer
    if torch.__version__ != timing_policy['torch_version']:
        raise ValueError('torch version differs from pinned timing contract')
    if 'flag_gems' in sys.modules or 'triton' in sys.modules:
        raise RuntimeError('vendor operator packages must not be imported')
    threads = int(os.environ['OMP_NUM_THREADS']); torch.set_num_threads(threads)
    if threads != timing_policy['num_threads']:
        raise ValueError('thread count differs from timing contract')
    affinity = sorted(os.sched_getaffinity(0))
    if affinity != timing_policy['cpu_affinity']:
        raise ValueError('CPU affinity differs from timing contract')
    env = environment(torch,threads,affinity)
    tensors = {item['name']:materialize(torch,item) for item in case['inputs']}
    inputs = [describe_input(item,tensors[item['name']]) for item in case['inputs']]
    evidence = {'purpose':'measured','case_id':case['id'],'production_health_before':health,
                'timer_source':inspect.getsource(Timer.timeit),'torch_config':torch.__config__.show(),
                'tensor_hash_contract':'little-endian logical row-major CPU tensor bytes v1',
                'implementation_sha256':digest({k:entry[k] for k in ['id','operator_id','code','run']}),
                'inputs':inputs,'attrs':case['attrs'],'environment':env,
                'baseline_module':{'path':Path(inspect.getfile(baseline)).name,
                                   'callable':baseline.__name__,'source':inspect.getsource(baseline),
                                   'sha256':file_hash(Path(inspect.getfile(baseline)))}}
    checks, samples, output_meta = [], [], []
    timing = None
    try:
        call, partial = build_call(op,case,tensors,baseline,reference_id,partial)
        with torch.inference_mode():
            value = call()
            outputs = [value] if isinstance(value,torch.Tensor) else list(value)
            status, reason, checks = verify(torch,op,case,tensors,outputs,partial)
            output_meta = [{'name':name,'shape':list(t.shape),'dtype':str(t.dtype).removeprefix('torch.'),
                            'strides':list(t.stride()),'content_sha256':tensor_hash(t)}
                           for name,t in zip(['out'] if partial else op['returns']['names'],outputs)]
            del value, outputs
            if status != 'failed':
                for _ in range(timing_policy['explicit_warmup']): call()
                timer = Timer(stmt='call()',globals={'call':call},num_threads=threads,timer=time.perf_counter)
                for _ in range(timing_policy['repetitions']):
                    sample = timer.timeit(number=timing_policy['number_per_run'])
                    samples.append(sample.median * 1000)
                measured = {'unit':'ms','scope':'call','statistic':'median','preparation':'excluded',
                            'synchronization':'CPU synchronous; torch.utils.benchmark.Timer(time.perf_counter)',
                            'timing_contract_sha256':contract['sha256'],
                            'warmup':timing_policy['explicit_warmup'] + timing_policy['repetitions']*timing_policy['timer_warmup_per_sample'],
                            'repetitions':len(samples),'samples_ms':samples,'median_ms':statistics.median(samples)}
                if partial: evidence['sdpa_out_only_timing'] = measured
                else: timing = measured
    except Exception as error:
        status, reason = 'failed', {'code':type(error).__name__,'message':str(error)}
        evidence['traceback'] = traceback.format_exc()
    after = [describe_input(item,tensors[item['name']]) for item in case['inputs']]
    if after != inputs:
        status, reason, timing = 'failed', {'code':'INPUT_MUTATION','message':'baseline mutated materialized inputs'}, None
    evidence.update(correctness={'status':status,'reason':reason,'checks':checks},outputs=output_meta,
                    samples_ms=samples,input_unchanged=after==inputs,
                    imported_vendor_modules=[name for name in ['flag_gems','triton'] if name in sys.modules])
    evidence_path = args.output.with_suffix('.evidence.json'); write_json(evidence_path,evidence)
    m = {'case_id':case['id'],'case_contract_sha256':digest({'case':case,
         'operator':{k:op[k] for k in ['id','call','returns']},'target':spec['suite']['target'],'policies':spec['policies']}),
         'inputs_sha256':digest({'inputs':inputs,'attrs':case['attrs']}),'inputs':inputs,'attrs':case['attrs'],
         'environment_id':env['id'],'recorded_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
         'correctness':{'status':status,'reason':reason,'checks':checks},'timing':timing,
         'artifacts':[{'path':evidence_path.name,'sha256':file_hash(evidence_path),'size_bytes':evidence_path.stat().st_size}]}
    result = {'$schema':'../../../../../../schemas/baseline_results.schema.json','schema_version':'1.0.0',
              'purpose':'measured','suite_id':spec['suite']['id'],'baseline_id':entry['id'],
              'implementation_sha256':evidence['implementation_sha256'],'environments':[env],'measurements':[m]}
    write_json(args.output,result)
    print(json.dumps({'op':op['id'],'status':status,'median_ms':None if timing is None else timing['median_ms'],
                      'sdpa_out_only_ms':evidence.get('sdpa_out_only_timing',{}).get('median_ms'),'reason':reason}),flush=True)
    return 1 if status == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit('Run a per-operator baseline module, for example abs.py')
