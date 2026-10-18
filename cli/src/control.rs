use anyhow::{Result, ensure};
use clap::Subcommand;
use rvbench_protocol::{JobRecord, JobRequest, WorkerSubmission, terminal, validate};
use serde_json::Value;
use std::{
    path::PathBuf,
    time::{Duration, Instant},
};
#[derive(Subcommand)]
pub enum Action {
    Submit {
        file: PathBuf,
        #[arg(long)]
        direct: bool,
        #[arg(long)]
        wait: bool,
    },
    Get {
        job_id: String,
        #[arg(long)]
        wait: bool,
    },
    Jobs,
    Workers,
    Capabilities,
    Cancel {
        job_id: String,
    },
    Drain {
        worker_id: String,
    },
}
pub fn run(base: Option<&str>, action: &Action) -> Result<()> {
    let base = base
        .unwrap_or("http://127.0.0.1:18800")
        .trim_end_matches('/');
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(35))
        .build()?;
    let (method, path, body, wait) = match action {
        Action::Submit { file, direct, wait } => {
            let value: Value = crate::load_json(file)?;
            validate("JobRequest", &value)?;
            let request: JobRequest = serde_json::from_value(value.clone())?;
            (
                reqwest::Method::POST,
                "/jobs".into(),
                Some(if *direct {
                    serde_json::to_value(WorkerSubmission {
                        request,
                        assignment: None,
                    })?
                } else {
                    value
                }),
                *wait,
            )
        }
        Action::Get { job_id, wait } => {
            (reqwest::Method::GET, format!("/jobs/{job_id}"), None, *wait)
        }
        Action::Jobs => (reqwest::Method::GET, "/jobs".into(), None, false),
        Action::Workers => (reqwest::Method::GET, "/cluster/workers".into(), None, false),
        Action::Capabilities => (reqwest::Method::GET, "/capabilities".into(), None, false),
        Action::Cancel { job_id } => (
            reqwest::Method::DELETE,
            format!("/jobs/{job_id}"),
            None,
            false,
        ),
        Action::Drain { worker_id } => (
            reqwest::Method::POST,
            format!("/cluster/workers/{worker_id}/drain"),
            None,
            false,
        ),
    };
    let mut req = client.request(method, format!("{base}{path}"));
    if let Some(body) = body {
        req = req.json(&body);
    }
    let response = req.send()?;
    let status = response.status();
    let mut value: Value = response.json()?;
    ensure!(status.is_success(), "HTTP {status}: {value}");
    if wait {
        validate("JobRecord", &value)?;
        let until = Instant::now() + Duration::from_secs(3600);
        loop {
            let record: JobRecord = serde_json::from_value(value.clone())?;
            if terminal(&record.state) {
                break;
            }
            ensure!(
                Instant::now() < until,
                "wait exceeded one hour; job remains queryable"
            );
            value = client
                .get(format!("{base}/jobs/{}?wait=25", record.job_id))
                .send()?
                .error_for_status()?
                .json()?;
            validate("JobRecord", &value)?;
        }
    }
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}
