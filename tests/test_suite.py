"""clients/suite.py 单测：suite-v1 校验正本 + expand 落盘/幂等 + from_baseline + CLI。
跑法：python -m unittest discover -s tests -v（stdlib-only，全部走 tempfile）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from clients import suite  # noqa: E402


def mk_doc(**over):
    doc = {"schema": "suite-v1", "name": "t-suite", "backend": "k3",
           "defaults": {"warmup": 5, "rep": 5, "timeout_min": 30},
           "ops": [{"op": "add", "shape": "64,512,512", "dtype": "float32"},
                   {"op": "_reshape_alias", "shape": "framework", "dtype": "float32"}]}
    doc.update(over)
    return doc


class ValidateTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(suite.validate(mk_doc()), [])
        self.assertEqual(suite.validate(
            {k: v for k, v in mk_doc().items() if k != "defaults"}), [])  # defaults 可缺省

    def test_schema_missing_or_wrong(self):
        for doc in ({k: v for k, v in mk_doc().items() if k != "schema"},
                    mk_doc(schema="suite-v2")):
            self.assertTrue(any("schema" in e for e in suite.validate(doc)))

    def test_bad_backend(self):
        errs = suite.validate(mk_doc(backend="nope"))
        self.assertTrue(any("backend" in e and "nope" in e for e in errs))

    def test_backend_names_from_profiles_or_fallback(self):
        self.assertLessEqual({"k3", "k1", "sg2042", "rvv_general"}, suite.backend_names())

    def test_ops_empty_and_bad_entries(self):
        self.assertTrue(suite.validate(mk_doc(ops=[])))
        bad = [
            {"op": "add", "shape": "64,512,512"},                    # 缺 dtype
            {"op": "add", "shape": "64,512,512", "dtype": "f32", "x": 1},  # 多余键
            {"op": "add", "shape": "64 x 512", "dtype": "float32"},  # 坏 shape
            {"op": "add", "shape": 64, "dtype": "float32"},          # shape 非 str
        ]
        for entry in bad:
            errs = suite.validate(mk_doc(ops=[entry]))
            self.assertEqual([e for e in errs if e.startswith("ops[0]")], errs, entry)

    def test_extra_top_key(self):
        errs = suite.validate(mk_doc(foo=1))
        self.assertTrue(any("foo" in e for e in errs))

    def test_bad_defaults(self):
        self.assertTrue(any("defaults" in e for e in suite.validate(mk_doc(defaults={"warmup": 0}))))
        self.assertTrue(any("defaults" in e for e in suite.validate(mk_doc(defaults={"nope": 1}))))

    def test_dup_op_and_bad_name(self):
        self.assertTrue(any("重复" in e for e in suite.validate(
            mk_doc(ops=[{"op": "add", "shape": "1", "dtype": "float32"}] * 2))))
        self.assertTrue(suite.validate(mk_doc(name="a/b c")))
        self.assertTrue(suite.validate(mk_doc(name="")))

    def test_load(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text(json.dumps(mk_doc()), encoding="utf-8")
            self.assertEqual(suite.load(p)["name"], "t-suite")
            p.write_text(json.dumps(mk_doc(backend="nope")), encoding="utf-8")
            with self.assertRaises(suite.SuiteError) as cm:
                suite.load(p)
            self.assertIn("backend", str(cm.exception))


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.wave = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def expand(self, doc=None, **kw):
        return suite.expand(doc or mk_doc(), self.wave, **kw)

    def test_fields_match_relay_precendent(self):
        items = self.expand(lane="timing", attempt=2)
        exp = {"kind": "timing-final", "op": "add", "shape": "64,512,512",
               "dtype": "float32", "attempt": 2, "candidate_sha256": None,
               "candidate_path": None, "incumbent_timing": None,
               "is_base_incumbent": True,
               "payload_hint": {"deploy_path": "flag_gems/ops/add.py",
                                "timeout_min": 30,
                                "notes": "suite t-suite timing lane r2"}}
        self.assertEqual(items[0]["ticket"], exp)  # 字段逐一对照 _synth_relay_ticket 先例
        self.assertEqual(items[0]["stem"], "t-suite--add--r2")
        self.assertEqual(items[0]["path"],
                         self.wave / "suite" / "add" / "tickets" / "t-suite--add--r2.ticket.json")
        with open(items[0]["path"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), exp)  # 落盘内容与返回一致
        self.assertFalse(items[0]["skipped"])
        self.assertEqual(items[1]["ticket"]["shape"], "framework")  # 符号值原样透传

    def test_lane_kind_mapping(self):
        for lane, kind in (("correctness", "correctness"), ("func", "func")):
            items = self.expand(lane=lane)
            self.assertEqual(items[0]["ticket"]["kind"], kind)
        with self.assertRaises(suite.SuiteError):
            self.expand(lane="perf")

    def test_stochastic_notes_injection(self):
        self.assertIn("dropout", suite._stochastic())  # ast 抽取自 eval_pipeline 单一正本
        doc = mk_doc(ops=[{"op": "dropout", "shape": "framework", "dtype": "float32"},
                          {"op": "add", "shape": "1", "dtype": "float32"}])
        items = self.expand(doc)
        notes = [it["ticket"]["payload_hint"]["notes"] for it in items]
        self.assertTrue(notes[0].endswith(" | stochastic: needs_stats_check"))
        self.assertNotIn("stochastic", notes[1])

    def test_dry_run_writes_nothing(self):
        items = self.expand(dry_run=True)
        self.assertEqual(len(items), 2)
        self.assertEqual(list(self.wave.glob("**/*")), [])  # 未落任何文件

    def test_idempotent_rerun_skips_on_raw(self):
        first = self.expand()
        raw = self.wave / "suite" / "add" / "results" / "t-suite--add--r1.raw.json"
        raw.parent.mkdir(parents=True)
        raw.write_text("{}", encoding="utf-8")
        second = self.expand()
        self.assertTrue(second[0]["skipped"])
        self.assertFalse(second[1]["skipped"])
        self.assertEqual(first[1]["ticket"], second[1]["ticket"])  # 重跑内容不变
        # skipped 项不落盘：ticket 文件保持首跑原样
        self.assertEqual(sorted(p.name for p in self.wave.glob("**/*.ticket.json")),
                         ["t-suite--_reshape_alias--r1.ticket.json", "t-suite--add--r1.ticket.json"])

    def test_defaults_flow(self):
        doc = mk_doc(defaults={"warmup": 7, "rep": 3, "timeout_min": 45})
        it = self.expand(doc)[0]
        self.assertEqual(it["ticket"]["payload_hint"]["timeout_min"], 45)
        self.assertEqual((it["warmup"], it["rep"]), (7, 3))
        plain = self.expand(mk_doc())[0]  # 缺省回退档案值（当前 build_body 硬编码 5/5）
        self.assertEqual(plain["ticket"]["payload_hint"]["timeout_min"], 30)
        self.assertEqual((plain["warmup"], plain["rep"]), (5, 5))

    def test_invalid_doc_rejected(self):
        with self.assertRaises(suite.SuiteError):
            self.expand(mk_doc(backend="nope"))


class FromBaselineTests(unittest.TestCase):
    def test_230_jobs_to_ops(self):
        jobs = [{"op": f"op_{i:03d}", "shape": "64,512,512", "dtype": "float32", "w": i}
                for i in range(230)]
        jobs[0] = {"op": "_reshape_alias", "shape": "framework", "dtype": "float32", "w": 0}
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "baseline_manifest.json"
            mp.write_text(json.dumps({"title": "A100 baseline", "created": "2026-09-09",
                                      "jobs": jobs}), encoding="utf-8")
            doc = suite.from_baseline(mp, name="e1-ops-r2")
        self.assertEqual(len(doc["ops"]), 230)
        self.assertEqual(doc["ops"][0], {"op": "_reshape_alias", "shape": "framework",
                                         "dtype": "float32"})  # 多余键剔除、framework 保留
        self.assertEqual(doc["ops"][5]["shape"], "64,512,512")
        self.assertEqual(doc["title"], "A100 baseline")
        self.assertEqual(suite.validate(doc), [])


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / "clients" / "suite.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", cwd=REPO)

    def test_validate_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            good, bad = Path(td) / "g.json", Path(td) / "b.json"
            good.write_text(json.dumps(mk_doc()), encoding="utf-8")
            bad.write_text(json.dumps(mk_doc(backend="nope")), encoding="utf-8")
            r = self.run_cli("validate", str(good))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("共 2 条 op", r.stdout)
            r = self.run_cli("validate", str(bad))
            self.assertEqual(r.returncode, 2)
            self.assertIn("nope", r.stderr)

    def test_expand_and_from_baseline_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            sp, wave, out = Path(td) / "s.json", Path(td) / "wave", Path(td) / "out.json"
            sp.write_text(json.dumps(mk_doc()), encoding="utf-8")
            r = self.run_cli("expand", str(sp), "--wave", wave, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(list(wave.glob("**/*")), [])
            r = self.run_cli("expand", str(sp), "--wave", wave, "--lane", "func")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((wave / "suite" / "add" / "tickets" /
                             "t-suite--add--r1.ticket.json").is_file())
            mp = Path(td) / "m.json"
            mp.write_text(json.dumps({"jobs": [{"op": "abs", "shape": "1", "dtype": "float32"}]}),
                          encoding="utf-8")
            r = self.run_cli("from-baseline", str(mp), "--name", "n1", "-o", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["schema"], "suite-v1")


if __name__ == "__main__":
    unittest.main()
