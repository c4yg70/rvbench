# rvbench-core

通用 Rust crate，不依赖具体平台后端，也不加载 torch。

- `src/json.rs`：拒绝重复 key 与非有限值；排序 UTF-8 canonical JSON、Python 兼容数字表示与 SHA-256。
- `src/spec.rs`：公开合同、8 算子输出推导与 readiness。
- `src/baseline.rs`：可选基准包、历史测速证据、文件核验与无环摘要。
- `src/hidden.rs`：公开摘要绑定、隐藏引用与解析；诊断不输出私有参数。
- `src/results.rs`：结果证据、计时样本、身份、清理和汇总一致性。
- `src/compat.rs`：旧 raw-v1/result-v2 读写视图。
- `src/suite.rs`：旧 suite-v1 定义与展开。

Schema 从仓库 `schemas/` 嵌入构建。用户入口为 `rvbench validate spec|hidden|results|baseline-results` 和 `rvbench digest`。`digest FILE --baseline ID` 输出实现与 case 合同摘要。110 组公开/合成样例用于验证原 Python 合同行为，fixture 不包含真实隐藏定义；baseline 用独立正反例覆盖新约束。
