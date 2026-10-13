//! Durable native ticket submit/poll/finalize loop. Ticket paths own their wave.
use crate::{Options, client::Client, finalize_into, safe_name};
use anyhow::{Result, ensure};
use reqwest::Method;
use rvbench_core::{load_json, spec_digest};
use rvbench_runtime::files::atomic_json;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    thread,
    time::Duration,
};
use walkdir::WalkDir;

pub fn expand(
    doc: &Value,
    wave: &Path,
    cond: &str,
    lane: &str,
    attempt: i64,
    dry: bool,
) -> Result<Vec<Value>> {
    ensure!(
        rvbench_core::suite::validate_suite(doc, &["k3", "k1", "sg2042", "rvv_general"]).is_empty(),
        "invalid suite"
    );
    ensure!(
        ["timing", "correctness", "func"].contains(&lane) && attempt >= 1,
        "invalid lane/attempt"
    );
    for part in cond.split('/') {
        safe_name(part)?;
    }
    let kind = match lane {
        "timing" => "timing-final",
        "func" => "func",
        _ => "correctness",
    };
    let mut out = Vec::new();
    for op in doc["ops"].as_array().unwrap() {
        let name = op["op"].as_str().unwrap();
        safe_name(name)?;
        let stem = format!("{}--{name}--r{attempt}", doc["name"].as_str().unwrap());
        safe_name(&stem)?;
        let mut note = format!(
            "suite {} {lane} lane r{attempt}",
            doc["name"].as_str().unwrap()
        );
        if rvbench_k3::judge::STOCHASTIC.contains(&name) {
            note.push_str(" | stochastic: needs_stats_check");
        }
        let ticket = json!({"kind":kind,"op":op["op"],"shape":op["shape"],"dtype":op["dtype"],"attempt":attempt,"candidate_sha256":null,"candidate_path":null,"incumbent_timing":null,"is_base_incumbent":true,"payload_hint":{"deploy_path":format!("flag_gems/ops/{name}.py"),"timeout_min":doc["defaults"].get("timeout_min").cloned().unwrap_or(json!(30)),"notes":note}});
        let path = wave
            .join(cond)
            .join(name)
            .join("tickets")
            .join(format!("{stem}.ticket.json"));
        let skipped = wave
            .join(cond)
            .join(name)
            .join("results")
            .join(format!("{stem}.raw.json"))
            .is_file();
        if !dry && !skipped {
            atomic_json(&path, &ticket)?;
        }
        out.push(json!({"stem":stem,"op":name,"path":path,"skipped":skipped,"warmup":doc["defaults"].get("warmup").cloned().unwrap_or(json!(5)),"rep":doc["defaults"].get("rep").cloned().unwrap_or(json!(5)),"ticket":ticket}));
    }
    Ok(out)
}
fn tickets(options: &Options) -> Result<Vec<PathBuf>> {
    let runs = options.workspace().join("runs");
    ensure!(runs.is_dir(), "workspace must contain runs/");
    let mut paths = Vec::new();
    for entry in WalkDir::new(&runs).follow_links(false) {
        let entry = entry?;
        if entry.file_type().is_file()
            && entry
                .file_name()
                .to_string_lossy()
                .ends_with(".ticket.json")
        {
            paths.push(entry.into_path());
        }
    }
    paths.sort_by_key(|p| fs::metadata(p).and_then(|m| m.modified()).ok());
    Ok(paths)
}
fn stem(path: &Path) -> Result<&str> {
    path.file_name()
        .and_then(|v| v.to_str())
        .and_then(|v| v.strip_suffix(".ticket.json"))
        .ok_or_else(|| anyhow::anyhow!("invalid ticket filename"))
}
fn wave_key(path: &Path, workspace: &Path, cond: &str, op: &str, backend: &str) -> Result<String> {
    let rel = path.strip_prefix(workspace)?;
    let parts: Vec<_> = rel
        .iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect();
    let prefix = parts.get(1).and_then(|wave| match wave.as_str() {
        "astra_0911_expand_late" => Some("expand_late"),
        "astra_0911_expand_next" => Some("expand_next"),
        "astra_0911_expand24" => Some("expand24"),
        "astra_0911_3" => Some("astra7"),
        "astra_0911_2" => Some("astra6"),
        "astra_0910_5" => Some("astra5"),
        "astra_0910_1" => Some("astra"),
        _ => None,
    });
    if backend != "k3" {
        return Ok(rvbench_k3::judge::registry_key(cond, op, backend));
    }
    Ok(prefix
        .map(|prefix| format!("{prefix}/{cond}/{op}"))
        .unwrap_or_else(|| rvbench_k3::judge::registry_key(cond, op, backend)))
}
fn context(path: &Path, workspace: &Path) -> Result<(String, String, PathBuf)> {
    let op_dir = path
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow::anyhow!("ticket path is too shallow"))?;
    let relative = op_dir.strip_prefix(workspace.join("runs"))?;
    let parts: Vec<_> = relative
        .iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect();
    ensure!(
        parts.len() >= 3,
        "ticket must be runs/wave/condition/op/tickets/stem.ticket.json"
    );
    for part in &parts {
        safe_name(part)?;
    }
    let op = parts.last().unwrap().clone();
    let cond = parts[1..parts.len() - 1].join("/");
    Ok((cond, op, op_dir.join("results")))
}
fn body(options: &Options, ticket: &Value) -> Result<Value> {
    let op = ticket["op"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("ticket lacks op"))?;
    safe_name(op)?;
    let hint = &ticket["payload_hint"];
    let kind = ticket["kind"].as_str().unwrap_or("correctness");
    let lane = match kind {
        "func" => "func",
        "correctness" => "correctness",
        "timing-final" => "timing",
        _ => anyhow::bail!("unknown ticket kind"),
    };
    let mut shape = ticket["shape"].clone();
    if shape.is_array() && shape.as_array().unwrap().iter().all(Value::is_number) {
        shape = json!(
            shape
                .as_array()
                .unwrap()
                .iter()
                .map(Value::to_string)
                .collect::<Vec<_>>()
                .join(",")
        );
    }
    if shape == "framework" {
        let manifest = load_json(
            options
                .workspace()
                .join("results/e1-ops/baseline_manifest.json"),
        )?;
        shape = manifest["jobs"]
            .as_array()
            .and_then(|jobs| jobs.iter().find(|v| v["op"] == op))
            .map(|v| v["shape"].clone())
            .ok_or_else(|| anyhow::anyhow!("framework shape lacks pinned manifest"))?;
    }
    let count = shape.as_array().map(Vec::len).unwrap_or(1);
    let mut result = json!({"op":op,"shape":shape,"dtype":ticket.get("dtype").cloned().unwrap_or(json!("float32")),"lane":lane,"timeout_min":(hint["timeout_min"].as_f64().unwrap_or(30.)*count as f64).min(480.),"warmup":hint["warmup"].as_u64().unwrap_or(5),"rep":hint["rep"].as_u64().unwrap_or(5)});
    if ticket["is_base_incumbent"] != true {
        let candidate = ticket["candidate_path"].as_str().ok_or_else(|| {
            anyhow::anyhow!("candidate path is missing; refuse baseline substitution")
        })?;
        let path = Path::new(candidate);
        let path = if path.is_absolute() {
            path.to_owned()
        } else {
            options.workspace().join(path)
        };
        ensure!(
            path.is_file(),
            "candidate file is missing; refuse evaluation of another candidate"
        );
        let content = fs::read_to_string(&path)?;
        if let Some(hash) = ticket["candidate_sha256"].as_str() {
            ensure!(
                rvbench_core::json::sha256(content.as_bytes()) == hash,
                "candidate identity mismatch"
            );
        }
        let deploy = hint["deploy_path"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| format!("flag_gems/ops/{op}.py"));
        result["files"] = json!([{"path":deploy,"content":content}]);
    }
    Ok(result)
}
fn state_path(options: &Options) -> PathBuf {
    options.workspace().join("runs/.rvbench-pipeline.json")
}
fn request_id(ticket: &Value, path: &str) -> String {
    let id = spec_digest(&json!({"ticket":ticket,"path":path}));
    format!("ticket:{}", &id[..56])
}
pub fn run(options: &Options, once: bool, interval: u64) -> Result<()> {
    ensure!(interval >= 1, "interval must be positive");
    let file = state_path(options);
    fs::create_dir_all(file.parent().unwrap())?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(file.with_extension("lock"))?;
    fs2::FileExt::try_lock_exclusive(&lock)?;
    let mut state = if file.is_file() {
        load_json(&file)?
    } else {
        json!({"schema":"rvbench.pipeline-v1","inflight":{},"blocked":{}})
    };
    ensure!(
        state["schema"] == "rvbench.pipeline-v1"
            && state["inflight"].is_object()
            && state["blocked"].is_object(),
        "invalid pipeline state"
    );
    loop {
        let active = state["inflight"].as_object().unwrap().clone();
        for (key, entry) in active {
            let result = (|| -> Result<bool> {
                let client = Client::new(
                    entry["base"].as_str().unwrap(),
                    options.token.clone().or_else(|| {
                        load_json(
                            options
                                .repo()
                                .join("backends/spacemit/k3/evald/config.json"),
                        )
                        .ok()
                        .and_then(|v| v["token"].as_str().map(str::to_owned))
                    }),
                )?;
                let id = entry["job_id"].as_str().unwrap();
                ensure!(
                    crate::client::uuid_is_safe(id),
                    "invalid job id in pipeline state"
                );
                let job = client.request(Method::GET, &format!("/jobs/{id}"), None)?;
                if !["done", "failed", "timeout", "canceled"]
                    .contains(&job["state"].as_str().unwrap_or(""))
                {
                    return Ok(false);
                }
                let path = options.workspace().join(&key);
                let ticket = load_json(&path)?;
                let (cond, op, output) = context(&path, &options.workspace())?;
                let registry = wave_key(&path, &options.workspace(), &cond, &op, &options.backend)?;
                finalize_into(
                    options,
                    &job,
                    &cond,
                    Some(stem(&path)?),
                    ticket["attempt"].as_u64().unwrap_or(1) as u32,
                    &client.base,
                    crate::ResultDestination {
                        output: Some(&output),
                        registry_key: Some(&registry),
                    },
                )?;
                Ok(true)
            })();
            match result {
                Ok(true) => {
                    state["inflight"].as_object_mut().unwrap().remove(&key);
                }
                Ok(false) => {}
                Err(e) => eprintln!("pipeline poll {key}: {e}"),
            }
        }
        atomic_json(&file, &state)?;
        let mut admitted = 0;
        for path in tickets(options)? {
            let key = path
                .strip_prefix(options.workspace())?
                .to_string_lossy()
                .replace('\\', "/");
            if state["inflight"].get(&key).is_some() || state["blocked"].get(&key).is_some() {
                continue;
            }
            let (_, _, output) = context(&path, &options.workspace())?;
            if output.join(format!("{}.raw.json", stem(&path)?)).is_file() {
                continue;
            }
            let result = (|| -> Result<Value> {
                let ticket = load_json(&path)?;
                let mut body = body(options, &ticket)?;
                // Equal tickets in different waves are independent evaluations.
                body["client_request_id"] = json!(request_id(&ticket, &key));
                let lane = body["lane"].as_str().unwrap();
                let client = options.client(lane)?;
                let response = client.submit(&body, Duration::from_secs(1))?;
                Ok(json!({"job_id":response["job_id"],"base":client.base}))
            })();
            match result {
                Ok(entry) => {
                    state["inflight"][&key] = entry;
                    admitted += 1;
                    atomic_json(&file, &state)?;
                }
                Err(error) => {
                    let message = error.to_string();
                    if message.contains("HTTP 400")
                        || message.contains("candidate")
                        || message.contains("unknown ticket")
                        || message.contains("manifest")
                    {
                        state["blocked"][&key] = json!({"reason":message});
                        atomic_json(&file, &state)?;
                    } else {
                        eprintln!("pipeline admission {key}: {message}");
                    }
                }
            }
        }
        println!(
            "pipeline: admitted={admitted} inflight={} blocked={}",
            state["inflight"].as_object().unwrap().len(),
            state["blocked"].as_object().unwrap().len()
        );
        if once {
            break;
        }
        thread::sleep(Duration::from_secs(interval));
    }
    fs2::FileExt::unlock(&lock)?;
    Ok(())
}
pub fn status(options: &Options, ops: &[String]) -> Result<Value> {
    let state = load_json(state_path(options)).unwrap_or(json!({"inflight":{},"blocked":{}}));
    let mut result = Vec::new();
    for path in tickets(options)? {
        let (_, op, output) = context(&path, &options.workspace())?;
        if !ops.is_empty() && !ops.contains(&op) {
            continue;
        }
        let key = path
            .strip_prefix(options.workspace())?
            .to_string_lossy()
            .replace('\\', "/");
        let status = if output.join(format!("{}.raw.json", stem(&path)?)).is_file() {
            "completed"
        } else if state["inflight"].get(&key).is_some() {
            "inflight"
        } else if state["blocked"].get(&key).is_some() {
            "blocked"
        } else {
            "pending"
        };
        result.push(json!({"op":op,"ticket":key,"state":status}));
    }
    Ok(json!({"tickets":result}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;
    #[test]
    fn missing_candidate_never_silently_becomes_a_baseline() {
        let options = Options::parse_from(["rvbench", "pipeline", "--once"]);
        let mut ticket =
            json!({"op":"abs","kind":"correctness","shape":"4","is_base_incumbent":false});
        assert!(
            body(&options, &ticket)
                .unwrap_err()
                .to_string()
                .contains("candidate")
        );
        ticket["is_base_incumbent"] = json!(true);
        assert!(body(&options, &ticket).unwrap().get("files").is_none());
    }
    #[test]
    fn retry_identity_is_stable_and_distinct_between_waves() {
        let ticket = json!({"op":"abs","shape":"4"});
        let path = "runs/wave1/G/abs/tickets/r1.ticket.json";
        assert_eq!(request_id(&ticket, path), request_id(&ticket, path));
        assert_ne!(
            request_id(&ticket, path),
            request_id(&ticket, "runs/wave2/G/abs/tickets/r1.ticket.json")
        );
    }
}
