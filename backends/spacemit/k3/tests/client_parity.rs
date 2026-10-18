use serde_json::{Value, json};
#[test]
fn original_legacy_client_contract_cases() {
    let records = rvbench_core::parse_json(include_str!("fixtures/clients.json")).unwrap();
    for (index, r) in records.as_array().unwrap().iter().enumerate() {
        let args = r["args"].as_array().unwrap();
        let k = &r["kwargs"];
        let actual = match r["kind"].as_str().unwrap() {
            "suite" => json!(
                rvbench_core::suite::validate_suite(
                    &args[0],
                    &["k3", "k1", "sg2042", "rvv_general"]
                )
                .is_empty()
            ),
            "registry" => {
                let backend = k["backend"]
                    .as_str()
                    .or_else(|| args.get(2).and_then(Value::as_str))
                    .unwrap_or("k3");
                json!(rvbench_k3::judge::registry_key(
                    args[0].as_str().unwrap(),
                    args[1].as_str().unwrap(),
                    backend
                ))
            }
            "base_label" => json!(rvbench_k3::profiles::base_label(args[0].as_str().unwrap())),
            "comparable" => json!(rvbench_k3::profiles::comparable(
                args[0].as_str(),
                args[1].as_str()
            )),
            "to_result" => rvbench_core::compat::to_result_v2(
                &args[0],
                k["backend"].as_str().unwrap(),
                k["host_tag"].as_str().unwrap(),
                k["lane"].as_str().unwrap(),
                k.get("meta").cloned(),
            ),
            "from_raw" => rvbench_core::compat::from_raw_v1(&args[0]),
            _ => panic!("unknown sample"),
        };
        let expected = if r["kind"] == "suite" {
            json!(r["expected"].as_array().unwrap().is_empty())
        } else {
            r["expected"].clone()
        };
        assert_eq!(actual, expected, "client sample {index}");
    }
}
