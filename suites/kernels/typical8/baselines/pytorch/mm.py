"""Native PyTorch baseline for matrix multiplication."""
import torch


def baseline(a, b):
    return torch.mm(a, b)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('mm', baseline, 'torch.mm'))
