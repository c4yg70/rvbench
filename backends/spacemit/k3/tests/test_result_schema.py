# test_result_schema — clients/result_schema.py 单测（B2 result-v2 统一视图）。
# 跑法（仓库根）：python -m unittest discover -s tests -v
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients"))

from result_schema import RESULT_V2, from_raw_v1, load_result_v2, to_result_v2

# evald 终态作业 JSON（字段与 evald.Job.to_json 一致：无顶层 ts，有 finished_at）
JOB_TIMING = {
    "job_id": "j-t1", "seq": 7, "state": "done",
    "submitted_at": "2026-10-07T09:57:00+08:00", "started_at": "2026-10-07T09:57:01+08:00",
    "finished_at": "2026-10-07T09:58:26+08:00",
    "queue_wait_s": 0.3, "duration_s": 85.2,
    "request": {"op": "mm", "shape": "384,512", "dtype": "float32"},
    "result": {"torch_ms": 1.23, "gems_ms": 0.61, "speedup": 2.01, "correct": True,
               "max_abs_err": 0.0, "psr": 16},
    "kope_mem": {"speedup": 2.01, "pass_rate": 1.0},
    "logs": {"stdout_tail": "...", "stderr_tail": ""},
    "error": None,
}
JOB_CORR = {
    "job_id": "j-c1", "state": "done", "finished_at": "2026-10-07T10:10:00+08:00",
    "queue_wait_s": 0.2, "duration_s": 9.5,
    "request": {"op": "add", "shape": "64,512,512", "dtype": "float32", "lane": "correctness"},
    "result": {"correct": True, "max_abs_err": 1e-07, "psr": 2},
    "kope_mem": {"speedup": None, "pass_rate": 1.0},
    "logs": {"stdout_tail": "", "stderr_tail": ""},
    "error": None,
}
JOB_FAIL = {
    "job_id": "j-f1", "state": "failed", "finished_at": "2026-10-07T10:20:00+08:00",
    "queue_wait_s": 0.1, "duration_s": 3.0,
    "request": {"op": "softmax", "shape": "8,4096", "dtype": "float32"},
    "result": None, "kope_mem": None,
    "logs": {"stdout_tail": "boom", "stderr_tail": "traceback"},
    "error": "candidate raised: name 'x' is not defined",
}

# raw-v1 落盘（字段与 ev.py finalize / eval_pipeline.finalize_ticket 实写一致）
RAW_V1 = {
    "schema": "raw-v1", "writer": "ev-v1", "condition": "KOPE", "kind": "timing-final",
    "op": "mm", "shape": "384,512", "dtype": "float32", "attempt": 1,
    "candidate_sha256": "ab" * 32, "candidate_path": "candidates/mm.py",
    "deploy_path": "flag_gems/ops/mm.py",
    "job_id": "j-t1", "state": "done",
    "evald_ts": "2026-10-07T09:58:26+08:00", "evald_host": "k3",
    "queue_wait_s": 0.4, "duration_s": 80.0,
    "shape_sent": "384,512", "shape_warn": None,
    "result": {"torch_ms": 1.2, "gems_ms": 0.6, "speedup": 2.0, "correct": True,
               "max_abs_err": 0.0},
    "kope_mem": {"speedup": 2.0, "pass_rate": 1.0},
    "stdout_tail": "lots of output", "stderr_tail": "", "error": None,
}


class ToResultV2(unittest.TestCase):
    def test_timing_job_full_mapping(self):
        v = to_result_v2(JOB_TIMING, backend="k3", host_tag="k3", lane="timing")
        self.assertEqual(v["schema"], RESULT_V2)
        self.assertEqual((v["backend"], v["host_tag"], v["lane"]), ("k3", "k3", "timing"))
        self.assertEqual(v["job_id"], "j-t1")
        self.assertEqual((v["op"], v["shape"], v["dtype"]), ("mm", "384,512", "float32"))
        self.assertEqual(v["metrics"], {"torch_ms": 1.23, "gems_ms": 0.61, "speedup": 2.01})
        self.assertEqual((v["correct"], v["max_abs_err"]), (True, 0.0))
        self.assertIsNone(v["error"])
        self.assertEqual((v["duration_s"], v["queue_wait_s"]), (85.2, 0.3))
        self.assertEqual(v["finished_at"], "2026-10-07T09:58:26+08:00")   # 顶层 finished_at
        self.assertEqual(v["source_schema"], "evald-job-v1")
        self.assertNotIn("meta", v)                       # meta 缺省不出现
        self.assertNotIn("stdout_tail", v)                # 大块不复制
        self.assertNotIn("kope_mem", v)

    def test_meta_attached_when_given(self):
        meta = {"stem": "ev-120000", "attempt": 2}
        v = to_result_v2(JOB_TIMING, backend="k3", host_tag="k3", lane="timing", meta=meta)
        self.assertEqual(v["meta"], meta)

    def test_correctness_job_none_propagation(self):
        """正确性车道无统计意义：metrics 三件套全 None，correct 照实。"""
        v = to_result_v2(JOB_CORR, backend="k3", host_tag="k3-acc", lane="correctness")
        self.assertEqual(v["metrics"], {"torch_ms": None, "gems_ms": None, "speedup": None})
        self.assertIs(v["correct"], True)
        self.assertEqual(v["max_abs_err"], 1e-07)
        self.assertEqual((v["host_tag"], v["lane"]), ("k3-acc", "correctness"))

    def test_failed_job_none_propagation(self):
        v = to_result_v2(JOB_FAIL, backend="k3", host_tag="k3", lane="timing")
        self.assertEqual(v["metrics"], {"torch_ms": None, "gems_ms": None, "speedup": None})
        self.assertIsNone(v["correct"])
        self.assertIsNone(v["max_abs_err"])
        self.assertIn("not defined", v["error"])
        self.assertEqual(v["op"], "softmax")

    def test_bare_job_all_none(self):
        v = to_result_v2({}, backend="sg2042", host_tag="sg2042", lane="timing")
        self.assertEqual(v["schema"], RESULT_V2)
        for k in ("job_id", "op", "shape", "dtype", "correct", "max_abs_err",
                  "error", "duration_s", "queue_wait_s", "finished_at"):
            self.assertIsNone(v[k], msg=k)
        self.assertEqual(v["metrics"], {"torch_ms": None, "gems_ms": None, "speedup": None})

    def test_k1_proxy_flattened_job_uses_ts_fallback(self):
        """k1-proxy 展平作业（adapt_job）只有顶层 ts，finished_at 兜底取它。"""
        job = dict(JOB_TIMING, finished_at=None, ts="2026-10-07T10:00:00+08:00")
        v = to_result_v2(job, backend="k1", host_tag="k1", lane="timing")
        self.assertEqual(v["finished_at"], "2026-10-07T10:00:00+08:00")


class FromRawV1(unittest.TestCase):
    def test_timing_raw_mapping(self):
        v = from_raw_v1(RAW_V1)
        self.assertEqual(v["schema"], RESULT_V2)
        self.assertEqual(v["backend"], "k3")              # 缺省 k3
        self.assertEqual(v["host_tag"], "k3")             # evald_host 原样
        self.assertEqual(v["lane"], "timing")             # kind=timing-final → timing
        self.assertEqual((v["job_id"], v["op"], v["shape"], v["dtype"]),
                         ("j-t1", "mm", "384,512", "float32"))
        self.assertEqual(v["metrics"], {"torch_ms": 1.2, "gems_ms": 0.6, "speedup": 2.0})
        self.assertEqual(v["finished_at"], RAW_V1["evald_ts"])
        self.assertEqual(v["source_schema"], "raw-v1")
        self.assertEqual(v["meta"]["condition"], "KOPE")
        self.assertEqual(v["meta"]["attempt"], 1)
        self.assertEqual(v["meta"]["writer"], "ev-v1")
        self.assertEqual(v["meta"]["candidate_path"], "candidates/mm.py")
        self.assertNotIn("stdout_tail", v)                # 大块不复制
        self.assertNotIn("result_cases", v)

    def test_acc_raw_lane_and_host_inference(self):
        raw = dict(RAW_V1, kind="correctness", evald_host="k3-acc",
                   result={"correct": True, "max_abs_err": 2e-07})
        v = from_raw_v1(raw)
        self.assertEqual((v["lane"], v["host_tag"]), ("correctness", "k3-acc"))
        self.assertEqual(v["metrics"], {"torch_ms": None, "gems_ms": None, "speedup": None})
        self.assertIs(v["correct"], True)

    def test_func_and_uninferable(self):
        self.assertEqual(from_raw_v1(dict(RAW_V1, kind="func"))["lane"], "func")
        v = from_raw_v1({"job_id": "j-x", "op": "add"})   # kind/evald_host 缺失 → None
        self.assertIsNone(v["lane"])
        self.assertIsNone(v["host_tag"])
        self.assertEqual(v["backend"], "k3")
        self.assertEqual(v["meta"]["writer"], None)


class LoadResultV2(unittest.TestCase):
    def test_v2_passthrough_raw_v1_convert_bad_schema(self):
        with tempfile.TemporaryDirectory() as d:
            v2 = to_result_v2(JOB_TIMING, backend="k3", host_tag="k3", lane="timing")
            p2 = Path(d) / "v2.json"
            p2.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_result_v2(p2), v2)                     # v2 原样
            self.assertEqual(load_result_v2(str(p2)), v2)                # str 路径也行
            p1 = Path(d) / "raw.json"
            p1.write_text(json.dumps(RAW_V1, ensure_ascii=False), encoding="utf-8")
            conv = load_result_v2(p1)                                    # raw-v1 → v2
            self.assertEqual(conv["source_schema"], "raw-v1")
            self.assertEqual(conv["op"], "mm")
            pb = Path(d) / "bad.json"
            pb.write_text(json.dumps({"schema": "nope-v9"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_result_v2(pb)
            pb.write_text("[]", encoding="utf-8")                        # 非 dict 同样拒绝
            with self.assertRaises(ValueError):
                load_result_v2(pb)

    def test_roundtrip_v2_survives_json(self):
        """from_raw_v1 往返：raw-v1 → v2 → JSON 落盘 → load 原样读回。"""
        v = from_raw_v1(RAW_V1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "conv.json"
            p.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_result_v2(p), v)


if __name__ == "__main__":
    unittest.main()
