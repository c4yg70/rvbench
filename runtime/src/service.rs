use crate::{
    Backend, Execution, PreparedJob, WorkerSpec,
    files::{Deployment, FileLocks, atomic_json},
    metrics::Metrics,
};
use anyhow::{Result, ensure};
use axum::{
    Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path as HttpPath, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
};
use chrono::{Local, TimeZone};
use rvbench_core::{json::sha256, load_json, parse_json};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::Write,
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Notify;

pub fn epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}
fn iso(t: f64) -> String {
    Local
        .timestamp_opt(t as i64, 0)
        .single()
        .unwrap()
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string()
}
fn terminal(state: &str) -> bool {
    ["done", "failed", "timeout", "canceled"].contains(&state)
}
fn tail(text: &str) -> String {
    text.chars()
        .rev()
        .take(4096)
        .collect::<String>()
        .chars()
        .rev()
        .collect()
}

#[derive(Clone)]
struct Job {
    prepared: PreparedJob,
    record: Value,
    submitted: f64,
    started: Option<f64>,
    finished: Option<f64>,
    stdout: String,
    stderr: String,
}
impl Job {
    fn view(&self, full: bool) -> Value {
        let mut value = self.record.clone();
        value["submitted_at"] = json!(iso(self.submitted));
        value["started_at"] = json!(self.started.map(iso));
        value["finished_at"] = json!(self.finished.map(iso));
        value["queue_wait_s"] = json!(
            self.started
                .map(|n| ((n - self.submitted) * 10.0).round() / 10.0)
        );
        value["duration_s"] = json!(
            self.started
                .zip(self.finished)
                .map(|(s, f)| ((f - s) * 10.0).round() / 10.0)
        );
        if full {
            value["logs"] =
                json!({"stdout_tail":tail(&self.stdout),"stderr_tail":tail(&self.stderr)});
        } else {
            value.as_object_mut().unwrap().remove("logs");
        }
        value
    }
}
#[derive(Default)]
struct Store {
    jobs: BTreeMap<String, Job>,
    order: Vec<String>,
    queues: BTreeMap<String, VecDeque<String>>,
    ids: BTreeMap<String, String>,
    seq: u64,
    metrics: Metrics,
}
impl Store {
    fn prune(&mut self, limit: usize) {
        while self.order.len() > limit {
            let Some(index) = self
                .order
                .iter()
                .position(|id| terminal(self.jobs[id].record["state"].as_str().unwrap_or("")))
            else {
                break;
            };
            let id = self.order.remove(index);
            self.jobs.remove(&id);
            self.ids.retain(|_, value| value != &id);
        }
    }
}
pub struct Service {
    backend: Arc<dyn Backend>,
    store: Mutex<Store>,
    work: Condvar,
    completed: Notify,
    stop: AtomicBool,
    longpoll: AtomicUsize,
    threads: Mutex<Vec<thread::JoinHandle<()>>>,
    _instance_lock: File,
}
impl Service {
    pub fn start(backend: Arc<dyn Backend>) -> Result<Arc<Self>> {
        let cfg = backend.settings();
        fs::create_dir_all(cfg.data_dir.join("jobs"))?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(cfg.data_dir.join("service.lock"))?;
        fs2::FileExt::try_lock_exclusive(&lock)?;
        let mut store = Store::default();
        for lane in ["timing", "correctness", "func"] {
            store.queues.insert(lane.into(), VecDeque::new());
        }
        // Load both legacy journals and new per-job snapshots. Request payloads
        // remain private and are never returned in API responses.
        let mut journal = BTreeMap::new();
        if let Ok(text) = fs::read_to_string(cfg.data_dir.join("jobs.jsonl")) {
            for line in text.lines() {
                if let Ok(v) = parse_json(line)
                    && let Some(id) = v["job_id"].as_str()
                {
                    journal.insert(id.to_owned(), v);
                }
            }
        }
        for ent in fs::read_dir(cfg.data_dir.join("jobs"))? {
            let ent = ent?;
            if !ent.file_type()?.is_dir() {
                continue;
            }
            let id = ent.file_name().to_string_lossy().into_owned();
            if uuid::Uuid::parse_str(&id).is_err() {
                continue;
            }
            let dir = ent.path();
            let payload = load_json(dir.join("request.json")).unwrap_or(Value::Null);
            let record = load_json(dir.join("result.json"))
                .ok()
                .or_else(|| journal.get(&id).cloned());
            let mut record = if let Some(record) = record {
                record
            } else {
                if !payload.is_object() {
                    continue;
                }
                let prepared = backend.validate(&payload)?;
                store.seq += 1;
                json!({"job_id":id,"seq":store.seq,"state":"running","request":prepared.request,"result":null,"kope_mem":null,"error":null})
            };
            let epochs = journal.get(&id).unwrap_or(&record);
            let submitted = epochs["submitted_epoch"].as_f64().unwrap_or_else(epoch);
            let started = epochs["started_epoch"].as_f64();
            let finished = epochs["finished_epoch"].as_f64();
            let lane = record["request"]["lane"]
                .as_str()
                .unwrap_or("timing")
                .to_owned();
            for key in ["submitted_epoch", "started_epoch", "finished_epoch"] {
                if let Some(map) = record.as_object_mut() {
                    map.remove(key);
                }
            }
            let prepared = PreparedJob {
                request: record["request"].clone(),
                payload: Value::Null,
                lane,
            };
            let seq = record["seq"].as_u64().unwrap_or(0);
            store.seq = store.seq.max(seq);
            let mut job = Job {
                prepared,
                record,
                submitted,
                started,
                finished,
                stdout: fs::read_to_string(dir.join("stdout.log")).unwrap_or_default(),
                stderr: fs::read_to_string(dir.join("stderr.log")).unwrap_or_default(),
            };
            if !terminal(job.record["state"].as_str().unwrap_or("")) {
                if dir.join("manifest.json").exists() {
                    let raw = load_json(dir.join("manifest.json"))?;
                    let targets =
                        raw.as_array()
                            .ok_or_else(|| anyhow::anyhow!("invalid recovery manifest"))?
                            .iter()
                            .map(|v| {
                                Ok((
                                    backend.resolve_path(v["path"].as_str().ok_or_else(
                                        || anyhow::anyhow!("invalid recovery path"),
                                    )?)?,
                                    true,
                                ))
                            })
                            .collect::<Result<Vec<_>>>()?;
                    let _locks = FileLocks::acquire(&targets, &cfg.path_lock_dir, None)?;
                    let mut deployment = Deployment::from_manifest(&dir, backend.as_ref())?;
                    deployment.restore()?;
                }
                job.record["state"] = json!("failed");
                job.record["error"] = json!("service restarted during job; deployment recovered");
                job.finished = Some(epoch());
                persist(cfg, &id, &job)?;
            }
            if let Some(cid) = job.prepared.request["client_request_id"].as_str() {
                store.ids.insert(cid.to_owned(), id.clone());
            }
            store.metrics.submissions += 1;
            store.metrics.last_submission = store.metrics.last_submission.max(submitted);
            let state = job.record["state"].as_str().unwrap_or("failed");
            *store.metrics.jobs.entry(state.into()).or_default() += 1;
            store.jobs.insert(id.clone(), job);
            store.order.push(id);
        }
        store
            .order
            .sort_by_key(|id| store.jobs[id].record["seq"].as_u64().unwrap_or(0));
        let mut recent: Vec<_> = store
            .jobs
            .values()
            .filter(|j| j.finished.is_some())
            .collect();
        recent.sort_by(|a, b| b.finished.unwrap().total_cmp(&a.finished.unwrap()));
        for job in recent.into_iter().take(3) {
            let mut view = job.view(false);
            view["finished_epoch"] = json!(job.finished);
            store.metrics.recent.push_back(view);
            store.metrics.last_completion =
                store.metrics.last_completion.max(job.finished.unwrap());
        }
        store.prune(cfg.history_mem);
        let service = Arc::new(Self {
            backend,
            store: Mutex::new(store),
            work: Condvar::new(),
            completed: Notify::new(),
            stop: AtomicBool::new(false),
            longpoll: AtomicUsize::new(0),
            threads: Mutex::new(Vec::new()),
            _instance_lock: lock,
        });
        for worker in service.backend.workers() {
            let owner = service.clone();
            service
                .threads
                .lock()
                .unwrap()
                .push(thread::spawn(move || owner.worker(worker)));
        }
        Ok(service)
    }
    pub fn router(self: &Arc<Self>) -> Router {
        Router::new()
            .route("/healthz", get(health))
            .route("/metrics", get(metrics))
            .route("/", get(index))
            .route("/jobs", get(history).post(submit))
            .route("/jobs/{id}", get(job).delete(cancel))
            .fallback(not_found)
            .layer(DefaultBodyLimit::max(
                self.backend.settings().body_max_bytes,
            ))
            .with_state(self.clone())
    }
    pub async fn serve(self: &Arc<Self>) -> Result<()> {
        let cfg = self.backend.settings();
        let listener = tokio::net::TcpListener::bind((cfg.host.as_str(), cfg.port)).await?;
        println!("rvbench-evald listening on {}", listener.local_addr()?);
        let owner = self.clone();
        axum::serve(listener, self.router())
            .with_graceful_shutdown(async move {
                shutdown_signal().await;
                owner.stop.store(true, Ordering::SeqCst);
                owner.work.notify_all();
            })
            .await?;
        let owner = self.clone();
        tokio::task::spawn_blocking(move || owner.shutdown()).await?;
        Ok(())
    }
    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::SeqCst);
        self.work.notify_all();
        for thread in self.threads.lock().unwrap().drain(..) {
            let _ = thread.join();
        }
    }
    fn stats(&self, store: &Store) -> Value {
        let mut lanes = json!({});
        for lane in ["timing", "correctness", "func"] {
            lanes[lane] = json!({"queued":store.queues.get(lane).map(VecDeque::len).unwrap_or(0),"running":store.jobs.values().filter(|j|j.prepared.lane==lane&&j.record["state"]=="running").count()});
        }
        lanes
    }
    fn position(store: &Store, id: &str) -> Value {
        let Some(job) = store.jobs.get(id) else {
            return Value::Null;
        };
        json!(
            store
                .queues
                .get(&job.prepared.lane)
                .and_then(|q| q.iter().position(|v| v == id))
                .map(|n| n + 1)
        )
    }
    fn get(&self, id: &str, full: bool) -> Option<Value> {
        let store = self.store.lock().unwrap();
        store.jobs.get(id).map(|job| {
            let mut view = job.view(full);
            if job.record["state"] == "queued" {
                view["queue_position"] = Self::position(&store, id);
            }
            view
        })
    }
    fn response(&self, path: &str, code: StatusCode, value: Value) -> Response {
        *self
            .store
            .lock()
            .unwrap()
            .metrics
            .http
            .entry((path.into(), code.as_u16()))
            .or_default() += 1;
        (code, axum::Json(value)).into_response()
    }
    fn authorized(&self, headers: &HeaderMap) -> bool {
        headers
            .get("X-K3-Token")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| constant_time(v.as_bytes(), self.backend.settings().token.as_bytes()))
    }
    fn enqueue(&self, prepared: PreparedJob) -> Result<(Value, bool)> {
        let cfg = self.backend.settings();
        let mut store = self.store.lock().unwrap();
        ensure!(!self.stop.load(Ordering::SeqCst), "service is stopping");
        if let Some(cid) = prepared.request["client_request_id"].as_str()
            && let Some(id) = store.ids.get(cid).cloned()
        {
            let job = store.jobs.get_mut(&id).unwrap();
            job.record["dedup_hit"] = json!(job.record["dedup_hit"].as_u64().unwrap_or(0) + 1);
            persist(cfg, &id, job)?;
            return Ok((
                json!({"job_id":id,"state":store.jobs[&id].record["state"],"position":Self::position(&store,&id),"deduplicated":true}),
                true,
            ));
        }
        ensure!(
            store.queues.values().map(VecDeque::len).sum::<usize>() < cfg.queue_max,
            "queue full"
        );
        if let Some(max) = cfg.lane_queue_max[&prepared.lane].as_u64() {
            ensure!(
                store.queues[&prepared.lane].len() < (max as usize),
                "queue full"
            );
        }
        let id = uuid::Uuid::now_v7().to_string();
        store.seq += 1;
        let submitted = epoch();
        let mut record = json!({"job_id":id,"seq":store.seq,"state":"queued","request":prepared.request,"result":null,"kope_mem":null,"error":null});
        if let Some(extra) = cfg.result_extra.as_object() {
            for (key, v) in extra {
                ensure!(
                    !record.as_object().unwrap().contains_key(key),
                    "result_extra overlaps reserved job field"
                );
                record[key] = v.clone();
            }
        }
        let job = Job {
            prepared: PreparedJob {
                request: prepared.request.clone(),
                payload: Value::Null,
                lane: prepared.lane.clone(),
            },
            record,
            submitted,
            started: None,
            finished: None,
            stdout: String::new(),
            stderr: String::new(),
        };
        let dir = cfg.data_dir.join("jobs").join(&id);
        fs::create_dir_all(&dir)?;
        atomic_json(&dir.join("request.json"), &prepared.payload)?;
        persist(cfg, &id, &job)?;
        if let Some(cid) = prepared.request["client_request_id"].as_str() {
            store.ids.insert(cid.into(), id.clone());
        }
        store.metrics.submissions += 1;
        store.metrics.last_submission = submitted;
        store.jobs.insert(id.clone(), job);
        store.order.push(id.clone());
        store
            .queues
            .get_mut(&prepared.lane)
            .unwrap()
            .push_back(id.clone());
        let reply = json!({"job_id":id,"state":"queued","position":Self::position(&store,&id)});
        drop(store);
        self.work.notify_all();
        Ok((reply, false))
    }
    fn worker(self: &Arc<Self>, worker: WorkerSpec) {
        let mut executor = self.backend.executor(&worker);
        loop {
            let mut store = self.store.lock().unwrap();
            while store.queues[&worker.lane].is_empty() && !self.stop.load(Ordering::SeqCst) {
                store = self.work.wait(store).unwrap();
            }
            if self.stop.load(Ordering::SeqCst) {
                break;
            }
            let id = store
                .queues
                .get_mut(&worker.lane)
                .unwrap()
                .pop_front()
                .unwrap();
            let started = epoch();
            let job = store.jobs.get_mut(&id).unwrap();
            job.started = Some(started);
            job.record["state"] = json!("running");
            let job = job.clone();
            if let Err(error) = persist(self.backend.settings(), &id, &job) {
                drop(store);
                self.finish(
                    &id,
                    Execution::failed(format!("cannot persist running state: {error}")),
                );
                continue;
            }
            store
                .metrics
                .observe("queue_wait", &worker.lane, started - job.submitted);
            drop(store);
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.run_job(&id, &job, &worker, executor.as_mut())
            }))
            .unwrap_or_else(|_| Execution::failed("worker panic; job failed"));
            self.finish(&id, outcome);
        }
    }
    fn run_job(
        &self,
        id: &str,
        job: &Job,
        worker: &WorkerSpec,
        executor: &mut dyn crate::Executor,
    ) -> Execution {
        let cfg = self.backend.settings();
        let dir = cfg.data_dir.join("jobs").join(id);
        let targets = match self.backend.lock_targets(&job.prepared) {
            Ok(v) => v,
            Err(e) => return Execution::failed(e.to_string()),
        };
        let _locks =
            match FileLocks::acquire(&targets, &cfg.path_lock_dir, worker.timing_lock.as_deref()) {
                Ok(v) => v,
                Err(e) => {
                    return Execution::failed(format!("cannot acquire experiment locks: {e}"));
                }
            };
        self.store
            .lock()
            .unwrap()
            .metrics
            .observe("pathlock_wait", "all", _locks.waited_s);
        let mut deployment = Deployment::new(&dir);
        let mut deployed = false;
        let mut attempted_deploy = false;
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<Execution> {
                let request = &job.prepared.request;
                if let Some(expected) = request["expect_sha256"].as_str() {
                    let target = self
                        .backend
                        .resolve_path(request["expect_path"].as_str().unwrap())?;
                    ensure!(
                        target.is_file() && sha256(&fs::read(target)?) == expected,
                        "slot_poisoned: current slot identity mismatch"
                    );
                }
                if !request["files"].as_array().is_none_or(Vec::is_empty) {
                    let payload = load_json(dir.join("request.json"))?;
                    let expected = request["files"].as_array().unwrap();
                    let actual = payload["files"]
                        .as_array()
                        .ok_or_else(|| anyhow::anyhow!("stored deployment payload is missing"))?;
                    ensure!(
                        expected.len() == actual.len()
                            && expected
                                .iter()
                                .zip(actual)
                                .all(|(e, v)| e["path"] == v["path"]
                                    && v["content"].as_str().is_some_and(
                                        |content| e["sha256"] == sha256(content.as_bytes())
                                    )),
                        "loaded_mismatch: stored candidate identity changed after admission"
                    );
                    attempted_deploy = true;
                    let started = Instant::now();
                    deployment.deploy(self.backend.as_ref(), &payload["files"])?;
                    deployed = true;
                    self.store.lock().unwrap().metrics.observe(
                        "deploy",
                        "all",
                        started.elapsed().as_secs_f64(),
                    );
                }
                let timeout =
                    Duration::from_secs_f64(request["timeout_min"].as_f64().unwrap() * 60.0);
                let mut execution = executor.execute(id, request, timeout)?;
                if execution.result.is_object() {
                    if worker.lane != "timing" {
                        execution.result["lane"] = json!(worker.lane);
                    } else {
                        execution.result["output_selfcheck"] =
                            json!(self.backend.output_selfcheck());
                    }
                    if deployed {
                        execution.result["loaded_files"] = deployment.loaded_files();
                    }
                }
                if worker.lane == "func" && execution.state == "done" {
                    execution.kope_mem = json!({"pass_rate":1.0});
                }
                Ok(execution)
            }));
        let mut execution = match result {
            Ok(Ok(v)) => v,
            Ok(Err(e)) => Execution::failed(e.to_string()),
            Err(_) => Execution::failed("executor panic"),
        };
        if attempted_deploy && (!deployed || job.prepared.request["restore"] == true) {
            let start = Instant::now();
            if let Err(error) = deployment.restore() {
                execution.state = "failed".into();
                execution.error = Some(format!(
                    "{}; restore failed: {error}",
                    execution.error.unwrap_or_default()
                ));
                self.store.lock().unwrap().metrics.restore_fail += 1;
            }
            self.store.lock().unwrap().metrics.observe(
                "restore",
                "all",
                start.elapsed().as_secs_f64(),
            );
        }
        if execution
            .error
            .as_ref()
            .is_some_and(|s| s.contains("slot_poisoned"))
        {
            self.store.lock().unwrap().metrics.slot_drift += 1;
            execution.result = json!({"infra_blocked":true,"reason":execution.error});
        }
        if execution
            .error
            .as_ref()
            .is_some_and(|s| s.contains("loaded_mismatch"))
        {
            self.store.lock().unwrap().metrics.loaded_mismatch += 1;
        }
        if execution.state == "failed" {
            if !execution.result.is_object() {
                execution.result = json!({});
            }
            execution.result["stderr_path"] = json!(dir.join("stderr.log"));
            if let Some(code) = execution.exit_code {
                execution.result["exit_code"] = json!(code);
                if code < 0 {
                    execution.result["signal_name"] = json!(match -code {
                        9 => "SIGKILL".into(),
                        11 => "SIGSEGV".into(),
                        n => format!("SIG{n}"),
                    });
                }
            }
        }
        execution
    }
    fn finish(&self, id: &str, result: Execution) {
        let cfg = self.backend.settings();
        let mut store = self.store.lock().unwrap();
        if result.fallback {
            store.metrics.fallback += 1;
        }
        let job = store.jobs.get_mut(id).unwrap();
        job.finished = Some(epoch());
        job.record["state"] = json!(result.state);
        job.record["result"] = result.result;
        job.record["kope_mem"] = result.kope_mem;
        job.record["error"] = json!(result.error);
        job.stdout = result.stdout;
        job.stderr = result.stderr;
        let mut view = job.view(false);
        view["finished_epoch"] = json!(job.finished);
        let finished = job.finished.unwrap();
        if let Err(error) = persist(cfg, id, job) {
            job.record["state"] = json!("failed");
            job.record["error"] = json!(format!("result persistence failed: {error}"));
        }
        job.stdout = tail(&job.stdout);
        job.stderr = tail(&job.stderr);
        store.metrics.finished(&view, finished);
        store.prune(cfg.history_mem);
        drop(store);
        self.completed.notify_waiters();
    }
}
fn persist(cfg: &crate::Settings, id: &str, job: &Job) -> Result<()> {
    let dir = cfg.data_dir.join("jobs").join(id);
    atomic_json(&dir.join("result.json"), &job.view(true))?;
    crate::files::atomic_write(&dir.join("stdout.log"), job.stdout.as_bytes())?;
    crate::files::atomic_write(&dir.join("stderr.log"), job.stderr.as_bytes())?;
    let mut record = job.view(false);
    record["submitted_epoch"] = json!(job.submitted);
    record["started_epoch"] = json!(job.started);
    record["finished_epoch"] = json!(job.finished);
    let mut journal = OpenOptions::new()
        .append(true)
        .create(true)
        .open(cfg.data_dir.join("jobs.jsonl"))?;
    journal.write_all(&serde_json::to_vec(&record)?)?;
    journal.write_all(b"\n")?;
    journal.sync_all()?;
    Ok(())
}
fn constant_time(a: &[u8], b: &[u8]) -> bool {
    let mut diff = a.len() ^ b.len();
    for index in 0..a.len().max(b.len()) {
        diff |= (a.get(index).copied().unwrap_or(0) ^ b.get(index).copied().unwrap_or(0)) as usize;
    }
    diff == 0
}
fn denied(service: &Service, path: &str) -> Response {
    service.response(
        path,
        StatusCode::UNAUTHORIZED,
        json!({"error":"bad or missing X-K3-Token"}),
    )
}
async fn health(State(service): State<Arc<Service>>) -> Response {
    let store = service.store.lock().unwrap();
    let lanes = service.stats(&store);
    let queued = store.queues.values().map(VecDeque::len).sum::<usize>();
    let running = store
        .jobs
        .values()
        .filter(|j| j.record["state"] == "running")
        .count();
    drop(store);
    service.response("/healthz",StatusCode::OK,json!({"ok":true,"queued":queued,"running":running,"lanes":lanes,"loadavg":fs::read_to_string("/proc/loadavg").ok().map(|s|s.split_whitespace().take(3).map(str::to_owned).collect::<Vec<_>>())}))
}
async fn metrics(State(service): State<Arc<Service>>) -> Response {
    let store = service.store.lock().unwrap();
    let lanes = service.stats(&store);
    let mut oldest = json!({});
    for (lane, q) in &store.queues {
        oldest[lane] = json!(
            q.front()
                .map(|id| epoch() - store.jobs[id].submitted)
                .unwrap_or(0.0)
        );
    }
    let cfg = service.backend.settings();
    let text = store.metrics.render(
        &lanes,
        &cfg.lane_queue_max,
        cfg.queue_max,
        service.longpoll.load(Ordering::SeqCst),
        &oldest,
    );
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        text,
    )
        .into_response()
}
async fn index(State(service): State<Arc<Service>>, headers: HeaderMap) -> Response {
    if !service.authorized(&headers) {
        return denied(&service, "/");
    }
    service.response(
        "/",
        StatusCode::OK,
        json!({"endpoints":["/healthz","/metrics","/jobs","/jobs/{id}?wait=N"]}),
    )
}
async fn submit(State(service): State<Arc<Service>>, headers: HeaderMap, bytes: Bytes) -> Response {
    if !service.authorized(&headers) {
        return denied(&service, "/jobs");
    }
    if bytes.is_empty() {
        return service.response(
            "/jobs",
            StatusCode::PAYLOAD_TOO_LARGE,
            json!({"error":"empty body"}),
        );
    }
    let result = std::str::from_utf8(&bytes)
        .map_err(|_| anyhow::anyhow!("invalid UTF-8"))
        .and_then(parse_json)
        .and_then(|v| service.backend.validate(&v));
    let prepared = match result {
        Ok(v) => v,
        Err(error) => {
            return service.response(
                "/jobs",
                StatusCode::BAD_REQUEST,
                json!({"error":error.to_string()}),
            );
        }
    };
    match service.enqueue(prepared) {
        Ok((v, _)) => service.response("/jobs", StatusCode::ACCEPTED, v),
        Err(error) => service.response(
            "/jobs",
            if error.to_string() == "queue full" {
                StatusCode::TOO_MANY_REQUESTS
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            },
            json!({"error":error.to_string()}),
        ),
    }
}
async fn history(
    State(service): State<Arc<Service>>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    if !service.authorized(&headers) {
        return denied(&service, "/jobs");
    }
    let store = service.store.lock().unwrap();
    let limit = query
        .get("limit")
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(50)
        .clamp(1, 500);
    let since = query.get("since");
    if since.is_some_and(|id| !store.jobs.contains_key(id)) {
        drop(store);
        return service.response(
            "/jobs",
            StatusCode::NOT_FOUND,
            json!({"error":"unknown since id"}),
        );
    }
    let seq = since
        .map(|id| store.jobs[id].record["seq"].as_u64().unwrap())
        .unwrap_or(0);
    let jobs: Vec<_> = store
        .order
        .iter()
        .rev()
        .filter(|id| store.jobs[*id].record["seq"].as_u64().unwrap_or(0) > seq)
        .take(limit)
        .map(|id| store.jobs[id].view(false))
        .collect();
    drop(store);
    service.response("/jobs", StatusCode::OK, json!({"jobs":jobs}))
}
async fn job(
    State(service): State<Arc<Service>>,
    headers: HeaderMap,
    HttpPath(id): HttpPath<String>,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    if !service.authorized(&headers) {
        return denied(&service, "/jobs/{id}");
    }
    let wait = query
        .get("wait")
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0)
        .min(service.backend.settings().longpoll_max_s);
    let deadline = Instant::now() + Duration::from_secs(wait);
    loop {
        let ready = service.completed.notified();
        tokio::pin!(ready);
        ready.as_mut().enable();
        let Some(view) = service.get(&id, true) else {
            return service.response(
                "/jobs/{id}",
                StatusCode::NOT_FOUND,
                json!({"error":"unknown job_id"}),
            );
        };
        if wait == 0 || terminal(view["state"].as_str().unwrap()) || Instant::now() >= deadline {
            return service.response("/jobs/{id}", StatusCode::OK, view);
        }
        let _guard = PollGuard::new(&service.longpoll);
        let _ = tokio::time::timeout(
            deadline.saturating_duration_since(Instant::now()),
            &mut ready,
        )
        .await;
    }
}
async fn cancel(
    State(service): State<Arc<Service>>,
    headers: HeaderMap,
    HttpPath(id): HttpPath<String>,
) -> Response {
    if !service.authorized(&headers) {
        return denied(&service, "/jobs/{id}");
    }
    let mut store = service.store.lock().unwrap();
    let Some(job) = store.jobs.get_mut(&id) else {
        drop(store);
        return service.response(
            "/jobs/{id}",
            StatusCode::NOT_FOUND,
            json!({"error":"unknown job_id"}),
        );
    };
    if job.record["state"] != "queued" {
        drop(store);
        return service.response(
            "/jobs/{id}",
            StatusCode::CONFLICT,
            json!({"error":"job is not queued"}),
        );
    }
    job.record["state"] = json!("canceled");
    job.finished = Some(epoch());
    let lane = job.prepared.lane.clone();
    let result = persist(service.backend.settings(), &id, job);
    store.queues.get_mut(&lane).unwrap().retain(|v| v != &id);
    drop(store);
    service.completed.notify_waiters();
    match result {
        Ok(()) => service.response(
            "/jobs/{id}",
            StatusCode::OK,
            json!({"job_id":id,"state":"canceled"}),
        ),
        Err(e) => service.response(
            "/jobs/{id}",
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"error":e.to_string()}),
        ),
    }
}
async fn not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        axum::Json(json!({"error":"not found"})),
    )
        .into_response()
}
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("SIGTERM handler registration");
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{}}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
struct PollGuard<'a>(&'a AtomicUsize);
impl<'a> PollGuard<'a> {
    fn new(counter: &'a AtomicUsize) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        Self(counter)
    }
}
impl Drop for PollGuard<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}
