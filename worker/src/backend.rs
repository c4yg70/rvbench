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

pub const TRITON_OPS: &[&str] = &[
    "add", "sub", "mul", "div", "abs", "exp", "relu", "sum", "softmax", "mm", "mv", "aminmax",
    "topk", "conv2d",
];
pub const NATIVE_C_OPS: &[&str] = &["add", "abs"];
const NATIVE_BINARY: &str = if cfg!(windows) {
    "candidate.exe"
} else {
    "candidate"
};
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
        let ops = if implementation(r) == KernelImplementation::NativeC {
            NATIVE_C_OPS
        } else {
            TRITON_OPS
        };
        ensure!(ops.contains(&r.op.as_str()), "unsupported op {}", r.op);
        ensure!(
            implementation(r) != KernelImplementation::NativeC
                || r.dtype == JobRequestDtype::Float32,
            "native C supports float32"
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
        // Compile artifacts belong to this request on either hardware backend.
        command.env("TRITON_CACHE_DIR", dir.join("triton-cache"));
        let out = crate::process::run(&mut command, Duration::from_secs(timeout))?;
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
        let mut modes = json!({});
        for mode in [KernelImplementation::NativeC, KernelImplementation::Triton] {
            // Never reuse a candidate or JIT cache left by a previous probe.
            let probe_dir = dir.join(mode.as_str()).join(id());
            std::fs::create_dir_all(&probe_dir)?;
            let result = (|| -> Result<Value> {
                let mut details = if mode == KernelImplementation::NativeC {
                    let version = self.command(&self.config.cc, &["--version"], &probe_dir, 20)?;
                    json!({"compiler":version.lines().next(),"compiler_flags":"-O3 -fno-fast-math","ops":NATIVE_C_OPS,"dtypes":["float32"]})
                } else {
                    let code = "import torch,triton,flag_gems,json; print(json.dumps(dict(torch=torch.__version__,triton=triton.__version__,flag_gems=str(flag_gems.__file__))))";
                    let out = self.command(&self.config.python, &["-c", code], &probe_dir, 90)?;
                    let mut v: Value = serde_json::from_str(out.lines().last().unwrap_or(""))?;
                    v["ops"] = json!(TRITON_OPS);
                    v["dtypes"] = json!(["float32", "float16"]);
                    v["compiler_flags"] = json!("torch/flag_gems-jit");
                    v["unavailable_ops"] = json!({"flash_attention_forward":{"ready":false,"reason":"Full Flash auxiliary-output contract is pending; SDPA is not an equivalent adapter and failed SpacemiT compilation in probed cases."}});
                    v
                };
                let mut request: JobRequest =
                    serde_json::from_str(include_str!("../../contract/examples/job.json"))?;
                request.backend = self.config.backend.clone();
                request.implementation = Some(mode.clone());
                request.kind = JobKind::Func;
                request.shape = vec![128];
                request.timeout_secs = 120;
                let prepared = self.prepare(&request, &probe_dir)?;
                details["probe_compilation"] = serde_json::to_value(prepared.compilation)?;
                details["ready"] = json!(true);
                Ok(details)
            })();
            modes[mode.as_str()] = match result {
                Ok(details) => details,
                Err(error) => json!({"ready":false,"error":format!("{error:#}")}),
            };
        }
        ensure!(
            modes
                .as_object()
                .unwrap()
                .values()
                .any(|v| v["ready"] == true),
            "no usable operator implementation: {modes}"
        );
        let default = default_implementation(&self.config.backend);
        // Legacy schedulers see only the original runtime's capabilities.
        let mut details = modes[default.as_str()].clone();
        if details["ready"] != true {
            details["ops"] = json!([]);
            details["dtypes"] = json!([]);
        }
        details["default_implementation"] = json!(default);
        details["implementations"] = modes;
        details["execution_cpus"] = json!(self.config.cpus);
        details["ai_thread"] = json!(self.config.ai_thread);
        details["host_kernel"] = json!(
            std::fs::read_to_string("/proc/sys/kernel/osrelease")
                .unwrap_or_default()
                .trim()
        );
        details["architecture"] = json!(std::env::consts::ARCH);
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
        if implementation(r) == KernelImplementation::Triton {
            std::fs::write(
                dir.join("harness.py"),
                include_str!("../adapters/triton.py"),
            )?;
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
            std::fs::write(dir.join("harness.c"), include_str!("../adapters/native.c"))?;
            self.command(
                &self.config.cc,
                &[
                    "-O3",
                    "-fno-fast-math",
                    "harness.c",
                    "candidate.c",
                    "-lm",
                    "-o",
                    NATIVE_BINARY,
                ],
                dir,
                r.timeout_secs as u64,
            )?;
            let n = r.shape.iter().product::<i64>().to_string();
            self.command(
                &dir.join(NATIVE_BINARY).canonicalize()?.to_string_lossy(),
                &[&r.op, &n, "func", "0", "1", &r.seed.to_string()],
                dir,
                (r.timeout_secs as u64).saturating_sub(started.elapsed().as_secs()),
            )?;
            names.extend(["candidate.c", "harness.c", NATIVE_BINARY]);
        }
        let mut artifacts = names
            .into_iter()
            .map(|name| artifact(dir, name))
            .collect::<Result<Vec<_>>>()?;
        if implementation(r) == KernelImplementation::Triton {
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
            json!({"implementation":implementation(r),"compiler":if implementation(r)==KernelImplementation::Triton {"torch/flag_gems-jit"} else {"native-c"},"request_sha256":digest(r),"source_sha256":digest(&r.source),"smoke_invocation":true}),
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
        let out = if implementation(r) == KernelImplementation::Triton {
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
                &p.dir.join(NATIVE_BINARY).canonicalize()?.to_string_lossy(),
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

#[cfg(test)]
mod tests {
    use super::*;

    fn backend(hardware: BackendId) -> Backend {
        Backend {
            config: BackendConfig {
                backend: hardware,
                python: "rvbench-missing-python".into(),
                cc: std::env::var("CC").unwrap_or_else(|_| "gcc".into()),
                environment: Default::default(),
                cpus: None,
                ai_thread: false,
            },
        }
    }
    fn request(hardware: BackendId, mode: KernelImplementation) -> JobRequest {
        let mut r: JobRequest =
            serde_json::from_str(include_str!("../../contract/examples/job.json")).unwrap();
        r.backend = hardware;
        r.implementation = Some(mode);
        r.shape = vec![2, 17];
        r.repetitions = 3;
        r
    }
    #[test]
    fn operation_and_dtype_support_depends_on_implementation() {
        for hardware in [BackendId::K1, BackendId::K3] {
            let b = backend(hardware.clone());
            let mut c = request(hardware.clone(), KernelImplementation::NativeC);
            b.supported(&c).unwrap();
            c.dtype = JobRequestDtype::Float16;
            assert!(b.supported(&c).is_err());
            c.dtype = JobRequestDtype::Float32;
            c.op = "softmax".into();
            assert!(b.supported(&c).is_err());
            let mut t = request(hardware, KernelImplementation::Triton);
            t.dtype = JobRequestDtype::Float16;
            for op in TRITON_OPS {
                t.op = (*op).into();
                t.shape = if *op == "conv2d" {
                    vec![1, 2, 4, 4]
                } else {
                    vec![2, 17]
                };
                b.supported(&t).unwrap();
            }
        }
    }
    #[test]
    fn native_c_compilation_correctness_and_timing_on_both_backends() {
        for hardware in [BackendId::K1, BackendId::K3] {
            let b = backend(hardware.clone());
            for op in NATIVE_C_OPS {
                for kind in [JobKind::Func, JobKind::Correctness, JobKind::Performance] {
                    let dir = tempfile::tempdir().unwrap();
                    let mut r = request(hardware.clone(), KernelImplementation::NativeC);
                    r.op = (*op).into();
                    r.kind = kind.clone();
                    let p = b.prepare(&r, dir.path()).unwrap();
                    assert_eq!(p.compilation.status, PhaseStatus::Passed);
                    assert_eq!(json!(p.compilation.details)["implementation"], "native-c");
                    assert!(
                        p.compilation
                            .artifacts
                            .iter()
                            .any(|a| a.path == NATIVE_BINARY)
                    );
                    assert!(!dir.path().join("harness.py").exists());
                    if kind == JobKind::Func {
                        continue;
                    }
                    let e = b.evaluate(&r, &p).unwrap();
                    assert_eq!(e.status, PhaseStatus::Passed);
                    let details = json!(e.details);
                    assert_eq!(details["correct"], true);
                    if kind == JobKind::Performance {
                        assert_eq!(details["samples_ms"].as_array().unwrap().len(), 3);
                        assert_eq!(details["compilation_excluded"], true);
                    } else {
                        assert!(details.get("samples_ms").is_none());
                    }
                }
            }
            let dir = tempfile::tempdir().unwrap();
            let mut r = request(hardware, KernelImplementation::NativeC);
            r.kind = JobKind::Performance;
            r.source = Some("void candidate(const float*a,const float*b,float*out,int n){for(int i=0;i<n;i++)out[i]=0;}".into());
            let p = b.prepare(&r, dir.path()).unwrap();
            let e = b.evaluate(&r, &p).unwrap();
            assert_eq!(e.status, PhaseStatus::Failed);
            assert!(json!(e.details).get("samples_ms").is_none());
            r.source = Some("invalid C".into());
            assert!(b.prepare(&r, tempfile::tempdir().unwrap().path()).is_err());
        }
    }
    #[test]
    fn triton_routes_to_python_and_preserves_candidate_on_both_backends() {
        for hardware in [BackendId::K1, BackendId::K3] {
            let b = backend(hardware.clone());
            let mut r = request(hardware, KernelImplementation::Triton);
            r.source = Some("def candidate(a,b): return a+b".into());
            let dir = tempfile::tempdir().unwrap();
            assert!(b.prepare(&r, dir.path()).is_err()); // Python is intentionally missing.
            assert!(dir.path().join("harness.py").is_file());
            assert!(dir.path().join("candidate.py").is_file());
            assert!(!dir.path().join("harness.c").exists());
        }
    }
    #[test]
    fn probes_are_independent_and_do_not_advertise_missing_triton() {
        for hardware in [BackendId::K1, BackendId::K3] {
            let mut b = backend(hardware.clone());
            let dir = tempfile::tempdir().unwrap();
            let env = b.probe(dir.path()).unwrap();
            assert_eq!(env["implementations"]["native-c"]["ready"], true);
            assert_eq!(env["implementations"]["triton"]["ready"], false);
            if hardware == BackendId::K3 {
                assert_eq!(env["ops"], json!([]));
            }
            b.config.cc = "rvbench-missing-cc".into();
            assert!(b.probe(dir.path()).is_err());
        }
    }

    #[test]
    #[ignore = "requires a board Python with Torch/Triton/FlagGems; set RVBENCH_TEST_PYTHON"]
    fn real_triton_correctness_and_performance_on_both_backends() {
        for hardware in [BackendId::K1, BackendId::K3] {
            let mut b = backend(hardware.clone());
            b.config.python =
                std::env::var("RVBENCH_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
            for kind in [JobKind::Func, JobKind::Correctness, JobKind::Performance] {
                let dir = tempfile::tempdir().unwrap();
                let mut r = request(hardware.clone(), KernelImplementation::Triton);
                r.kind = kind.clone();
                let p = b.prepare(&r, dir.path()).unwrap();
                assert_eq!(p.compilation.status, PhaseStatus::Passed);
                assert!(
                    p.compilation
                        .artifacts
                        .iter()
                        .any(|a| a.path.starts_with("triton-cache/"))
                );
                if kind != JobKind::Func {
                    let e = b.evaluate(&r, &p).unwrap();
                    assert_eq!(e.status, PhaseStatus::Passed);
                    assert_eq!(json!(e.details)["correct"], true);
                }
            }
        }
    }
}
