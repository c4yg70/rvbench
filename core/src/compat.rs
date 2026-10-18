use crate::s;
use anyhow::{Result, bail};
use serde_json::{Value, json};

pub const RESULT_V2: &str = "result-v2";
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64() != Some(0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn metrics(result: &Value) -> Value {
    json!({"torch_ms":result["torch_ms"],"gems_ms":result["gems_ms"],"speedup":result["speedup"]})
}
pub fn to_result_v2(
    job: &Value,
    backend: &str,
    host: &str,
    lane: &str,
    meta: Option<Value>,
) -> Value {
    let mut view = json!({"schema":RESULT_V2,"backend":backend,"host_tag":host,"lane":lane,"job_id":job["job_id"],"op":job["request"]["op"],"shape":job["request"]["shape"],"dtype":job["request"]["dtype"],"metrics":metrics(&job["result"]),"correct":job["result"]["correct"],"max_abs_err":job["result"]["max_abs_err"],"error":job["error"],"duration_s":job["duration_s"],"queue_wait_s":job["queue_wait_s"],"finished_at":if !truthy(&job["finished_at"]){&job["ts"]}else{&job["finished_at"]},"source_schema":"evald-job-v1"});
    if let Some(meta) = meta
        && truthy(&meta)
    {
        view["meta"] = meta;
    }
    view
}
pub fn from_raw_v1(raw: &Value) -> Value {
    let lane = match s(&raw["kind"]) {
        "timing-final" => json!("timing"),
        "correctness" => json!("correctness"),
        "func" => json!("func"),
        _ => Value::Null,
    };
    let mut meta =
        json!({"condition":raw["condition"],"attempt":raw["attempt"],"writer":raw["writer"]});
    for key in [
        "candidate_sha256",
        "candidate_path",
        "deploy_path",
        "shape_warn",
    ] {
        if !raw[key].is_null() {
            meta[key] = raw[key].clone();
        }
    }
    json!({"schema":RESULT_V2,"backend":"k3","host_tag":raw["evald_host"],"lane":lane,"job_id":raw["job_id"],"op":raw["op"],"shape":raw["shape"],"dtype":raw["dtype"],"metrics":metrics(&raw["result"]),"correct":raw["result"]["correct"],"max_abs_err":raw["result"]["max_abs_err"],"error":raw["error"],"duration_s":raw["duration_s"],"queue_wait_s":raw["queue_wait_s"],"finished_at":raw["evald_ts"],"source_schema":"raw-v1","meta":meta})
}
pub fn load_result_v2(value: &Value) -> Result<Value> {
    match s(&value["schema"]) {
        RESULT_V2 => Ok(value.clone()),
        "raw-v1" => Ok(from_raw_v1(value)),
        _ => bail!("unknown result schema"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_metadata_and_flattened_timestamp_match_legacy_view() {
        let job = json!({"finished_at":"","ts":"legacy-proxy-time","result":null});
        let view = to_result_v2(&job, "k3", "k3", "timing", Some(json!({})));
        assert!(view.get("meta").is_none());
        assert_eq!(view["finished_at"], "legacy-proxy-time");
        assert!(view["metrics"]["gems_ms"].is_null());
    }
}
