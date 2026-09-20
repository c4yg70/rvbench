# fallback_refs —— 框架兜底层 torch 参照注册表（43 op）

规格依据：`results/panel/fallback-reference-plan.md`（A 直构 37 / B 组构 2 / C 不可构 4）；
容差正本：`results/panel/numeric-contract.csv`；shape 口径：`bin/baseline_shapes.yaml`（主口径
`[64,512,512]`，16.7M 元素档）。

- `bin/fallback_refs.py` **只做参照与判定，不含任何计时逻辑**；`bin/bench_op.py` 的挂点在
  B 侧完成后另行接入（挂点伪代码见 §3，可直接抄）。
- 依赖声明（运行期）：读 `../results/panel/numeric-contract.csv`（相对 bin/ 即 repo 根）；
  CSV 缺席（板上路径白名单场景）自动回退内嵌 `_CONTRACT_FALLBACK`（2026-09-08 从该 CSV
  编译，`--selftest` 会校验两者逐值一致）。CSV 变更无需重生成代码。
- **fp16 扩展：只改 CONTRACT，不动注册表**——注册表 43 条的 ref_fn/input_builder 与 dtype
  无关（builder 均接收 dtype 参数），扩 float16 时在 CONTRACT 按 dtype 取值
  （fp16 rtol=1e-3 口径，见 feedback-states 数值口径），或依赖 compare 的回退容差
  （fp32 `(5e-3, 5e-3)` / fp16 `(5e-2, 5e-2)`，与 bench_op `_outputs_match` 同口径）。
- torch 调用习惯：`F.*` 优先（板上 silu/gelu/sigmoid/tanh 的 `torch.` 顶层名被 patch 的坑，
  本批虽不含这四个，但 batch_norm/group_norm/pixel_shuffle/prelu/threshold/margin_ranking_loss
  统一走 `F.*`）。Aten 私有名直调条目（`_chunk_cat`、`_unsafe_masked_index`、
  `_jagged_to_padded_dense_forward`、`_functional_sym_constrain_range`、`_assert_async`）
  均 test 文件本身在用（板上已证可调用），并给出公开组合等价式降级。

## 1. 对外接口

```python
import sys; sys.path.insert(0, "<repo>/bin")
import fallback_refs

fallback_refs.REFS            # dict[str, RefEntry]，43 条
fallback_refs.CONTRACT        # dict[op, (rtol, atol)]，42 条（dropout 为 statistical 不入表）
fallback_refs.build_inputs(op, shape=(64,512,512), dtype=torch.float32, device="cpu")
fallback_refs.compare(op, inputs, gems_out)
```

### RefEntry 字段

| 字段 | 说明 |
|------|------|
| `fn` | torch 参照函数，签名 = input_builder 产出顺序；仅 C 类 `dropout`/`sparse_semi_structured_mm` 为 None |
| `input_builder` | `(shape, dtype, device) -> tuple`，内部 `torch.manual_seed(0)`，fp32 主口径；硬表类条目忽略/特判 shape |
| `gems_fn` | gems 侧闭包覆盖（`fused_adam` 列表包装签名；`dropout` 返回 (out,mask)；`full`/`linspace`/`logspace` dtype/device keyword-only——直调 4 位置参会 TypeError）；缺省 = 挂点 use_gems 重放参照 fn，异常再落四级 resolver |
| `out_adapter` | `(ref_out, gems_out) -> 归一化可比对 (r, g)`；`kthvalue`（只取 values，索引平局不入比对）、`dropout`（取 (out,mask) 的 out），缺省恒等 |
| `mode` | `contract` / `exact` / `no_exception` / `statistical` / `unjudgeable` |
| `reduce_scale_fn` | atol 缩放系数 `fn(inputs)`；仅 `group_norm`（×组内元素数 `(C/G)×∏spatial`）与 `safe_softmax`（×末维），缺省 ×1 |
| `note` | 特判/坑位说明（Aten 私有名直调、硬表取代表等） |

### mode 分布（43 条，`--selftest` 强制校验）

| mode | 条数 | 条目 | 判定语义 |
|------|------|------|----------|
| `contract` | 37 | A 类 36 条（`bitwise_and` 除外）+ `fused_adam` | `CONTRACT[op]` 容差 `allclose(..., equal_nan=True)`；**rtol=atol=0 → NaN-aware torch.equal**；归约缩放类乘 `reduce_scale_fn` |
| `exact` | 2 | `bitwise_and`（int32 位运算）、`resize_output`（纯形状变换） | 定义性 torch.equal，不经容差机制 |
| `no_exception` | 2 | `assert_async`、`make_dep_token` | 参照调用不抛异常 + gems 侧有返回 → `correct=True` |
| `statistical` | 1 | `dropout` | 统计校验（见 §4） |
| `unjudgeable` | 1 | `sparse_semi_structured_mm` | `correct` 恒 None |

### compare(op, inputs, gems_out)

返回 `{"correct": bool|None, "max_abs_err": float|None, "correct_mode": mode}`（参照侧抛异常
等诊断场景附加 `"error"` 字符串，且 `correct=None`——**不误报 incorrect**）：

- NaN 伪报排除：两侧同 NaN 视为相等（0/0 路径 `(r==g) | (isnan(r)&isnan(g))`；容差路径
  `allclose(equal_nan=True)`），单侧 NaN 照常判 False。
- 输出结构递归支持：tensor / 标量(bool,int,float) / tuple/list（chunk、tensor_split、
  split_with_sizes_copy、fused_adam 的列表输出）。
- int/bool 域输出（bitwise_and、compare 类 bool 张量）走逐位 `torch.equal`。
- 非数值对象（dep_token）：恒等（`is`）或 `==` 即可；比较抛异常 → `correct=None`。
- 未注册 op：`KeyError`——挂点侧先查 `op in REFS`（见 §3）。

## 2. 注册表构成（43 条）

- **A 类 37 条一行式**：ref 表达式与 FlagGems `benchmark/test_<op>.py` 的 `torch_op` 逐条一致
  （plan §2 A 类表）。代表例子：`add_relu = lambda a,b: torch.relu(a+b)`、
  `kthvalue = lambda x,k,dim: torch.kthvalue(x,k,dim=dim)`（+out_adapter 取 values）、
  `safe_softmax = lambda x,dim: torch.softmax(x,dim)`（= `_safe_softmax` 稳定语义）。
- **B 类 2 条组构**：
  - `fused_adam`：抄 test_fused_adam.py 内置 torch_op 组合语义
    （`m=β1*m+(1-β1)*g; v=β2*v+(1-β2)*g*g; param-=lr*(m/(1-β1**t))/(sqrt(v/(1-β2**t))+eps)`，
    lr=1e-3, β1=0.9, β2=0.999, eps=1e-8, t=1, amsgrad=False）；返回新 param 不原地改；
    `gems_fn` 对齐 test set_gems 的列表包装签名；builder 每次新建独立输入实例（有副作用）。
    TODO(board)：rtol=1.3e-6 可守与否需上板实测（CSV special_rules）。
  - `resize_output`：`x.reshape([numel])`（target_size 与 numel 恒等 → resize 退化为纯形状
    变换值保留）；test 的 torch_op 是 dummy（=gems 自身），故不用。
- **C 类 4 条**处置见 §4。

## 3. 与 bench_op 的挂点约定（B 完成后的实现者直接抄）

约定：挂点放在 `_framework_bench(op, dtype_str, warmup, rep)` 计时成功返回**之后**的调用侧
（`main()` 拿到 `(lat_t, lat_g)` 后）；参照重放与计时解耦（Generic 族自定义 run 不走基类
`get_latency`，故不在 `_rec` 里抓输出）。JSON 行按 plan §3.4 扩展。

```python
# ---- bin/bench_op.py 框架兜底层挂点伪代码（main() 内 _framework_bench 成功后） ----
import fallback_refs                                   # 与 bench_op 同目录
lat_t, lat_g = _framework_bench(args.op, args.dtype, args.warmup, args.rep)
correct = max_err = cmode = None
if args.op in fallback_refs.REFS:                      # 未注册 op 保持 correct: null（向后兼容）
    try:
        shape = tuple(int(x) for x in _FW_SHAPE.get(args.op, "64,512,512").split(","))
        inputs = fallback_refs.build_inputs(args.op, shape, DTYPES[args.dtype], "cpu")
        entry = fallback_refs.REFS[args.op]
        gems_out = (entry.gems_fn or getattr(flag_gems, args.op))(*inputs)
        res = fallback_refs.compare(args.op, inputs, gems_out)
        correct, max_err, cmode = res["correct"], res["max_abs_err"], res["correct_mode"]
    except Exception as exc:                           # 挂点失败不阻塞计时输出
        sys.stderr.write(f"[bench_op] fallback_refs 挂点失败({exc}): {args.op}\n")
        correct, max_err, cmode = None, None, None
print(json.dumps({
    "op": args.op, "shape": "framework", "dtype": args.dtype,
    "torch_ms": round(lat_t, 4), "gems_ms": round(lat_g, 4),
    "speedup": round(lat_t / lat_g, 4) if lat_g > 0 else None,
    "correct": correct,
    **({} if max_err is None else {"max_abs_err": round(max_err, 6)}),
    **({} if cmode is None else {"correct_mode": cmode}),
    "framework": True,
}))
```

一行摘要：`build_inputs → (entry.gems_fn or getattr(flag_gems, op))(*inputs) → compare →
{correct, max_abs_err, correct_mode} 进 JSON；未注册 op 或挂点异常一律保持 correct=null 不阻塞计时`。

## 4. C 类处置说明

| op | mode | 处置 |
|----|------|------|
| `assert_async` | `no_exception` | 全真（全 1 bool）输入下 `torch._assert_async(x)` 正常返回 None；compare 只验证参照调用不抛异常 → `correct=True`。异常路径（False 触发 assert）按合同不构造用例 |
| `make_dep_token` | `no_exception` | 真实 op 返回 dep_token 内部对象，无数值语义；test 的 torch_op 是占位 `torch.zeros(1)`（计时锚点，禁止入比对）。处置 = 不抛异常且返回结构存在 → `correct=True`。dep_token 构造走 best-effort 私有 API 链（`torch._C._make_dep_token` → `aten.make_dep_token`），均失败时 `correct=None`（不误报） |
| `dropout` | `statistical` | p=0.5, training=True，与参照 RNG 不同不可逐元素比。三检查：① 非零比例 ∈ [0.45,0.55]（零比例）；② 非零值 ∈ 2x 值域集合、③ ≈ 2×对应元素（值域+缩放，sorted 后 searchsorted 最近邻 + 合同浮点比对 (1.3e-6, 1e-4)；fp16 扩展时随 CONTRACT 约定调整）。形状/dtype 不一致直接 False |
| `sparse_semi_structured_mm` | `unjudgeable` | test 的 torch_op = gems 自身（自比）；torch CPU 无 2:4 等价，meta 编码（bool (M,K4) → 2-of-4 mask 位序）未定 → `correct` 恒 None（七态判定只看性能）。TODO(gemm)：B 的 GEMM 结果确认 meta 语义后补组合参照 `(mat1 * mask_expand(mat1_meta)) @ mat2` 转正 |

## 5. CLI 自测

```bash
python bin/fallback_refs.py --list            # 注册表全表 + mode 分布
python bin/fallback_refs.py --selftest        # 无 torch：43 条齐全、mode 37/2/2/1/1、CONTRACT 编译/CSV 逐值一致
python bin/fallback_refs.py --selftest-torch  # 有 torch：43 条 builder+ref 全执行、compare 自洽/失配/NaN/统计管线
```

## 6. 已知 TODO / 风险

- **覆盖缺口（2026-09-08 记）**：`full_like` / `unsqueeze` 在 bench_op 走通用直调层
  （`getattr(flag_gems, op)` 探测命中即可计时＋`_outputs_match` 宽松比对，correct_mode
  为空），不进入框架兜底层、因而绕过本注册表挂点——属可接受行为（直调层已提供数值
  参考）；如需合同容差/correct_mode 口径，须把这两 op 强制路由进框架兜底（直调层排除表）。
- `fused_adam`：rtol=1.3e-6 需上板实测 fp32 误差分布（CSV special_rules TODO(board)）。
- `sparse_semi_structured_mm`：TODO(gemm) 待 meta 语义确认后转正。
- `_jagged_to_padded_dense_forward` 的 offsets dtype 取 int32（板上若报 schema 错误，
  改 `_b_jagged` 的 `.to(torch.int64)` 一处即可）。
- `functional_sym_constrain_range` / `make_dep_token` 依赖 dep_token 私有构造 API；本地
  torch 若无该 API，compare 返回 `correct=None`（不误报 incorrect），上板（2.8.0+spacemit.0）应有。
- 板上 FlagGems pin `75cf3ea55` 与上游 master 的 43 个 test 文件一致性需按 plan §3.5-1 核对。

## 7. 2026-09-08 复验结果与新增发现（22 op 重跑，fallback-verify43-rerun.jsonl）

- 终态 18 True / 2 False / 2 null；仍 null 均计划内：`sparse_semi_structured_mm`
  （unjudgeable）、`functional_sym_constrain_range`（板上 torch 2.8.0+spacemit.0 无
  `_make_dep_token`/`aten.make_dep_token`，dep=None 参照必抛 sym_strides，不误报）。
- **新发现（只报告）**：`chunk_cat`、`jagged_to_padded_dense_forward` 的 gems kernel
  输出形状/结构与 aten 参照不符（contract err=None 即 shape/len mismatch），疑似真 bug 待上游。
- **builder 修正**：`unsafe_masked_index` 旧 builder 的 value/mask/indices 形状不一致，
  use_gems 路由进 gems kernel 后越界 → spine_triton driver segfault（rc=-11，板上两次实证；
  kernel 对不合约束入参不设防，附带报告）。已按 test 语义改为三输入同 shape。
- fused_adam 实际挂载在 `flag_gems.ops._fused_adam`（四级 importlib resolver 命中），
  kernel 返回 5 元组且 out[0] 为原地更新 params；对照组合参照 max_abs_err≈3.7e-9，
  合同 (1.3e-6,1e-4) 可守——原 TODO(board) 解除。
