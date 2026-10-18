use std::{path::Path, process::Command};

#[test]
fn contract_commands_work_outside_repository_and_reject_duplicate_keys() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let cli = env!("CARGO_BIN_EXE_rvbench");
    let public = root.join("suites/kernels/typical8/spec.json");
    let result = Command::new(cli)
        .current_dir(temp.path())
        .args(["validate", "spec"])
        .arg(&public)
        .output()
        .unwrap();
    assert!(result.status.success(), "{:?}", result);
    let document = rvbench_core::load_json(&public).unwrap();
    assert!(String::from_utf8_lossy(&result.stdout).contains(&format!(
        "readiness blockers: {}",
        rvbench_core::readiness_blockers(&document).len()
    )));
    let ready = Command::new(cli)
        .current_dir(temp.path())
        .args(["validate", "spec"])
        .arg(&public)
        .arg("--require-ready")
        .output()
        .unwrap();
    assert_eq!(ready.status.code(), Some(2));
    let digest = Command::new(cli)
        .current_dir(temp.path())
        .arg("digest")
        .arg(&public)
        .output()
        .unwrap();
    assert!(digest.status.success());
    assert_eq!(
        String::from_utf8_lossy(&digest.stdout).trim(),
        rvbench_core::spec_digest(&document)
    );
    let results = Command::new(cli)
        .current_dir(temp.path())
        .args(["validate", "results"])
        .arg(root.join("examples/eval_results.json"))
        .arg("--spec")
        .arg(&public)
        .output()
        .unwrap();
    assert!(results.status.success(), "{:?}", results);
    let bad = temp.path().join("duplicate.json");
    std::fs::write(&bad, "{\"x\":1,\"x\":2}").unwrap();
    assert!(
        !Command::new(cli)
            .arg("digest")
            .arg(bad)
            .output()
            .unwrap()
            .status
            .success()
    );
}

#[test]
fn baseline_validation_and_digest_work_outside_repository() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let cli = env!("CARGO_BIN_EXE_rvbench");
    let spec = root.join("examples/baseline/spec.json");
    for args in [vec!["validate", "spec"], vec!["digest"]] {
        let mut command = Command::new(cli);
        command.current_dir(temp.path()).args(&args).arg(&spec);
        if args[0] == "digest" {
            command.args(["--baseline", "torch_abs"]);
        }
        let result = command.output().unwrap();
        assert!(result.status.success(), "{:?}", result);
        if args[0] == "digest" {
            let digest: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
            assert_eq!(digest["baseline_id"], "torch_abs");
            assert_eq!(digest["implementation_sha256"].as_str().unwrap().len(), 64);
            assert_eq!(digest["cases"].as_array().unwrap().len(), 1);
        }
    }
    let result = Command::new(cli)
        .current_dir(temp.path())
        .args(["validate", "baseline-results"])
        .arg(root.join("examples/baseline/baseline-results.json"))
        .arg("--spec")
        .arg(&spec)
        .output()
        .unwrap();
    assert!(result.status.success(), "{:?}", result);
    let wrong = Command::new(cli)
        .current_dir(temp.path())
        .arg("digest")
        .arg(&spec)
        .args(["--baseline", "does-not-exist"])
        .output()
        .unwrap();
    assert!(!wrong.status.success());
}
