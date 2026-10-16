use rvbench_tools::{bundle::build_bundle, trace};
use serde_json::json;
use std::{fs, path::Path};

#[test]
fn traces_keep_d4_policy_and_detect_cross_root_evidence() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let fixtures = root.join("backends/spacemit/k3/tools/trace_fixtures");
    // The historical 'good' fixture predates D4 and fails in the old implementation too.
    let old = trace::check(&fixtures.join("good")).unwrap();
    assert_eq!(old["summary"]["errors"], 6);
    assert_eq!(old["summary"]["warnings"], 0);
    let bad = trace::check(&fixtures.join("bad")).unwrap();
    assert_eq!(bad["summary"]["errors"], 9);
    assert_eq!(bad["summary"]["warnings"], 1);
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("E1/G/add/trace.jsonl");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let valid = fs::read_to_string(fixtures.join("good/E1/G/add/trace.jsonl"))
        .unwrap()
        .replace("results/conditions/full/memory", "runs/E1/G/memory");
    fs::write(&path, valid).unwrap();
    assert_eq!(trace::check(&path).unwrap()["ok"], true);
}

#[test]
fn bundle_verifies_target_excludes_private_data_and_refuses_overwrite() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let binary = temp.path().join("rvbench-worker");
    fs::write(&binary, b"MZ").unwrap();
    assert!(build_bundle(root, &temp.path().join("wrong"), Some(&binary)).is_err());
    let mut elf = vec![0u8; 64];
    elf[..4].copy_from_slice(b"\x7fELF");
    elf[4] = 2;
    elf[5] = 1;
    elf[18..20].copy_from_slice(&243u16.to_le_bytes());
    fs::write(&binary, elf).unwrap();
    let output = temp.path().join("bundle");
    let manifest = build_bundle(root, &output, Some(&binary)).unwrap();
    assert_eq!(manifest["control_plane"], "rust");
    for item in manifest["files"].as_array().unwrap() {
        let path = item["path"].as_str().unwrap();
        assert!(!path.ends_with("/config.json") && !path.contains("spec.hidden"));
        assert_ne!(path, "evald/evald.py");
        let bytes = fs::read(output.join(path)).unwrap();
        assert_eq!(item["sha256"], json!(rvbench_core::json::sha256(&bytes)));
    }
    assert!(output.join("bin/rvbench-worker").is_file());
    assert!(output.join("worker/adapters/k3.py").is_file());
    assert!(!output.join("evald/config.example.json").exists());
    assert!(build_bundle(root, &output, Some(&binary)).is_err());
}
