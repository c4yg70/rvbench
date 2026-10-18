"""Native PyTorch baseline for top-k selection."""
import torch


def baseline(x, k, dim=-1, largest=True, sorted=True):
    return torch.topk(x, k, dim=dim, largest=largest, sorted=sorted)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('topk', baseline, 'torch.topk'))
