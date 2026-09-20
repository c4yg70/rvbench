# shims/ — bin/ 兼容垫片母本（2026-09-20 收拢）

正本迁入本仓后，工作区 `bin/` 里的同名入口改为垫片，保证 `python bin/ev.py ...` 等
旧命令行逐字不变。本目录是垫片的**母本**（随仓版本化）；`bin/` 下是部署副本。

改动垫片时改这里的母本，然后在工作区根重新部署：

```bash
cp riscv-flagos-kernel-eval-server/shims/*.py riscv-flagos-kernel-eval-server/shims/*.sh bin/
```

- python 垫片：`runpy.run_path` 直跑正本，并把正本所在目录插到 `sys.path[0]`
  （ev.py 惰性 `from eval_pipeline import ...` 依赖此路径）；作为模块被 import 时空操作。
- shell 垫片：`exec bash` 转发正本，argv 原样透传。
- 清单：ev / eval_pipeline / op_status / recover_inflight（clients）· pssh / precompile /
  evald_selftest_local（ops）· trace_check（tools）· run_baseline_group / run_recall /
  run_verify43_rerun / run_gemm_panel（clients/archive）· precompile_candidate.sh（bash）。
