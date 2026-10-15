"""Native PyTorch baseline for aminmax."""
import torch


def baseline(x, dim=None, keepdim=False):
    return torch.aminmax(x, dim=dim, keepdim=keepdim)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('aminmax', baseline, 'torch.aminmax'))
