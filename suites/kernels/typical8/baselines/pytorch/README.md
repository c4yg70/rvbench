# PyTorch 原生 baseline

本目录为 typical8 的公开 C01 提供可运行代码、运行命令和 HUMAN-K3 的真实采样。输入完全按 spec 的 shape、dtype、布局、seed 和属性生成；baseline 不导入 FlagGems 或 Triton，不做隐式 FP16 → FP32 执行回退。

每个算子都有独立源码文件，其中 `baseline()` 直接调用 PyTorch 原生接口。suite 中对应条目的 `code.files` 固定该文件摘要，`run.argv` 直接运行该文件。

| 算子 | 基准源码 | 原生调用 |
|---|---|---|
| abs | [abs.py](abs.py) | `torch.abs(x)` |
| aminmax | [aminmax.py](aminmax.py) | `torch.aminmax(x, dim=dim, keepdim=keepdim)` |
| softmax | [softmax.py](softmax.py) | `torch.ops.aten._softmax.default(x, dim, half_to_float)` |
| mv | [mv.py](mv.py) | `torch.mv(matrix, vector)` |
| mm | [mm.py](mm.py) | `torch.mm(a, b)` |
| conv2d | [conv2d.py](conv2d.py) | `torch.nn.functional.conv2d(...)` |
| topk | [topk.py](topk.py) | `torch.topk(...)` |
| _flash_attention_forward | [flash_attention_forward.py](flash_attention_forward.py) | `torch.nn.functional.scaled_dot_product_attention(...)`，仅 out |

例如 mm 的基准代码就是：

```python
import torch

def baseline(a, b):
    return torch.mm(a, b)
```

共用的 `runner.py` 只负责绑定输入、计时和证据采集，执行每个源码文件传入的 `baseline()`。数值检查可使用独立的 PyTorch 组合，例如 amin/amax、sort/gather；这些检查在计时范围外。

计时使用官方 [torch.utils.benchmark.Timer](https://docs.pytorch.org/docs/2.8/benchmark_utils.html)，固定 `num_threads=4`、CPU 8–15、`torch.inference_mode()`、5 次显式预热、20 个单次调用样本。板上 2.8.0 的 `Timer.timeit(1)` 每次额外预热 2 次，因此记录 warmup=45；Timer 源码和原始样本保存于证据。完整协议见 [timing-contract.json](timing-contract.json)。

PyTorch [SDPA](https://docs.pytorch.org/docs/2.8/generated/torch.nn.functional.scaled_dot_product_attention.html) 只返回 out。它的 BSHD/BHSD 适配及 contiguous 输出拷贝在单独测量内，但不会冒充 `_flash_attention_forward` 的五返回项合同；该用例的正式 timing 为 null，SDPA 采样保存在 evidence 中。

## 运行

使用板上已有 PyTorch venv，先注册 AI CPU 簇，再按 spec 中的 baseline ID 和 case ID 调用。例如当前板端测试目录：

```bash
bash -c '
  echo $$ > /proc/set_ai_thread
  source /home/c4y/autoke/scripts/run_env.sh
  cd /home/c4y/autoke/rvbench-pytorch-per-op-20261016/baselines/pytorch
  OMP_NUM_THREADS=4 exec python abs.py \
    --spec /home/c4y/autoke/rvbench-pytorch-per-op-20261016/spec.json \
    --case-id typical8.abs.C01 --baseline-id pytorch_abs \
    --output /home/c4y/autoke/rvbench-pytorch-per-op-20261016/baselines/pytorch/results/abs-rerun.json
'
```

runner 校验代码、计时合同、PyTorch 版本和 CPU 亲和，独占既有 benchmark 锁；生产队列或作业非空时退出。采集不通过现有 evald，也不覆盖安装树。输出正式 `baseline-results` 与同名 `.evidence.json`，包含实际输入内容摘要、输出描述、检查、环境快照、Timer 源码和 PyTorch 构建配置。

输入物化、哈希、检查、预热、序列化均在计时范围外；算子调用、输出分配/释放属于计时范围。所有 case 使用同一次物化输入完成本次检查与采样，并在结束后重新核对输入未被修改。

## 校验和状态

2026-10-16，HUMAN-K3（10.18.60.100），PyTorch `2.8.0+spacemit.0`；每项 20 个真实单次调用样本，单位 ms：

| 算子 | 中位延迟 | 正确性状态 | 记录 |
|---|---:|---|---|
| abs | 0.712987 | passed：精确值 | [结果](results/abs.json) |
| aminmax | 13.599102 | passed：min/max 精确值 | [结果](results/aminmax.json) |
| softmax | 21.685550 | unverifiable：数值合同 pending | [结果](results/softmax.json) |
| mv | 0.476914 | unverifiable：数值合同 pending | [结果](results/mv.json) |
| mm | 113.948773 | unverifiable：数值合同 pending | [结果](results/mm.json) |
| conv2d | 4907.431731 | unverifiable：数值合同 pending | [结果](results/conv2d.json) |
| topk | 51.943361 | passed：值选择、索引范围/唯一性/gather | [结果](results/topk.json) |
| _flash_attention_forward | — | unverifiable：SDPA 只有 out；完整接口未实现 | [结果](results/flash_attention_forward.json) |

SDPA 的 out-only 中位延迟为 139.045038 ms，见 [独立证据](results/flash_attention_forward.evidence.json)。该数字不能与五返回项 Flash 实现直接比较。FP16 Conv2D 的原生 CPU 路径单次约 4.9 秒，完整预热及采样耗时较长，记录保留了原始 dtype。

```sh
rvbench validate spec suites/kernels/typical8/spec.json
rvbench validate baseline-results suites/kernels/typical8/baselines/pytorch/results/abs.json --spec suites/kernels/typical8/spec.json
```

`purpose=measured` 表示数据来自真机，不等于数值合同已通过。Abs、Aminmax 和 TopK 执行其既有精确/选择语义检查；Softmax、Mv、Mm 和 Conv2D 的数值容差尚未裁决，记录真实延迟并保持 `correctness=unverifiable`。Attention 的五返回项尚未接入，保留未验证结果及独立 SDPA 测量。suite 仍为 draft，FlagGems 候选的 pending_probe 保留。

`test_runner.py` 在板上通过 `python -m unittest test_runner -v` 运行，覆盖 8 个独立基准模块可导入、采集器确实调用模块传入的函数、实际 dtype/seed、strided 布局、未知 generator 拒绝、调用绑定与 Attention 部分接口的状态。
