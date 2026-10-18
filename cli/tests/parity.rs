use rvbench_cli::{
    parse_json, spec_digest, validate_eval_results, validate_hidden_spec, validate_spec,
};
use std::fs;

#[test]
fn matches_original_public_and_synthetic_contract_regressions() {
    let records = parse_json(include_str!("fixtures/contracts.json")).unwrap();
    let mut differences = Vec::new();
    for (index, record) in records.as_array().unwrap().iter().enumerate() {
        let temp = tempfile::tempdir().unwrap();
        for (name, content) in record["files"].as_object().unwrap() {
            let target = temp.path().join(name);
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            let hex = content.as_str().unwrap();
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            fs::write(target, bytes).unwrap();
        }
        let base = if record["files"].as_object().unwrap().is_empty() {
            None
        } else {
            Some(temp.path())
        };
        let errors = match record["kind"].as_str().unwrap() {
            "spec" => validate_spec(&record["document"], base),
            "hidden" => validate_hidden_spec(&record["document"], &record["public"], base),
            "results" => validate_eval_results(
                &record["document"],
                record.get("public").filter(|v| !v.is_null()),
                record.get("hidden").filter(|v| !v.is_null()),
                base,
            ),
            _ => panic!("unknown oracle sample"),
        };
        if errors.is_empty() != record["valid"].as_bool().unwrap() {
            differences.push(format!(
                "sample {index} {} expected valid={} errors={errors:?}",
                record["kind"], record["valid"]
            ));
        }
        if record["kind"] == "spec" && record["valid"] == true {
            assert_eq!(
                spec_digest(&record["document"]),
                record["digest"].as_str().unwrap(),
                "sample {index} digest changed"
            );
        }
    }
    assert!(differences.is_empty(), "{}", differences.join("\n"));
}
