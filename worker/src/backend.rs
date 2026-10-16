//! Compiler and evaluator are separate backend components; func stops after prepare.
use anyhow::{Result, bail, ensure};
use rvbench_protocol::*;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};

pub const K3_OPS: &[&str] = &[
    "add", "sub", "mul", "div", "abs", "exp", "relu", "sum", "softmax", "mm", "mv", "aminmax",
    "topk", "conv2d",
];
pub const K1_OPS: &[&str] = &["add", "abs"];
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackendConfig {
    pub backend: BackendId,
    #[serde(default = "python_default")]
    pub python: String,
    #[serde(default = "cc_default")]
    pub cc: String,
    #[serde(default)]
    pub environment: std::collections::BTreeMap<String, String>,
    #[serde(default)]
    pub cpus: Option<String>,
    #[serde(default)]
    pub ai_thread: bool,
}
fn python_default() -> String {
    "python3".into()
}
fn cc_default() -> String {
    "gcc".into()
}
pub struct Prepared {
    pub dir: PathBuf,
    pub compilation: PhaseResult,
}
pub trait Compiler {
    fn prepare(&self, request: &JobRequest, dir: &Path) -> Result<Prepared>;
}
pub trait Evaluator {
    fn evaluate(&self, request: &JobRequest, prepared: &Prepared) -> Result<PhaseResult>;
}
pub struct Backend {
    pub config: BackendConfig,
}
impl Backend {
    pub fn supported(&self, r: &JobRequest) -> Result<()> {
        validate_request(r)?;
        ensure!(r.backend == self.config.backend, "backend mismatch");
        let ops = if r.backend == BackendId::K1 {
            K1_OPS
        } else {
            K3_OPS
        };
        ensure!(ops.contains(&r.op.as_str()), "unsupported op {}", r.op);
        ensure!(
            r.backend != BackendId::K1 || r.dtype == JobRequestDtype::Float32,
            "K1 native C supports float32"
        );
        match r.op.as_str() {
            "mm" | "mv" => ensure!(r.shape.len() == 2, "mm/mv require rank 2"),
            "conv2d" => ensure!(r.shape.len() == 4, "conv2d requires NCHW"),
            _ => {}
        }
        Ok(())
    }
    fn command(&self, program: &str, args: &[&str], dir: &Path, timeout: u64) -> Result<String> {
        let mut command = if self.config.ai_thread {
            let mut c = Command::new("bash");
            c.args([
                "-c",
                "echo $$ > /proc/set_ai_thread; exec taskset -c \"$1\" \"${@:2}\"",
                "rvbench",
            ])
            .arg(self.config.cpus.as_deref().unwrap_or("8-15"))
            .arg(program);
            c
        } else if self.config.cpus.is_some() {
            let mut c = Command::new("taskset");
            c.arg("-c")
                .arg(self.config.cpus.as_ref().unwrap())
                .arg(program);
            c
        } else {
            Command::new(program)
        };
        command
            .args(args)
            .current_dir(dir)
            .envs(&self.config.environment);
        if self.config.backend == BackendId::K3 {
            // Compile artifacts belong to this request, including cached JIT objects.
            command.env("TRITON_CACHE_DIR", dir.join("triton-cache"));
        }
        let out = rvbench_runtime::process::run(&mut command, Duration::from_secs(timeout))?;
        std::fs::write(dir.join("last-stdout.log"), &out.stdout)?;
        std::fs::write(dir.join("last-stderr.log"), &out.stderr)?;
        if out.timed_out {
            bail!("process timeout after {timeout}s");
        }
        ensure!(
            out.code == 0,
            "process exit {}: {}",
            out.code,
            out.stderr.chars().take(4000).collect::<String>()
        );
        Ok(out.stdout)
    }
    pub fn probe(&self, dir: &Path) -> Result<Value> {
        std::fs::create_dir_all(dir)?;
        let details = if self.config.backend == BackendId::K1 {
            let version = self.command(&self.config.cc, &["--version"], dir, 20)?;
            json!({"compiler":version.lines().next(),"runtime":"native-c","ops":K1_OPS,"dtypes":["float32"]})
        } else {
            let code = "import torch,triton,flag_gems,json; print(json.dumps(dict(torch=torch.__version__,triton=triton.__version__,flag_gems=str(flag_gems.__file__))))";
            let out = self.command(&self.config.python, &["-c", code], dir, 90)?;
            let mut v: Value = serde_json::from_str(out.lines().last().unwrap_or(""))?;
            v["ops"] = json!(K3_OPS);
            v["dtypes"] = json!(["float32", "float16"]);
            v["unavailable_ops"] = json!({"flash_attention_forward":{"ready":false,"reason":"Full Flash auxiliary-output contract is pending; SDPA is not an equivalent adapter and failed SpacemiT compilation in probed cases."}});
            v
        };
        Ok(details)
    }
}
pub fn artifact(dir: &Path, name: &str) -> Result<Artifact> {
    let data = std::fs::read(dir.join(name))?;
    Ok(Artifact {
        path: name.into(),
        sha256: format!("{:x}", Sha256::digest(&data)),
        size_bytes: data.len() as i64,
    })
}
fn phase(details: Value, artifacts: Vec<Artifact>) -> Result<PhaseResult> {
    Ok(PhaseResult {
        status: PhaseStatus::Passed,
        details: serde_json::from_value(details)?,
        artifacts,
    })
}
impl Compiler for Backend {
    fn prepare(&self, r: &JobRequest, dir: &Path) -> Result<Prepared> {
        let started = Instant::now();
        self.supported(r)?;
        std::fs::create_dir_all(dir)?;
        std::fs::write(dir.join("request.json"), serde_json::to_vec_pretty(r)?)?;
        let mut names = vec!["request.json"];
        if r.backend == BackendId::K3 {
            std::fs::write(dir.join("harness.py"), include_str!("../adapters/k3.py"))?;
            if let Some(source) = &r.source {
                std::fs::write(dir.join("candidate.py"), source)?;
                names.push("candidate.py");
            }
            self.command(
                &self.config.python,
                &[
                    "harness.py",
                    "prepare",
                    if self.config.ai_thread { "ai" } else { "cpu" },
                ],
                dir,
                r.timeout_secs as u64,
            )?;
            names.extend(["harness.py", "prepare.json", "implementation.py"]);
        } else {
            let source=r.source.clone().unwrap_or_else(|| if r.op=="add" { "void candidate(const float*a,const float*b,float*out,int n){for(int i=0;i<n;i++)out[i]=a[i]+b[i];}".into() } else { "#include <math.h>\nvoid candidate(const float*a,const float*b,float*out,int n){(void)b;for(int i=0;i<n;i++)out[i]=fabsf(a[i]);}".into() });
            std::fs::write(dir.join("candidate.c"), source)?;
            std::fs::write(dir.join("harness.c"), include_str!("../adapters/k1.c"))?;
            self.command(
                &self.config.cc,
                &[
                    "-O3",
                    "-fno-fast-math",
                    "harness.c",
                    "candidate.c",
                    "-lm",
                    "-o",
                    "candidate",
                ],
                dir,
                r.timeout_secs as u64,
            )?;
            let n = r.shape.iter().product::<i64>().to_string();
            self.command(
                "./candidate",
                &[&r.op, &n, "func", "0", "1", &r.seed.to_string()],
                dir,
                (r.timeout_secs as u64).saturating_sub(started.elapsed().as_secs()),
            )?;
            names.extend(["candidate.c", "harness.c", "candidate"]);
        }
        let mut artifacts = names
            .into_iter()
            .map(|name| artifact(dir, name))
            .collect::<Result<Vec<_>>>()?;
        if r.backend == BackendId::K3 {
            for entry in walkdir::WalkDir::new(dir.join("triton-cache")).follow_links(false) {
                let entry = entry?;
                if entry.file_type().is_file() {
                    let relative = entry
                        .path()
                        .strip_prefix(dir)?
                        .to_string_lossy()
                        .replace('\\', "/");
                    artifacts.push(artifact(dir, &relative)?);
                }
            }
        }
        std::fs::write(
            dir.join("compile-manifest.json"),
            serde_json::to_vec_pretty(&artifacts)?,
        )?;
        artifacts.push(artifact(dir, "compile-manifest.json")?);
        let compilation = phase(
            json!({"compiler":if r.backend==BackendId::K3 {"torch/flag_gems-jit"} else {"native-c"},"request_sha256":digest(r),"source_sha256":digest(&r.source),"smoke_invocation":true}),
            artifacts,
        )?;
        Ok(Prepared {
            dir: dir.into(),
            compilation,
        })
    }
}
impl Evaluator for Backend {
    fn evaluate(&self, r: &JobRequest, p: &Prepared) -> Result<PhaseResult> {
        let out = if r.backend == BackendId::K3 {
            self.command(
                &self.config.python,
                &[
                    "harness.py",
                    "evaluate",
                    if self.config.ai_thread { "ai" } else { "cpu" },
                ],
                &p.dir,
                r.timeout_secs as u64,
            )?
        } else {
            self.command(
                "./candidate",
                &[
                    &r.op,
                    &r.shape.iter().product::<i64>().to_string(),
                    r.kind.as_str(),
                    &r.warmup.to_string(),
                    &r.repetitions.to_string(),
                    &r.seed.to_string(),
                ],
                &p.dir,
                r.timeout_secs as u64,
            )?
        };
        let details: Value = serde_json::from_str(out.lines().last().unwrap_or(""))?;
        std::fs::write(
            p.dir.join("evaluation.json"),
            serde_json::to_vec_pretty(&details)?,
        )?;
        let mut result = phase(details.clone(), vec![artifact(&p.dir, "evaluation.json")?])?;
        if details["correct"].as_bool() == Some(false) {
            result.status = PhaseStatus::Failed;
        }
        Ok(result)
    }
}
