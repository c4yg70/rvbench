mod client;
mod pipeline;
use anyhow::{Result, ensure};
use clap::{Args, Parser, Subcommand};
use client::Client;
use reqwest::Method;
use rvbench_core::{
    hidden_readiness_blockers, load_json, readiness_blockers, spec_digest, validate_eval_results,
    validate_hidden_spec, validate_spec,
};
use rvbench_runtime::files::atomic_json;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Parser)]
#[command(
    name = "rvbench",
    version,
    about = "RISC-V benchmark contracts and evaluation client"
)]
pub struct Options {
    #[arg(long, global = true)]
    root: Option<PathBuf>,
    #[arg(long, global = true, env = "EV_WORKSPACE")]
    workspace: Option<PathBuf>,
    #[arg(long, global = true, env = "EV_BACKEND", default_value = "k3")]
    backend: String,
    #[arg(long, global = true)]
    base: Option<String>,
    #[arg(long, global = true, env = "EV_TOKEN", hide_env_values = true)]
    token: Option<String>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Validate {
        #[command(subcommand)]
        kind: Validation,
    },
    Digest {
        file: PathBuf,
    },
    Ev {
        #[command(subcommand)]
        action: Box<Ev>,
    },
    Suite {
        #[command(subcommand)]
        action: Suite,
    },
    ResultUpgrade {
        file: PathBuf,
        #[arg(long)]
        out: Option<PathBuf>,
    },
    Pipeline {
        #[arg(long)]
        once: bool,
        #[arg(long, default_value_t = 20)]
        interval_s: u64,
    },
    OpStatus {
        ops: Vec<String>,
    },
}
#[derive(Subcommand)]
enum Validation {
    Spec {
        file: PathBuf,
        #[arg(long)]
        require_ready: bool,
    },
    Hidden {
        file: PathBuf,
        #[arg(long)]
        require_ready: bool,
    },
    Results {
        file: PathBuf,
        #[arg(long)]
        spec: Option<PathBuf>,
        #[arg(long)]
        hidden_spec: Option<PathBuf>,
    },
}
#[derive(Args)]
struct Run {
    #[arg(long)]
    op: String,
    #[arg(long, required = true)]
    shape: Vec<String>,
    #[arg(long, default_value = "float32")]
    dtype: String,
    #[arg(long,default_value="timing",value_parser=["timing","correctness","func"])]
    lane: String,
    #[arg(long,default_value="op",value_parser=["op","gemm"])]
    bench: String,
    #[arg(long)]
    batch: Option<u32>,
    #[arg(long)]
    candidate: Option<PathBuf>,
    #[arg(long)]
    deploy_path: Option<String>,
    #[arg(long, default_value_t = 5)]
    warmup: u32,
    #[arg(long, default_value_t = 5)]
    rep: u32,
    #[arg(long, default_value_t = 15.0)]
    timeout_min: f64,
    #[arg(long)]
    keep_deployed: bool,
    #[arg(long)]
    executor: Option<String>,
    #[arg(long)]
    seed: Option<u32>,
    #[arg(long)]
    skip_torch: bool,
    #[arg(long)]
    client_request_id: Option<String>,
    #[arg(long)]
    judge: bool,
    #[arg(long, default_value = "KOPE")]
    cond: String,
    #[arg(long)]
    stem: Option<String>,
    #[arg(long, default_value_t = 1)]
    attempt: u32,
    #[arg(long, default_value_t = 480)]
    budget_s: u64,
    #[arg(long)]
    out: Option<PathBuf>,
}
#[derive(Subcommand)]
enum Ev {
    Run(Run),
    Submit(Run),
    Get {
        job_id: String,
        #[arg(long, default_value_t = 60)]
        wait: u64,
    },
    Judge {
        job_id: String,
        #[arg(long, default_value = "KOPE")]
        cond: String,
        #[arg(long)]
        stem: Option<String>,
        #[arg(long, default_value_t = 1)]
        attempt: u32,
    },
    Status {
        #[arg(long, default_value_t = 8)]
        limit: usize,
    },
    History {
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        since: Option<String>,
    },
    Cancel {
        job_id: String,
    },
}
#[derive(Subcommand)]
enum Suite {
    Validate {
        file: PathBuf,
    },
    Expand {
        file: PathBuf,
        wave: PathBuf,
        #[arg(long, default_value = "suite")]
        cond: String,
        #[arg(long, default_value = "timing")]
        lane: String,
        #[arg(long, default_value_t = 1)]
        attempt: i64,
        #[arg(long)]
        dry_run: bool,
    },
    FromBaseline {
        file: PathBuf,
        #[arg(long)]
        name: String,
        #[arg(long)]
        out: PathBuf,
    },
    Run {
        file: PathBuf,
        #[arg(long, default_value = "timing")]
        lane: String,
        #[arg(long, default_value_t = 480)]
        budget_s: u64,
        #[arg(long)]
        out: Option<PathBuf>,
    },
}
impl Options {
    pub fn repo(&self) -> PathBuf {
        self.root.clone().unwrap_or_else(|| {
            let cwd = std::env::current_dir().unwrap_or_default();
            cwd.ancestors()
                .find(|p| p.join("core/Cargo.toml").is_file() && p.join("schemas").is_dir())
                .map(Path::to_owned)
                .unwrap_or_else(|| {
                    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                        .parent()
                        .unwrap()
                        .to_owned()
                })
        })
    }
    pub fn workspace(&self) -> PathBuf {
        self.workspace
            .clone()
            .unwrap_or_else(|| self.repo().parent().unwrap().to_owned())
    }
    pub fn client(&self, lane: &str) -> Result<Client> {
        self.backend_client(lane, &self.backend)
    }
    fn backend_client(&self, lane: &str, backend: &str) -> Result<Client> {
        let profile =
            rvbench_k3::profiles::get(backend).ok_or_else(|| anyhow::anyhow!("unknown backend"))?;
        let base = self.base.clone().unwrap_or_else(|| {
            if lane != "timing" {
                profile.corr_base.unwrap_or(profile.timing_base.clone())
            } else {
                profile.timing_base
            }
        });
        ensure!(!base.is_empty(), "backend is not deployed");
        let token = self.token.clone().or_else(|| {
            load_json(self.repo().join("backends/spacemit/k3/evald/config.json"))
                .ok()
                .and_then(|v| v["token"].as_str().map(str::to_owned))
        });
        Client::new(&base, token)
    }
}
fn print(v: &Value) {
    println!("{}", serde_json::to_string_pretty(v).unwrap());
}
fn checked(errors: Vec<String>) -> Result<()> {
    ensure!(
        errors.is_empty(),
        "INVALID: {} contract violation(s)",
        errors.len()
    );
    Ok(())
}
fn safe_name(s: &str) -> Result<()> {
    ensure!(
        !s.is_empty()
            && s != "."
            && s != ".."
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c)),
        "unsafe path identifier"
    );
    Ok(())
}
fn validate(kind: &Validation) -> Result<i32> {
    match kind {
        Validation::Spec {
            file,
            require_ready,
        } => {
            let value = load_json(file)?;
            checked(validate_spec(&value, file.parent()))?;
            let blockers = readiness_blockers(&value);
            println!(
                "VALID: public spec\nsha256: {}\nreadiness blockers: {}",
                spec_digest(&value),
                blockers.len()
            );
            Ok(if *require_ready && !blockers.is_empty() {
                2
            } else {
                0
            })
        }
        Validation::Hidden {
            file,
            require_ready,
        } => {
            let value = load_json(file)?;
            let relative = rvbench_core::hidden::public_relative_path(&value)?;
            let public_path = file.parent().unwrap_or(Path::new(".")).join(relative);
            let public = load_json(&public_path)?;
            checked(validate_hidden_spec(&value, &public, public_path.parent()))?;
            let blockers = hidden_readiness_blockers(&value, &public);
            println!(
                "VALID: hidden references\nreadiness blockers: {}",
                blockers.len()
            );
            Ok(if *require_ready && !blockers.is_empty() {
                2
            } else {
                0
            })
        }
        Validation::Results {
            file,
            spec,
            hidden_spec,
        } => {
            ensure!(
                hidden_spec.is_none() || spec.is_some(),
                "--hidden-spec requires --spec"
            );
            let result = load_json(file)?;
            let public = spec.as_ref().map(load_json).transpose()?;
            let hidden = hidden_spec.as_ref().map(load_json).transpose()?;
            checked(validate_eval_results(
                &result,
                public.as_ref(),
                hidden.as_ref(),
                spec.as_ref().and_then(|p| p.parent()),
            ))?;
            println!("VALID: result schema and semantic consistency (does not attest execution)");
            Ok(0)
        }
    }
}
fn run_body(options: &Options, run: &Run) -> Result<Value> {
    let shape = if run.shape.len() == 1 {
        json!(run.shape[0])
    } else {
        json!(run.shape)
    };
    let mut body = json!({"op":run.op,"shape":shape,"dtype":run.dtype,"lane":run.lane,"bench":run.bench,"warmup":run.warmup,"rep":run.rep,"timeout_min":run.timeout_min,"restore":!run.keep_deployed});
    if let Some(batch) = run.batch {
        body["batch"] = json!(batch);
    }
    if let Some(executor) = &run.executor {
        body["executor"] = json!(executor);
    }
    if let Some(seed) = run.seed {
        body["seed"] = json!(seed);
    }
    if run.skip_torch {
        body["skip_torch"] = json!(true);
    }
    if let Some(id) = &run.client_request_id {
        body["client_request_id"] = json!(id);
    }
    if let Some(candidate) = &run.candidate {
        let source = if candidate.is_file() {
            candidate.clone()
        } else {
            options.workspace().join(candidate)
        };
        let path = run
            .deploy_path
            .clone()
            .unwrap_or_else(|| format!("flag_gems/ops/{}.py", run.op));
        body["files"] = json!([{"path":path,"content":fs::read_to_string(source)?}]);
    }
    Ok(body)
}
pub fn finalize(
    options: &Options,
    job: &Value,
    cond: &str,
    stem: Option<&str>,
    attempt: u32,
    base: &str,
) -> Result<Value> {
    finalize_into(
        options,
        job,
        cond,
        stem,
        attempt,
        base,
        ResultDestination::default(),
    )
}
#[derive(Default)]
pub struct ResultDestination<'a> {
    pub output: Option<&'a Path>,
    pub registry_key: Option<&'a str>,
}
pub fn finalize_into(
    options: &Options,
    job: &Value,
    cond: &str,
    stem: Option<&str>,
    attempt: u32,
    base: &str,
    destination: ResultDestination<'_>,
) -> Result<Value> {
    for segment in cond.split('/') {
        safe_name(segment)?;
    }
    let generated = format!("ev-{}", job["job_id"].as_str().unwrap_or("result"));
    let stem = stem.unwrap_or(&generated);
    safe_name(stem)?;
    let op = job["request"]["op"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("job lacks op"))?;
    safe_name(op)?;
    ensure!(
        ["done", "failed", "timeout", "canceled"].contains(&job["state"].as_str().unwrap_or("")),
        "job is not terminal"
    );
    let workspace = options.workspace();
    let registry_path = workspace.join("runs/E1/.incumbent-registry.json");
    fs::create_dir_all(registry_path.parent().unwrap())?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(registry_path.with_extension("lock"))?;
    fs2::FileExt::lock_exclusive(&lock)?;
    let mut registry = if registry_path.is_file() {
        load_json(&registry_path)?
    } else {
        json!({})
    };
    let lane = job["request"]["lane"].as_str().unwrap_or("timing");
    let kind = match lane {
        "timing" => "timing-final",
        "func" => "func",
        _ => "correctness",
    };
    let ticket =
        json!({"op":op,"kind":kind,"cond":cond,"attempt":attempt,"backend":options.backend});
    let (outcome, note) =
        rvbench_k3::judge::judge(&ticket, job, &mut registry, base, destination.registry_key);
    let raw = json!({"schema":"raw-v1","stem":stem,"kind":kind,"condition":cond,"attempt":attempt,"op":op,"shape":job["request"]["shape"],"dtype":job["request"]["dtype"],"job_id":job["job_id"],"evald_host":rvbench_k3::profiles::base_label(base),"evald_ts":job["finished_at"],"result":job["result"],"error":job["error"],"duration_s":job["duration_s"],"queue_wait_s":job["queue_wait_s"],"outcome":outcome,"note":note,"writer":"rvbench-rust"});
    let output = destination.output.map(Path::to_owned).unwrap_or_else(|| {
        workspace
            .join("runs/E1")
            .join(cond)
            .join(op)
            .join("results")
    });
    atomic_json(&output.join(format!("{stem}.raw.json")), &raw)?;
    let upgraded = rvbench_core::compat::to_result_v2(
        job,
        &options.backend,
        &rvbench_k3::profiles::base_label(base),
        lane,
        Some(json!({"condition":cond,"attempt":attempt,"outcome":outcome,"note":note})),
    );
    atomic_json(&output.join(format!("{stem}.result-v2.json")), &upgraded)?;
    atomic_json(&registry_path, &registry)?;
    fs2::FileExt::unlock(&lock)?;
    Ok(json!({"outcome":outcome,"note":note,"raw":output.join(format!("{stem}.raw.json"))}))
}
fn ev(options: &Options, action: &Ev) -> Result<()> {
    match action {
        Ev::Run(run) | Ev::Submit(run) => {
            let body = run_body(options, run)?;
            let client = options.client(&run.lane)?;
            let submitted = client.submit(&body, Duration::from_secs(run.budget_s))?;
            let mut result = if matches!(action, Ev::Run(_)) {
                client.wait(
                    submitted["job_id"]
                        .as_str()
                        .ok_or_else(|| anyhow::anyhow!("submission lacks job id"))?,
                    Duration::from_secs(run.budget_s),
                )?
            } else {
                submitted
            };
            if run.judge && matches!(action, Ev::Run(_)) {
                result["judgment"] = finalize(
                    options,
                    &result,
                    &run.cond,
                    run.stem.as_deref(),
                    run.attempt,
                    &client.base,
                )?;
            }
            if let Some(out) = &run.out {
                atomic_json(out, &result)?;
            }
            print(&result);
        }
        Ev::Get { job_id, wait } => {
            ensure!(client::uuid_is_safe(job_id), "invalid job identifier");
            print(&options.client("timing")?.request(
                Method::GET,
                &format!("/jobs/{job_id}?wait={}", wait.min(&60)),
                None,
            )?);
        }
        Ev::Judge {
            job_id,
            cond,
            stem,
            attempt,
        } => {
            ensure!(client::uuid_is_safe(job_id), "invalid job identifier");
            let client = options.client("timing")?;
            let job = client.request(Method::GET, &format!("/jobs/{job_id}"), None)?;
            print(&finalize(
                options,
                &job,
                cond,
                stem.as_deref(),
                *attempt,
                &client.base,
            )?);
        }
        Ev::Status { limit } => {
            let client = options.client("timing")?;
            print(
                &json!({"health":client.request(Method::GET,"/healthz",None)?,"history":client.request(Method::GET,&format!("/jobs?limit={}",limit.clamp(&1,&500)),None)?}),
            );
        }
        Ev::History { limit, since } => {
            let path = if let Some(id) = since {
                ensure!(client::uuid_is_safe(id), "invalid since identifier");
                format!("/jobs?limit={limit}&since={id}")
            } else {
                format!("/jobs?limit={limit}")
            };
            print(
                &options
                    .client("timing")?
                    .request(Method::GET, &path, None)?,
            );
        }
        Ev::Cancel { job_id } => {
            ensure!(client::uuid_is_safe(job_id), "invalid job identifier");
            print(&options.client("timing")?.request(
                Method::DELETE,
                &format!("/jobs/{job_id}"),
                None,
            )?);
        }
    }
    Ok(())
}
fn suite(options: &Options, action: &Suite) -> Result<()> {
    match action {
        Suite::Validate { file } => {
            checked(rvbench_core::suite::validate_suite(
                &load_json(file)?,
                &["k3", "k1", "sg2042", "rvv_general"],
            ))?;
            println!("VALID: suite-v1");
        }
        Suite::Expand {
            file,
            wave,
            cond,
            lane,
            attempt,
            dry_run,
        } => {
            let doc = load_json(file)?;
            let expanded = pipeline::expand(&doc, wave, cond, lane, *attempt, *dry_run)?;
            print(&json!(expanded));
        }
        Suite::FromBaseline { file, name, out } => {
            let doc = load_json(file)?;
            let ops: Vec<_> = doc["jobs"]
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("manifest lacks jobs"))?
                .iter()
                .map(|v| json!({"op":v["op"],"shape":v["shape"],"dtype":v["dtype"]}))
                .collect();
            let mut suite =
                json!({"schema":"suite-v1","name":name,"backend":options.backend,"ops":ops});
            if let Some(title) = doc.get("title") {
                suite["title"] = title.clone();
            }
            checked(rvbench_core::suite::validate_suite(
                &suite,
                &["k3", "k1", "sg2042", "rvv_general"],
            ))?;
            atomic_json(out, &suite)?;
        }
        Suite::Run {
            file,
            lane,
            budget_s,
            out,
        } => {
            let doc = load_json(file)?;
            let tickets = rvbench_core::suite::expand(&doc, "suite", lane, 1)?;
            let client = options.backend_client(lane, doc["backend"].as_str().unwrap())?;
            let mut results = Vec::new();
            for ticket in tickets {
                let body = json!({"op":ticket["op"],"shape":ticket["shape"],"dtype":ticket["dtype"],"lane":lane,"warmup":ticket["warmup"],"rep":ticket["rep"],"timeout_min":ticket["timeout_min"]});
                let submitted = client.submit(&body, Duration::from_secs(*budget_s))?;
                results.push(client.wait(
                    submitted["job_id"].as_str().unwrap(),
                    Duration::from_secs(*budget_s),
                )?);
            }
            let value = json!({"suite":doc["name"],"jobs":results});
            if let Some(out) = out {
                atomic_json(out, &value)?;
            }
            print(&value);
        }
    }
    Ok(())
}
fn execute(options: &Options) -> Result<i32> {
    match &options.command {
        Command::Validate { kind } => return validate(kind),
        Command::Digest { file } => println!("{}", spec_digest(&load_json(file)?)),
        Command::Ev { action } => ev(options, action)?,
        Command::Suite { action } => suite(options, action)?,
        Command::ResultUpgrade { file, out } => {
            let value = rvbench_core::compat::load_result_v2(&load_json(file)?)?;
            if let Some(out) = out {
                atomic_json(out, &value)?;
            } else {
                print(&value);
            }
        }
        Command::Pipeline { once, interval_s } => pipeline::run(options, *once, *interval_s)?,
        Command::OpStatus { ops } => print(&pipeline::status(options, ops)?),
    }
    Ok(0)
}
fn main() {
    let options = Options::parse();
    match execute(&options) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("{error:#}");
            std::process::exit(1);
        }
    }
}
