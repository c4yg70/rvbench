use rvbench_core::{
    baseline::*, json::sha256, load_json, readiness_blockers, spec_digest,
    validate_baseline_results, validate_eval_results, validate_spec,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_owned()
}
fn fixture() -> (tempfile::TempDir, Value, Value) {
    let temp = tempfile::tempdir().unwrap();
    fs::create_dir(temp.path().join("code")).unwrap();
    for file in [
        "code/abs.py",
        "synthetic-evidence.txt",
        "baseline-results.json",
    ] {
        fs::copy(
            root().join("examples/baseline").join(file),
            temp.path().join(file),
        )
        .unwrap();
    }
    let spec = load_json(root().join("examples/baseline/spec.json")).unwrap();
    let result = load_json(temp.path().join("baseline-results.json")).unwrap();
    (temp, spec, result)
}
fn unreferenced(spec: &Value) -> Value {
    let mut spec = spec.clone();
    spec["baseline"]["entries"][0]["reference_results"] = json!([]);
    spec
}
fn errors(spec: &Value, result: &Value, base: &Path) -> Vec<String> {
    validate_baseline_results(result, Some(&unreferenced(spec)), Some(base), Some(base))
}
fn refresh_inputs(result: &mut Value) {
    let m = &mut result["measurements"][0];
    m["inputs_sha256"] = json!(inputs_digest(&m["inputs"], &m["attrs"]));
}

#[test]
fn examples_validate_and_baseline_is_optional_for_both_spec_versions() {
    let (temp, spec, result) = fixture();
    assert!(validate_spec(&spec, Some(temp.path())).is_empty());
    assert!(errors(&spec, &result, temp.path()).is_empty());
    assert!(!validate_spec(&spec, None).is_empty());
    let legacy = rvbench_core::parse_json(include_str!("fixtures/contracts.json")).unwrap();
    let old = legacy
        .as_array()
        .unwrap()
        .iter()
        .find(|v| {
            v["kind"] == "spec" && v["valid"] == true && v["files"].as_object().unwrap().is_empty()
        })
        .unwrap()["document"]
        .clone();
    let blockers = readiness_blockers(&old);
    for version in ["2.0.0", "2.1.0"] {
        let mut doc = old.clone();
        doc["schema_version"] = json!(version);
        assert!(validate_spec(&doc, None).is_empty());
        assert_eq!(readiness_blockers(&doc), blockers);
    }
    let mut wrong = spec.clone();
    wrong["schema_version"] = json!("2.0.0");
    assert!(!validate_spec(&wrong, Some(temp.path())).is_empty());
    let mut absent = unreferenced(&spec);
    absent.as_object_mut().unwrap().remove("baseline");
    assert_eq!(readiness_blockers(&spec), readiness_blockers(&absent));
    let mut command_data = unreferenced(&spec);
    command_data["baseline"]["entries"][0]["run"]["env"]["status"] = json!("pending");
    command_data["baseline"]["entries"][0]["run"]["env"]["kind"] = json!("pending");
    assert!(validate_spec(&command_data, Some(temp.path())).is_empty());
    assert_eq!(
        readiness_blockers(&command_data),
        readiness_blockers(&absent)
    );
    command_data["baseline"]["entries"][0]["run"]["env"]["status"] = json!("resolved");
    command_data["baseline"]["entries"][0]["run"]["env"]["path"] = json!("not-a-contract");
    command_data["baseline"]["entries"][0]["run"]["env"]["sha256"] = json!("b".repeat(64));
    assert!(validate_spec(&command_data, Some(temp.path())).is_empty());
}

#[test]
fn rejects_bad_declarations_commands_paths_and_code_identity() {
    let (temp, spec, _) = fixture();
    for (pointer, value) in [
        ("/baseline", Value::Null),
        ("/baseline/entries/0/operator_id", json!("missing")),
        ("/baseline/entries/0/case_ids", json!(["typical8.mm.C01"])),
        ("/baseline/entries/0/code/root", json!("../outside")),
        (
            "/baseline/entries/0/code/files/0/path",
            json!("/absolute.py"),
        ),
        (
            "/baseline/entries/0/code/files/0/path",
            json!("C:\\secret.py"),
        ),
        (
            "/baseline/entries/0/code/files/0/sha256",
            json!("b".repeat(64)),
        ),
        ("/baseline/entries/0/run/cwd", json!("missing")),
        ("/baseline/entries/0/run/cwd", json!("../outside")),
        (
            "/baseline/entries/0/run/argv",
            json!(["python", "{unknown}"]),
        ),
        ("/baseline/entries/0/run/argv", json!(["python", "{spec"])),
        ("/baseline/entries/0/run/argv", json!(["python", "}"])),
        ("/baseline/entries/0/run/argv", json!(["python\u{0}"])),
        ("/baseline/entries/0/run/env", json!({"BAD-NAME":"x"})),
        (
            "/baseline/entries/0/reference_results/0/path",
            json!("../outside.json"),
        ),
        (
            "/baseline/entries/0/reference_results/0/sha256",
            json!("b".repeat(64)),
        ),
    ] {
        let mut doc = spec.clone();
        *doc.pointer_mut(pointer).unwrap() = value;
        assert!(
            !validate_spec(&doc, Some(temp.path())).is_empty(),
            "accepted {pointer}"
        );
    }
    let mut duplicate = spec.clone();
    let entry = duplicate["baseline"]["entries"][0].clone();
    duplicate["baseline"]["entries"]
        .as_array_mut()
        .unwrap()
        .push(entry);
    assert!(
        validate_spec(&duplicate, Some(temp.path()))
            .iter()
            .any(|e| e.contains("duplicate baseline id"))
    );
    let mut unknown = spec.clone();
    unknown["baseline"]["entries"][0]["run"]["shell"] = json!("echo");
    assert!(!validate_spec(&unknown, Some(temp.path())).is_empty());
    fs::write(temp.path().join("code/abs.py"), "changed").unwrap();
    assert!(!validate_spec(&spec, Some(temp.path())).is_empty());
}

#[test]
fn historical_bindings_have_no_digest_cycle_and_track_effective_contracts() {
    let (_, mut spec, _) = fixture();
    let original = implementation_digest(&spec["baseline"]["entries"][0]);
    let case_digest = case_contract_digest(
        &spec,
        &spec["operators"][0],
        &spec["operators"][0]["cases"][0],
    );
    spec["baseline"]["entries"][0]["reference_results"] = json!([]);
    spec["baseline"]["entries"][0]["case_ids"] = json!(["coverage-change"]);
    spec["version"] = json!("0.2.0");
    assert_eq!(
        original,
        implementation_digest(&spec["baseline"]["entries"][0])
    );
    assert_eq!(
        case_digest,
        case_contract_digest(
            &spec,
            &spec["operators"][0],
            &spec["operators"][0]["cases"][0]
        )
    );
    spec["operators"][0]["cases"][0]["inputs"][0]["generator"]["seed"] = json!(8);
    assert_ne!(
        case_digest,
        case_contract_digest(
            &spec,
            &spec["operators"][0],
            &spec["operators"][0]["cases"][0]
        )
    );
    spec["baseline"]["entries"][0]["run"]["env"]["OMP_NUM_THREADS"] = json!("2");
    assert_ne!(
        original,
        implementation_digest(&spec["baseline"]["entries"][0])
    );
}

#[test]
fn rejects_inconsistent_historical_measurements() {
    let (temp, spec, result) = fixture();
    for (pointer, value) in [
        ("/schema_version", json!("9.0.0")),
        ("/baseline_id", json!("unknown")),
        ("/suite_id", json!("other")),
        ("/implementation_sha256", json!("b".repeat(64))),
        ("/environments/0/fingerprint_sha256", json!("b".repeat(64))),
        ("/measurements/0/case_id", json!("missing")),
        (
            "/measurements/0/case_contract_sha256",
            json!("b".repeat(64)),
        ),
        ("/measurements/0/inputs_sha256", json!("b".repeat(64))),
        ("/measurements/0/environment_id", json!("missing")),
        (
            "/measurements/0/recorded_at",
            json!("2026-10-16T10:00:00+08:00"),
        ),
        ("/measurements/0/recorded_at", json!("2026-02-30T00:00:00Z")),
        ("/measurements/0/timing/median_ms", json!(2.0)),
        ("/measurements/0/timing/repetitions", json!(4)),
        (
            "/measurements/0/timing/samples_ms",
            json!([-1.0, 0.46, 0.47]),
        ),
        ("/measurements/0/timing", Value::Null),
        ("/measurements/0/correctness/checks", json!([])),
        (
            "/measurements/0/correctness/checks/0/outputs",
            json!(["not-an-output"]),
        ),
        ("/measurements/0/artifacts/0/size_bytes", json!(0)),
        ("/measurements/0/artifacts/0/path", json!("../evidence.txt")),
        ("/measurements/0/artifacts/0/sha256", json!("b".repeat(64))),
    ] {
        let mut doc = result.clone();
        *doc.pointer_mut(pointer).unwrap() = value;
        assert!(
            !errors(&spec, &doc, temp.path()).is_empty(),
            "accepted {pointer}"
        );
    }
    let mut layout = result.clone();
    layout["measurements"][0]["inputs"][0]["strides"] = json!([1, 1024]);
    refresh_inputs(&mut layout);
    assert!(
        errors(&spec, &layout, temp.path())
            .iter()
            .any(|e| e.contains("layout"))
    );
    let mut offset = result.clone();
    offset["measurements"][0]["inputs"][0]["storage_offset"] = json!(5);
    refresh_inputs(&mut offset);
    assert!(
        errors(&spec, &offset, temp.path())
            .iter()
            .any(|e| e.contains("layout"))
    );
    let mut measured = result.clone();
    measured["purpose"] = json!("measured");
    assert!(
        errors(&spec, &measured, temp.path())
            .iter()
            .any(|e| e.contains("native execution evidence"))
    );
    let mut changed = result.clone();
    changed["measurements"][0]["inputs"][0]["generator"]["seed"] = json!(9);
    refresh_inputs(&mut changed);
    assert!(
        errors(&spec, &changed, temp.path())
            .iter()
            .any(|e| e.contains("descriptor"))
    );
}

#[test]
fn referenced_file_must_be_semantically_valid_even_with_a_fresh_file_hash() {
    let (temp, mut spec, mut result) = fixture();
    result["measurements"][0]["timing"]["median_ms"] = json!(999.0);
    let bytes = serde_json::to_vec(&result).unwrap();
    fs::write(temp.path().join("baseline-results.json"), &bytes).unwrap();
    spec["baseline"]["entries"][0]["reference_results"][0]["sha256"] = json!(sha256(&bytes));
    assert!(
        validate_spec(&spec, Some(temp.path()))
            .iter()
            .any(|e| e.contains("median"))
    );
}

#[test]
fn accepts_even_and_zero_samples_and_keeps_failed_records_distinct() {
    let (temp, spec, result) = fixture();
    for (samples, median) in [(json!([1.0, 3.0, 2.0, 4.0]), 2.5), (json!([0.0, 0.0]), 0.0)] {
        let mut doc = result.clone();
        doc["measurements"][0]["timing"]["repetitions"] = json!(samples.as_array().unwrap().len());
        doc["measurements"][0]["timing"]["samples_ms"] = samples;
        doc["measurements"][0]["timing"]["median_ms"] = json!(median);
        assert!(errors(&spec, &doc, temp.path()).is_empty());
    }
    for status in ["failed", "unverifiable"] {
        let mut doc = result.clone();
        doc["measurements"][0]["timing"] = Value::Null;
        doc["measurements"][0]["correctness"] = json!({"status":status,"reason":{"code":"TEST","message":"Explicit test failure"},"checks":[]});
        assert!(errors(&spec, &doc, temp.path()).is_empty());
        doc["measurements"][0]["correctness"]["reason"] = Value::Null;
        assert!(!errors(&spec, &doc, temp.path()).is_empty());
    }
}

#[test]
fn measured_records_bind_native_execution_timing_and_evidence_without_promoting_candidate() {
    let (temp, mut spec, mut result) = fixture();
    let contract = b"Unit test timing contract, not a real measurement.";
    fs::write(temp.path().join("timing.txt"), contract).unwrap();
    spec["policies"]["timing"]["contract"] =
        json!({"status":"resolved","path":"timing.txt","sha256":sha256(contract)});
    assert_eq!(
        spec["operators"][0]["cases"][0]["execution"]["status"],
        "pending_probe"
    );
    result["purpose"] = json!("measured");
    result["measurements"][0]["timing"]["timing_contract_sha256"] = json!(sha256(contract));
    let mut execution = result["measurements"][0]["correctness"]["checks"][0].clone();
    execution["id"] = json!("native-execution");
    execution["kind"] = json!("execution");
    execution["method"] = json!("pytorch_native_call_v1");
    result["measurements"][0]["correctness"]["checks"]
        .as_array_mut()
        .unwrap()
        .push(execution);
    result["measurements"][0]["case_contract_sha256"] = json!(case_contract_digest(
        &spec,
        &spec["operators"][0],
        &spec["operators"][0]["cases"][0]
    ));
    assert!(errors(&spec, &result, temp.path()).is_empty());
    let mut mismatched = result.clone();
    mismatched["measurements"][0]["timing"]["timing_contract_sha256"] = json!("c".repeat(64));
    assert!(
        errors(&spec, &mismatched, temp.path())
            .iter()
            .any(|e| e.contains("timing contract digest"))
    );
    result["measurements"][0]["artifacts"] = json!([]);
    assert!(
        errors(&spec, &result, temp.path())
            .iter()
            .any(|e| e.contains("evidence artifacts"))
    );
}

#[test]
fn new_eval_result_version_accepts_spec_21_without_weakening_version_10() {
    let legacy = rvbench_core::parse_json(include_str!("fixtures/contracts.json")).unwrap();
    let pair = legacy
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["kind"] == "results" && v["valid"] == true)
        .unwrap();
    let mut spec = pair["public"].clone();
    let mut result = pair["document"].clone();
    spec["schema_version"] = json!("2.1.0");
    result["specs"]["public"]["schema_version"] = json!("2.1.0");
    result["specs"]["public"]["sha256"] = json!(spec_digest(&spec));
    assert!(!validate_eval_results(&result, Some(&spec), None, None).is_empty());
    result["schema_version"] = json!("1.1.0");
    assert!(validate_eval_results(&result, Some(&spec), None, None).is_empty());
}

#[cfg(unix)]
#[test]
fn symlinks_cannot_escape_code_or_result_roots() {
    use std::os::unix::fs::symlink;
    let (temp, mut spec, _) = fixture();
    let outside = tempfile::tempdir().unwrap();
    fs::write(outside.path().join("outside.py"), "outside").unwrap();
    symlink(outside.path(), temp.path().join("escape")).unwrap();
    spec["baseline"]["entries"][0]["code"]["root"] = json!("escape");
    assert!(
        validate_spec(&spec, Some(temp.path()))
            .iter()
            .any(|e| e.contains("symlink"))
    );
}
