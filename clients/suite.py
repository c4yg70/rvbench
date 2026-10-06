"""suite-v1 评测集（Suite）：一次成批评测的算子清单 → 展开为 ticket 落盘。

schema 校验正本在本模块（validate/load）；ticket 字段口径对齐
eval_pipeline._synth_relay_ticket/_perf_ticket_from 先例（kind/op/shape/dtype/attempt/
candidate_*/incumbent_timing/is_base_incumbent/payload_hint）。展开出的票进常规
tickets/ 目录，完全复用既有 scan/submit/finalize 链路——suite 不进服务端。
"""
import argparse
import ast
import functools
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "suite-v1"
_SELF = Path(__file__).resolve().parent
# shape 允许 "framework" 符号值或逗号数字串（口径同 eval_pipeline.RE_SHAPE_OK，服务端终审）
RE_SHAPE = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
RE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")  # suite 名进 stem/文件名，须文件名安全
RE_OP = re.compile(r"^[A-Za-z0-9_]+$")      # op 进路径段与 deploy_path，须模块名安全
LANE_KIND = {"timing": "timing-final", "correctness": "correctness", "func": "func"}


class SuiteError(ValueError):
    """suite 文档/参数非法；消息含 ops 条目序号。"""


# ---- backends 档案（先试 import，未落地则回退） --------------------------------
# TODO(backends): clients/backends.py 尚不存在（并行开发中）。落地后主线统一改走
# from backends import PROFILES：合法名单取 PROFILES 键、warmup/rep 取档案缺省。
try:
    sys.path.insert(0, str(_SELF))
    from backends import PROFILES as _PROFILES  # type: ignore
except ImportError:
    _PROFILES = None
_FALLBACK_BACKENDS = ("k3", "k1", "sg2042", "rvv_general")


def backend_names() -> set:
    """合法 backend 名：优先 backends.PROFILES 键，回退硬编码四名。"""
    return set(_PROFILES) if _PROFILES is not None else set(_FALLBACK_BACKENDS)


def _profile_params(backend: str) -> tuple:
    """该档案下将生效的 (warmup, rep)。回退 (5,5) 与 build_body 当前硬编码一致。"""
    if _PROFILES is not None:
        p = _PROFILES[backend]
        get = (lambda k, d: p.get(k, d)) if isinstance(p, dict) else \
              (lambda k, d: getattr(p, k, d))
        return int(get("warmup", 5)), int(get("rep", 5))
    return 5, 5


# ---- STOCHASTIC 单一正本 -------------------------------------------------------
# 不能 import eval_pipeline：其模块级语句读 config.json 并强校验 baseline_manifest
# （eval_pipeline:46/:51-56），工作区不就位即 SystemExit。改用 ast 抽取集合字面量
# ——正本仍只有 eval_pipeline.py 一份，这里不复制集合。
@functools.lru_cache(maxsize=1)
def _stochastic() -> frozenset:
    tree = ast.parse((_SELF / "eval_pipeline.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and \
                any(isinstance(t, ast.Name) and t.id == "STOCHASTIC" for t in node.targets) \
                and isinstance(node.value, ast.Set):
            return frozenset(e.value for e in node.value.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str))
    raise SuiteError("eval_pipeline.py 里找不到 STOCHASTIC 集合（上游改名？）")


# ---- schema 校验 ---------------------------------------------------------------
_TOP_KEYS = {"schema", "name", "backend", "title", "defaults", "ops", "notes"}
_DEFAULT_KEYS = {"warmup", "rep", "timeout_min"}
_OP_KEYS = {"op", "shape", "dtype"}


def _is_pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def validate(doc) -> list:
    """校验 suite-v1 文档，返回错误清单（空 = 合法）。"""
    if not isinstance(doc, dict):
        return ["文档必须是 JSON object"]
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema 必须是 \"{SCHEMA}\"，实为 {doc.get('schema')!r}")
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        errs.append("name 必须是非空字符串")
    elif not RE_NAME.fullmatch(name):
        errs.append(f"name {name!r} 含文件名不安全字符（只许 [A-Za-z0-9._-]）")
    backend = doc.get("backend")
    if not isinstance(backend, str) or not backend:
        errs.append("backend 必须是非空字符串")
    elif backend not in backend_names():
        errs.append(f"backend {backend!r} 不在档案名单 {sorted(backend_names())} 内")
    for k in ("title", "notes"):
        if k in doc and not isinstance(doc[k], str):
            errs.append(f"{k} 若给出必须是字符串")
    for k in sorted(set(doc) - _TOP_KEYS):
        errs.append(f"顶层多余键 {k!r}（只许 {sorted(_TOP_KEYS)}）")
    if "defaults" in doc:
        d = doc["defaults"]
        if not isinstance(d, dict):
            errs.append("defaults 必须是 object")
        else:
            for k, v in d.items():
                if k not in _DEFAULT_KEYS:
                    errs.append(f"defaults.{k} 是多余键（只许 {sorted(_DEFAULT_KEYS)}）")
                elif not _is_pos_int(v):
                    errs.append(f"defaults.{k} 必须是 >=1 的整数，实为 {v!r}")
    if "ops" not in doc or not isinstance(doc["ops"], list) or not doc["ops"]:
        errs.append("ops 必须是非空数组")
        return errs
    seen = set()
    for i, it in enumerate(doc["ops"]):
        if not isinstance(it, dict):
            errs.append(f"ops[{i}] 必须是 object")
            continue
        if set(it) != _OP_KEYS:
            errs.append(f"ops[{i}] 必须恰含 op/shape/dtype 三键，实为 {sorted(it)}")
            continue
        op = it["op"]
        if not isinstance(op, str) or not RE_OP.fullmatch(op):
            errs.append(f"ops[{i}].op {op!r} 须为模块名安全串（[A-Za-z0-9_]）")
        elif op in seen:
            errs.append(f"ops[{i}].op {op!r} 重复——stem 不含 shape，同 op 会互相覆盖工单")
        seen.add(op if isinstance(op, str) else "")
        shape = it["shape"]
        if shape != "framework" and \
                not (isinstance(shape, str) and RE_SHAPE.fullmatch(shape)):
            errs.append(f"ops[{i}].shape {shape!r} 须为 \"framework\" 或逗号数字串")
        if not isinstance(it["dtype"], str) or not it["dtype"]:
            errs.append(f"ops[{i}].dtype 必须是非空字符串")
    return errs


def load(path) -> dict:
    """读入 suite JSON 并校验；非法抛 SuiteError（消息含条目序号）。"""
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise SuiteError(f"读不了 {p}: {e}") from e
    except json.JSONDecodeError as e:
        raise SuiteError(f"{p} 不是合法 JSON: {e}") from e
    errs = validate(doc)
    if errs:
        raise SuiteError(f"{p} 不是合法 suite-v1 文档:\n  " + "\n  ".join(errs))
    return doc


def _atomic_write(path: Path, obj) -> None:
    """与 eval_pipeline.atomic_write 同语义（tmp + os.replace 原子落盘）。
    不直接 import——该模块 import 有副作用（见 _stochastic 注）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---- 展开 ----------------------------------------------------------------------
def expand(doc, wave_root, cond="suite", lane="timing", attempt=1, dry_run=False) -> list:
    """每条 op 合成一张 ticket 落盘 wave_root/cond/op/tickets/{stem}.ticket.json，
    stem=f"{suite名}--{op}--r{attempt}"（避开既有 _r1/_r2 stem）。is_base_incumbent=True
    即不带候选文件评估当前部署代码（build_body 对 base incumbent 不带 files）。
    返回逐项 {stem, op, path, skipped, warmup, rep, ticket}：warmup/rep 为将生效值
    （doc.defaults 优先，否则取档案缺省）供日志，不进 ticket（服务端按 profile 定）；
    op 目录已有同 stem 的 results/{stem}.raw.json 则 skipped（幂等重跑，不再落盘）；
    dry_run=True 只返回不落盘。"""
    if lane not in LANE_KIND:
        raise SuiteError(f"lane 须为 {'/'.join(LANE_KIND)} 之一，实为 {lane!r}")
    errs = validate(doc)
    if errs:
        raise SuiteError("文档非法: " + "; ".join(errs))
    kind = LANE_KIND[lane]
    st = _stochastic()
    d = doc.get("defaults") or {}
    pw, pr = _profile_params(doc["backend"])
    warmup, rep = d.get("warmup", pw), d.get("rep", pr)
    timeout_min = d.get("timeout_min", 30)
    root = Path(wave_root)
    out = []
    for it in doc["ops"]:
        op, stem = it["op"], f"{doc['name']}--{it['op']}--r{attempt}"
        notes = f"suite {doc['name']} {lane} lane r{attempt}"
        if op in st:  # 随机算子要查统计量，走 needs_stats_check（口径单一正本）
            notes += " | stochastic: needs_stats_check"
        tk = {"kind": kind, "op": op, "shape": it["shape"], "dtype": it["dtype"],
              "attempt": attempt, "candidate_sha256": None, "candidate_path": None,
              "incumbent_timing": None, "is_base_incumbent": True,
              "payload_hint": {"deploy_path": f"flag_gems/ops/{op}.py",
                               "timeout_min": timeout_min, "notes": notes}}
        tpath = root / cond / op / "tickets" / f"{stem}.ticket.json"
        skipped = (root / cond / op / "results" / f"{stem}.raw.json").exists()
        if not skipped and not dry_run:
            _atomic_write(tpath, tk)
        out.append({"stem": stem, "op": op, "path": tpath, "skipped": skipped,
                    "warmup": warmup, "rep": rep, "ticket": tk})
    return out


def from_baseline(manifest_path, *, name, backend="k3") -> dict:
    """baseline_manifest.json（{title, created, jobs:[{op,shape,dtype}]}）→ suite-v1 文档。"""
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    doc = {"schema": SCHEMA, "name": name, "backend": backend,
           "ops": [{k: j[k] for k in ("op", "shape", "dtype")} for j in raw["jobs"]]}
    if raw.get("title"):
        doc["title"] = raw["title"]
    errs = validate(doc)
    if errs:
        raise SuiteError("转换结果非法: " + "; ".join(errs))
    return doc


# ---- CLI -----------------------------------------------------------------------
def _cli() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="suite-v1 评测集工具（validate/expand/from-baseline）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="校验 suite 文档")
    p.add_argument("suite")
    p = sub.add_parser("expand", help="展开为 ticket 落盘（复用 scan/submit/finalize 链路）")
    p.add_argument("suite")
    p.add_argument("--wave", required=True, help="wave 根目录（runs/… 语义）")
    p.add_argument("--cond", default="suite")
    p.add_argument("--lane", default="timing", choices=sorted(LANE_KIND))
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--dry-run", action="store_true", help="只预览不落盘")
    p = sub.add_parser("from-baseline", help="baseline_manifest.json → suite-v1 文档")
    p.add_argument("manifest")
    p.add_argument("--name", required=True)
    p.add_argument("--backend", default="k3")
    p.add_argument("-o", "--out", help="输出路径（缺省打印 stdout）")
    a = ap.parse_args()
    try:
        if a.cmd == "validate":
            doc = load(a.suite)
            print(f"OK：{doc['name']}（backend={doc['backend']}）共 {len(doc['ops'])} 条 op")
            return 0
        if a.cmd == "expand":
            doc = load(a.suite)
            items = expand(doc, a.wave, cond=a.cond, lane=a.lane, attempt=a.attempt,
                           dry_run=a.dry_run)
            n_st = sum(1 for x in items
                       if "stochastic" in x["ticket"]["payload_hint"]["notes"])
            print(f"{doc['name']} lane={a.lane} cond={a.cond} r{a.attempt}：共 {len(items)} 张"
                  f" ticket（stochastic {n_st}），warmup/rep={items[0]['warmup']}/"
                  f"{items[0]['rep']}{'，dry-run 预览：' if a.dry_run else '，落盘：'}")
            for x in items:
                tag = "skip(raw 已在)" if x["skipped"] else ("dry-run" if a.dry_run else "已写")
                print(f"  [{tag}] {x['path']}")
            return 0
        doc = from_baseline(a.manifest, name=a.name, backend=a.backend)
        if a.out:
            _atomic_write(Path(a.out), doc)
            print(f"已写 {a.out}（{len(doc['ops'])} 条 op，backend={doc['backend']}）")
        else:
            print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    except (SuiteError, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"[suite] 失败：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
