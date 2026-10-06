# test_backends — clients/backends.py 单测（B1 档案 + B3 键/可比性）。
# 跑法（仓库根）：python -m unittest discover -s tests -v
# 注意：eval_pipeline 在 import 时读 config.json 的 token 并要求 baseline_manifest
# 存在（缺失 raise SystemExit）——有副作用、不能假设可导入。因此 cond_key 旧键
# 规则以快照断言为正本（全量无条件运行）；本机恰好可导入时再叠加 live 逐例对照。
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients"))

import backends

try:
    from eval_pipeline import cond_key as _pipe_cond_key   # 有 import 副作用，见文件头
    _HAVE_PIPELINE = True
except SystemExit:      # 缺 baseline_manifest：eval_pipeline 直接 SystemExit（非 Exception 子类）
    _HAVE_PIPELINE = False
except Exception:       # 缺 config.json token 等
    _HAVE_PIPELINE = False

# 覆盖 cond_key 全部 8 个分支 + 默认分支的条件名（含跨波重叠的 r1/E、base）
_LIVE_CONDS = ["KOPE", "kope", "full", "mask_on", "transfer_on",
               "r1/E", "r1/H", "r2/T", "r4/S", "r5/H", "r5/full",
               "r1/F", "r2/R", "r3/S", "_base", "_heldout",
               "r2/E", "r3/H", "retime", "base", "totally_new"]


class RegistryKey(unittest.TestCase):
    def test_k3_snapshot_all_branches(self):
        """快照断言（正本）：registry_key 的 k3 旧格式逐分支等于 cond_key 规则。"""
        cases = {
            ("KOPE", "add"): "KOPE/add",                # E1 默认分支（cond 不进键）
            ("SOMETHING_NEW", "mm"): "KOPE/mm",         # 未收录条件名同落默认分支
            ("kope", "add"): "astra/kope/add",          # 小写 kope 属 astra 臂，勿混 KOPE
            ("full", "mm"): "astra/full/mm",
            ("mask_random", "add"): "astra/mask_random/add",
            ("r1/E", "mm"): "expand_late/r1/E/mm",      # expand_late 最先命中
            ("base", "add"): "expand_late/base/add",
            ("r5/H", "mm"): "astra5/r5/H/mm",
            ("r5/full", "add"): "astra5/r5/full/add",
            ("r1/F", "mm"): "astra6/r1/F/mm",
            ("_heldout", "add"): "astra6/_heldout/add",
            ("r3/E", "mm"): "astra7/r3/E/mm",
            ("retime", "add"): "astra7/retime/add",
        }
        for (cond, op), want in cases.items():
            self.assertEqual(backends.registry_key(cond, op), want, msg=f"{cond}/{op}")

    def test_other_backend_format(self):
        self.assertEqual(backends.registry_key("KOPE", "add", "k1"), "b:k1/KOPE/add")
        self.assertEqual(backends.registry_key("full", "mm", "sg2042"), "b:sg2042/full/mm")
        self.assertEqual(backends.registry_key("KOPE", "add", backend="rvv_general"),
                         "b:rvv_general/KOPE/add")

    @unittest.skipUnless(_HAVE_PIPELINE, "eval_pipeline 不可导入（缺工作区 manifest/config）；快照断言已覆盖")
    def test_live_matches_eval_pipeline_cond_key(self):
        for cond in _LIVE_CONDS:
            for op in ("mm", "add"):
                self.assertEqual(backends.registry_key(cond, op), _pipe_cond_key(cond, op),
                                 msg=f"{cond}/{op}")


class Comparable(unittest.TestCase):
    def test_same_tag_comparable(self):
        self.assertTrue(backends.comparable("k3", "k3"))
        self.assertTrue(backends.comparable("k1", "k1"))

    def test_cross_host_or_missing_not_comparable(self):
        self.assertFalse(backends.comparable("k3", "k3-acc"))   # 同板不同 lane 也不可比
        self.assertFalse(backends.comparable("k3", "k1"))
        self.assertFalse(backends.comparable(None, "k3"))
        self.assertFalse(backends.comparable("k3", None))
        self.assertFalse(backends.comparable("", ""))
        self.assertFalse(backends.comparable(None, None))


class Resolve(unittest.TestCase):
    def test_hit_by_timing_base(self):
        p = backends.resolve("http://10.18.60.100:8788")
        self.assertEqual(p.name, "k3")
        self.assertEqual((p.timing_host_tag, p.corr_host_tag), ("k3", "k3-acc"))
        self.assertEqual(backends.resolve("http://10.18.60.241:8788").name, "k1")

    def test_hit_by_corr_base_alone(self):
        """k3 的 8791 corr 单独给也要认。"""
        p = backends.resolve("http://10.18.60.100:8791")
        self.assertEqual(p.name, "k3")
        self.assertEqual(p.corr_host_tag, "k3-acc")

    def test_corr_base_param_and_k1_single_instance(self):
        self.assertEqual(backends.resolve("http://10.18.60.100:8788",
                                          corr_base="http://10.18.60.100:8791").name, "k3")
        # corr_base=None 档案按"同 timing_base"参与 corr 匹配
        self.assertEqual(backends.resolve("http://10.18.60.241:8788",
                                          corr_base="http://10.18.60.241:8788").name, "k1")

    def test_trailing_slash_ignored(self):
        self.assertEqual(backends.resolve("http://10.18.60.100:8788/").name, "k3")
        self.assertEqual(backends.resolve("http://10.18.60.100:8791//").name, "k3")

    def test_unknown_url_raises_value_error_with_url(self):
        with self.assertRaises(ValueError) as ctx:
            backends.resolve("http://10.0.0.1:9999")
        self.assertIn("http://10.0.0.1:9999", str(ctx.exception))
        with self.assertRaises(ValueError):
            backends.resolve("")


class BaseLabel(unittest.TestCase):
    def test_three_branches(self):
        self.assertEqual(backends.base_label("http://10.18.60.100:8788"), "k3")
        self.assertEqual(backends.base_label("http://10.18.60.100:8788/"), "k3")   # 尾斜杠
        self.assertEqual(backends.base_label("http://10.18.60.100:8791"), "k3-acc")

    def test_fallback_passthrough(self):
        """其余原样返回：未收录 URL 与 k1-proxy 等历史标签透明透传。"""
        self.assertEqual(backends.base_label("http://10.18.60.241:8788"),
                         "http://10.18.60.241:8788")
        self.assertEqual(backends.base_label("k1-proxy"), "k1-proxy")


class Profiles(unittest.TestCase):
    def test_four_profiles_with_expected_fields(self):
        self.assertEqual(set(backends.PROFILES), {"k3", "k1", "sg2042", "rvv_general"})
        k3 = backends.PROFILES["k3"]
        self.assertEqual(k3.timing_base, "http://10.18.60.100:8788")
        self.assertEqual(k3.corr_base, "http://10.18.60.100:8791")
        self.assertEqual((k3.timing_host_tag, k3.corr_host_tag), ("k3", "k3-acc"))
        k1 = backends.PROFILES["k1"]
        self.assertEqual(k1.corr_base, None)                    # k1 单实例
        self.assertEqual((k1.timing_host_tag, k1.corr_host_tag), ("k1", "k1"))
        for name in ("sg2042", "rvv_general"):
            p = backends.PROFILES[name]
            self.assertEqual(p.timing_base, "")                 # 空串=未部署占位
            self.assertEqual(p.corr_base, None)
            self.assertEqual(p.corr_host_tag, f"{name}-acc")
        for p in backends.PROFILES.values():                    # 默认基准参数
            self.assertEqual((p.warmup, p.rep, p.timeout_min), (5, 5, 15))

    def test_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            backends.get("k3").warmup = 9

    def test_get_unknown_lists_available(self):
        self.assertIs(backends.get("k3"), backends.PROFILES["k3"])
        with self.assertRaises(KeyError) as ctx:
            backends.get("nope")
        self.assertIn("sg2042", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
