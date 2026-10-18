use rvbench_k3::{K3Config, executor::K3Executor};
use rvbench_runtime::Executor;
use serde_json::json;
use std::{thread, time::Duration};

#[test]
#[ignore = "native child fixture invoked by the executor tests"]
fn benchmark_child() {
    println!("compiler diagnostic");
    println!(
        "{}",
        json!({"correct":true,"torch_ms":2.0,"gems_ms":1.0,"speedup":2.0})
    );
}
#[test]
#[ignore = "native child fixture invoked by the executor tests"]
fn bad_child() {
    println!("non JSON diagnostic");
}
#[test]
#[ignore = "native child fixture invoked by the executor tests"]
fn timeout_child() {
    thread::sleep(Duration::from_secs(10));
}

fn execute(mode: &str, timeout: Duration) -> rvbench_runtime::Execution {
    let temp = tempfile::tempdir().unwrap();
    let cfg = K3Config::from_value(json!({"token":"test","flag_gems_root":temp.path().join("fg"),"scripts_root":temp.path().join("scripts"),"run_env":"unused","executor":"subprocess","correctness_workers":0,"bench_argv_override":[std::env::current_exe().unwrap(),"--ignored","--exact",mode,"--nocapture"],"evaluators":{"perf":{"enabled":true},"acc":{"enabled":false},"func":{"enabled":false}}}),temp.path()).unwrap();
    let worker = cfg.workers[0].clone();
    let request = json!({"bench":"op","op":"abs","shape":"4","dtype":"float32"});
    let mut executor = K3Executor::new(cfg, worker);
    executor.execute(mode, &request, timeout).unwrap()
}

#[test]
fn native_subprocess_executor_parses_bridge_rows() {
    let result = execute("benchmark_child", Duration::from_secs(3));
    assert_eq!(result.state, "done");
    assert_eq!(result.result["speedup"], 2.0);
    assert!(result.stdout.contains("compiler diagnostic"));
}
#[test]
fn native_subprocess_executor_rejects_missing_rows_and_times_out() {
    assert_eq!(execute("bad_child", Duration::from_secs(3)).state, "failed");
    assert_eq!(
        execute("timeout_child", Duration::from_millis(150)).state,
        "timeout"
    );
}
