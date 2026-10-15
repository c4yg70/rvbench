"""Native PyTorch baseline preserving the suite's half_to_float argument."""
import torch


def baseline(x, dim, half_to_float=False):
    return torch.ops.aten._softmax.default(x, dim, half_to_float)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('softmax', baseline, 'torch.ops.aten._softmax.default'))
