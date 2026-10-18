"""Native PyTorch baseline for matrix-vector multiplication."""
import torch


def baseline(matrix, vector):
    return torch.mv(matrix, vector)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('mv', baseline, 'torch.mv'))
