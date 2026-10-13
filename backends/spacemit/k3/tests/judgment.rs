use rvbench_k3::judge::judge;
use serde_json::json;

#[test]
fn same_host_ratchet_nullable_and_stochastic_results_preserve_registry() {
    let ticket = json!({"op":"abs","kind":"correctness","cond":"KOPE","attempt":1,"backend":"k3"});
    let mut registry = json!({});
    let mut job = json!({"job_id":"one","state":"done","result":{"correct":true,"gems_ms":10.0}});
    let base = "http://127.0.0.1:8788";
    assert_eq!(
        judge(&ticket, &job, &mut registry, base, None).0,
        "accepted_correctness"
    );
    let first = registry.clone();
    job["result"]["gems_ms"] = json!(9.6);
    assert_eq!(
        judge(&ticket, &job, &mut registry, base, None).0,
        "rejected_slower"
    );
    assert_eq!(registry, first);
    job["result"]["gems_ms"] = json!(9.0);
    assert_eq!(
        judge(&ticket, &job, &mut registry, base, None).0,
        "accepted_correctness_and_faster"
    );
    let faster = registry.clone();
    job["result"]["correct"] = json!(null);
    assert_ne!(
        judge(&ticket, &job, &mut registry, base, None).0,
        "accepted_correctness"
    );
    assert_eq!(registry, faster);
    let stochastic = json!({"op":"rand","kind":"correctness","cond":"KOPE"});
    assert_eq!(
        judge(&stochastic, &job, &mut registry, base, None).0,
        "needs_stats_check"
    );
    assert_eq!(registry, faster);
}

#[test]
fn changing_host_establishes_reference_without_comparing_timings() {
    let ticket = json!({"op":"abs","kind":"correctness","cond":"KOPE"});
    let mut registry = json!({});
    let mut job = json!({"state":"done","result":{"correct":true,"gems_ms":1.0}});
    judge(&ticket, &job, &mut registry, "http://first:8788", None);
    job["result"]["gems_ms"] = json!(100.0);
    assert_eq!(
        judge(&ticket, &job, &mut registry, "http://second:8788", None).0,
        "accepted_correctness"
    );
}
