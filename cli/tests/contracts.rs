//! 合同正本与随 crate 发布副本的一致性。
//!
//! 正本：`contracts/openapi.json`（本仓）。
//! 副本：`rvbench-protocol` 编译期嵌入的 `SPEC`。两侧必须逐字节相同，
//! 否则说明改了正本却没同步协议仓，或动了生成物却没改正本。

#[test]
fn shipped_spec_matches_contract() {
    let canonical = include_str!("../../contracts/openapi.json");
    assert_eq!(
        canonical, rvbench_protocol::SPEC,
        "contracts/openapi.json 与 rvbench-protocol::SPEC 不一致：先跑 python tools/sync_contract.py，再在协议仓重新生成类型"
    );
}

#[test]
fn contract_is_canonical_json_without_crlf() {
    let canonical = include_str!("../../contracts/openapi.json");
    assert!(
        !canonical.contains('\r'),
        "contracts/openapi.json 必须是 LF 换行，否则与协议仓副本的字节比较会漂移"
    );
    let parsed: serde_json::Value = serde_json::from_str(canonical).expect("contract must be JSON");
    assert_eq!(parsed["openapi"], "3.1.0");
    assert_eq!(
        parsed["paths"], serde_json::json!({}),
        "共享合同只定义 components/schemas；HTTP paths 属于各服务的 openapi.yaml"
    );
}
