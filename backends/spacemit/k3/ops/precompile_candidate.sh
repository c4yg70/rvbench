#!/bin/bash
# X100 预编译（ADR-0012 / t4-runbook §8.2）：候选先在 X100 编译写共享 TRITON 缓存
# （~/.triton/cache，与 evald 作业同缓存、同 run_env 口径），随后 evald 作业（A100）
# 热缓存执行——cache key 按 kernel 源码 hash、与部署路径无关
# （实证：results/panel/l1-offload-probe-20260909.md）。
#
# 用法: bin/precompile_candidate.sh <候选文件本地路径> <op> <shape> <dtype> [--keep] [--no-fallback] [--board NAME]
# 例  : bin/precompile_candidate.sh results/conditions/full/exp/candidates/r1.py exp 64,512,512 float32
#
# 流程: scp 候选+驱动到板上 ~/autoke/tmp_precompile/ → X100 默认调度（不注册 A100、
# 不碰 /tmp/k3_bench.lock）临时 python 进程：import 候选模块 → 按 bench_op 直调层
# 顺序解析入口（<op>/<op>_func/<op>_forward/<op>_run/<op>_launch）→ 构造目标
# shape/dtype 输入 → use_gems() 下调用一次触发 JIT → 打印 PRECOMPILE_OK/FAIL 与
# 缓存条目增量。复杂入口驱动失败时回退 bench_op.py --warmup 0 --rep 1
# --candidate-dir（flock 计时锁内；需板上 bench_op.py 已同步本仓库版本）。
#
# 输出契约: PRECOMPILE_OK <op> ... / PRECOMPILE_FAIL <op> <原因> / PRECOMPILE_JSON {...}
# 退出码  : 0=OK 或 FAIL（FAIL 不阻塞主流程——调用方决定是否仍提交作业，grep 输出区分）
#           2=用法错误；4=板不可达/基础设施错误
#
# ⚠ 2026-09-20 收拢注记（spec-20260920）：本脚本板端传输仍走 bin/k3ssh/k3scp——
# 该通道 2026-09-09 起已废弃（ssh.exe 失效，正本通道＝python bin/pssh.py，见
# 实验环境.md §0），k3ssh/k3scp 已删除。下次使用前须先迁移到 pssh 传输。
set -euo pipefail
cd "$(dirname "$0")/../.."   # 工作区根（原 bin/ 位置时为 ".."）

BOARD=Home-SpacemitK3
TDIR=/home/c4y/autoke/tmp_precompile
RUN_ENV=/home/c4y/autoke/scripts/run_env.sh
BENCH_OP=/home/c4y/autoke/scripts/bench_op.py

KEEP=0 FALLBACK=1
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1 ;;
    --no-fallback) FALLBACK=0 ;;
    --board) BOARD="${2:?--board 缺参数}"; shift ;;
    -h|--help) grep '^#' "$0" | head -22; exit 0 ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
[ "${#ARGS[@]}" -eq 4 ] || { grep '^#' "$0" | head -10 >&2; exit 2; }
CAND_LOCAL="${ARGS[0]}" OP="${ARGS[1]}" SHAPE="${ARGS[2]}" DTYPE="${ARGS[3]}"

# 注入面收口：所有值进 ssh 命令串前过白名单正则（与 evald 同风格）
[[ "$OP" =~ ^[a-z_][a-z0-9_]{0,47}$ ]] || { echo "FATAL: op 不合法: $OP" >&2; exit 2; }
[[ "$SHAPE" =~ ^[0-9]{1,10}(,[0-9]{1,10}){0,5}$ ]] || { echo "FATAL: shape 不合法: $SHAPE" >&2; exit 2; }
case "$DTYPE" in float32|float16|int32|int64|bool) ;; *) echo "FATAL: dtype 不支持: $DTYPE" >&2; exit 2 ;; esac
[[ -f "$CAND_LOCAL" ]] || { echo "FATAL: 候选文件不存在: $CAND_LOCAL" >&2; exit 2; }
CAND_NAME=$(basename "$CAND_LOCAL")
[[ "$CAND_NAME" =~ ^[A-Za-z0-9._-]+\.py$ ]] || { echo "FATAL: 候选文件名须为 [A-Za-z0-9._-]+.py: $CAND_NAME" >&2; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---- 板上驱动（每次随脚本重新上传，防版本漂移）----
cat > "$TMP/_precompile_driver.py" <<'PYDRIVER_EOF'
#!/usr/bin/env python
"""X100 预编译驱动（板上运行，由 bin/precompile_candidate.sh 生成并 scp）。

独立进程 import 候选模块（顶层模块，非部署）→ 按 bench_op 直调层顺序解析入口 →
构造目标 shape/dtype 输入 → use_gems() 下调用一次触发 triton JIT（写共享缓存）。
FAIL 退出码 0：不阻塞主流程，调用方读输出决定是否仍提交作业。
"""
import importlib.util
import inspect
import json
import os
import re
import sys
import time
from contextlib import nullcontext

if len(sys.argv) != 5:
    sys.stderr.write("usage: _precompile_driver.py <candidate.py> <op> <shape> <dtype>\n")
    raise SystemExit(2)
CAND_PATH, OP, SHAPE_STR, DTYPE_STR = sys.argv[1:5]


def _cache_root():
    return os.environ.get("TRITON_CACHE_DIR") or os.path.expanduser("~/.triton/cache")


def _cache_stat():
    root = _cache_root()
    entries = files = 0
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and not os.path.islink(p):
                entries += 1
                for _d, _dn, fs in os.walk(p):
                    files += len(fs)
    return {"root": root, "entries": entries, "files": files}


def _allowed_cpus():
    try:
        for line in open("/proc/self/status"):
            if line.startswith("Cpus_allowed_list"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _fail(reason):
    print(f"PRECOMPILE_FAIL {OP} {reason}")
    print("PRECOMPILE_JSON " + json.dumps(
        {"ok": False, "op": OP, "candidate": CAND_PATH, "reason": str(reason)[:500]},
        ensure_ascii=False))
    sys.stdout.flush()
    raise SystemExit(0)


def _main():
    t_wall = time.perf_counter()
    if not os.path.isfile(CAND_PATH):
        _fail(f"候选文件不存在: {CAND_PATH}")
    if DTYPE_STR not in ("float32", "float16", "int32", "int64", "bool"):
        _fail(f"dtype 不支持: {DTYPE_STR}")
    if not re.match(r"^[a-z_][a-z0-9_]{0,47}$", OP):
        _fail(f"op 不合法: {OP}")
    try:
        shape = tuple(int(x) for x in SHAPE_STR.split(","))
    except ValueError:
        _fail(f"shape 不合法: {SHAPE_STR}")

    stat0 = _cache_stat()  # 须在 import torch/triton 之前计数

    import torch  # noqa: E402
    import flag_gems  # noqa: E402

    stem = os.path.splitext(os.path.basename(CAND_PATH))[0]
    modname = "k3_precompile_cand_" + re.sub(r"\W", "_", stem)
    spec = importlib.util.spec_from_file_location(modname, CAND_PATH)
    if spec is None or spec.loader is None:
        _fail(f"无法构造 import spec: {CAND_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)

    entry = entry_name = None
    for name in (OP, OP + "_func", OP + "_forward", OP + "_run", OP + "_launch"):
        fn = getattr(mod, name, None)
        if callable(fn):
            entry, entry_name = fn, name
            break
    if entry is None:
        _fail(f"模块内未解析到可调用入口（试过 {OP}/{OP}_func/{OP}_forward/{OP}_run/{OP}_launch）")

    dt = {"float32": torch.float32, "float16": torch.float16, "int32": torch.int32,
          "int64": torch.int64, "bool": torch.bool}[DTYPE_STR]

    def make_input(sh, d):
        if d.is_floating_point:
            return torch.randn(sh, dtype=d)
        if d == torch.bool:
            return torch.rand(sh) > 0.5
        return torch.randint(-100, 100, sh, dtype=d)

    torch.manual_seed(0)
    a = make_input(shape, dt)
    try:
        n_req = sum(1 for p in inspect.signature(entry).parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    except (TypeError, ValueError):
        n_req = 1

    ref_args = None
    gems_ctx = getattr(flag_gems, "use_gems", None)
    t0 = time.perf_counter()
    with (gems_ctx() if callable(gems_ctx) else nullcontext()):
        if n_req <= 1:
            out = entry(a)
            ref_args = (a,)
        elif n_req == 2:
            b = make_input(shape, dt)
            try:
                out = entry(a, b)
                ref_args = (a, b)
            except Exception:  # noqa: BLE001 归约类 dim 参数试探（bench_op 同款）
                out = entry(a, -1)
                ref_args = (a, -1)
        elif n_req == 3:
            cond = torch.rand(shape) > 0.5
            out = entry(cond, a, a)
            ref_args = (cond, a, a)
        else:
            _fail(f"入口必选参数 {n_req} 个 >3，超出驱动能力，走 bench_op 回退")
    call_s = time.perf_counter() - t0

    correct = max_err = None
    try:
        t_op = getattr(torch, OP, None)
        if callable(t_op) and ref_args is not None and isinstance(out, torch.Tensor):
            ref = t_op(*ref_args)
            if isinstance(ref, torch.Tensor) and ref.shape == out.shape:
                rt = at = 5e-2 if dt == torch.float16 else 5e-3
                correct = bool(torch.allclose(out.float(), ref.float(), rtol=rt, atol=at))
                max_err = round(float((out.float() - ref.float()).abs().max()), 6)
    except Exception:  # noqa: BLE001 数值对照尽力而为，不阻塞预编译结论
        pass

    stat1 = _cache_stat()
    rec = {"ok": True, "op": OP, "candidate": CAND_PATH, "entry": entry_name,
           "shape": SHAPE_STR, "dtype": DTYPE_STR, "arity": n_req,
           "call_s": round(call_s, 3), "total_s": round(time.perf_counter() - t_wall, 1),
           "cache_before": stat0, "cache_after": stat1,
           "cache_entries_delta": stat1["entries"] - stat0["entries"],
           "cache_files_delta": stat1["files"] - stat0["files"],
           "cpus_allowed": _allowed_cpus(), "correct": correct, "max_abs_err": max_err}
    print(f"PRECOMPILE_OK {OP} call_s={rec['call_s']} total_s={rec['total_s']} "
          f"cache_entries {stat0['entries']}->{stat1['entries']} (+{rec['cache_entries_delta']}) "
          f"cache_files {stat0['files']}->{stat1['files']} (+{rec['cache_files_delta']}) "
          f"entry={entry_name} cpus={rec['cpus_allowed']}")
    print("PRECOMPILE_JSON " + json.dumps(rec, ensure_ascii=False))
    sys.stdout.flush()


try:
    _main()
except SystemExit:
    raise
except Exception as exc:  # noqa: BLE001 一切异常 -> FAIL（不阻塞主流程）
    _fail(f"{type(exc).__name__}: {exc}")
PYDRIVER_EOF

echo "[precompile] 1/4 上传驱动与候选 -> $BOARD:$TDIR/"
bin/k3ssh "$BOARD" "mkdir -p $TDIR" >/dev/null
bin/k3scp "$TMP/_precompile_driver.py" "$BOARD:$TDIR/_precompile_driver.py" >/dev/null
bin/k3scp "$CAND_LOCAL" "$BOARD:$TDIR/$CAND_NAME" >/dev/null

cache_count() {  # 3 行：cache 根目录 / 顶层条目数 / 文件总数
  bin/k3ssh "$BOARD" "source $RUN_ENV >/dev/null 2>&1; C=\${TRITON_CACHE_DIR:-\$HOME/.triton/cache}; echo \"\$C\"; find \"\$C\" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l; find \"\$C\" -type f 2>/dev/null | wc -l"
}

echo "[precompile] 2/4 X100 驱动候选编译（默认调度，不注册 A100）"
set +e
OUT=$(bin/k3ssh "$BOARD" "source $RUN_ENV >/dev/null 2>&1; timeout 300 python $TDIR/_precompile_driver.py '$TDIR/$CAND_NAME' '$OP' '$SHAPE' '$DTYPE'" 2>&1)
RC=$?
set -e
if [ "$RC" -eq 255 ]; then
  echo "FATAL: 板不可达（ssh rc=255）: $OUT" >&2
  exit 4
fi
echo "$OUT" | grep -E '^PRECOMPILE_(OK|FAIL|JSON)' || true

if echo "$OUT" | grep -q '^PRECOMPILE_OK'; then
  [ "$KEEP" = 1 ] || bin/k3ssh "$BOARD" "rm -f '$TDIR/$CAND_NAME'" >/dev/null
  echo "[precompile] 4/4 完成（driver 路径）"
  exit 0
fi

# ---- 回退：bench_op 直调/框架兜底驱动（warmup 0 rep 1，仅触发编译不计性能）----
if [ "$FALLBACK" = 1 ]; then
  echo "[precompile] 3/4 driver 驱动失败，回退 bench_op --candidate-dir（flock 计时锁内）"
  read -r CROOT FB_E0 FB_F0 <<<"$(cache_count | tr '\n' ' ')"
  set +e
  FB=$(bin/k3ssh "$BOARD" "source $RUN_ENV >/dev/null 2>&1; export PYTHONPATH=$TDIR:\${PYTHONPATH:-}; timeout 300 flock /tmp/k3_bench.lock python $BENCH_OP --op $OP --shape $SHAPE --dtype $DTYPE --warmup 0 --rep 1 --candidate-dir $TDIR" 2>&1)
  FRC=$?
  set -e
  FB_JSON=$(echo "$FB" | grep -E '^\{' | tail -1 || true)
  echo "$FB" | tail -3
  read -r _ FB_E1 FB_F1 <<<"$(cache_count | tr '\n' ' ')"
  if [ "$FRC" -eq 0 ] && [ -n "$FB_JSON" ]; then
    echo "PRECOMPILE_OK $OP (bench_op 回退) cache_entries $FB_E0->$FB_E1 (+$((FB_E1 - FB_E0))) cache_files $FB_F0->$FB_F1 (+$((FB_F1 - FB_F0)))"
    echo "PRECOMPILE_JSON {\"ok\": true, \"path\": \"bench_op_fallback\", \"op\": \"$OP\", \"shape\": \"$SHAPE\", \"dtype\": \"$DTYPE\", \"cache_entries_delta\": $((FB_E1 - FB_E0)), \"cache_files_delta\": $((FB_F1 - FB_F0)), \"bench_op\": $FB_JSON}"
    [ "$KEEP" = 1 ] || bin/k3ssh "$BOARD" "rm -f '$TDIR/$CAND_NAME'" >/dev/null
    echo "[precompile] 4/4 完成（bench_op 回退路径）"
    exit 0
  fi
  echo "PRECOMPILE_FAIL $OP bench_op 回退亦失败 (rc=$FRC)"
else
  echo "PRECOMPILE_FAIL $OP driver 驱动失败且已禁用回退"
fi
[ "$KEEP" = 1 ] || bin/k3ssh "$BOARD" "rm -f '$TDIR/$CAND_NAME'" >/dev/null
exit 0
