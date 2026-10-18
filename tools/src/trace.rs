//! Native harness trace checks with the original D2/D3/D4 policies.
use anyhow::{Result, ensure};
use regex::Regex;
use rvbench_cli::parse_json;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};
use walkdir::WalkDir;
const REQUIRED: [&str; 16] = [
    "schema_version",
    "run_id",
    "task",
    "condition",
    "op",
    "fixed_model_revision",
    "kernel_parent_sha256",
    "candidate_sha256",
    "injected_evidence_ids",
    "selected_strategy_id",
    "strategy_reason",
    "result_state",
    "evald_job_id",
    "cost_checkpoint_id",
    "state_update_ref",
    "memory_root",
];
const CONDITIONS: [&str; 10] = [
    "BARE",
    "KOPE",
    "E_ONLY",
    "R_ONLY",
    "G",
    "LOOSE",
    "FROZEN_MEMORY",
    "FROZEN_STRATEGY",
    "FLAT_G",
    "M",
];
const TASKS: [&str; 7] = ["E1", "E3", "E9", "E10_PASS2", "E4", "E7", "PILOT"];
const STATES: [&str; 7] = [
    "accepted_correctness_and_faster",
    "rejected_correctness",
    "rejected_slower",
    "repaired",
    "rolled_back",
    "timeout",
    "crash",
];
fn text(v: &Value) -> String {
    v.as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string())
}
fn issue(
    issues: &mut Vec<Value>,
    severity: &str,
    line: Option<usize>,
    code: &str,
    field: Option<&str>,
    message: impl Into<String>,
) {
    let item =
        json!({"severity":severity,"line":line,"code":code,"field":field,"message":message.into()});
    if !issues.contains(&item) {
        issues.push(item);
    }
}
fn hash(value: &Value, condition: &str, strategy: bool) -> bool {
    let Some(s) = value.as_str() else {
        return false;
    };
    let s = s.trim();
    let real = s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit());
    if strategy && ["FROZEN_MEMORY", "FROZEN_STRATEGY"].contains(&condition) {
        real
    } else {
        real || s.eq_ignore_ascii_case("pending")
            || (strategy
                && s.eq_ignore_ascii_case("none")
                && ["BARE", "KOPE", "E_ONLY", "R_ONLY", "LOOSE"].contains(&condition))
    }
}
pub fn check_file(path: &Path) -> Result<Value> {
    let mut issues = Vec::new();
    let content = fs::read_to_string(path)?;
    let mut records = Vec::new();
    let mut lines = 0;
    let parents: Vec<_> = path
        .ancestors()
        .skip(1)
        .take(3)
        .map(|p| {
            p.file_name()
                .map(|v| v.to_string_lossy().into_owned())
                .unwrap_or_default()
        })
        .collect();
    let identity = if parents.len() == 3 {
        Some((
            parents[2].as_str(),
            parents[1].as_str(),
            parents[0].as_str(),
        ))
    } else {
        None
    };
    let mem = Regex::new(r"(?i)(?:^|[^0-9A-Za-z_./\-])mem/")?;
    let runs = Regex::new(r"(?i)runs/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")?;
    for (index, line) in content.trim_start_matches('\u{feff}').lines().enumerate() {
        let number = index + 1;
        if line.trim().is_empty() {
            continue;
        }
        lines += 1;
        let rec = match parse_json(line.trim()) {
            Ok(v) if v.is_object() => v,
            Ok(_) => {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_LINE_NOT_OBJECT",
                    None,
                    "line must be an object",
                );
                continue;
            }
            Err(_) => {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_LINE_JSON",
                    None,
                    "invalid JSON line",
                );
                continue;
            }
        };
        let mut valid_fields = BTreeSet::new();
        for field in REQUIRED {
            let Some(value) = rec.get(field) else {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_MISSING_FIELD",
                    Some(field),
                    "required field missing",
                );
                continue;
            };
            if field != "injected_evidence_ids" && (value.is_array() || value.is_object()) {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_BAD_TYPE",
                    Some(field),
                    "expected scalar",
                );
                continue;
            }
            let empty = if field == "injected_evidence_ids" {
                !value.is_array() || value.as_array().unwrap().is_empty()
            } else {
                value.is_null()
                    || value.is_boolean()
                    || value.as_str().is_some_and(|s| s.trim().is_empty())
            };
            if empty
                && !(field == "injected_evidence_ids"
                    && value.is_array()
                    && text(&rec["condition"]).trim().eq_ignore_ascii_case("BARE"))
            {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_EMPTY_FIELD",
                    Some(field),
                    "required field is empty",
                );
                continue;
            }
            valid_fields.insert(field);
        }
        if valid_fields.contains("schema_version")
            && text(&rec["schema_version"]).trim() != "kelso.harness-trace/1.0"
        {
            issue(
                &mut issues,
                "ERROR",
                Some(number),
                "E_SCHEMA_VERSION",
                Some("schema_version"),
                "unsupported trace schema",
            );
        }
        let task = if valid_fields.contains("task") {
            text(&rec["task"])
        } else {
            String::new()
        };
        let condition = if valid_fields.contains("condition") {
            text(&rec["condition"])
        } else {
            String::new()
        };
        let op = if valid_fields.contains("op") {
            text(&rec["op"])
        } else {
            String::new()
        };
        for (field, valid) in [
            ("task", TASKS.contains(&task.as_str())),
            ("condition", CONDITIONS.contains(&condition.as_str())),
            (
                "result_state",
                STATES.contains(&text(&rec["result_state"]).as_str()),
            ),
        ] {
            if valid_fields.contains(field) && !valid {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_BAD_ENUM",
                    Some(field),
                    "unknown enum value",
                );
            }
        }
        for (key, value) in rec.as_object().unwrap() {
            if key.ends_with("_sha256")
                && !value.is_null()
                && !hash(
                    value,
                    &condition,
                    ["strategy_before_sha256", "strategy_after_sha256"].contains(&key.as_str()),
                )
            {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_BAD_HASH",
                    Some(key),
                    "invalid hash for condition",
                );
            }
        }
        for field in ["outcome", "state_update", "cost", "optional_mask"] {
            if rec.get(field).is_some_and(|v| !v.is_object()) {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_BAD_TYPE",
                    Some(field),
                    "expected object",
                );
            }
        }
        for field in ["external_idea_ids", "experience_case_ids"] {
            if rec.get(field).is_some_and(|v| !v.is_array()) {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_BAD_TYPE",
                    Some(field),
                    "expected list",
                );
            }
        }
        let prefix = format!("runs/{task}/{condition}/").to_lowercase();
        if !task.is_empty()
            && !condition.is_empty()
            && let Some(root) = rec["memory_root"].as_str().filter(|s| !s.trim().is_empty())
        {
            let root = root.trim().replace('\\', "/").to_lowercase();
            if !root.starts_with("runs/") {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_MEMORY_ROOT",
                    Some("memory_root"),
                    "memory_root must start with runs/",
                );
            }
            if !root.starts_with(&prefix) {
                issue(
                    &mut issues,
                    "ERROR",
                    Some(number),
                    "E_CROSS_ROOT",
                    Some("memory_root"),
                    "memory_root is outside this run",
                );
            }
        }
        rvbench_cli::spec::walk(&rec, String::new(), &mut |field, value| {
            if field == ".memory_root" {
                return;
            }
            if let Some(value) = value.as_str() {
                let norm = value.replace('\\', "/");
                let low = norm.to_lowercase();
                if low.contains("kope-mem") {
                    issue(
                        &mut issues,
                        "ERROR",
                        Some(number),
                        "E_CROSS_ROOT",
                        Some(field.trim_start_matches('.')),
                        "reference to shared kope-mem root",
                    );
                }
                if mem.is_match(&norm) {
                    issue(
                        &mut issues,
                        "ERROR",
                        Some(number),
                        "E_CROSS_ROOT",
                        Some(field.trim_start_matches('.')),
                        "reference to shared mem root",
                    );
                }
                if !task.is_empty() && !condition.is_empty() {
                    for token in runs.find_iter(&norm) {
                        if !token
                            .as_str()
                            .to_lowercase()
                            .starts_with(prefix.trim_end_matches('/'))
                        {
                            issue(
                                &mut issues,
                                "ERROR",
                                Some(number),
                                "E_CROSS_ROOT",
                                Some(field.trim_start_matches('.')),
                                format!("foreign runs path {}", token.as_str()),
                            );
                        }
                    }
                }
            }
        });
        if let Some((pt, pc, po)) = identity
            && !task.is_empty()
            && !condition.is_empty()
            && !op.is_empty()
            && (task != pt || condition != pc || op != po)
        {
            issue(
                &mut issues,
                "ERROR",
                None,
                "E_PATH_MISMATCH",
                Some("task/condition/op"),
                format!("record identity {task}/{condition}/{op} differs from directory"),
            );
        }
        records.push((number, rec));
    }
    if lines == 0 {
        issue(
            &mut issues,
            "ERROR",
            None,
            "E_EMPTY_FILE",
            None,
            "no candidate lines",
        );
    }
    let attempts: Vec<_> = records
        .iter()
        .filter_map(|(line, v)| v.get("attempt").map(|v| (*line, v)))
        .collect();
    if !attempts.is_empty() {
        if attempts.len() < records.len() {
            issue(
                &mut issues,
                "WARN",
                None,
                "W_ATTEMPT_MISSING",
                Some("attempt"),
                "attempt missing on some records",
            );
        }
        let mut values = Vec::new();
        for (line, value) in attempts {
            if let Some(n) = value.as_i64() {
                values.push(n);
            } else {
                issue(
                    &mut issues,
                    "WARN",
                    Some(line),
                    "W_ATTEMPT_TYPE",
                    Some("attempt"),
                    "attempt must be integer",
                );
            }
        }
        if !values.is_empty() && values.len() == records.len() {
            let expected: BTreeSet<_> = (1..=records.len() as i64).collect();
            let set: BTreeSet<_> = values.iter().copied().collect();
            if expected.difference(&set).next().is_some() {
                issue(
                    &mut issues,
                    "WARN",
                    None,
                    "W_ATTEMPT_GAP",
                    Some("attempt"),
                    "missing attempt number",
                );
            }
            if set.difference(&expected).next().is_some() {
                issue(
                    &mut issues,
                    "WARN",
                    None,
                    "W_ATTEMPT_GAP",
                    Some("attempt"),
                    "attempt outside expected range",
                );
            }
            if set.len() < values.len() {
                issue(
                    &mut issues,
                    "WARN",
                    None,
                    "W_ATTEMPT_DUP",
                    Some("attempt"),
                    "duplicate attempt",
                );
            }
            let mut ordered = values.clone();
            ordered.sort();
            if ordered != values {
                issue(
                    &mut issues,
                    "WARN",
                    None,
                    "W_ATTEMPT_ORDER",
                    Some("attempt"),
                    "attempts are not in file order",
                );
            }
        }
    }
    let mut jobs = BTreeMap::new();
    for (_, record) in &records {
        if record["evald_job_id"].is_string() || record["evald_job_id"].is_i64() {
            *jobs.entry(text(&record["evald_job_id"])).or_insert(0) += 1;
        }
    }
    for (id, count) in jobs {
        if !id.trim().is_empty() && count > 1 {
            issue(
                &mut issues,
                "WARN",
                None,
                "W_DUP_JOB",
                Some("evald_job_id"),
                format!("job id {id} appears on multiple records"),
            );
        }
    }
    let errors = issues.iter().filter(|i| i["severity"] == "ERROR").count();
    let warnings = issues.iter().filter(|i| i["severity"] == "WARN").count();
    Ok(
        json!({"path":path.to_string_lossy().replace('\\',"/"),"task":identity.map(|x|x.0),"condition":identity.map(|x|x.1),"op":identity.map(|x|x.2),"lines":lines,"errors":errors,"warnings":warnings,"issues":issues}),
    )
}
pub fn check(base: &Path) -> Result<Value> {
    let mut reports = Vec::new();
    if base.is_file() {
        reports.push(check_file(base)?);
    } else {
        for entry in WalkDir::new(base).follow_links(false) {
            let entry = entry?;
            if entry.file_type().is_file() && entry.file_name() == "trace.jsonl" {
                reports.push(check_file(entry.path())?);
            }
        }
    }
    ensure!(!reports.is_empty(), "no trace.jsonl files found");
    let errors = reports
        .iter()
        .map(|r| r["errors"].as_u64().unwrap())
        .sum::<u64>();
    let warnings = reports
        .iter()
        .map(|r| r["warnings"].as_u64().unwrap())
        .sum::<u64>();
    let lines = reports
        .iter()
        .map(|r| r["lines"].as_u64().unwrap())
        .sum::<u64>();
    let mut by_task = BTreeMap::<String, Value>::new();
    let mut by_condition = BTreeMap::<String, Value>::new();
    for report in &reports {
        for (field, groups) in [("task", &mut by_task), ("condition", &mut by_condition)] {
            let group = groups
                .entry(report[field].as_str().unwrap_or("unknown").into())
                .or_insert_with(|| json!({"files":0,"lines":0,"errors":0,"warnings":0}));
            group["files"] = json!(group["files"].as_u64().unwrap() + 1);
            for key in ["lines", "errors", "warnings"] {
                group[key] = json!(group[key].as_u64().unwrap() + report[key].as_u64().unwrap());
            }
        }
    }
    Ok(
        json!({"tool":"trace_check","version":"1.1-rust","generated_at":chrono::Utc::now().to_rfc3339(),"base":base,"ok":errors==0,"summary":{"files":reports.len(),"lines":lines,"errors":errors,"warnings":warnings},"by_task":by_task,"by_condition":by_condition,"files":reports}),
    )
}
