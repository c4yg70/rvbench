"""Native PyTorch baseline for 2D convolution."""
import torch.nn.functional as F


def baseline(x, weight, bias=None, stride=1, padding=0, dilation=1, groups=1):
    return F.conv2d(x, weight, bias, stride=stride, padding=padding,
                    dilation=dilation, groups=groups)


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('conv2d', baseline, 'torch.nn.functional.conv2d'))
