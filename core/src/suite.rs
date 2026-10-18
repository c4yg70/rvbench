//! Legacy suite-v1 validation and expansion without importing a campaign driver.
use crate::{a, keys, s, set};
use anyhow::{Result, ensure};
use serde_json::{Value, json};

pub fn validate_suite(doc: &Value, backends: &[&str]) -> Vec<String> {
    let mut errors = Vec::new();
    let mut check = |condition: bool, message: &str| {
        if !condition {
            errors.push(message.to_owned());
        }
    };
    check(doc["schema"] == "suite-v1", "schema must be suite-v1");
    let name = s(&doc["name"]);
    check(
        !name.is_empty()
            && name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c)),
        "name must be a safe file identifier",
    );
    check(backends.contains(&s(&doc["backend"])), "unknown backend");
    check(
        keys(doc).is_subset(
            &[
                "schema", "name", "backend", "title", "defaults", "ops", "notes",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        ),
        "unknown suite key",
    );
    check(
        doc["ops"].is_array() && !a(&doc["ops"]).is_empty(),
        "ops must be a nonempty array",
    );
    let ns: Vec<_> = a(&doc["ops"])
        .iter()
        .map(|v| s(&v["op"]).to_owned())
        .collect();
    check(ns.len() == set(&ns).len(), "duplicate op");
    for key in ["title", "notes"] {
        if let Some(v) = doc.get(key) {
            check(v.is_string(), "title/notes must be strings");
        }
    }
    if let Some(defaults) = doc.get("defaults") {
        check(defaults.is_object(), "defaults must be an object");
        check(
            keys(defaults).is_subset(
                &["warmup", "rep", "timeout_min"]
                    .iter()
                    .map(|s| s.to_string())
                    .collect(),
            ),
            "unknown defaults key",
        );
        for key in ["warmup", "rep", "timeout_min"] {
            if let Some(v) = defaults.get(key) {
                check(v.as_u64().is_some_and(|n| n >= 1), "invalid defaults value");
            }
        }
    }
    for op in a(&doc["ops"]) {
        check(
            keys(op)
                == ["op", "shape", "dtype"]
                    .iter()
                    .map(|s| s.to_string())
                    .collect(),
            "op entry must contain op, shape and dtype",
        );
        let name = s(&op["op"]);
        check(
            !name.is_empty() && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_'),
            "invalid op name",
        );
        let shape = s(&op["shape"]);
        check(
            shape == "framework"
                || (!shape.is_empty()
                    && shape.split(',').count() <= 6
                    && shape.split(',').all(|p| {
                        !p.is_empty() && p.len() <= 10 && p.bytes().all(|c| c.is_ascii_digit())
                    })),
            "invalid shape",
        );
        check(
            op["dtype"].as_str().is_some_and(|s| !s.is_empty()),
            "invalid dtype",
        );
    }
    errors
}

pub fn expand(doc: &Value, cond: &str, lane: &str, attempt: i64) -> Result<Vec<Value>> {
    ensure!(
        validate_suite(doc, &["k3", "k1", "sg2042", "rvv_general"]).is_empty(),
        "invalid suite"
    );
    let kind = match lane {
        "timing" => "timing-final",
        "correctness" => "correctness",
        "func" => "func",
        _ => anyhow::bail!("invalid lane"),
    };
    ensure!(attempt >= 1, "attempt must be positive");
    Ok(a(&doc["ops"]).iter().map(|op|json!({"op":op["op"],"shape":op["shape"],"dtype":op["dtype"],"cond":cond,"attempt":attempt,"kind":kind,"warmup":doc["defaults"].get("warmup").cloned().unwrap_or(json!(5)),"rep":doc["defaults"].get("rep").cloned().unwrap_or(json!(5)),"timeout_min":doc["defaults"].get("timeout_min").cloned().unwrap_or(json!(15)),"suite":doc["name"]})).collect())
}
