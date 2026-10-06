#!/usr/bin/env python
"""backends — B1 客户端侧后端档案 + B3 可比性/registry 键口径（正本，stdlib-only）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendProfile:
    name: str                 # k3 | k1 | sg2042 | rvv_general
    timing_base: str          # 计时车道 base URL；空串=未部署占位
    corr_base: str | None     # correctness/func 车道 base；None=同 timing_base
    timing_host_tag: str      # registry host 标签（计时）
    corr_host_tag: str        # registry host 标签（正确性）
    warmup: int = 5
    rep: int = 5
    timeout_min: int = 15
    notes: str = ""


PROFILES: dict[str, BackendProfile] = {
    "k3": BackendProfile(
        name="k3", timing_base="http://10.18.60.100:8788",
        corr_base="http://10.18.60.100:8791",
        timing_host_tag="k3", corr_host_tag="k3-acc",
        notes="生产在役，SpacemiT K3 Milan A100+X100 双簇"),
    "k1": BackendProfile(
        name="k1", timing_base="http://10.18.60.241:8788", corr_base=None,
        timing_host_tag="k1", corr_host_tag="k1",
        notes="SpacemiT K1，规划中；另有 .243/.233 同型板可扩"),
    "sg2042": BackendProfile(
        name="sg2042", timing_base="", corr_base=None,
        timing_host_tag="sg2042", corr_host_tag="sg2042-acc",
        notes="Sophgo SG2042，规划中未布点"),
    "rvv_general": BackendProfile(
        name="rvv_general", timing_base="", corr_base=None,
        timing_host_tag="rvv_general", corr_host_tag="rvv_general-acc",
        notes="通用 RVV 1.0 参考后端（qemu 或 CI 宿主），规划中"),
}


def get(name: str) -> BackendProfile:
    """按名取档案；未知名抛 KeyError（消息里列出可用名）。"""
    if name not in PROFILES:
        raise KeyError(f"未知 backend {name!r}；可用：{'/'.join(PROFILES)}")
    return PROFILES[name]


def resolve(base_url: str, corr_base: str | None = None) -> BackendProfile:
    """按 URL 匹配档案：base_url 命中 timing_base 或 corr_base（k3 的 :8791 corr
    单独给也要认）；corr_base=None 的档案按"同 timing_base"参与 corr 匹配。
    尾随 "/" 不参与比较；匹配不上抛 ValueError（消息含原 URL）。"""
    u = (base_url or "").rstrip("/")
    c = (corr_base or "").rstrip("/")
    for p in PROFILES.values():
        tb = p.timing_base.rstrip("/")
        cb = (p.corr_base or p.timing_base).rstrip("/")
        if (u and u in (tb, cb)) or (c and c == cb):
            return p
    known = ", ".join(f"{n}={p.timing_base or '(未部署)'}" for n, p in PROFILES.items())
    raise ValueError(f"URL 不匹配任何后端档案：base={base_url!r} corr={corr_base!r}"
                     f"；已知档案：{known}")


def base_label(base_url: str) -> str:
    """旧 base_label 的统一正本（clients/eval_pipeline.py 与 clients/ev.py 各自的
    base_label 收敛于此）。":8791" 结尾 → "k3-acc"（K3 X100 分核正确性旁路）；
    恰为 k3 timing_base → "k3"（A100 计时正本）；其余原样返回（k1-proxy 等历史
    标签的兜底，未收录 URL 也透明透传，绝不臆造标签）。"""
    b = str(base_url or "").rstrip("/")
    if b == PROFILES["k3"].timing_base:
        return "k3"
    if b.endswith(":8791"):
        return "k3-acc"
    return str(base_url)


# --- eval_pipeline.cond_key 前缀规则的分量快照（同步点：clients/eval_pipeline.py）---
# eval_pipeline 在 import 时读 config.json 的 token、并要求 baseline_manifest 存在
# （缺失直接 SystemExit）——不能假设可无副作用导入，故 cond 前缀规则快照到此；
# 改 eval_pipeline 的条件表/分支必须同步这里（tests/test_backends.py 在可导入
# 环境下会做 live 逐例对照）。四个 expand/astra 条件表内容有重叠，分支先后序
# 即优先级（expand_late 最先），照抄勿动。
_EXPANDL = ("r1/E", "r1/H", "base")                                    # astra_0911_expand_late
_EXPANDN = ("r1/E", "r1/H", "base")                                    # astra_0911_expand_next
_EXPAND24 = ("r1/E", "r1/H", "base")                                   # astra_0911_expand24
_ASTRA7 = tuple(f"r{r}/{a}" for r in (1, 2, 3) for a in ("E", "H")) + ("base", "retime")
_ASTRA6 = tuple(f"r{r}/{a}" for r in (1, 2) for a in ("F", "R", "S")) + ("_base", "_heldout")
_ASTRA5 = tuple(f"r{r}/{a}" for r in (1, 2, 3, 4) for a in ("E", "H", "T")) + ("r5/H", "r5/full")
_ASTRA = ("full", "kope", "seed_fixed", "zero_fixed", "zero_adaptive",
          "strategy_only", "full_adaptive", "mask_on", "mask_off", "mask_random",
          "transfer_on", "target_local")


def registry_key(cond: str, op: str, backend: str = "k3") -> str:
    """registry 分臂键。backend=="k3" 走旧格式：cond 前缀规则照抄
    eval_pipeline.cond_key（424 个既有键零漂移；默认分支 cond 整个不进键——
    E1-KOPE 历史键就是 KOPE/<op>）。其他 backend 返回 b:<backend>/<cond>/<op>。

    B3 口径：backend + host_tag 两轴合起来即 (backend, lane, op) 三元组——lane
    不单独立键，由 host_tag 承载（k3=计时正本 vs k3-acc=X100 正确性旁路；两 lane
    数字本就不可直比，见 comparable / ADR-0012）。"""
    if backend != "k3":
        return f"b:{backend}/{cond}/{op}"
    if cond in _EXPANDL:
        return f"expand_late/{cond}/{op}"
    if cond in _EXPANDN:
        return f"expand_next/{cond}/{op}"
    if cond in _EXPAND24:
        return f"expand24/{cond}/{op}"
    if cond in _ASTRA7:
        return f"astra7/{cond}/{op}"
    if cond in _ASTRA6:
        return f"astra6/{cond}/{op}"
    if cond in _ASTRA5:
        return f"astra5/{cond}/{op}"
    if cond in _ASTRA:
        return f"astra/{cond}/{op}"
    return f"KOPE/{op}"


def comparable(host_a: str | None, host_b: str | None) -> bool:
    """仅同 host 标签可比（两侧均非空且同串才 True）。口径红线：跨后端/跨 host
    数字永不直比（同 op 不同机器/分核实测 3–40% 系统差，ADR-0012）；failover
    只许同 host 标签内进行。None/空串一律不可比。"""
    return bool(host_a) and host_a == host_b
