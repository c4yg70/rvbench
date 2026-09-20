#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trace_check.py -- T3 harness-trace 落盘管线本地校验（stdlib only）。

校验对象：runs/<task>/<condition>/<op>/trace.jsonl（每候选一行 JSON）。

检查项（对应 T3 spec 执行内容 3 / ADR-0005 / ADR-0008 / feedback-states V06）：
  1. 必填字段存在且非空（模型身份、任务/条件标识、起点与候选 hash、注入证据引用、
     选取策略与理由、结果状态、evald job_id、成本断点指针、记忆/策略更新指针）。
  2. 枚举合法：result_state ∈ V06 七态；condition / task 白名单。
  3. hash 格式：sha256 十六进制 64 位，或显式 "pending"。
  4. 跨根污染审计：trace 内所有字符串值里的记忆根路径引用必须落在本 run 自己的
     runs/<task>/<condition>/ 前缀下；出现 kope-mem、共享 mem/、其他条件/task
     的 runs/ 路径即 ERROR。
  5. 同 op 候选序完整性（WARN 级）：attempt 序须为 1..N 连续不重复；同一
     trace 内 evald_job_id 不得重复。

退出码：0 = 无 ERROR（WARN 不影响）；1 = 存在 ERROR；2 = 用法/IO 错误。

v1.1（2026-09-09，runs/PILOT 端到端演练缺陷 D2/D3/D4 协调者裁决回写）：
  - D2 条件感知必填：injected_evidence_ids 允许空列表，当且仅当 condition=BARE；
    其余条件仍必须非空。
  - D3 条件感知 hash：strategy_*_sha256 合法值 {64-hex, "pending", "none"}，其中
    "none" 仅限无策略语义条件 {BARE, KOPE, E_ONLY, R_ONLY, LOOSE}；
    FROZEN_MEMORY / FROZEN_STRATEGY 的策略 hash 必须是真实 64-hex（恒等断言语义，
    不接受 "pending"/"none"）。
  - D4 新增 E_MEMORY_ROOT：memory_root 必须是以 runs/ 开头的仓库相对路径
    （禁止裸 mem/ 等非规范形）；runs/<task>/<condition>/ 前缀检查（E_CROSS_ROOT）不变。

用法：
  python tools/trace_check.py --dir results/conditions/full/add       # 单目录（或其子树）
  python tools/trace_check.py --root runs/              # 递归汇总
  python tools/trace_check.py --root runs/ --json       # 机器可读 JSON（stdout）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

TOOL = "trace_check"
TOOL_VERSION = "1.1"

# data/harness-trace.template.json 的 schema 版本（不修改模板本身）
SCHEMA_VERSION = "kelso.harness-trace/1.0"

# results/panel/feedback-states.md：V06 反馈状态机七态（单值枚举）
RESULT_STATES = frozenset({
    "accepted_correctness_and_faster",
    "rejected_correctness",
    "rejected_slower",
    "repaired",
    "rolled_back",
    "timeout",
    "crash",
})

# T3-orchestration.md 条件目录
CONDITIONS = frozenset({
    "BARE", "KOPE", "E_ONLY", "R_ONLY", "G", "LOOSE",
    "FROZEN_MEMORY", "FROZEN_STRATEGY", "FLAT_G", "M",
})

# 任务白名单（PILOT 为端到端演练 run）
TASKS = frozenset({"E1", "E3", "E9", "E10_PASS2", "E4", "E7", "PILOT"})

# 必填字段：(字段名, 期望种类 "str" | "list")
# 模板原生字段照 template；标注 [扩展] 的是编排层新增字段（模板不动，
# 语义见 tools/trace_check.README.md）。
REQUIRED_FIELDS = [
    ("schema_version", "str"),        # 必须 == kelso.harness-trace/1.0
    ("run_id", "str"),                # 模板：run 标识
    ("task", "str"),                  # [扩展] 任务标识，须与目录一致
    ("condition", "str"),             # [扩展] 条件标识，须与目录一致
    ("op", "str"),                    # [扩展] 算子标识，须与目录一致
    ("fixed_model_revision", "str"),  # 模板：模型身份（ADR-0005 口径）
    ("kernel_parent_sha256", "str"),  # 模板：起点 hash
    ("candidate_sha256", "str"),      # 模板：候选 hash
    ("injected_evidence_ids", "list"),  # 模板：注入证据引用（非空列表；v1.1 起 BARE 允许空列表，D2）
    ("selected_strategy_id", "str"),  # 模板：选取策略
    ("strategy_reason", "str"),       # 模板：选取理由
    ("result_state", "str"),          # [扩展] V06 七态结果（单值）
    ("evald_job_id", "str"),          # [扩展] evald 作业号（T3 执行内容 5）
    ("cost_checkpoint_id", "str"),    # [扩展] ADR-0005 成本断点指针
    ("state_update_ref", "str"),      # [扩展] 记忆/策略更新记录指针
    ("memory_root", "str"),           # [扩展] 本 run 记忆根（隔离审计锚点）
]

# 若出现则必须是 object / list 的模板字段（类型检查，不强制非空）
OBJECT_FIELDS = ("outcome", "state_update", "cost", "optional_mask")
LIST_FIELDS = ("external_idea_ids", "experience_case_ids")

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PENDING_MARK = "pending"
TRACE_FILENAME = "trace.jsonl"

# v1.1 条件感知规则（runs/PILOT 演练 D2/D3/D4 裁决，2026-09-09）
BARE_ONLY_EMPTY_EVIDENCE = "BARE"  # D2: 唯一允许 injected_evidence_ids 为空列表的条件
NONE_MARK = "none"  # D3: 无策略条件的 strategy hash 空态常量
NO_STRATEGY_CONDITIONS = frozenset({"BARE", "KOPE", "E_ONLY", "R_ONLY", "LOOSE"})
FROZEN_IDENTITY_CONDITIONS = frozenset({"FROZEN_MEMORY", "FROZEN_STRATEGY"})
STRATEGY_HASH_FIELDS = frozenset({"strategy_before_sha256", "strategy_after_sha256"})
RUNS_ROOT_MARK = "runs/"  # D4: memory_root 的仓库相对路径前缀

# 跨根污染审计模式
KOPE_MARK = "kope-mem"  # 共享 kope-mem 根（任意出现即违规，大小写不敏感）
MEM_ROOT_RE = re.compile(r"(?:^|[^0-9A-Za-z_./\-])mem/", re.IGNORECASE)
RUNS_TOKEN_RE = re.compile(r"runs/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", re.IGNORECASE)


# ---------------------------------------------------------------- utilities

def norm_slash(s: str) -> str:
    return s.replace("\\", "/")


def is_empty_value(value, kind: str) -> bool:
    """必填字段空值判定：None/空串/空列表/bool/dict 视为空。"""
    if value is None or isinstance(value, bool):
        return True
    if kind == "list":
        return not (isinstance(value, list) and len(value) > 0)
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (int, float)):
        return False
    return True  # dict 等意外类型按空处理（另报 E_BAD_TYPE）


def hash_format_ok(value) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(HEX64_RE.match(s)) or s.lower() == PENDING_MARK


def strategy_hash_format_ok(value, condition) -> bool:
    """v1.1（D3）：strategy_*_sha256 的条件感知合法值。

    - FROZEN_MEMORY / FROZEN_STRATEGY：必须真实 64-hex（恒等断言，不接受 pending/none）。
    - 无策略条件 {BARE, KOPE, E_ONLY, R_ONLY, LOOSE}：64-hex / "pending" / "none"。
    - 其余条件（G / FLAT_G / M 等）：64-hex / "pending"。
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    low = s.lower()
    if condition in FROZEN_IDENTITY_CONDITIONS:
        return bool(HEX64_RE.match(s))
    if low == NONE_MARK:
        return condition in NO_STRATEGY_CONDITIONS
    return bool(HEX64_RE.match(s)) or low == PENDING_MARK


def iter_strings(obj, path=""):
    """深度优先产出 (jsonpath, str value)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else str(k)
            yield from iter_strings(v, child)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def derive_path_identity(trace_path: Path):
    """从 .../<task>/<condition>/<op>/trace.jsonl 推 (task, condition, op)。"""
    parents = trace_path.parents
    if len(parents) < 3:
        return None
    return parents[2].name, parents[1].name, parents[0].name


def run_prefix(task: str, condition: str) -> str:
    return norm_slash(f"runs/{task}/{condition}/").lower()


# ---------------------------------------------------------------- checking

class FileReport:
    def __init__(self, path: Path, identity):
        self.path = path
        self.identity = identity  # (task, condition, op) or None
        self.lines = 0
        self.issues: list[dict] = []
        self._seen = set()

    def add(self, severity, line, code, field, message):
        key = (severity, line, code, field, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.issues.append({
            "severity": severity,
            "line": line,
            "code": code,
            "field": field,
            "message": message,
        })

    @property
    def errors(self):
        return sum(1 for i in self.issues if i["severity"] == "ERROR")

    @property
    def warnings(self):
        return sum(1 for i in self.issues if i["severity"] == "WARN")

    def to_json(self):
        return {
            "path": self.path.as_posix(),
            "task": self.identity[0] if self.identity else None,
            "condition": self.identity[1] if self.identity else None,
            "op": self.identity[2] if self.identity else None,
            "lines": self.lines,
            "errors": self.errors,
            "warnings": self.warnings,
            "issues": self.issues,
        }


def check_required_fields(rec, lineno, rep):
    """检查项 1/2/3：必填字段、枚举、schema 版本。返回 (task, condition, op)。"""
    task = condition = op = None

    for field, kind in REQUIRED_FIELDS:
        if field not in rec:
            rep.add("ERROR", lineno, "E_MISSING_FIELD", field,
                    f"required field missing: {field}")
            continue
        value = rec[field]
        if kind == "str" and isinstance(value, (dict, list)):
            rep.add("ERROR", lineno, "E_BAD_TYPE", field,
                    f"expected scalar, got {type(value).__name__}")
            continue
        if is_empty_value(value, kind):
            # v1.1（D2）：injected_evidence_ids 允许空列表，当且仅当 condition=BARE
            if (field == "injected_evidence_ids" and isinstance(value, list)
                    and str(rec.get("condition") or "").strip().upper()
                    == BARE_ONLY_EMPTY_EVIDENCE):
                pass
            else:
                rep.add("ERROR", lineno, "E_EMPTY_FIELD", field,
                        f"required field present but empty: {field}")
                continue

        if field == "schema_version":
            if str(value).strip() != SCHEMA_VERSION:
                rep.add("ERROR", lineno, "E_SCHEMA_VERSION", field,
                        f"expected {SCHEMA_VERSION!r}, got {value!r}")
        elif field == "task":
            task = str(value)
            if task not in TASKS:
                rep.add("ERROR", lineno, "E_BAD_ENUM", field,
                        f"task {task!r} not in {sorted(TASKS)}")
        elif field == "condition":
            condition = str(value)
            if condition not in CONDITIONS:
                rep.add("ERROR", lineno, "E_BAD_ENUM", field,
                        f"condition {condition!r} not in {sorted(CONDITIONS)}")
        elif field == "op":
            op = str(value)
        elif field == "result_state":
            rs = str(value)
            if rs not in RESULT_STATES:
                rep.add("ERROR", lineno, "E_BAD_ENUM", field,
                        f"result_state {rs!r} not in V06 seven states {sorted(RESULT_STATES)}")

    return task, condition, op


def check_hashes(rec, lineno, rep, condition=None):
    """检查项 3：所有 *_sha256 字段（若非 null）做 hash 格式检查。

    strategy_*_sha256 自 v1.1（D3）走条件感知规则（见 strategy_hash_format_ok）。
    """
    for key, value in rec.items():
        if not str(key).endswith("_sha256"):
            continue
        if value is None:
            continue  # 模板大量字段允许 null；必填空缺已由检查项 1 报
        if key in STRATEGY_HASH_FIELDS:
            if not strategy_hash_format_ok(value, condition):
                rep.add("ERROR", lineno, "E_BAD_HASH", key,
                        f"strategy hash must be 64-hex; \"pending\" allowed; "
                        f"\"none\" only for no-strategy conditions "
                        f"{sorted(NO_STRATEGY_CONDITIONS)}; frozen_memory/"
                        f"frozen_strategy require a real 64-hex "
                        f"(condition={condition!r}): {value!r}")
            continue
        if not hash_format_ok(value):
            rep.add("ERROR", lineno, "E_BAD_HASH", key,
                    f"not 64-hex sha256 nor \"pending\": {value!r}")


def check_typed_fields(rec, lineno, rep):
    for key in OBJECT_FIELDS:
        if key in rec and not isinstance(rec[key], dict):
            rep.add("ERROR", lineno, "E_BAD_TYPE", key,
                    f"expected object, got {type(rec[key]).__name__}")
    for key in LIST_FIELDS:
        if key in rec and not isinstance(rec[key], list):
            rep.add("ERROR", lineno, "E_BAD_TYPE", key,
                    f"expected list, got {type(rec[key]).__name__}")


def check_memory_root(rec, lineno, rep, task, condition):
    """memory_root 必须是以 runs/ 开头的仓库相对路径（v1.1 D4），且落在本 run 前缀下。"""
    value = rec.get("memory_root")
    if not (isinstance(value, str) and value.strip()) or not task or not condition:
        return  # 缺失/为空已报过
    norm = norm_slash(value.strip())
    # v1.1（D4）：规范形锚点——禁止裸 mem/ 或其他非 runs/ 相对路径
    if not norm.lower().startswith(RUNS_ROOT_MARK):
        rep.add(
            "ERROR", lineno, "E_MEMORY_ROOT", "memory_root",
            f"memory_root {value!r} must be a repo-relative path beginning "
            f"with runs/ (bare mem/ or other forms forbidden)",
        )
    if not norm.lower().startswith(run_prefix(task, condition)):
        rep.add(
            "ERROR", lineno, "E_CROSS_ROOT", "memory_root",
            f"memory_root {value!r} outside own run prefix "
            f"runs/{task}/{condition}/",
        )


def cross_root_scan(rec, lineno, rep, task, condition):
    """检查项 4：全字符串扫描记忆根引用（kope-mem / 共享 mem/ / 外部 runs 路径）。"""
    prefix = run_prefix(task, condition) if task and condition else None
    for path, value in iter_strings(rec):
        if path == "memory_root":
            continue  # 已由 check_memory_root 专项覆盖
        norm = norm_slash(value)
        low = norm.lower()
        if KOPE_MARK in low:
            rep.add("ERROR", lineno, "E_CROSS_ROOT", path,
                    f"reference to shared kope-mem root: {value!r}")
        if MEM_ROOT_RE.search(norm):
            rep.add("ERROR", lineno, "E_CROSS_ROOT", path,
                    f"reference to shared mem/ root outside runs/: {value!r}")
        if prefix is not None:
            for m in RUNS_TOKEN_RE.finditer(norm):
                token = m.group(0).rstrip("/").lower()
                if not token.startswith(prefix.rstrip("/")):
                    rep.add("ERROR", lineno, "E_CROSS_ROOT", path,
                            f"foreign runs/ path {m.group(0)!r} outside own "
                            f"prefix runs/{task}/{condition}/: {value!r}")


def check_path_identity(rep, task, condition, op):
    """行内 task/condition/op 与目录结构一致性。"""
    if rep.identity is None or not (task and condition and op):
        return
    p_task, p_cond, p_op = rep.identity
    if task != p_task or condition != p_cond or op != p_op:
        rep.add("ERROR", None, "E_PATH_MISMATCH", "task/condition/op",
                f"line identity ({task}, {condition}, {op}) != directory "
                f"identity ({p_task}, {p_cond}, {p_op})")


def check_sequence(records, rep):
    """检查项 5（WARN 级）：attempt 序 1..N 连续；evald_job_id 不重复。"""
    dict_recs = [(ln, r) for ln, r in records if isinstance(r, dict)]

    with_attempt = [(ln, r.get("attempt")) for ln, r in dict_recs if "attempt" in r]
    if with_attempt:
        if len(with_attempt) < len(dict_recs):
            missing = [ln for ln, r in dict_recs if "attempt" not in r]
            rep.add("WARN", None, "W_ATTEMPT_MISSING", "attempt",
                    f"attempt present on some lines only; missing on lines {missing}")
        seq = []
        for ln, val in with_attempt:
            if isinstance(val, bool) or not isinstance(val, int):
                rep.add("WARN", ln, "W_ATTEMPT_TYPE", "attempt",
                        f"attempt must be an integer, got {val!r}")
            else:
                seq.append(val)
        if seq and len(seq) == len(dict_recs):
            expected = set(range(1, len(dict_recs) + 1))
            got = set(seq)
            gaps = sorted(expected - got)
            extras = sorted(v for v in got - expected)
            if gaps:
                rep.add("WARN", None, "W_ATTEMPT_GAP", "attempt",
                        f"attempt sequence has gaps, missing {gaps}")
            if extras:
                rep.add("WARN", None, "W_ATTEMPT_GAP", "attempt",
                        f"attempt values outside 1..{len(dict_recs)}: {extras}")
            counts = Counter(seq)
            dups = sorted(v for v, c in counts.items() if c > 1)
            if dups:
                rep.add("WARN", None, "W_ATTEMPT_DUP", "attempt",
                        f"duplicated attempt number(s): {dups}")
            if seq != sorted(seq):
                rep.add("WARN", None, "W_ATTEMPT_ORDER", "attempt",
                        f"attempt not in file order (line number = attempt "
                        f"order): {seq}")

    job_lines = defaultdict(list)
    for ln, r in dict_recs:
        jid = r.get("evald_job_id")
        if isinstance(jid, (str, int)) and not isinstance(jid, bool):
            job_lines[str(jid).strip()].append(ln)
    for jid, lns in sorted(job_lines.items()):
        if jid and len(lns) > 1:
            rep.add("WARN", None, "W_DUP_JOB", "evald_job_id",
                    f"evald_job_id {jid!r} appears on lines {lns}")


def check_file(trace_path: Path) -> FileReport:
    identity = derive_path_identity(trace_path)
    rep = FileReport(trace_path, identity)
    records = []

    try:
        text = trace_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        rep.add("ERROR", None, "E_IO", None, f"cannot read file: {exc}")
        return rep

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        rep.lines += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            rep.add("ERROR", lineno, "E_LINE_JSON", None,
                    f"invalid JSON: {exc.msg} (col {exc.colno})")
            records.append((lineno, None))
            continue
        if not isinstance(rec, dict):
            rep.add("ERROR", lineno, "E_LINE_NOT_OBJECT", None,
                    f"line must be a JSON object, got {type(rec).__name__}")
            records.append((lineno, None))
            continue
        records.append((lineno, rec))

        task, condition, op = check_required_fields(rec, lineno, rep)
        check_typed_fields(rec, lineno, rep)
        check_hashes(rec, lineno, rep, condition)
        check_memory_root(rec, lineno, rep, task, condition)
        cross_root_scan(rec, lineno, rep, task, condition)
        check_path_identity(rep, task, condition, op)

    if rep.lines == 0:
        rep.add("ERROR", None, "E_EMPTY_FILE", None,
                "trace.jsonl contains no candidate lines")
    check_sequence(records, rep)
    return rep


# ---------------------------------------------------------------- reporting

def collect_targets(base: Path):
    """返回 (targets, error_message)。"""
    if base.is_file() and base.name == TRACE_FILENAME:
        return [base], None
    direct = base / TRACE_FILENAME
    if direct.is_file():
        return [direct], None
    if base.is_dir():
        return sorted(base.rglob(TRACE_FILENAME)), None
    return None, f"path does not exist: {base}"


def aggregate(files: list[FileReport], key_index: int) -> dict:
    """按 (task|condition|op) 聚合。key_index: 0=task 1=condition 2=op。"""
    out = defaultdict(lambda: {"files": 0, "lines": 0, "errors": 0, "warnings": 0})
    for f in files:
        key = f.identity[key_index] if f.identity else "(unknown)"
        out[key]["files"] += 1
        out[key]["lines"] += f.lines
        out[key]["errors"] += f.errors
        out[key]["warnings"] += f.warnings
    return dict(sorted(out.items()))


def build_json(mode, base, files: list[FileReport]):
    total_lines = sum(f.lines for f in files)
    total_errors = sum(f.errors for f in files)
    total_warnings = sum(f.warnings for f in files)
    return {
        "tool": TOOL,
        "version": TOOL_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "base": base.as_posix(),
        "ok": total_errors == 0,
        "summary": {
            "files": len(files),
            "lines": total_lines,
            "errors": total_errors,
            "warnings": total_warnings,
        },
        "by_task": aggregate(files, 0),
        "by_condition": aggregate(files, 1),
        "files": [f.to_json() for f in files],
    }


def format_issue(issue: dict) -> str:
    loc = f"line {issue['line']}" if issue["line"] is not None else "file"
    field = f" field={issue['field']}" if issue["field"] else ""
    return f"  [{loc}] {issue['severity']} {issue['code']}{field}: {issue['message']}"


def print_human(mode, base, files: list[FileReport], stream):
    total_lines = sum(f.lines for f in files)
    total_errors = sum(f.errors for f in files)
    total_warnings = sum(f.warnings for f in files)
    verdict = "PASS" if total_errors == 0 else "FAIL"

    print(f"== {TOOL} {mode}={base.as_posix()} ==", file=stream)
    for f in files:
        ident = ""
        if f.identity:
            ident = f" task={f.identity[0]} cond={f.identity[1]} op={f.identity[2]}"
        print(f"{f.path.as_posix()}{ident}: {f.lines} line(s), "
              f"{f.errors} ERROR, {f.warnings} WARN", file=stream)
        for issue in f.issues:
            print(format_issue(issue), file=stream)
    if not files:
        print("(no trace.jsonl found)", file=stream)

    print(f"-- per-task --", file=stream)
    for task, stat in aggregate(files, 0).items():
        print(f"  {task}: {stat['files']} file(s), {stat['lines']} line(s), "
              f"{stat['errors']}E {stat['warnings']}W", file=stream)
    print(f"RESULT: {verdict} ({total_errors} ERROR, {total_warnings} WARN "
          f"across {len(files)} file(s), {total_lines} line(s))", file=stream)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="T3 harness-trace 落盘管线本地校验（必填字段/枚举/hash/"
                    "跨根污染/候选序）。",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", metavar="DIR",
                       help="校验单目录：目录内直接含 trace.jsonl，或递归其子树")
    group.add_argument("--root", metavar="DIR",
                       help="从 runs 根递归收集所有 trace.jsonl 并汇总")
    parser.add_argument("--json", action="store_true",
                        help="stdout 输出机器可读 JSON（人读摘要转 stderr）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    mode = "dir" if args.dir else "root"
    base = Path(args.dir if args.dir else args.root)
    targets, err = collect_targets(base)
    if err:
        print(f"{TOOL}: error: {err}", file=sys.stderr)
        return 2
    if not targets:
        print(f"{TOOL}: error: no {TRACE_FILENAME} found under {base.as_posix()}",
              file=sys.stderr)
        return 2

    files = [check_file(p) for p in targets]

    if args.json:
        payload = build_json(mode, base, files)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print_human(mode, base, files, sys.stderr)
    else:
        print_human(mode, base, files, sys.stdout)

    return 0 if sum(f.errors for f in files) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
