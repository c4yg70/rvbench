use crate::spec::{expected_tensor_outputs, readiness_blockers};
use crate::{a, require, s, schema_errors, spec_digest, unique, validate_spec};
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};

fn resolve(public: &Value, hidden: &Value) -> Result<(Value, Value)> {
    let op = a(&public["operators"])
        .iter()
        .find(|o| o["id"] == hidden["operator"])
        .ok_or_else(|| anyhow::anyhow!("operator is not defined in spec.json"))?;
    let template = a(&op["cases"])
        .iter()
        .find(|c| c["id"] == hidden["contract_ref"])
        .ok_or_else(|| {
            anyhow::anyhow!("contract_ref must name a public case of the same operator")
        })?;
    let mut inputs = BTreeSet::new();
    for input in a(&hidden["inputs"]) {
        inputs.insert(s(&input["name"]));
    }
    ensure!(
        inputs
            == a(&template["inputs"])
                .iter()
                .map(|v| s(&v["name"]))
                .collect(),
        "input names must match the referenced public contract"
    );
    for input in a(&hidden["inputs"]) {
        let base = a(&template["inputs"])
            .iter()
            .find(|v| v["name"] == input["name"])
            .unwrap();
        ensure!(
            base["kind"] == input["kind"] && base.get("dtype") == input.get("dtype"),
            "input kind/dtype requires a matching contract in spec.json"
        );
    }
    let mut case = template.clone();
    for key in ["inputs", "attrs", "execution"] {
        case[key] = hidden[key].clone();
    }
    let expected = expected_tensor_outputs(op, &case)?.ok_or_else(|| {
        anyhow::anyhow!("output inference for this public operator is not implemented")
    })?;
    for expected in expected {
        let actual = case["outputs"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|v| s(&v["name"]) == expected.name)
            .ok_or_else(|| anyhow::anyhow!("missing output slot"))?;
        ensure!(
            s(&actual["dtype"]) == expected.dtype,
            "output dtype requires a matching contract in spec.json"
        );
        actual["shape"] = json!(expected.shape);
        if a(&actual["layout"]["axes"]).len() != expected.shape.len() {
            actual["layout"]["axes"] = json!(
                (0..expected.shape.len())
                    .map(|i| format!("dim_{i}"))
                    .collect::<Vec<_>>()
            );
        }
    }
    Ok((op.clone(), case))
}

pub fn resolve_hidden_case(public: &Value, hidden: &Value) -> Result<Value> {
    let (op, case) = resolve(public, hidden)?;
    Ok(
        json!({"id":hidden["id"],"operator":op["id"],"call":op["call"],"returns":op["returns"],"inputs":case["inputs"],"attrs":case["attrs"],"outputs":case["outputs"],"verification":case["verification"],"execution":case["execution"]}),
    )
}

pub fn public_relative_path(hidden: &Value) -> Result<&Path> {
    let path = s(&hidden["public_spec"]["path"]);
    let relative = Path::new(path);
    ensure!(
        !relative.has_root()
            && !path.starts_with(['/', '\\'])
            && !path.contains(':')
            && relative.file_name().is_some_and(|n| n == "spec.json"),
        "public_spec.path must be a relative path to spec.json"
    );
    Ok(relative)
}

pub fn validate_hidden_spec(hidden: &Value, public: &Value, base: Option<&Path>) -> Vec<String> {
    let mut errors = schema_errors(hidden, "hidden");
    if !errors.is_empty() {
        return errors;
    }
    if let Err(e) = public_relative_path(hidden) {
        return vec![e.to_string()];
    }
    errors = validate_spec(public, base);
    if !errors.is_empty() {
        return errors;
    }
    if s(&hidden["public_spec"]["sha256"]) != spec_digest(public) {
        return vec!["public spec.json digest mismatch; revalidate before repinning".into()];
    }
    if hidden["suite_id"] != public["suite"]["id"] {
        return vec!["suite_id does not match public spec.json".into()];
    }
    unique(
        &mut errors,
        crate::names(&hidden["cases"], "id"),
        "hidden case id",
    );
    let ids: BTreeSet<_> = a(&public["operators"])
        .iter()
        .flat_map(|o| a(&o["cases"]).iter().map(|c| s(&c["id"]).to_owned()))
        .collect();
    let public_payloads: BTreeSet<_> = a(&public["operators"])
        .iter()
        .flat_map(|o| {
            a(&o["cases"]).iter().map(|c| {
                (
                    s(&o["id"]).to_owned(),
                    spec_digest(&json!({"inputs":c["inputs"],"attrs":c["attrs"]})),
                )
            })
        })
        .collect();
    let mut payloads = BTreeSet::new();
    for h in a(&hidden["cases"]) {
        let label = s(&h["id"]);
        require(
            &mut errors,
            !ids.contains(label),
            format!("{label}: duplicate or public case id"),
        );
        let payload = (
            s(&h["operator"]).to_owned(),
            spec_digest(&json!({"inputs":h["inputs"],"attrs":h["attrs"]})),
        );
        require(
            &mut errors,
            !public_payloads.contains(&payload),
            format!("{label}: hidden test duplicates an already public test"),
        );
        require(
            &mut errors,
            payloads.insert(payload),
            format!("{label}: duplicate hidden test parameters"),
        );
        let prefix = format!("{}.{}.H", s(&hidden["suite_id"]), s(&h["operator"]));
        let valid = label.strip_prefix(&prefix).is_some_and(|n| {
            n.len() >= 2
                && n.bytes().all(|c| c.is_ascii_digit())
                && if n.starts_with('0') {
                    n.len() == 2 && n != "00"
                } else {
                    true
                }
        });
        require(
            &mut errors,
            valid,
            format!("{label}: hidden case id must be suite.operator.Hxx"),
        );
        match resolve(public, h) {
            Ok((mut op, case)) => {
                let mut view = public.clone();
                view["suite"]["status"] = json!("draft");
                op["cases"] = json!([case]);
                view["operators"] = json!([op]);
                errors.extend(
                    validate_spec(&view, base)
                        .into_iter()
                        .map(|e| format!("{label}: {e}")),
                );
            }
            Err(e) => errors.push(format!("{label}: {e}")),
        }
    }
    if errors.is_empty() && hidden["status"] == "ready" {
        require(
            &mut errors,
            hidden_readiness_blockers(hidden, public).is_empty(),
            "hidden status=ready cannot contain pending contracts or unready cases",
        );
    }
    errors
}

pub fn hidden_readiness_blockers(hidden: &Value, public: &Value) -> Vec<String> {
    let mut blockers = Vec::new();
    if hidden["status"] != "ready" {
        blockers.push("hidden status is draft".into());
    }
    for h in a(&hidden["cases"]) {
        match resolve(public, h) {
            Ok((_, case)) => {
                let view = json!({"suite":{"status":"ready"},"policies":public["policies"],"operators":[{"cases":[case]}]});
                blockers.extend(
                    readiness_blockers(&view)
                        .into_iter()
                        .map(|v| format!("{}: {v}", s(&h["id"]))),
                );
            }
            Err(e) => blockers.push(e.to_string()),
        }
    }
    blockers
}
