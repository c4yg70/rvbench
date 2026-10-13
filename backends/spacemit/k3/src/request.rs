use super::config::{K3Config, LANES};
use anyhow::{Result, ensure};
use regex::Regex;
use rvbench_core::json::sha256;
use rvbench_runtime::PreparedJob;
use serde_json::{Value, json};
use std::{collections::BTreeSet, sync::LazyLock};

static OP: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z_][a-z0-9_]{0,47}$").unwrap());
static SHAPE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$").unwrap());
fn int(v: &Value, min: u64, max: u64, name: &str) -> Result<u64> {
    let n = v
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("{name} must be an integer"))?;
    ensure!(n >= min && n <= max, "{name} out of range");
    Ok(n)
}
fn is_hash(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit()))
}

pub fn validate_request(cfg: &K3Config, body: &Value) -> Result<PreparedJob> {
    ensure!(body.is_object(), "body must be a JSON object");
    let bench = body
        .get("bench")
        .unwrap_or(&json!("op"))
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("bench must be a string"))?
        .to_owned();
    ensure!(["op", "gemm"].contains(&bench.as_str()), "invalid bench");
    let op = body["op"]
        .as_str()
        .filter(|s| OP.is_match(s))
        .ok_or_else(|| anyhow::anyhow!("invalid op"))?;
    if bench == "gemm" {
        ensure!(
            ["mm", "addmm", "bmm"].contains(&op),
            "gemm only supports mm/addmm/bmm"
        );
    }
    let dtype = body
        .get("dtype")
        .unwrap_or(&json!("float32"))
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("dtype must be string"))?
        .to_owned();
    ensure!(
        ["float32", "float16", "int32", "int64", "bool"].contains(&dtype.as_str()),
        "invalid dtype"
    );
    let shapes = if body["shape"].is_string() {
        vec![body["shape"].clone()]
    } else {
        body["shape"]
            .as_array()
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("shape must be a string or list"))?
    };
    ensure!(
        !shapes.is_empty()
            && shapes.len() <= 16
            && shapes
                .iter()
                .all(|v| v.as_str().is_some_and(|s| SHAPE.is_match(s))),
        "invalid shape"
    );
    ensure!(
        shapes
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect::<BTreeSet<_>>()
            .len()
            == shapes.len(),
        "duplicate shape"
    );
    ensure!(
        bench != "gemm" || body["shape"].is_string(),
        "gemm requires a single shape-list string"
    );
    let perf = &cfg.value["evaluators"]["perf"];
    let warmup = int(
        body.get("warmup").unwrap_or(&perf["warmup_times"]),
        0,
        10000,
        "warmup",
    )?;
    let rep = int(
        body.get("rep").unwrap_or(&perf["repeat_times"]),
        1,
        10000,
        "rep",
    )?;
    let batch = int(body.get("batch").unwrap_or(&json!(8)), 1, 4096, "batch")?;
    if let Some(v) = body.get("skip_torch") {
        ensure!(
            bench == "op" && v.is_boolean(),
            "skip_torch must be boolean and bench=op"
        );
    }
    if let Some(v) = body.get("seed") {
        ensure!(bench == "op", "seed only supports bench=op");
        int(v, 0, 2147483647, "seed")?;
    }
    let restore = body
        .get("restore")
        .unwrap_or(&Value::Bool(true))
        .as_bool()
        .ok_or_else(|| anyhow::anyhow!("restore must be boolean"))?;
    let raw_lane = body
        .get("lane")
        .unwrap_or(&json!("timing"))
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("lane must be string"))?
        .to_owned();
    let evaluator = LANES
        .iter()
        .find(|(lane, _)| *lane == raw_lane)
        .map(|(_, name)| *name)
        .ok_or_else(|| anyhow::anyhow!("invalid lane"))?;
    ensure!(
        cfg.value["evaluators"][evaluator]["enabled"] == true,
        "requested evaluator is disabled"
    );
    let lane = cfg
        .value
        .get("force_lane")
        .and_then(Value::as_str)
        .unwrap_or(&raw_lane);
    let timeout = body
        .get("timeout_min")
        .or_else(|| cfg.value["evaluators"][evaluator].get("timeout_min"))
        .unwrap_or(&cfg.value["timeout_default_min"]);
    let timeout = timeout
        .as_f64()
        .filter(|n| {
            n.is_finite()
                && *n >= 0.1
                && *n
                    <= cfg.value["timeout_max_min"].as_f64().unwrap()
                        * if body["shape"].is_array() {
                            shapes.len() as f64
                        } else {
                            1.0
                        }
        })
        .ok_or_else(|| anyhow::anyhow!("invalid timeout_min"))?;
    let executor = body.get("executor").cloned().unwrap_or(Value::Null);
    ensure!(
        executor.is_null() || ["subprocess", "warmfork"].contains(&executor.as_str().unwrap_or("")),
        "invalid executor"
    );
    let files = body.get("files").cloned().unwrap_or(json!([]));
    ensure!(
        files.is_array() && files.as_array().unwrap().len() <= 32,
        "files must be an array of at most 32 entries"
    );
    let mut paths = BTreeSet::new();
    let mut identities = Vec::new();
    for file in files.as_array().unwrap() {
        let path = file["path"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("file path must be string"))?;
        let content = file["content"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("file content must be string"))?;
        ensure!(paths.insert(path), "duplicate deployment path");
        cfg.resolve_path(path)?;
        if let Some(v) = file.get("expect_sha256") {
            ensure!(is_hash(v), "invalid file expect_sha256");
        }
        identities
            .push(json!({"path":path,"sha256":sha256(content.as_bytes()),"bytes":content.len()}));
    }
    if let Some(mode) = body.get("mode") {
        ensure!(
            ["deploy", "current_verified"].contains(&mode.as_str().unwrap_or("")),
            "invalid mode"
        );
        if mode == "deploy" {
            ensure!(!paths.is_empty(), "deploy requires files");
        } else {
            ensure!(
                paths.is_empty()
                    && body.get("expect_path").is_some()
                    && body.get("expect_sha256").is_some(),
                "current_verified requires identity and no files"
            );
        }
    }
    ensure!(
        cfg.value["require_verified_base"] != true
            || !paths.is_empty()
            || body.get("mode").is_some(),
        "require_verified_base rejects unverified current slots"
    );
    let mut sanitized = json!({"op":op,"shape":body["shape"],"dtype":dtype,"warmup":warmup,"rep":rep,"bench":bench,"restore":restore,"timeout_min":timeout,"executor":executor,"files":identities});
    for key in ["skip_torch", "seed", "mode"] {
        if let Some(v) = body.get(key) {
            sanitized[key] = v.clone();
        }
    }
    if body.get("expect_path").is_some() || body.get("expect_sha256").is_some() {
        let path = body["expect_path"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("invalid expect_path"))?;
        cfg.resolve_path(path)?;
        ensure!(is_hash(&body["expect_sha256"]), "invalid expect_sha256");
        sanitized["expect_path"] = body["expect_path"].clone();
        sanitized["expect_sha256"] = body["expect_sha256"].clone();
    }
    if let Some(cid) = body.get("client_request_id") {
        let cid = cid
            .as_str()
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 64
                    && s.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(&c))
            })
            .ok_or_else(|| anyhow::anyhow!("invalid client_request_id"))?;
        sanitized["client_request_id"] = json!(cid);
    }
    if body.get("lane").is_some() || cfg.value.get("force_lane").is_some_and(Value::is_string) {
        sanitized["lane"] = json!(lane);
    }
    if lane != "timing" {
        sanitized["warmup"] = json!(0);
        sanitized["rep"] = json!(1);
        sanitized["requested_warmup"] = json!(warmup);
        sanitized["requested_rep"] = json!(rep);
    }
    if bench == "gemm" {
        sanitized["batch"] = json!(batch);
    }
    Ok(PreparedJob {
        request: sanitized,
        payload: body.clone(),
        lane: lane.into(),
    })
}
