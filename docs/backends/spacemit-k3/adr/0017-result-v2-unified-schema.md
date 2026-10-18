# 统一结果 schema result-v2：一切指标可 null

raw-v1（`{stem}.raw.json`，ev.py/eval_pipeline 落盘）隐含"speedup 总有值"——单后端计时作业成立，多后端化后系统性破坏：正确性/func 车道 speedup 无统计意义、跨后端无同簇基线时比值无定义、gemm 车道主指标是 gflops、多 shape 批量顶层刻意不放 speedup 标量（防老驱动只看第一个 shape 的静默错判，evald `_map_rows` 既有纪律）。result-v2（`clients/result_schema.py`）把统一视图定成**一切指标允许 None**：`{schema, backend, host_tag, lane, job_id, op/shape/dtype, metrics:{torch_ms, gems_ms, speedup}, correct, max_abs_err, error, duration_s, queue_wait_s, finished_at, source_schema, meta}`——消费端必须显式处理 None，缺数据是显式状态而非异常路径。backend/host_tag/lane 三轴进 schema（与 ADR-0016 的 registry 三元组对齐），旧 raw-v1 经 `from_raw_v1()` 无损升级视图（backend 缺省 k3、按落盘 host 字段推断 host_tag——历史数据全是 k3 系，推断不丢信息）。历史文件**不迁移**：`load_result_v2()` 读侧双 schema 兼容，rollup 按需升级。

被否方案：v1 加 optional 字段原地演进（"speedup 总有值"的默认预期已在消费端扎根，原地演进等于继续骗人）；服务端改输出（同 ADR-0016 的 Byte-freeze 代价论证，且 result-v2 的消费者全在客户端侧）。
