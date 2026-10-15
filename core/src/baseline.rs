//! Optional, offline baseline packages and historical measurement evidence.
use crate::{
    a, f, i, json::sha256, load_json, names, require, s, schema_errors, spec_digest, unique,
};
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

/// No result reference or coverage list participates in implementation identity.
pub fn implementation_digest(entry: &Value) -> String {
    spec_digest(
        &json!({"id":entry["id"],"operator_id":entry["operator_id"],"code":entry["code"],"run":entry["run"]}),
    )
}

/// Includes the selected case and its effective operator/target/policy contracts.
pub fn case_contract_digest(spec: &Value, op: &Value, case: &Value) -> String {
    spec_digest(
        &json!({"case":case,"operator":{"id":op["id"],"call":op["call"],"returns":op["returns"]},"target":spec["suite"]["target"],"policies":spec["policies"]}),
    )
}

pub fn inputs_digest(inputs: &Value, attrs: &Value) -> String {
    spec_digest(&json!({"inputs":inputs,"attrs":attrs}))
}

/// Environment snapshot v1: every field except display ID and the digest itself.
pub fn environment_digest(environment: &Value) -> String {
    let mut snapshot = environment.clone();
    if let Some(map) = snapshot.as_object_mut() {
        map.remove("id");
        map.remove("fingerprint_sha256");
    }
    spec_digest(&snapshot)
}

fn relative(value: &str, dot: bool) -> Result<()> {
    ensure!(
        !value.is_empty() && !value.contains(['\\', ':', '\0']) && !value.starts_with('/'),
        "expected a relative POSIX path"
    );
    ensure!(
        (dot && value == ".")
            || value
                .split('/')
                .all(|p| !p.is_empty() && p != "." && p != ".."),
        "path contains an empty/dot/parent component"
    );
    Ok(())
}

fn contained(base: &Path, value: &str, dot: bool) -> Result<PathBuf> {
    relative(value, dot)?;
    let root = base.canonicalize()?;
    let target = root.join(value).canonicalize()?;
    ensure!(
        target.starts_with(&root),
        "path escapes root through a symlink"
    );
    Ok(target)
}

fn check_file(base: Option<&Path>, file: &Value) -> Result<PathBuf> {
    relative(s(&file["path"]), false)?;
    let base = base.ok_or_else(|| anyhow::anyhow!("base directory required to verify file"))?;
    let path = contained(base, s(&file["path"]), false)?;
    ensure!(path.is_file(), "expected a regular file");
    let bytes = fs::read(&path)?;
    ensure!(sha256(&bytes) == s(&file["sha256"]), "file sha256 mismatch");
    if let Some(size) = file.get("size_bytes") {
        ensure!(
            size.as_u64() == Some(bytes.len() as u64),
            "file size mismatch"
        );
    }
    Ok(path)
}

fn command_tokens(text: &str) -> Result<()> {
    let mut rest = text;
    while let Some(start) = rest.find(['{', '}']) {
        ensure!(
            rest.as_bytes()[start] == b'{',
            "unmatched command placeholder"
        );
        let end = rest[start..]
            .find('}')
            .ok_or_else(|| anyhow::anyhow!("unclosed command placeholder"))?
            + start;
        ensure!(
            ["{spec}", "{case_id}", "{output}"].contains(&&rest[start..=end]),
            "unknown command placeholder"
        );
        rest = &rest[end + 1..];
    }
    Ok(())
}

pub(crate) fn validate_baseline(spec: &Value, base: Option<&Path>) -> Vec<String> {
    let Some(baseline) = spec.get("baseline") else {
        return Vec::new();
    };
    let mut errors = Vec::new();
    unique(
        &mut errors,
        names(&baseline["entries"], "id"),
        "baseline id",
    );
    for entry in a(&baseline["entries"]) {
        let label = format!("baseline {}", s(&entry["id"]));
        let op = a(&spec["operators"])
            .iter()
            .find(|op| op["id"] == entry["operator_id"]);
        require(
            &mut errors,
            op.is_some(),
            format!("{label}: unknown operator"),
        );
        if let Some(op) = op {
            for id in a(&entry["case_ids"]) {
                require(
                    &mut errors,
                    a(&op["cases"]).iter().any(|c| &c["id"] == id),
                    format!("{label}: case does not belong to operator"),
                );
            }
        }
        for arg in a(&entry["run"]["argv"]) {
            if let Err(e) = command_tokens(s(arg)) {
                errors.push(format!("{label}: {e}"));
            }
            require(
                &mut errors,
                !s(arg).contains('\0'),
                format!("{label}: command argument contains NUL"),
            );
        }
        require(
            &mut errors,
            entry["run"]["env"]
                .as_object()
                .is_none_or(|env| env.values().all(|v| !s(v).contains('\0'))),
            format!("{label}: environment value contains NUL"),
        );
        unique(
            &mut errors,
            names(&entry["code"]["files"], "path"),
            "baseline code path",
        );
        let code = (|| -> Result<PathBuf> {
            relative(s(&entry["code"]["root"]), true)?;
            let root = contained(
                base.ok_or_else(|| anyhow::anyhow!("base directory required for baseline"))?,
                s(&entry["code"]["root"]),
                true,
            )?;
            ensure!(root.is_dir(), "code root must be a directory");
            ensure!(
                contained(&root, s(&entry["run"]["cwd"]), true)?.is_dir(),
                "command cwd must be a directory"
            );
            Ok(root)
        })();
        match code {
            Ok(root) => {
                for file in a(&entry["code"]["files"]) {
                    if let Err(e) = check_file(Some(&root), file) {
                        errors.push(format!("{label}: {e}"));
                    }
                }
            }
            Err(e) => errors.push(format!("{label}: {e}")),
        }
        unique(
            &mut errors,
            names(&entry["reference_results"], "path"),
            "baseline result path",
        );
        for file in a(&entry["reference_results"]) {
            match check_file(base, file).and_then(|path| Ok((load_json(&path)?, path))) {
                Ok((value, path)) => {
                    require(
                        &mut errors,
                        value["baseline_id"] == entry["id"],
                        format!("{label}: reference belongs to another baseline"),
                    );
                    errors.extend(validate_results_inner(&value, Some(spec), path.parent()));
                }
                Err(e) => errors.push(format!("{label}: result reference: {e}")),
            }
        }
    }
    errors
}

/// Validate public contracts once, then resolve records without recursively following spec references.
pub fn validate_baseline_results(
    result: &Value,
    spec: Option<&Value>,
    spec_base: Option<&Path>,
    result_base: Option<&Path>,
) -> Vec<String> {
    if let Some(spec) = spec {
        let errors = crate::validate_spec(spec, spec_base);
        if !errors.is_empty() {
            return errors;
        }
    }
    validate_results_inner(result, spec, result_base)
}

fn validate_results_inner(
    result: &Value,
    spec: Option<&Value>,
    base: Option<&Path>,
) -> Vec<String> {
    let mut errors = schema_errors(result, "baseline-results");
    if !errors.is_empty() {
        return errors;
    }
    unique(
        &mut errors,
        names(&result["environments"], "id"),
        "baseline environment id",
    );
    for env in a(&result["environments"]) {
        require(
            &mut errors,
            s(&env["fingerprint_sha256"]) == environment_digest(env),
            "baseline environment fingerprint mismatch",
        );
        require(
            &mut errors,
            a(&env["cpu"]["affinity"])
                .iter()
                .all(|cpu| i(cpu) < i(&env["cpu"]["logical_cpus"])),
            "baseline CPU affinity outside logical CPU range",
        );
    }
    let entry = spec.and_then(|spec| {
        a(&spec["baseline"]["entries"])
            .iter()
            .find(|e| e["id"] == result["baseline_id"])
    });
    if let Some(spec) = spec {
        require(
            &mut errors,
            result["suite_id"] == spec["suite"]["id"],
            "baseline suite identity mismatch",
        );
        require(
            &mut errors,
            entry.is_some(),
            "unknown baseline implementation",
        );
        if let Some(entry) = entry {
            require(
                &mut errors,
                s(&result["implementation_sha256"]) == implementation_digest(entry),
                "baseline implementation digest mismatch",
            );
        }
    }
    let mut records = BTreeSet::new();
    for m in a(&result["measurements"]) {
        require(
            &mut errors,
            records.insert((
                s(&m["case_id"]),
                s(&m["environment_id"]),
                s(&m["recorded_at"]),
            )),
            "duplicate baseline measurement identity",
        );
        require(
            &mut errors,
            a(&result["environments"])
                .iter()
                .any(|env| env["id"] == m["environment_id"]),
            "unknown baseline measurement environment",
        );
        require(
            &mut errors,
            s(&m["inputs_sha256"]) == inputs_digest(&m["inputs"], &m["attrs"]),
            "baseline actual input digest mismatch",
        );
        unique(
            &mut errors,
            names(&m["inputs"], "name"),
            "baseline input name",
        );
        for input in a(&m["inputs"]) {
            if input["kind"] == "tensor" {
                require(
                    &mut errors,
                    input["shape"].is_array()
                        && input["strides"].is_array()
                        && a(&input["shape"]).len() == a(&input["strides"]).len()
                        && input["content_sha256"].is_string()
                        && input["dtype"].is_string()
                        && input["generator"].is_object()
                        && input["storage_offset"].as_u64().is_some(),
                    "baseline tensor input evidence incomplete",
                );
            } else {
                require(
                    &mut errors,
                    [
                        "shape",
                        "dtype",
                        "strides",
                        "content_sha256",
                        "generator",
                        "storage_offset",
                    ]
                    .iter()
                    .all(|k| input[*k].is_null()),
                    "baseline None input metadata must be null",
                );
            }
        }
        let passed = m["correctness"]["status"] == "passed";
        let checks = a(&m["correctness"]["checks"]);
        unique(
            &mut errors,
            names(&m["correctness"]["checks"], "id"),
            "baseline check id",
        );
        require(
            &mut errors,
            if passed {
                m["correctness"]["reason"].is_null()
                    && !checks.is_empty()
                    && checks
                        .iter()
                        .all(|c| c["status"] == "passed" && c["reason"].is_null())
            } else {
                m["correctness"]["reason"].is_object()
            },
            "baseline correctness status/evidence mismatch",
        );
        let timing = &m["timing"];
        require(
            &mut errors,
            !passed || timing.is_object(),
            "passed baseline measurement requires timing",
        );
        if timing.is_object() {
            let mut samples: Vec<_> = a(&timing["samples_ms"]).iter().map(f).collect();
            let finite = samples.iter().all(|v| v.is_finite() && *v >= 0.0)
                && f(&timing["median_ms"]).is_finite();
            samples.sort_by(f64::total_cmp);
            let n = samples.len();
            let median = if n == 0 {
                f64::NAN
            } else if n.is_multiple_of(2) {
                samples[n / 2 - 1] / 2.0 + samples[n / 2] / 2.0
            } else {
                samples[n / 2]
            };
            require(
                &mut errors,
                n as i64 == i(&timing["repetitions"]),
                "baseline sample count mismatch",
            );
            require(
                &mut errors,
                finite
                    && (median - f(&timing["median_ms"])).abs()
                        <= 1e-12_f64.max(1e-9 * median.abs()),
                "baseline median/sample mismatch",
            );
        }
        for artifact in a(&m["artifacts"]) {
            if let Err(e) = check_file(base, artifact) {
                errors.push(format!("baseline evidence artifact: {e}"));
            }
        }
        if result["purpose"] == "measured" && passed {
            require(
                &mut errors,
                !a(&m["artifacts"]).is_empty(),
                "measured baseline pass requires evidence artifacts",
            );
        }
        if let (Some(spec), Some(entry)) = (spec, entry) {
            let binding = a(&spec["operators"])
                .iter()
                .find(|op| op["id"] == entry["operator_id"])
                .and_then(|op| {
                    a(&op["cases"])
                        .iter()
                        .find(|c| c["id"] == m["case_id"])
                        .map(|c| (op, c))
                });
            require(
                &mut errors,
                a(&entry["case_ids"]).contains(&m["case_id"]) && binding.is_some(),
                "measurement case outside baseline coverage",
            );
            if let Some((op, case)) = binding {
                require(
                    &mut errors,
                    s(&m["case_contract_sha256"]) == case_contract_digest(spec, op, case),
                    "baseline case contract digest mismatch",
                );
                require(
                    &mut errors,
                    m["attrs"] == case["attrs"]
                        && a(&m["inputs"]).len() == a(&case["inputs"]).len(),
                    "baseline input/attribute contract mismatch",
                );
                for (actual, expected) in a(&m["inputs"]).iter().zip(a(&case["inputs"])) {
                    require(
                        &mut errors,
                        actual["name"] == expected["name"]
                            && actual["kind"] == expected["kind"]
                            && (actual["kind"] == "none"
                                || (actual["shape"] == expected["shape"]
                                    && actual["dtype"] == expected["dtype"]
                                    && actual["generator"] == expected["generator"])),
                        "baseline input descriptor differs from case",
                    );
                    if actual["kind"] == "tensor" {
                        let (strides, offset) = if expected["layout"]["kind"] == "strided" {
                            (
                                expected["layout"]["strides"].clone(),
                                expected["layout"]
                                    .get("storage_offset")
                                    .cloned()
                                    .unwrap_or(json!(0)),
                            )
                        } else {
                            let mut stride = 1_u64;
                            let mut values = Vec::new();
                            for dim in a(&expected["shape"]).iter().rev() {
                                values.push(stride);
                                stride = stride.saturating_mul(dim.as_u64().unwrap_or(0).max(1));
                            }
                            values.reverse();
                            (json!(values), json!(0))
                        };
                        require(
                            &mut errors,
                            actual["strides"] == strides && actual["storage_offset"] == offset,
                            "baseline input layout differs from case",
                        );
                    }
                }
                let outputs: BTreeSet<_> = a(&op["returns"]["names"]).iter().map(s).collect();
                let covered: BTreeSet<_> = checks
                    .iter()
                    .filter(|c| {
                        c["status"] == "passed" && ["numeric", "semantic"].contains(&s(&c["kind"]))
                    })
                    .flat_map(|c| a(&c["outputs"]).iter().map(s))
                    .collect();
                require(
                    &mut errors,
                    checks
                        .iter()
                        .all(|c| a(&c["outputs"]).iter().all(|o| outputs.contains(s(o))))
                        && (!passed || covered == outputs),
                    "baseline checks do not cover declared outputs",
                );
                if result["purpose"] == "measured" && passed {
                    require(
                        &mut errors,
                        checks
                            .iter()
                            .any(|c| c["kind"] == "execution" && c["status"] == "passed")
                            && spec["policies"]["timing"]["contract"]["status"] == "resolved",
                        "measured baseline pass requires native execution evidence and timing contract",
                    );
                    crate::spec::walk(case, String::new(), &mut |_, v| {
                        require(
                            &mut errors,
                            !(v["status"] == "pending"
                                || v["method"] == "pending"
                                || v["kind"] == "pending"),
                            "measured baseline pass has pending case contracts",
                        );
                    });
                }
                if timing.is_object()
                    && spec["policies"]["timing"]["contract"]["status"] == "resolved"
                {
                    require(
                        &mut errors,
                        timing["timing_contract_sha256"]
                            == spec["policies"]["timing"]["contract"]["sha256"],
                        "baseline timing contract digest mismatch",
                    );
                }
            }
        }
    }
    errors
}
