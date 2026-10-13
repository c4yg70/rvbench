use crate::{
    a, b, f, hidden_readiness_blockers, i, names, readiness_blockers, require, resolve_hidden_case,
    s, schema_errors, set, spec_digest, unique, validate_hidden_spec, validate_spec,
};
use chrono::{DateTime, FixedOffset};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path};

const OUTCOMES: [&str; 6] = [
    "passed",
    "failed",
    "skipped",
    "unverifiable",
    "infra_error",
    "canceled",
];
fn time(v: &Value) -> Option<DateTime<FixedOffset>> {
    v.as_str()
        .and_then(|v| DateTime::parse_from_rfc3339(v).ok())
}
fn close(v: &Value, expected: f64) -> bool {
    let actual = f(v);
    actual.is_finite()
        && expected.is_finite()
        && (actual - expected).abs() <= 1e-12_f64.max(1e-9 * actual.abs().max(expected.abs()))
}
fn median(samples: &Value) -> f64 {
    let mut values: Vec<_> = a(samples).iter().map(f).collect();
    values.sort_by(f64::total_cmp);
    if values.is_empty() {
        return f64::NAN;
    }
    let mid = values.len() / 2;
    if values.len() % 2 == 0 {
        (values[mid - 1] + values[mid]) / 2.0
    } else {
        values[mid]
    }
}

pub fn validate_eval_results(
    result: &Value,
    public: Option<&Value>,
    hidden: Option<&Value>,
    base: Option<&Path>,
) -> Vec<String> {
    let mut errors = schema_errors(result, "results");
    if !errors.is_empty() {
        return errors;
    }
    let run = &result["run"];
    let execution = &result["execution"];
    let start = time(&run["created_at"]);
    let finish = time(&run["finished_at"]);
    require(
        &mut errors,
        start.is_some() && finish.is_none_or(|end| start.is_some_and(|begin| end >= begin)),
        "run timestamps out of order",
    );
    if ["completed", "canceled"].contains(&s(&run["status"])) {
        require(
            &mut errors,
            finish.is_some(),
            "terminal run requires finished_at",
        );
    }
    if run["mode"] == "standalone" {
        require(
            &mut errors,
            run["manager_arch"].is_null(),
            "standalone manager_arch must be null",
        );
    } else {
        require(
            &mut errors,
            !run["manager_arch"].is_null(),
            "distributed requires manager_arch",
        );
    }
    if execution["mode"] == "perf" {
        require(
            &mut errors,
            !execution["timing_contract_sha256"].is_null(),
            "perf requires timing contract digest",
        );
    }
    if execution["candidate"]["kind"] != "reference" {
        require(
            &mut errors,
            !execution["candidate"]["sha256"].is_null(),
            "candidate code requires sha256",
        );
    }
    if !result["specs"]["hidden"].is_null() {
        require(
            &mut errors,
            result["specs"]["hidden"]["suite_id"] == result["specs"]["public"]["suite_id"],
            "public/hidden suite identity mismatch",
        );
    }
    unique(
        &mut errors,
        names(&result["environments"], "id"),
        "environment id",
    );
    let environments: BTreeMap<_, _> = a(&result["environments"])
        .iter()
        .map(|v| (s(&v["id"]).to_owned(), v))
        .collect();
    for env in environments.values() {
        if run["mode"] == "distributed" {
            require(
                &mut errors,
                !env["worker_id"].is_null() && !env["boot_id"].is_null(),
                "distributed environment requires worker and boot identities",
            );
        }
        require(
            &mut errors,
            a(&env["cpu"]["affinity"])
                .iter()
                .all(|cpu| i(cpu) < i(&env["cpu"]["logical_cpus"])),
            "CPU affinity outside logical CPU range",
        );
    }
    let mut public_cases = BTreeMap::new();
    let mut hidden_cases = BTreeMap::new();
    if let Some(public) = public {
        errors.extend(validate_spec(public, base));
        if !errors.is_empty() {
            return errors;
        }
        let expected = json!({"suite_id":public["suite"]["id"],"schema_version":public["schema_version"],"version":public["version"],"sha256":spec_digest(public)});
        require(
            &mut errors,
            result["specs"]["public"] == expected,
            "public spec version/digest mismatch",
        );
        for op in a(&public["operators"]) {
            for case in a(&op["cases"]) {
                public_cases.insert(s(&case["id"]), (op, case));
            }
        }
        if run["purpose"] == "measured"
            && a(&result["cases"]).iter().any(|c| c["status"] == "passed")
        {
            require(
                &mut errors,
                readiness_blockers(public).is_empty(),
                "measured pass requires ready public contracts",
            );
        }
    }
    if let Some(hidden) = hidden {
        if let Some(public) = public {
            errors.extend(validate_hidden_spec(hidden, public, base));
            if !errors.is_empty() {
                return errors;
            }
            let expected = json!({"suite_id":hidden["suite_id"],"schema_version":hidden["schema_version"],"version":hidden["version"],"sha256":spec_digest(hidden)});
            require(
                &mut errors,
                result["specs"]["hidden"] == expected,
                "hidden spec version/digest mismatch",
            );
            for case in a(&hidden["cases"]) {
                hidden_cases.insert(s(&case["id"]), case);
            }
            if run["purpose"] == "measured"
                && a(&result["cases"])
                    .iter()
                    .any(|c| c["source"] == "hidden" && c["status"] == "passed")
            {
                require(
                    &mut errors,
                    hidden_readiness_blockers(hidden, public).is_empty(),
                    "measured pass requires ready hidden contracts",
                );
            }
        } else {
            errors.push("hidden binding requires public spec".into());
        }
    }
    if !errors.is_empty() {
        return errors;
    }
    unique(&mut errors, names(&result["cases"], "case_id"), "case id");
    let mut attempt_ids = std::collections::BTreeSet::new();
    let mut speedups: BTreeMap<String, Vec<f64>> = environments
        .keys()
        .map(|id| (id.clone(), Vec::new()))
        .collect();
    for case in a(&result["cases"]) {
        let label = s(&case["case_id"]);
        let mut check = |condition: bool, message: &str| {
            require(&mut errors, condition, format!("{label}: {message}"))
        };
        check(
            case["source"] != "hidden" || !result["specs"]["hidden"].is_null(),
            "hidden case requires hidden spec reference",
        );
        let redacted = run["visibility"] == "public" && case["source"] == "hidden";
        check(b(&case["redacted"]) == redacted, "invalid redaction policy");
        if redacted {
            check(
                a(&case["attempts"]).is_empty()
                    && case["selected_attempt_id"].is_null()
                    && case["reason"].is_null(),
                "public hidden case must remove attempts, selection and free-text reason",
            );
        } else if case["status"] != "passed" {
            check(!case["reason"].is_null(), "non-pass case requires reason");
        }
        if case["source"] == "public" && public.is_some() {
            check(public_cases.contains_key(label), "unknown public case");
            if let Some((op, _)) = public_cases.get(label) {
                check(
                    case["operator"] == op["id"] && s(&case["contract_ref"]) == label,
                    "public case operator/contract mismatch",
                );
            }
        }
        if case["source"] == "hidden" && hidden.is_some() {
            check(hidden_cases.contains_key(label), "unknown hidden case");
            if let Some(bound) = hidden_cases.get(label) {
                check(
                    case["operator"] == bound["operator"]
                        && case["contract_ref"] == bound["contract_ref"],
                    "hidden case operator/contract mismatch",
                );
            }
        }
        if case["source"] == "hidden" && public.is_some() {
            check(
                public_cases.contains_key(s(&case["contract_ref"])),
                "unknown hidden contract_ref",
            );
        }
        let bound_case = if case["source"] == "public" {
            public_cases.get(label).map(|(_, v)| (*v).clone())
        } else {
            hidden_cases
                .get(label)
                .and_then(|h| public.and_then(|p| resolve_hidden_case(p, h).ok()))
        };
        let selected = if case["selected_attempt_id"].is_null() {
            None
        } else {
            a(&case["attempts"])
                .iter()
                .find(|a| a["id"] == case["selected_attempt_id"])
        };
        if !case["selected_attempt_id"].is_null() {
            check(selected.is_some(), "selected attempt not found");
        }
        if !redacted && case["status"] == "passed" {
            check(selected.is_some(), "passed requires selected attempt");
        }
        if let Some(selected) = selected {
            check(
                selected["status"] == case["status"]
                    && b(&selected["trusted"])
                    && selected["cleanup"]["status"] == "ok",
                "selected attempt must match outcome and be trusted/clean",
            );
        }
        for attempt in a(&case["attempts"]) {
            let aid = s(&attempt["id"]);
            let mut check = |condition: bool, message: &str| {
                require(&mut errors, condition, format!("{aid}: {message}"))
            };
            check(attempt_ids.insert(aid.to_owned()), "duplicate attempt id");
            check(
                environments.contains_key(s(&attempt["environment_id"])),
                "unknown environment",
            );
            if run["mode"] == "distributed" {
                check(
                    !attempt["job_id"].is_null() && !attempt["lease_epoch"].is_null(),
                    "distributed attempt requires job and lease epoch",
                );
            } else {
                check(
                    attempt["job_id"].is_null() && attempt["lease_epoch"].is_null(),
                    "standalone job/lease must be null",
                );
            }
            let ast = time(&attempt["started_at"]);
            let end = time(&attempt["finished_at"]);
            check(
                ast.is_some_and(|t| start.is_some_and(|r| t >= r))
                    && end.is_none_or(|t| ast.is_some_and(|r| t >= r)),
                "timestamps out of order",
            );
            check(
                finish.is_none_or(|r| end.is_some_and(|t| t <= r)),
                "unfinished/out-of-run attempt",
            );
            if attempt["status"] != "passed" {
                check(
                    !attempt["reason"].is_null(),
                    "non-pass attempt requires reason",
                );
            }
            if attempt["cleanup"]["status"] != "ok" {
                check(
                    !attempt["cleanup"]["reason"].is_null(),
                    "failed/unknown cleanup requires reason",
                );
            }
            if b(&attempt["trusted"]) {
                check(
                    attempt["cleanup"]["status"] == "ok"
                        && end.is_some()
                        && !attempt["exit_code"].is_null(),
                    "trusted requires finished execution and successful cleanup",
                );
                check(
                    attempt["loaded_candidate_sha256"] == execution["candidate"]["sha256"],
                    "loaded candidate digest mismatch",
                );
            }
            // Duplicate checks use local sets to retain a single errors borrow.
            for (items, key, description) in [
                (&attempt["inputs"], "name", "input name"),
                (&attempt["outputs"], "name", "output name"),
                (&attempt["checks"], "id", "check id"),
            ] {
                let ns = names(items, key);
                check(
                    set(&ns).len() == ns.len(),
                    &format!("duplicate {description}"),
                );
            }
            for input in a(&attempt["inputs"]) {
                if input["kind"] == "none" {
                    check(
                        ["shape", "dtype", "strides", "content_sha256", "generator"]
                            .iter()
                            .all(|k| input[*k].is_null()),
                        "none input metadata must be null",
                    );
                } else {
                    check(
                        !input["shape"].is_null()
                            && !input["dtype"].is_null()
                            && !input["strides"].is_null(),
                        "tensor input metadata missing",
                    );
                    if !input["shape"].is_null() && !input["strides"].is_null() {
                        check(
                            a(&input["shape"]).len() == a(&input["strides"]).len(),
                            "input rank/strides mismatch",
                        );
                    }
                }
            }
            let outputs = a(&attempt["outputs"]);
            let output_names = names(&attempt["outputs"], "name");
            if attempt["status"] == "passed" {
                if let Some((op, contract)) = public_cases.get(s(&case["contract_ref"])) {
                    check(
                        json!(output_names) == op["returns"]["names"],
                        "outputs must preserve every return slot in order",
                    );
                    check(
                        names(&attempt["inputs"], "name") == names(&contract["inputs"], "name"),
                        "input slots mismatch",
                    );
                }
                if let Some(bound) = &bound_case {
                    check(
                        attempt["attrs"] == bound["attrs"],
                        "attrs drift from bound case",
                    );
                    for input in a(&attempt["inputs"]) {
                        if let Some(expected) = a(&bound["inputs"])
                            .iter()
                            .find(|v| v["name"] == input["name"])
                        {
                            check(
                                ["kind", "shape", "dtype", "generator"]
                                    .iter()
                                    .all(|k| input[*k] == expected[*k]),
                                "input metadata drift from bound case",
                            );
                        }
                    }
                    for expected in a(&bound["outputs"]) {
                        if let Some(actual) = outputs.iter().find(|v| v["name"] == expected["name"])
                            && expected["kind"] != "pending"
                        {
                            check(
                                ["kind", "shape", "dtype"]
                                    .iter()
                                    .all(|k| actual[*k] == expected[*k]),
                                "output metadata drift from bound case",
                            );
                        }
                    }
                }
            }
            for out in outputs {
                if out["kind"] == "none" {
                    check(
                        ["shape", "dtype", "content_sha256"]
                            .iter()
                            .all(|k| out[*k].is_null()),
                        "none output metadata must be null",
                    );
                } else if out["status"] == "produced" {
                    check(
                        !out["dtype"].is_null() && !out["content_sha256"].is_null(),
                        "produced output metadata missing",
                    );
                    if out["kind"] == "tensor" {
                        check(!out["shape"].is_null(), "tensor output shape missing");
                    }
                }
                if out["status"] == "missing" {
                    check(
                        out["content_sha256"].is_null(),
                        "missing output cannot have content digest",
                    );
                }
            }
            for c in a(&attempt["checks"]) {
                check(
                    a(&c["outputs"])
                        .iter()
                        .all(|name| output_names.contains(&s(name).to_owned())),
                    "check references unknown output",
                );
                if c["status"] != "passed" {
                    check(!c["reason"].is_null(), "non-pass check requires reason");
                }
            }
            if attempt["status"] == "passed" {
                check(
                    b(&attempt["trusted"]) && attempt["exit_code"].as_i64() == Some(0),
                    "passed requires trusted zero-exit execution",
                );
                check(
                    !outputs.is_empty() && outputs.iter().all(|o| o["status"] == "produced"),
                    "passed requires all outputs produced",
                );
                check(
                    a(&attempt["inputs"])
                        .iter()
                        .all(|v| v["kind"] == "none" || !v["content_sha256"].is_null()),
                    "passed requires input content digests",
                );
                check(
                    !a(&attempt["checks"]).is_empty()
                        && a(&attempt["checks"])
                            .iter()
                            .all(|v| v["status"] == "passed"),
                    "passed requires passing checks",
                );
                if execution["mode"] == "func" {
                    check(
                        a(&attempt["checks"])
                            .iter()
                            .any(|v| v["kind"] == "execution"),
                        "func requires execution check",
                    );
                } else {
                    let verified: Vec<_> = a(&attempt["checks"])
                        .iter()
                        .filter(|c| c["kind"] != "execution")
                        .flat_map(|c| a(&c["outputs"]).iter().map(|v| s(v).to_owned()))
                        .collect();
                    check(
                        set(&verified) == set(&output_names),
                        "accuracy requires checks for every output",
                    );
                }
            }
            let timing = &attempt["timing"];
            if execution["mode"] == "perf" && attempt["status"] == "passed" {
                check(!timing.is_null(), "perf pass requires timing");
            }
            if !timing.is_null() {
                check(
                    timing["warmup"] == execution["warmup"]
                        && timing["repetitions"] == execution["repetitions"],
                    "timing policy mismatch",
                );
                for side in ["reference", "candidate"] {
                    let samples = &timing[format!("{side}_samples_ms")];
                    let med = &timing[format!("{side}_median_ms")];
                    check(
                        samples.is_null() == med.is_null(),
                        "samples/median availability mismatch",
                    );
                    if !samples.is_null() {
                        check(
                            a(samples).len() as i64 == i(&timing["repetitions"]),
                            "repetition count mismatch",
                        );
                        check(close(med, median(samples)), "median mismatch");
                    }
                }
                let comparable = execution["mode"] == "perf"
                    && b(&attempt["trusted"])
                    && attempt["status"] == "passed"
                    && !timing["reference_median_ms"].is_null()
                    && !timing["candidate_median_ms"].is_null();
                if comparable {
                    check(
                        close(
                            &timing["speedup"],
                            f(&timing["reference_median_ms"]) / f(&timing["candidate_median_ms"]),
                        ),
                        "speedup mismatch",
                    );
                    if selected.is_some_and(|v| std::ptr::eq(v, attempt))
                        && !timing["speedup"].is_null()
                        && let Some(ratios) = speedups.get_mut(s(&attempt["environment_id"]))
                    {
                        ratios.push(f(&timing["speedup"]));
                    }
                } else {
                    check(
                        timing["speedup"].is_null(),
                        "unavailable/non-perf speedup must be null",
                    );
                }
                if execution["mode"] == "perf" && attempt["status"] == "passed" {
                    check(comparable, "perf pass requires paired samples");
                }
            }
            for artifact in a(&attempt["artifacts"]) {
                let path = s(&artifact["path"]);
                check(
                    !path.starts_with('/')
                        && !path.split('/').any(|p| p == "..")
                        && !path.contains(['\\', ':']),
                    "artifact path must stay relative to result directory",
                );
            }
        }
    }
    let summary = &result["summary"];
    require(
        &mut errors,
        i(&summary["total"]) == a(&result["cases"]).len() as i64,
        "summary total mismatch",
    );
    let expected: serde_json::Map<_, _> = OUTCOMES
        .iter()
        .map(|state| {
            (
                state.to_string(),
                json!(
                    a(&result["cases"])
                        .iter()
                        .filter(|v| s(&v["status"]) == *state)
                        .count()
                ),
            )
        })
        .collect();
    require(
        &mut errors,
        summary["outcomes"] == Value::Object(expected),
        "summary outcome counts mismatch",
    );
    let group_names = names(&summary["timing_groups"], "environment_id");
    unique(&mut errors, group_names.clone(), "timing group");
    let expected_groups = speedups
        .iter()
        .filter(|(_, ratios)| !ratios.is_empty())
        .map(|(name, _)| name.clone())
        .collect();
    require(
        &mut errors,
        set(&group_names) == expected_groups,
        "timing groups must include exactly environments with eligible selected attempts",
    );
    for group in a(&summary["timing_groups"]) {
        let ratios = speedups
            .get(s(&group["environment_id"]))
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        require(
            &mut errors,
            i(&group["eligible_case_count"]) == ratios.len() as i64,
            "timing group case count mismatch",
        );
        if !ratios.is_empty() {
            let mean = (ratios.iter().map(|v| v.ln()).sum::<f64>() / ratios.len() as f64).exp();
            require(
                &mut errors,
                close(&group["geomean_speedup"], mean),
                "timing group geomean mismatch",
            );
        }
    }
    errors
}
