"""Runnable CPU Abs probe. Output is raw evidence, not baseline-results attestation.

Requires torch in the selected environment. This intentionally supports only the
example's contiguous float32 Abs case and rejects every other contract.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--case-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    op, case = next((op, case) for op in spec['operators'] for case in op['cases']
                    if case['id'] == args.case_id)
    xdef = case['inputs'][0]
    if (op['id'] != 'abs' or len(case['inputs']) != 1 or case['attrs'] != {}
            or xdef['kind'] != 'tensor' or xdef['dtype'] != 'float32'
            or xdef['layout']['kind'] != 'contiguous'
            or xdef['generator']['kind'] != 'torch_cpu_uniform_v1'):
        raise ValueError('probe supports only the declared contiguous float32 Abs contract')
    import torch
    torch.set_num_threads(int(os.environ.get('OMP_NUM_THREADS', '1')))
    gen = xdef['generator']
    generator = torch.Generator(device='cpu').manual_seed(gen['seed'])
    x = torch.rand(xdef['shape'], generator=generator, dtype=torch.float32)
    x = gen['low'] + (gen['high'] - gen['low']) * x
    for _ in range(5):
        torch.abs(x)
    samples = []
    for _ in range(5):
        start = time.perf_counter_ns()
        actual = torch.abs(x)
        samples.append((time.perf_counter_ns() - start) / 1e6)
    correct = bool(torch.equal(actual, torch.where(x < 0, -x, x)))
    evidence = {'probe_only': True, 'case_id': case['id'], 'torch': torch.__version__,
                'shape': list(x.shape), 'dtype': str(x.dtype), 'correct': correct,
                'warmup': 5, 'samples_ms': samples, 'median_ms': statistics.median(samples)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, allow_nan=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
