//! SpacemiT K3 execution adapter. Python remains only in the tensor bridge.
pub mod config;
pub mod executor;
pub mod judge;
pub mod profiles;
pub mod request;
pub use config::K3Config;

use anyhow::Result;
use rvbench_runtime::{Backend, Executor, PreparedJob, Settings, WorkerSpec};
use serde_json::Value;
use std::path::PathBuf;

impl Backend for K3Config {
    fn settings(&self) -> &Settings {
        &self.settings
    }
    fn workers(&self) -> Vec<WorkerSpec> {
        self.workers.clone()
    }
    fn validate(&self, body: &Value) -> Result<PreparedJob> {
        request::validate_request(self, body)
    }
    fn resolve_path(&self, path: &str) -> Result<PathBuf> {
        self.resolve_path(path)
    }
    fn lock_targets(&self, job: &PreparedJob) -> Result<Vec<(PathBuf, bool)>> {
        let mut paths = Vec::new();
        if let Some(files) = job.request["files"].as_array() {
            for file in files {
                paths.push((self.resolve_path(file["path"].as_str().unwrap())?, true));
            }
        }
        if let Some(path) = job.request["expect_path"].as_str() {
            paths.push((self.resolve_path(path)?, false));
        }
        paths.push((
            self.resolve_path(&format!(
                "flag_gems/ops/{}.py",
                job.request["op"].as_str().unwrap()
            ))?,
            false,
        ));
        paths.push((
            self.resolve_path(if job.request["bench"] == "gemm" {
                "scripts/bench_gemm.py"
            } else {
                "scripts/bench_op.py"
            })?,
            false,
        ));
        Ok(paths)
    }
    fn executor(&self, worker: &WorkerSpec) -> Box<dyn Executor> {
        Box::new(executor::K3Executor::new(self.clone(), worker.clone()))
    }
    fn output_selfcheck(&self) -> bool {
        self.value["warmd_output_selfcheck"] == true
    }
}
