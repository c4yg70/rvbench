"""Native PyTorch baseline for abs."""
import torch


def baseline(x):
    return torch.abs(x)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('abs', baseline, 'torch.abs'))
