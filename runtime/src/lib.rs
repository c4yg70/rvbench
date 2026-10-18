//! Shared evaluation service mechanisms; vendor code implements Backend.
pub mod files;
pub mod metrics;
pub mod process;
pub mod service;

use anyhow::{Result, ensure};
use serde_json::Value;
pub use service::Service;
use std::{path::PathBuf, time::Duration};

#[derive(Clone, Debug)]
pub struct Settings {
    pub host: String,
    pub port: u16,
    pub token: String,
    pub data_dir: PathBuf,
    pub path_lock_dir: PathBuf,
    pub queue_max: usize,
    pub body_max_bytes: usize,
    pub longpoll_max_s: u64,
    pub history_mem: usize,
    pub lane_queue_max: Value,
    pub result_extra: Value,
}
impl Settings {
    pub fn from_value(v: &Value) -> Result<Self> {
        let str_value = |key: &str| {
            v[key]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| anyhow::anyhow!("invalid config {key}"))
                .map(str::to_owned)
        };
        let number = |key: &str, min: u64| {
            let n = v[key]
                .as_u64()
                .ok_or_else(|| anyhow::anyhow!("invalid config {key}"))?;
            ensure!(n >= min, "invalid config {key}");
            Ok(n)
        };
        let port = u16::try_from(number("port", 0)?)?;
        let queue_max = usize::try_from(number("queue_max", 1)?)?;
        let body_max_bytes = usize::try_from(number("body_max_bytes", 1)?)?;
        Ok(Self {
            host: str_value("host")?,
            port,
            token: str_value("token")?,
            data_dir: PathBuf::from(str_value("data_dir")?),
            path_lock_dir: PathBuf::from(str_value("path_lock_dir")?),
            queue_max,
            body_max_bytes,
            longpoll_max_s: number("longpoll_max_s", 0)?,
            history_mem: number("history_mem", 1)? as usize,
            lane_queue_max: v["lane_queue_max"].clone(),
            result_extra: v.get("result_extra").cloned().unwrap_or(Value::Null),
        })
    }
}

#[derive(Clone, Debug)]
pub struct WorkerSpec {
    pub lane: String,
    pub index: usize,
    pub total: usize,
    pub cpus: Option<String>,
    pub omp_threads: Option<u32>,
    pub timing_lock: Option<PathBuf>,
    pub executor: String,
}
#[derive(Clone, Debug)]
pub struct PreparedJob {
    pub request: Value,
    pub payload: Value,
    pub lane: String,
}
#[derive(Clone, Debug)]
pub struct Execution {
    pub state: String,
    pub result: Value,
    pub kope_mem: Value,
    pub error: Option<String>,
    pub stdout: String,
    pub stderr: String,
    pub exit_code: Option<i32>,
    pub fallback: bool,
}
impl Execution {
    pub fn failed(error: impl Into<String>) -> Self {
        Self {
            state: "failed".into(),
            result: Value::Null,
            kope_mem: Value::Null,
            error: Some(error.into()),
            stdout: String::new(),
            stderr: String::new(),
            exit_code: None,
            fallback: false,
        }
    }
}
pub trait Executor: Send {
    fn execute(&mut self, id: &str, request: &Value, timeout: Duration) -> Result<Execution>;
}
pub trait Backend: Send + Sync {
    fn settings(&self) -> &Settings;
    fn workers(&self) -> Vec<WorkerSpec>;
    fn validate(&self, body: &Value) -> Result<PreparedJob>;
    fn resolve_path(&self, relative: &str) -> Result<PathBuf>;
    /// bool=true means write/exclusive; false means read/shared.
    fn lock_targets(&self, job: &PreparedJob) -> Result<Vec<(PathBuf, bool)>>;
    fn executor(&self, worker: &WorkerSpec) -> Box<dyn Executor>;
    fn output_selfcheck(&self) -> bool;
}
