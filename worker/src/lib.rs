//! Independent worker service. Direct, push and pull share durable admission/execution.
pub mod backend;
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::StatusCode,
    response::{Html, IntoResponse},
    routing::{get, post},
};
use backend::{Backend, BackendConfig, Compiler, Evaluator};
use rvbench_protocol::console::Console;
use rvbench_protocol::*;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub host_metrics_url: Option<String>,
    pub listen: String,
    pub url: String,
    pub worker_id: String,
    pub board_id: String,
    pub data_dir: PathBuf,
    pub kinds: Vec<JobKind>,
    pub delivery: DeliveryMode,
    pub manager: Option<String>,
    pub backend: BackendConfig,
}
#[derive(Default, Serialize, Deserialize)]
pub struct WorkerData {
    jobs: BTreeMap<String, LocalJob>,
    outbox: BTreeMap<String, JobEvent>,
    claim: Option<ClaimRequest>,
}
#[derive(Clone, Serialize, Deserialize)]
struct LocalJob {
    record: JobRecord,
    assignment: Option<Assignment>,
    acknowledged: bool,
}
pub struct Worker {
    pub console: Arc<Console>,
    pub config: Config,
    pub info: WorkerRegistration,
    store: Store<WorkerData>,
    client: reqwest::Client,
    _lock: std::fs::File,
}
impl Worker {
    pub async fn open(config: Config) -> anyhow::Result<Arc<Self>> {
        anyhow::ensure!(!config.kinds.is_empty(), "at least one kind required");
        std::fs::create_dir_all(&config.data_dir)?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(config.data_dir.join("worker.lock"))?;
        fs2::FileExt::try_lock_exclusive(&lock)?;
        let backend = Backend {
            config: config.backend.clone(),
        };
        let probe_dir = config.data_dir.join("probe");
        let environment = tokio::task::spawn_blocking(move || -> anyhow::Result<Value> {
            let mut env = backend.probe(&probe_dir)?;
            env["components"] = json!({"compiler":true,"evaluator":["correctness","performance"]});
            env["config_sha256"] = json!(digest(&backend.config));
            Ok(env)
        })
        .await??;
        let info = WorkerRegistration {
            observability: Some(serde_json::from_value(
                json!({"metrics_version":"1.0.0","metrics_path":"/metrics","host_metrics_url":config.host_metrics_url}),
            )?),
            worker_id: config.worker_id.clone(),
            board_id: config.board_id.clone(),
            instance_id: id(),
            backend: config.backend.backend.clone(),
            kinds: config.kinds.clone(),
            delivery: config.delivery.clone(),
            url: config.url.clone(),
            capacity: 1,
            direct_active: 0,
            ready: true,
            environment: serde_json::from_value(environment)?,
            protocol_version: WorkerRegistrationProtocolVersion::Variant100,
        };
        validate("WorkerRegistration", &serde_json::to_value(&info)?)?;
        let console = Console::new(
            "worker",
            Some((
                config.worker_id.clone(),
                config.board_id.clone(),
                config.backend.backend.to_string(),
            )),
        );
        let project = console.clone();
        let local_info = info.clone();
        let store = Store::<WorkerData>::open(&config.data_dir.join("worker.sqlite"))?.with_observer(move |s| {
            let mut info=local_info.clone();info.direct_active=s.jobs.values().filter(|j|j.assignment.is_none() && !terminal(&j.record.state)).count() as i64;
            let worker=serde_json::from_value(json!({"worker":info,"last_seen":seconds(),"online":true,"drained":false,"pending_ack":0,"acknowledged":0,"uncertain":0})).unwrap();
            let oldest=s.outbox.values().filter_map(|e|e.record.finished_at.as_deref().and_then(console::timestamp)).reduce(f64::min).unwrap_or(0.);
            project.update(s.jobs.values().map(|j|(&j.record,if j.assignment.is_some(){"managed"}else{"direct"})).collect(),vec![worker],vec![],s.outbox.len(),oldest);
        })?;
        store
            .tx(|s| {
                // Never silently reexecute an attempt after a process restart. systemd KillMode
                // cleans the execution cgroup before restart; retain the failure/outbox evidence.
                let ids: Vec<_> = s
                    .jobs
                    .iter()
                    .filter(|(_, j)| !terminal(&j.record.state))
                    .map(|(id, _)| id.clone())
                    .collect();
                for id in ids {
                    let j = s.jobs.get_mut(&id).unwrap();
                    j.record.state = JobState::Failed;
                    j.record.error = Some(
                        "worker restarted; execution was interrupted, manual resubmission required"
                            .into(),
                    );
                    j.record.finished_at = Some(now());
                    if let Some(a) = &j.assignment {
                        let e = event(a, &j.record);
                        s.outbox.insert(e.event_id.clone(), e);
                    }
                }
                // Keep a pending receipt across restart, including the old instance ID.
                // After registering the new instance we can replay it without granting new
                // work to the stopped instance, then durably report interrupted handoff.
                Ok(())
            })
            .await
            .map_err(|e| anyhow::anyhow!(e.1))?;
        Ok(Arc::new(Self {
            console,
            config,
            info,
            store,
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()?,
            _lock: lock,
        }))
    }
    pub fn start(self: &Arc<Self>) {
        let worker = self.clone();
        tokio::spawn(async move {
            worker.execute_loop().await;
        });
        if self.config.manager.is_some() {
            let worker = self.clone();
            tokio::spawn(async move {
                worker.cluster_loop().await;
            });
        }
    }
    async fn registration(&self) -> Result<WorkerRegistration, ApiError> {
        let mut info = self.info.clone();
        info.direct_active = self
            .store
            .tx(|s| {
                Ok(s.jobs
                    .values()
                    .filter(|j| j.assignment.is_none() && !terminal(&j.record.state))
                    .count() as i64)
            })
            .await?;
        Ok(info)
    }
    pub async fn admit(&self, submission: WorkerSubmission) -> Result<JobRecord, ApiError> {
        Backend {
            config: self.config.backend.clone(),
        }
        .supported(&submission.request)
        .map_err(ApiError::bad)?;
        if !worker_supports_request(&self.info, &submission.request) {
            return Err(ApiError::bad(
                "implementation/op/dtype unavailable on this worker",
            ));
        }
        if !self.config.kinds.contains(&submission.request.kind) {
            return Err(ApiError::bad("kind not enabled on this worker"));
        }
        let mut r = record(submission.request);
        r.worker_id = self.info.worker_id.clone();
        r.board_id = self.info.board_id.clone();
        r.instance_id = self.info.instance_id.clone();
        if let Some(a) = &submission.assignment {
            if a.worker_id != r.worker_id
                || a.instance_id != r.instance_id
                || a.request_sha256 != digest(&r.request)
                || digest(&a.request) != digest(&r.request)
            {
                return Err(ApiError::conflict("assignment identity/digest mismatch"));
            }
            r.job_id = a.job_id.clone();
            r.assignment_id = Some(a.assignment_id.clone());
            r.attempt_id = Some(a.attempt_id.clone());
            r.lease_id = Some(a.lease_id.clone());
        }
        self.store
            .tx(move |s| {
                if let Some(a) = &submission.assignment {
                    if let Some(old) = s.jobs.values().find(|j| {
                        j.assignment
                            .as_ref()
                            .is_some_and(|x| x.attempt_id == a.attempt_id)
                    }) {
                        if digest(&old.assignment) != digest(&submission.assignment) {
                            return Err(ApiError::conflict(
                                "attempt already belongs to another assignment",
                            ));
                        }
                        return Ok(old.record.clone());
                    }
                    if a.expires_at < seconds() {
                        return Err(ApiError::conflict("assignment expired before admission"));
                    }
                    if s.jobs
                        .values()
                        .any(|j| j.assignment.is_some() && !terminal(&j.record.state))
                    {
                        return Err(ApiError(
                            StatusCode::TOO_MANY_REQUESTS,
                            "worker occupied".into(),
                        ));
                    }
                } else if let Some(key) = &r.request.client_request_id
                    && let Some(old) = s.jobs.values().find(|j| {
                        j.assignment.is_none()
                            && j.record.request.client_request_id.as_ref() == Some(key)
                    })
                {
                    if digest(&old.record.request) != digest(&r.request) {
                        return Err(ApiError::conflict("client_request_id digest mismatch"));
                    }
                    return Ok(old.record.clone());
                }
                if submission.assignment.is_none()
                    && s.jobs
                        .values()
                        .filter(|j| j.assignment.is_none() && !terminal(&j.record.state))
                        .count()
                        >= 32
                {
                    return Err(ApiError(StatusCode::TOO_MANY_REQUESTS, "queue full".into()));
                }
                if s.jobs.contains_key(&r.job_id) {
                    return Err(ApiError::conflict("job_id collision"));
                }
                s.jobs.insert(
                    r.job_id.clone(),
                    LocalJob {
                        record: r.clone(),
                        acknowledged: submission.assignment.is_none(),
                        assignment: submission.assignment,
                    },
                );
                Ok(r)
            })
            .await
    }
    async fn execute_loop(self: Arc<Self>) {
        loop {
            let next = self
                .store
                .tx(|s| {
                    let key = s
                        .jobs
                        .iter()
                        .filter(|(_, j)| j.record.state == JobState::Queued && j.acknowledged)
                        .min_by_key(|(_, j)| j.record.submitted_at.clone())
                        .map(|(k, _)| k.clone());
                    Ok(key.map(|key| {
                        let j = s.jobs.get_mut(&key).unwrap();
                        j.record.state = JobState::Running;
                        j.record.started_at = Some(now());
                        j.record.clone()
                    }))
                })
                .await;
            if let Ok(Some(mut r)) = next {
                let backend = Backend {
                    config: self.config.backend.clone(),
                };
                let dir = self.config.data_dir.join("jobs").join(&r.job_id);
                let environment = self.info.environment.clone();
                let r = tokio::task::spawn_blocking(move || {
                    let start = Instant::now();
                    let queue_wait = r
                        .started_at
                        .as_deref()
                        .and_then(console::timestamp)
                        .zip(console::timestamp(&r.submitted_at))
                        .map(|(a, b)| (a - b).max(0.));
                    let mut timings = JobRecordPhaseTimingsSeconds {
                        compiler: None,
                        evaluator: None,
                        queue_wait,
                    };
                    let mut phase_start = Instant::now();
                    let mut evaluating = false;
                    let result = (|| -> anyhow::Result<()> {
                        let p = backend.prepare(&r.request, &dir)?;
                        timings.compiler = Some(phase_start.elapsed().as_secs_f64());
                        r.compilation = Some(p.compilation.clone());
                        if r.request.kind != JobKind::Func {
                            evaluating = true;
                            phase_start = Instant::now();
                            let mut request = r.request.clone();
                            request.timeout_secs =
                                (r.request.timeout_secs - start.elapsed().as_secs() as i64).max(0);
                            anyhow::ensure!(request.timeout_secs > 0, "job timeout");
                            r.evaluation = Some(backend.evaluate(&request, &p)?);
                            timings.evaluator = Some(phase_start.elapsed().as_secs_f64());
                        }
                        Ok(())
                    })();
                    if result.is_err() {
                        if evaluating {
                            timings.evaluator = Some(phase_start.elapsed().as_secs_f64());
                        } else {
                            timings.compiler = Some(phase_start.elapsed().as_secs_f64());
                        }
                    }
                    r.phase_timings_seconds = Some(timings);
                    match result {
                        Ok(()) => {
                            r.state = if r
                                .evaluation
                                .as_ref()
                                .is_some_and(|e| e.status == PhaseStatus::Failed)
                            {
                                JobState::Failed
                            } else {
                                JobState::Done
                            };
                        }
                        Err(e) => {
                            let msg = e.to_string();
                            r.state = if msg.contains("timeout") {
                                JobState::Timeout
                            } else {
                                JobState::Failed
                            };
                            let evidence = [
                                "request.json",
                                "candidate.c",
                                "candidate.py",
                                "harness.c",
                                "harness.py",
                                "last-stdout.log",
                                "last-stderr.log",
                            ]
                            .into_iter()
                            .filter_map(|name| backend::artifact(&dir, name).ok())
                            .collect();
                            let failed = PhaseResult {
                                status: PhaseStatus::Failed,
                                details: serde_json::from_value(json!({"error":msg})).unwrap(),
                                artifacts: evidence,
                            };
                            if r.compilation.is_none() {
                                r.compilation = Some(failed);
                            } else {
                                r.evaluation = Some(failed);
                            }
                            r.error = Some(msg);
                        }
                    }
                    if let Some(c) = &mut r.compilation {
                        c.details.additional_properties.insert(
                            "environment".into(),
                            serde_json::to_value(environment).unwrap(),
                        );
                    }
                    let logs =
                        std::fs::read_to_string(dir.join("last-stderr.log")).unwrap_or_default();
                    r.logs = logs.chars().take(8000).collect();
                    r.finished_at = Some(now());
                    r
                })
                .await;
                if let Ok(r) = r {
                    let result = self
                        .store
                        .tx(move |s| {
                            let j = s.jobs.get_mut(&r.job_id).ok_or_else(ApiError::missing)?;
                            j.record = r;
                            if let Some(a) = &j.assignment {
                                let e = event(a, &j.record);
                                s.outbox.insert(e.event_id.clone(), e);
                            }
                            Ok(())
                        })
                        .await;
                    if let Err(e) = result {
                        eprintln!("persist terminal: {}", e.1);
                    }
                }
            } else {
                tokio::time::sleep(Duration::from_millis(150)).await;
            }
        }
    }
    async fn cluster_tick(&self, manager: &str) -> anyhow::Result<()> {
        let info = self
            .registration()
            .await
            .map_err(|e| anyhow::anyhow!(e.1))?;
        self.client
            .post(format!("{manager}/cluster/workers/register"))
            .json(&info)
            .send()
            .await?
            .error_for_status()?;
        self.recover_claim(manager).await?;
        let (active, outbox) = self
            .store
            .tx(|s| {
                Ok((
                    s.jobs
                        .values()
                        .filter(|j| !terminal(&j.record.state))
                        .cloned()
                        .collect::<Vec<_>>(),
                    s.outbox.values().cloned().collect::<Vec<_>>(),
                ))
            })
            .await
            .map_err(|e| anyhow::anyhow!(e.1))?;
        for j in &active {
            if let Some(a) = &j.assignment {
                let ack = Ack {
                    worker_id: a.worker_id.clone(),
                    instance_id: a.instance_id.clone(),
                    lease_id: a.lease_id.clone(),
                    worker_job_id: j.record.job_id.clone(),
                };
                let endpoint = if j.acknowledged {
                    format!("{manager}/cluster/leases/{}/renew", a.lease_id)
                } else {
                    format!("{manager}/cluster/assignments/{}/ack", a.assignment_id)
                };
                if self
                    .client
                    .post(endpoint)
                    .json(&ack)
                    .send()
                    .await?
                    .status()
                    .is_success()
                    && !j.acknowledged
                {
                    let id = j.record.job_id.clone();
                    self.store
                        .tx(move |s| {
                            s.jobs.get_mut(&id).unwrap().acknowledged = true;
                            Ok(())
                        })
                        .await
                        .map_err(|e| anyhow::anyhow!(e.1))?;
                }
            }
        }
        for e in outbox {
            let response = self
                .client
                .post(format!(
                    "{manager}/cluster/assignments/{}/events",
                    e.assignment.assignment_id
                ))
                .json(&e)
                .send()
                .await?;
            if response.status().is_success() {
                self.store
                    .tx(move |s| {
                        s.outbox.remove(&e.event_id);
                        Ok(())
                    })
                    .await
                    .map_err(|e| anyhow::anyhow!(e.1))?;
            }
        }
        if self.config.delivery != DeliveryMode::Push && active.is_empty() {
            let instance = self.info.instance_id.clone();
            let worker = self.info.worker_id.clone();
            let claim = self
                .store
                .tx(move |s| {
                    let c = s.claim.get_or_insert_with(|| ClaimRequest {
                        worker_id: worker,
                        instance_id: instance,
                        request_id: id(),
                        wait_s: 1,
                    });
                    Ok(c.clone())
                })
                .await
                .map_err(|e| anyhow::anyhow!(e.1))?;
            let response = self
                .client
                .post(format!("{manager}/cluster/assignments/claim"))
                .json(&claim)
                .send()
                .await?;
            if response.status() == StatusCode::NO_CONTENT {
                self.store
                    .tx(|s| {
                        s.claim = None;
                        Ok(())
                    })
                    .await
                    .map_err(|e| anyhow::anyhow!(e.1))?;
            } else {
                let a: Assignment = response.error_for_status()?.json().await?;
                // Preserve the claim on network/admission failure; retry the same receipt.
                self.admit(WorkerSubmission {
                    request: a.request.clone(),
                    assignment: Some(a),
                })
                .await
                .map_err(|e| anyhow::anyhow!(e.1))?;
                self.store
                    .tx(|s| {
                        s.claim = None;
                        Ok(())
                    })
                    .await
                    .map_err(|e| anyhow::anyhow!(e.1))?;
            }
        }
        Ok(())
    }
    async fn recover_claim(&self, manager: &str) -> anyhow::Result<()> {
        let instance = self.info.instance_id.clone();
        let claim = self
            .store
            .tx(move |s| {
                Ok(s.claim
                    .as_ref()
                    .filter(|c| c.instance_id != instance)
                    .cloned())
            })
            .await
            .map_err(|e| anyhow::anyhow!(e.1))?;
        let Some(claim) = claim else { return Ok(()) };
        // Registration has already replaced the old instance. A missing receipt now
        // returns 409/404, so this replay cannot allocate work for the old instance.
        let response = self
            .client
            .post(format!("{manager}/cluster/assignments/claim"))
            .json(&claim)
            .send()
            .await?;
        let status = response.status();
        let assignment = if status == StatusCode::NO_CONTENT
            || status == StatusCode::CONFLICT
            || status == StatusCode::NOT_FOUND
        {
            None
        } else {
            Some(response.error_for_status()?.json::<Assignment>().await?)
        };
        if let Some(a) = &assignment {
            validate("Assignment", &serde_json::to_value(a)?)?;
            anyhow::ensure!(
                a.worker_id == claim.worker_id
                    && a.instance_id == claim.instance_id
                    && a.request_sha256 == digest(&a.request),
                "recovered claim identity mismatch"
            );
        }
        let board = self.info.board_id.clone();
        self.store
            .tx(move |s| {
                if let Some(a) = assignment {
                    if let Some(old) = s.jobs.get(&a.job_id) {
                        if digest(&old.assignment) != digest(&Some(a)) {
                            return Err(ApiError::conflict(
                                "recovered claim collides with local job",
                            ));
                        }
                    } else {
                        let mut r = record(a.request.clone());
                        r.job_id = a.job_id.clone();
                        r.worker_id = a.worker_id.clone();
                        r.instance_id = a.instance_id.clone();
                        r.board_id = board;
                        r.assignment_id = Some(a.assignment_id.clone());
                        r.attempt_id = Some(a.attempt_id.clone());
                        r.lease_id = Some(a.lease_id.clone());
                        r.state = JobState::Failed;
                        r.finished_at = Some(now());
                        r.error = Some(
                            "worker restarted after claim; interrupted handoff was not executed"
                                .into(),
                        );
                        let e = event(&a, &r);
                        s.outbox.insert(e.event_id.clone(), e);
                        s.jobs.insert(
                            r.job_id.clone(),
                            LocalJob {
                                record: r,
                                assignment: Some(a),
                                acknowledged: false,
                            },
                        );
                    }
                }
                s.claim = None;
                Ok(())
            })
            .await
            .map_err(|e| anyhow::anyhow!(e.1))?;
        Ok(())
    }
    async fn cluster_loop(self: Arc<Self>) {
        let manager = self.config.manager.as_ref().unwrap().trim_end_matches('/');
        loop {
            if let Err(e) = self.cluster_tick(manager).await {
                eprintln!("cluster: {e}");
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    }
}
fn event(a: &Assignment, r: &JobRecord) -> JobEvent {
    JobEvent {
        assignment: a.clone(),
        event_id: format!("{}:terminal", a.attempt_id),
        event_seq: 1,
        record: r.clone(),
    }
}
pub fn router(worker: Arc<Worker>) -> Router {
    let metrics = worker.console.clone();
    Router::new()
        .route("/metrics",get(|State(w):State<Arc<Worker>>|async move {([(axum::http::header::CONTENT_TYPE,"text/plain; version=0.0.4; charset=utf-8")],w.console.metrics())}))
        .route("/observability/summary",get(|State(w):State<Arc<Worker>>|async move {Json(w.console.summary(serde_json::from_value(json!({"status":"unavailable","errors":["Historical metrics are available through the manager; standalone execution is independent"]})).unwrap()))}))
        .route("/results",get(|State(w):State<Arc<Worker>>,Query(q):Query<ResultQuery>|async move {w.console.results(q).map(Json)}))
        .route("/leaderboard",get(|State(w):State<Arc<Worker>>,Query(q):Query<LeaderboardQuery>|async move {w.console.leaderboard(q).map(Json)}))
        .route(
            "/healthz",
            get(|| async {
                Json(json!({"ok":true,"service":"rvbench-worker","protocol_version":"1.0.0"}))
            }),
        )
        .route(
            "/capabilities",
            get(|State(w): State<Arc<Worker>>| async move {
                Ok::<_, ApiError>(Json(w.registration().await?))
            }),
        )
        .route("/jobs", post(submit).get(list))
        .route("/jobs/{id}", get(job).delete(cancel))
        .route("/attempts/{id}", get(attempt))
        .route("/ui", get(|| async { Html(include_str!("../../webui/console.html")) }))
        .layer(DefaultBodyLimit::max(8 * 1024 * 1024))
        .layer(axum::middleware::from_fn_with_state(metrics,console::http_metrics))
        .with_state(worker)
}
async fn submit(
    State(w): State<Arc<Worker>>,
    RawJson(v): RawJson,
) -> Result<impl IntoResponse, ApiError> {
    Ok((
        StatusCode::ACCEPTED,
        Json(w.admit(decode("WorkerSubmission", v)?).await?),
    ))
}
async fn list(State(w): State<Arc<Worker>>) -> Result<Json<Vec<JobRecord>>, ApiError> {
    Ok(Json(
        w.store
            .tx(|s| {
                let mut records: Vec<_> = s.jobs.values().map(|j| j.record.clone()).collect();
                records.sort_by(|a, b| b.submitted_at.cmp(&a.submitted_at));
                records.truncate(100);
                Ok(records)
            })
            .await?,
    ))
}
#[derive(Deserialize, Default)]
struct Wait {
    wait: Option<u64>,
}
async fn job(
    State(w): State<Arc<Worker>>,
    Path(id): Path<String>,
    Query(q): Query<Wait>,
) -> Result<Json<JobRecord>, ApiError> {
    let until = Instant::now() + Duration::from_secs(q.wait.unwrap_or(0).min(25));
    loop {
        let id = id.clone();
        let r = w
            .store
            .tx(move |s| {
                s.jobs
                    .get(&id)
                    .map(|j| j.record.clone())
                    .ok_or_else(ApiError::missing)
            })
            .await?;
        if terminal(&r.state) || Instant::now() >= until {
            return Ok(Json(r));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}
async fn attempt(
    State(w): State<Arc<Worker>>,
    Path(id): Path<String>,
) -> Result<Json<JobRecord>, ApiError> {
    Ok(Json(
        w.store
            .tx(move |s| {
                s.jobs
                    .values()
                    .find(|j| j.record.attempt_id.as_ref() == Some(&id))
                    .map(|j| j.record.clone())
                    .ok_or_else(ApiError::missing)
            })
            .await?,
    ))
}
async fn cancel(
    State(w): State<Arc<Worker>>,
    Path(id): Path<String>,
) -> Result<Json<JobRecord>, ApiError> {
    Ok(Json(
        w.store
            .tx(move |s| {
                let j = s.jobs.get_mut(&id).ok_or_else(ApiError::missing)?;
                if j.record.state != JobState::Queued || j.assignment.is_some() {
                    return Err(ApiError::conflict(
                        "only queued direct jobs can be canceled",
                    ));
                }
                j.record.state = JobState::Canceled;
                j.record.finished_at = Some(now());
                Ok(j.record.clone())
            })
            .await?,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn direct_and_managed_admission_share_limits_and_persistent_attempt_index() {
        let temp = tempfile::tempdir().unwrap();
        let data = temp.path().to_path_buf();
        let config = Config {
            host_metrics_url: None,
            listen: "127.0.0.1:0".into(),
            url: "http://127.0.0.1:1".into(),
            worker_id: "test".into(),
            board_id: "board".into(),
            data_dir: data.clone(),
            kinds: vec![JobKind::Performance],
            delivery: DeliveryMode::Hybrid,
            manager: None,
            backend: BackendConfig {
                backend: BackendId::K1,
                python: "python3".into(),
                cc: "gcc".into(),
                cpus: None,
                ai_thread: false,
                environment: Default::default(),
            },
        };
        let info:WorkerRegistration=serde_json::from_value(json!({"worker_id":"test","board_id":"board","instance_id":id(),"backend":"k1","kinds":["performance"],"delivery":"hybrid","url":"http://127.0.0.1:1","capacity":1,"direct_active":0,"ready":true,"environment":{"ops":["add","abs"],"dtypes":["float32"]},"protocol_version":"1.0.0"})).unwrap();
        let mut worker = Worker {
            console: Console::new("worker", None),
            config,
            info: info.clone(),
            store: Store::open(&data.join("test.sqlite")).unwrap(),
            client: reqwest::Client::new(),
            _lock: std::fs::File::create(data.join("lock")).unwrap(),
        };
        let mut req: JobRequest =
            serde_json::from_str(include_str!("../../contract/examples/job.json")).unwrap();
        req.backend = BackendId::K1;
        req.op = "add".into();
        req.kind = JobKind::Correctness;
        assert_eq!(
            worker
                .admit(WorkerSubmission {
                    request: req.clone(),
                    assignment: None
                })
                .await
                .unwrap_err()
                .0,
            400
        );
        req.kind = JobKind::Performance;
        // A direct job can race a manager capacity snapshot. Retain the one grant
        // in the same local queue, rather than stranding an uncertain reservation.
        worker
            .admit(WorkerSubmission {
                request: req.clone(),
                assignment: None,
            })
            .await
            .unwrap();
        let a = Assignment {
            assignment_id: id(),
            attempt_id: id(),
            lease_id: id(),
            job_id: id(),
            worker_id: info.worker_id.clone(),
            instance_id: info.instance_id.clone(),
            manager_epoch: id(),
            request_sha256: digest(&req),
            expires_at: seconds() + 30.0,
            request: req.clone(),
        };
        let sub = WorkerSubmission {
            request: req.clone(),
            assignment: Some(a.clone()),
        };
        let r = worker.admit(sub.clone()).await.unwrap();
        assert_eq!(worker.admit(sub).await.unwrap().job_id, r.job_id);
        let mut extra = a.clone();
        extra.attempt_id = id();
        extra.assignment_id = id();
        extra.lease_id = id();
        extra.job_id = id();
        assert_eq!(
            worker
                .admit(WorkerSubmission {
                    request: req.clone(),
                    assignment: Some(extra)
                })
                .await
                .unwrap_err()
                .0,
            429
        );
        let mut forged = a.clone();
        forged.lease_id = id();
        assert_eq!(
            worker
                .admit(WorkerSubmission {
                    request: req.clone(),
                    assignment: Some(forged)
                })
                .await
                .unwrap_err()
                .0,
            409
        );
        // Replayed delivery after expiry still returns the persisted same attempt.
        worker
            .store
            .tx(move |s| {
                s.jobs.get_mut(&r.job_id).unwrap().record.state = JobState::Done;
                Ok(())
            })
            .await
            .unwrap();
        let store = Store::<WorkerData>::open(&data.join("test.sqlite")).unwrap();
        assert_eq!(store.tx(|s| Ok(s.jobs.len())).await.unwrap(), 2);
        // Crash after a claim committed but before local admission: recover the old
        // receipt as an interrupted terminal report, without executing it again.
        let old_claim = ClaimRequest {
            worker_id: info.worker_id.clone(),
            instance_id: info.instance_id.clone(),
            request_id: id(),
            wait_s: 0,
        };
        worker
            .store
            .tx(move |s| {
                s.claim = Some(old_claim);
                Ok(())
            })
            .await
            .unwrap();
        worker.info.instance_id = id();
        let mut recovered = a.clone();
        recovered.job_id = id();
        recovered.attempt_id = id();
        recovered.assignment_id = id();
        recovered.lease_id = id();
        let expected_job = recovered.job_id.clone();
        let expected_instance = recovered.instance_id.clone();
        let app = Router::new().route(
            "/cluster/assignments/claim",
            post(move || {
                let a = recovered.clone();
                async move { Json(a) }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(axum::serve(listener, app).into_future());
        worker.recover_claim(&url).await.unwrap();
        worker.recover_claim(&url).await.unwrap();
        worker
            .store
            .tx(move |s| {
                assert!(s.claim.is_none());
                assert_eq!(s.outbox.len(), 1);
                let r = &s.jobs[&expected_job].record;
                assert_eq!(r.state, JobState::Failed);
                assert_eq!(r.instance_id, expected_instance);
                assert!(r.started_at.is_none());
                Ok(())
            })
            .await
            .unwrap();
        server.abort();
        req.repetitions = 0;
        assert_eq!(
            worker
                .admit(WorkerSubmission {
                    request: req,
                    assignment: None
                })
                .await
                .unwrap_err()
                .0,
            400
        );
    }
}
