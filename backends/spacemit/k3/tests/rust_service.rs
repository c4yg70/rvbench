use anyhow::Result;
use rvbench_k3::K3Config;
use rvbench_runtime::{
    Backend, Execution, Executor, PreparedJob, Service, Settings, WorkerSpec,
    files::{Deployment, atomic_json},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

#[derive(Clone)]
struct FakeBackend {
    cfg: K3Config,
    active: Arc<AtomicUsize>,
    peak: Arc<AtomicUsize>,
}
struct FakeExecutor {
    backend: FakeBackend,
}
impl Executor for FakeExecutor {
    fn execute(&mut self, _: &str, request: &Value, _: Duration) -> Result<Execution> {
        let active = self.backend.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.backend.peak.fetch_max(active, Ordering::SeqCst);
        if request["seed"] == 777 {
            // Keep the first worker active until the independent job enters execution.
            // A serial implementation times out here instead of accidentally passing.
            let deadline = std::time::Instant::now() + Duration::from_secs(4);
            while self.backend.peak.load(Ordering::SeqCst) < 2
                && std::time::Instant::now() < deadline
            {
                std::thread::sleep(Duration::from_millis(2));
            }
        }
        std::thread::sleep(Duration::from_millis(
            request["warmup"].as_u64().unwrap_or(0),
        ));
        let installed = fs::read_to_string(self.backend.cfg.resolve_path(&format!(
            "flag_gems/ops/{}.py",
            request["op"].as_str().unwrap()
        ))?)
        .unwrap_or_default();
        self.backend.active.fetch_sub(1, Ordering::SeqCst);
        if request["seed"] == 42 {
            let mut result = Execution::failed("simulated crash");
            result.exit_code = Some(42);
            result.stderr = "full diagnostic stderr".into();
            return Ok(result);
        }
        if request["seed"] == 99 {
            let mut result = Execution::failed("simulated timeout");
            result.state = "timeout".into();
            return Ok(result);
        }
        Ok(Execution {
            state: "done".into(),
            result: json!({"correct":true,"torch_ms":2.0,"gems_ms":1.0,"speedup":2.0,"observed":installed}),
            kope_mem: json!({"pass_rate":1.0,"speedup":2.0}),
            error: None,
            stdout: "fixture output".into(),
            stderr: String::new(),
            exit_code: Some(0),
            fallback: false,
        })
    }
}
impl Backend for FakeBackend {
    fn settings(&self) -> &Settings {
        &self.cfg.settings
    }
    fn workers(&self) -> Vec<WorkerSpec> {
        self.cfg.workers.clone()
    }
    fn validate(&self, v: &Value) -> Result<PreparedJob> {
        self.cfg.validate(v)
    }
    fn resolve_path(&self, p: &str) -> Result<PathBuf> {
        self.cfg.resolve_path(p)
    }
    fn lock_targets(&self, j: &PreparedJob) -> Result<Vec<(PathBuf, bool)>> {
        self.cfg.lock_targets(j)
    }
    fn executor(&self, _: &WorkerSpec) -> Box<dyn Executor> {
        Box::new(FakeExecutor {
            backend: self.clone(),
        })
    }
    fn output_selfcheck(&self) -> bool {
        false
    }
}
fn config(directory: &std::path::Path, workers: usize, queue: usize) -> K3Config {
    fs::create_dir_all(directory.join("fg/ops")).unwrap();
    fs::create_dir_all(directory.join("scripts")).unwrap();
    fs::write(directory.join("fg/ops/abs.py"), "original").unwrap();
    fs::write(directory.join("fg/ops/mm.py"), "matrix original").unwrap();
    K3Config::from_value(json!({"host":"127.0.0.1","port":0,"token":"test-token","flag_gems_root":directory.join("fg"),"scripts_root":directory.join("scripts"),"run_env":"unused","data_dir":directory.join("data"),"path_lock_dir":directory.join("locks"),"lock_file":directory.join("timing.lock"),"queue_max":queue,"executor":"subprocess","correctness_workers":0,"evaluators":{"perf":{"enabled":true,"workers":workers,"warmup_times":5,"repeat_times":5},"acc":{"enabled":false},"func":{"enabled":false}}}),directory).unwrap()
}
async fn app(
    cfg: K3Config,
) -> (
    Arc<Service>,
    FakeBackend,
    String,
    tokio::task::JoinHandle<()>,
) {
    let backend = FakeBackend {
        cfg,
        active: Arc::new(AtomicUsize::new(0)),
        peak: Arc::new(AtomicUsize::new(0)),
    };
    let service = Service::start(Arc::new(backend.clone())).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let router = service.router();
    let server = tokio::spawn(async move {
        axum_serve(listener, router).await;
    });
    (service, backend, url, server)
}
async fn axum_serve(listener: tokio::net::TcpListener, router: axum::Router) {
    axum::serve(listener, router).await.unwrap();
}
async fn submit(client: &reqwest::Client, url: &str, body: Value) -> Value {
    let response = client
        .post(format!("{url}/jobs"))
        .header("X-K3-Token", "test-token")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 202);
    response.json().await.unwrap()
}
async fn get(client: &reqwest::Client, url: &str, id: &str, wait: u64) -> Value {
    client
        .get(format!("{url}/jobs/{id}?wait={wait}"))
        .header("X-K3-Token", "test-token")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap()
}
async fn close(service: Arc<Service>, server: tokio::task::JoinHandle<()>) {
    service.shutdown();
    server.abort();
    let _ = server.await;
    drop(service);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn authenticated_lifecycle_dedup_and_restore() {
    let temp = tempfile::tempdir().unwrap();
    let (service, _, url, server) = app(config(temp.path(), 1, 64)).await;
    let client = reqwest::Client::new();
    assert_eq!(
        client
            .get(format!("{url}/healthz"))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(
        client
            .get(format!("{url}/jobs"))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let body = json!({"op":"abs","shape":"64","warmup":30,"client_request_id":"stable-1","files":[{"path":"flag_gems/ops/abs.py","content":"candidate"}]});
    let submitted = submit(&client, &url, body.clone()).await;
    let id = submitted["job_id"].as_str().unwrap();
    let duplicate = submit(&client, &url, body).await;
    assert_eq!(duplicate["job_id"], id);
    assert_eq!(duplicate["deduplicated"], true);
    let job = get(&client, &url, id, 5).await;
    assert_eq!(job["state"], "done");
    assert_eq!(job["result"]["observed"], "candidate");
    assert_eq!(
        job["result"]["loaded_files"][0]["sha256"],
        rvbench_core::json::sha256(b"candidate")
    );
    assert_eq!(
        fs::read_to_string(temp.path().join("fg/ops/abs.py")).unwrap(),
        "original"
    );
    let manifest =
        rvbench_core::load_json(temp.path().join("data/jobs").join(id).join("manifest.json"))
            .unwrap();
    assert_eq!(
        manifest[0]["restored_sha256"],
        rvbench_core::json::sha256(b"original")
    );
    let metrics = client
        .get(format!("{url}/metrics"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(metrics.contains("evald_up 1"));
    assert!(metrics.contains("evald_jobs_total{state=\"done\"} 1"));
    close(service, server).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn same_path_serializes_across_timing_workers() {
    let temp = tempfile::tempdir().unwrap();
    let (service, backend, url, server) = app(config(temp.path(), 2, 64)).await;
    let client = reqwest::Client::new();
    let mut ids = Vec::new();
    for candidate in ["one", "two"] {
        ids.push(submit(&client,&url,json!({"op":"abs","shape":"64","warmup":80,"files":[{"path":"flag_gems/ops/abs.py","content":candidate}]})).await["job_id"].as_str().unwrap().to_owned());
    }
    for (index, id) in ids.iter().enumerate() {
        let job = get(&client, &url, id, 5).await;
        assert_eq!(job["state"], "done");
        assert_eq!(
            job["result"]["observed"],
            if index == 0 { "one" } else { "two" }
        );
    }
    assert_eq!(backend.peak.load(Ordering::SeqCst), 1);
    assert_eq!(
        fs::read_to_string(temp.path().join("fg/ops/abs.py")).unwrap(),
        "original"
    );
    close(service, server).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn distinct_paths_run_in_parallel() {
    let temp = tempfile::tempdir().unwrap();
    let (service, backend, url, server) = app(config(temp.path(), 2, 64)).await;
    let client = reqwest::Client::new();
    let mut ids = Vec::new();
    for op in ["abs", "mm"] {
        ids.push(
            submit(
                &client,
                &url,
                json!({"op":op,"shape":"64","warmup":0,"seed":777}),
            )
            .await["job_id"]
                .as_str()
                .unwrap()
                .to_owned(),
        );
    }
    for id in &ids {
        assert_eq!(get(&client, &url, id, 5).await["state"], "done");
    }
    assert_eq!(backend.peak.load(Ordering::SeqCst), 2);
    close(service, server).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn queue_limits_and_cancel() {
    let temp = tempfile::tempdir().unwrap();
    let (service, _, url, server) = app(config(temp.path(), 1, 1)).await;
    let client = reqwest::Client::new();
    let first = submit(&client, &url, json!({"op":"abs","shape":"64","warmup":800})).await;
    let id = first["job_id"].as_str().unwrap();
    for _ in 0..100 {
        if get(&client, &url, id, 0).await["state"] == "running" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
    let queued = submit(&client, &url, json!({"op":"abs","shape":"64"})).await;
    assert_eq!(
        client
            .post(format!("{url}/jobs"))
            .header("X-K3-Token", "test-token")
            .json(&json!({"op":"abs","shape":"64"}))
            .send()
            .await
            .unwrap()
            .status(),
        429
    );
    let canceled = client
        .delete(format!("{url}/jobs/{}", queued["job_id"].as_str().unwrap()))
        .header("X-K3-Token", "test-token")
        .send()
        .await
        .unwrap();
    assert_eq!(canceled.status(), 200);
    assert_eq!(
        get(&client, &url, queued["job_id"].as_str().unwrap(), 0).await["state"],
        "canceled"
    );
    assert_eq!(get(&client, &url, id, 5).await["state"], "done");
    close(service, server).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_and_timeout_jobs_restore_candidates() {
    let temp = tempfile::tempdir().unwrap();
    let (service, _, url, server) = app(config(temp.path(), 1, 64)).await;
    let client = reqwest::Client::new();
    for (seed, state) in [(42, "failed"), (99, "timeout")] {
        let submitted=submit(&client,&url,json!({"op":"abs","shape":"64","seed":seed,"files":[{"path":"flag_gems/ops/abs.py","content":"candidate"}]})).await;
        let job = get(&client, &url, submitted["job_id"].as_str().unwrap(), 5).await;
        assert_eq!(job["state"], state);
        assert_eq!(
            fs::read_to_string(temp.path().join("fg/ops/abs.py")).unwrap(),
            "original"
        );
        if seed == 42 {
            assert_eq!(job["result"]["exit_code"], 42);
            assert!(
                job["logs"]["stderr_tail"]
                    .as_str()
                    .unwrap()
                    .contains("diagnostic")
            );
        }
    }
    close(service, server).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn restart_recovers_deployment_without_a_journal_record() {
    let temp = tempfile::tempdir().unwrap();
    let cfg = config(temp.path(), 1, 64);
    let id = "01900000-0000-7000-8000-000000000001";
    let dir = temp.path().join("data/jobs").join(id);
    fs::create_dir_all(&dir).unwrap();
    let payload = json!({"op":"abs","shape":"64","files":[{"path":"flag_gems/ops/abs.py","content":"interrupted"}]});
    atomic_json(&dir.join("request.json"), &payload).unwrap();
    let mut deploy = Deployment::new(&dir);
    deploy.deploy(&cfg, &payload["files"]).unwrap();
    drop(deploy);
    assert_eq!(
        fs::read_to_string(temp.path().join("fg/ops/abs.py")).unwrap(),
        "interrupted"
    );
    let (service, _, url, server) = app(cfg).await;
    assert_eq!(
        fs::read_to_string(temp.path().join("fg/ops/abs.py")).unwrap(),
        "original"
    );
    assert_eq!(
        get(&reqwest::Client::new(), &url, id, 0).await["state"],
        "failed"
    );
    close(service, server).await;
}
