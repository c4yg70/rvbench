#!/usr/bin/env python
"""result_schema — B2 统一结果视图 result-v2：evald 终态作业 / raw-v1 落盘 → 单一 dict。"""
from __future__ import annotations

import json
from pathlib import Path

RESULT_V2 = "result-v2"
_SOURCE_EVALD = "evald-job-v1"   # evald GET /jobs/<id> 终态 JSON 本身无 schema 字段，客户端标记
_KIND_TO_LANE = {"timing-final": "timing", "correctness": "correctness", "func": "func"}


def _metrics(r) -> dict:
    """计时三件套；correctness 车道/失败作业缺键 → None（B2 核心：None=无统计意义，
    不推断不补齐）。"""
    r = r if isinstance(r, dict) else {}
    return {"torch_ms": r.get("torch_ms"), "gems_ms": r.get("gems_ms"),
            "speedup": r.get("speedup")}


def to_result_v2(job: dict, *, backend: str, host_tag: str, lane: str,
                 meta: dict | None = None) -> dict:
    """evald 终态作业 JSON（request/result/kope_mem/logs，见 evald.Job.to_json）→ v2 视图。

    speedup/torch_ms/gems_ms/correct/max_abs_err 全部允许 None：正确性车道无统计
    意义、跨后端无同簇基线时置 None（B2 核心语义）；数值直接取 job["result"]，
    缺键置 None。stdout_tail/kope_mem 等大块一律不复制。"""
    r = job.get("result") if isinstance(job.get("result"), dict) else {}
    req_ = job.get("request") if isinstance(job.get("request"), dict) else {}
    out = {
        "schema": RESULT_V2, "backend": backend, "host_tag": host_tag, "lane": lane,
        "job_id": job.get("job_id"),
        "op": req_.get("op"), "shape": req_.get("shape"), "dtype": req_.get("dtype"),
        "metrics": _metrics(r), "correct": r.get("correct"), "max_abs_err": r.get("max_abs_err"),
        "error": job.get("error"),
        "duration_s": job.get("duration_s"), "queue_wait_s": job.get("queue_wait_s"),
        # evald 终态作业是 finished_at；k1-proxy 展平作业（adapt_job）只有 ts
        "finished_at": job.get("finished_at") or job.get("ts"),
        "source_schema": _SOURCE_EVALD,
    }
    if meta:
        out["meta"] = meta
    return out


def from_raw_v1(raw: dict) -> dict:
    """raw-v1 落盘（ev.py finalize / eval_pipeline.finalize_ticket 的 *.raw.json）→ v2 视图。

    纯函数，不读文件。backend 缺省 "k3"（raw-v1 时代全部数字产自 K3）；host_tag 取
    raw["evald_host"]（k3 / k3-acc / k1-proxy…原样透传），缺键置 None；lane 由
    raw["kind"] 推断（timing-final→timing；func→func），推断不出置 None。
    finished_at 取 evald_ts（作业 ts 或客户端落盘时刻，raw-v1 里无更精确字段）。
    result_cases/stdout_tail 等大块不复制；工单溯源（condition/attempt/candidate_*）
    收进 meta。"""
    r = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    meta: dict = {"condition": raw.get("condition"), "attempt": raw.get("attempt"),
                  "writer": raw.get("writer")}
    for k in ("candidate_sha256", "candidate_path", "deploy_path", "shape_warn"):
        if raw.get(k) is not None:
            meta[k] = raw[k]
    return {
        "schema": RESULT_V2, "backend": "k3",
        "host_tag": raw.get("evald_host"),
        "lane": _KIND_TO_LANE.get(raw.get("kind")),
        "job_id": raw.get("job_id"), "op": raw.get("op"),
        "shape": raw.get("shape"), "dtype": raw.get("dtype"),
        "metrics": _metrics(r), "correct": r.get("correct"), "max_abs_err": r.get("max_abs_err"),
        "error": raw.get("error"),
        "duration_s": raw.get("duration_s"), "queue_wait_s": raw.get("queue_wait_s"),
        "finished_at": raw.get("evald_ts"),
        "source_schema": "raw-v1", "meta": meta,
    }


def load_result_v2(path: str | Path) -> dict:
    """读 JSON：result-v2 原样返回；raw-v1 走 from_raw_v1；其余 schema 抛 ValueError。"""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = obj.get("schema") if isinstance(obj, dict) else None
    if schema == RESULT_V2:
        return obj
    if schema == "raw-v1":
        return from_raw_v1(obj)
    raise ValueError(f"{path}: 不认识的 schema={schema!r}（只认 {RESULT_V2} / raw-v1）")
