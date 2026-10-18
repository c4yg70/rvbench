use rvbench_k3::K3Config;
use rvbench_runtime::Backend;

#[test]
fn original_request_contract_cases() {
    let records = rvbench_core::parse_json(include_str!("fixtures/requests.json")).unwrap();
    let base = tempfile::tempdir().unwrap();
    let mut differences = Vec::new();
    for (index, record) in records.as_array().unwrap().iter().enumerate() {
        let config = K3Config::from_value(record["config"].clone(), base.path())
            .unwrap_or_else(|e| panic!("sample {index} config: {e}"));
        let result = config.validate(&record["body"]);
        let valid = record["valid"].as_bool().unwrap();
        if result.is_ok() != valid {
            differences.push(format!(
                "sample {index}: expected valid={valid}, result={result:?}"
            ));
        } else if let Ok(prepared) = result {
            // JSON numeric equality must account for 15 versus 15.0 across runtimes.
            let actual = serde_json::to_value(prepared.request).unwrap();
            fn equal(a: &serde_json::Value, b: &serde_json::Value) -> bool {
                match (a, b) {
                    (serde_json::Value::Number(a), serde_json::Value::Number(b)) => {
                        a.as_f64() == b.as_f64()
                    }
                    (serde_json::Value::Array(a), serde_json::Value::Array(b)) => {
                        a.len() == b.len() && a.iter().zip(b).all(|(a, b)| equal(a, b))
                    }
                    (serde_json::Value::Object(a), serde_json::Value::Object(b)) => {
                        a.len() == b.len()
                            && a.iter().all(|(k, v)| b.get(k).is_some_and(|b| equal(v, b)))
                    }
                    _ => a == b,
                }
            }
            if !equal(&actual, &record["sanitized"]) {
                differences.push(format!("sample {index}: sanitized request differs"));
            }
        }
    }
    assert!(differences.is_empty(), "{}", differences.join("\n"));
}
