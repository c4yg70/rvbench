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
    assert!(String::from_utf8_lossy(&result.stdout).contains("readiness blockers: 24"));
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
        "ec4aae80794f92c1af59a38c7b22a3883f256cdd38f9bb4c17fa4bd35a9120a3"
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
