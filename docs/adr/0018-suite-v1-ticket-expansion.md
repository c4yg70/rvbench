# 评测集 suite-v1：loader 展开为 ticket，复用 scan/submit/finalize 全链路

算子评测集（suite）定义"一次成批评测哪些算子"：suite-v1 JSON（`clients/suite.py` 为校验正本）＝`{schema, name, backend, defaults{warmup,rep,timeout_min}, ops[{op,shape,dtype}]}`——条目三键与 `results/e1-ops/baseline_manifest.json`（230 op，A100 R2 口径）逐字段同构，`from_baseline()` 可直接把既有基线清单升格为 suite。**suite 不进服务端**：loader `expand()` 把每条目落盘成一张普通 ticket（`<wave>/<cond>/<op>/tickets/<stem>.ticket.json`，stem `{suite}--{op}--r{attempt}`），此后完全复用 eval_pipeline 的 scan/submit/finalize——车道路由、deploy_path 避让、path_lock、registry、relay 链全部继承，零服务端改动（先例 `_synth_relay_ticket`：acc/perf relay 票同样走合成落盘）。缺省吸收纪律：STOCHASTIC 随机算子集**import 自 eval_pipeline 单一正本**（suite 不复制名单，漂移不可能）；warmup/rep 缺省从 backend profile 取（k3=5/5），suite defaults 可覆盖。lane 由 expand 参数指定（timing/correctness/func → kind 映射），stochastic 算子在 payload_hint.notes 标注 needs_stats_check（judge 的既有处理路径）。

被否方案：服务端批量提交 API（Byte-freeze＋队列语义全要动）；suite 进 registry（评测集是输入侧工件，与 incumbent 参照 registry 职责正交，混装会把"跑过什么"和"当前最优是谁"搅在一起）。
