use super::config::K3Config;
use anyhow::{Result, ensure};
use rvbench_core::parse_json;
use rvbench_runtime::{Execution, Executor, WorkerSpec, process};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::Duration,
};

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}
pub fn map_rows(request: &Value, rows: &[Value]) -> (Value, Value) {
    if request["bench"] == "gemm" {
        let mut result = if rows.len() == 1 {
            rows[0].clone()
        } else {
            json!({"metric":"gflops","rows":rows})
        };
        if result.get("metric").is_none() {
            result["metric"] = json!("gflops");
        }
        let ok = rows.iter().all(|v| v["correct"] == true);
        return (
            result,
            json!({"speedup":rows[0]["speedup"],"pass_rate":if ok{1.0}else{0.0}}),
        );
    }
    if let Some(shapes) = request["shape"].as_array().filter(|s| s.len() > 1) {
        let cases: Vec<_> = rows
            .iter()
            .enumerate()
            .map(|(i, row)| {
                let mut row = row.clone();
                if let Some(shape) = shapes.get(i) {
                    row["shape"] = shape.clone();
                }
                row
            })
            .collect();
        let rate = if cases.is_empty() {
            0.0
        } else {
            cases.iter().filter(|v| v["correct"] == true).count() as f64 / cases.len() as f64
        };
        let mut result = json!({"cases":cases,"shapes":shapes,"cases_expected":shapes.len(),"cases_returned":rows.len()});
        if rows.len() != shapes.len() {
            result["partial"] = json!(true);
        }
        return (result, json!({"pass_rate":rate}));
    }
    let last = rows.last().unwrap();
    (
        last.clone(),
        json!({"speedup":last["speedup"],"pass_rate":if last["correct"]==true{1.0}else{0.0}}),
    )
}
pub fn json_rows(output: &str) -> Vec<Value> {
    let mut rows: Vec<_> = output
        .lines()
        .rev()
        .filter_map(|line| parse_json(line.trim()).ok().filter(Value::is_object))
        .take(16)
        .collect();
    rows.reverse();
    rows
}

pub struct K3Executor {
    cfg: K3Config,
    worker: WorkerSpec,
    warm: Option<WarmBridge>,
}
impl K3Executor {
    pub fn new(cfg: K3Config, worker: WorkerSpec) -> Self {
        Self {
            cfg,
            worker,
            warm: None,
        }
    }
    fn environment(&self, command: &mut Command) {
        if self.worker.lane != "timing" {
            command.env("K3_LANE", &self.worker.lane);
        }
        if let Some(threads) = self.worker.omp_threads {
            command.env("OMP_NUM_THREADS", threads.to_string());
        }
    }
    fn bash(&self, text: &str) -> Command {
        #[cfg(unix)]
        let mut command = if let Some(cpus) = &self.worker.cpus {
            let mut cmd = Command::new("taskset");
            cmd.args(["-c", cpus, "bash"]);
            cmd
        } else {
            Command::new("bash")
        };
        #[cfg(not(unix))]
        let mut command = Command::new("bash");
        command.args(["-c", text]);
        self.environment(&mut command);
        command
    }
    fn benchmark(&self, request: &Value) -> Result<Command> {
        if let Some(argv) = self.cfg.value.get("bench_argv_override") {
            let argv = argv
                .as_array()
                .filter(|v| !v.is_empty())
                .ok_or_else(|| anyhow::anyhow!("invalid bench_argv_override"))?;
            let args = argv
                .iter()
                .map(|v| {
                    v.as_str()
                        .ok_or_else(|| anyhow::anyhow!("invalid bench argv"))
                })
                .collect::<Result<Vec<_>>>()?;
            let mut command = Command::new(args[0]);
            command.args(&args[1..]);
            self.environment(&mut command);
            return Ok(command);
        }
        if let Some(command) = self
            .cfg
            .value
            .get("bench_command_override")
            .and_then(Value::as_str)
        {
            return Ok(self.bash(command));
        }
        let cfg = &self.cfg.value;
        let scripts = cfg["scripts_root"].as_str().unwrap();
        let mut calls = Vec::new();
        let mut exports = Vec::new();
        if self.worker.lane != "timing" {
            exports.push(format!("export K3_LANE={}", shell_quote(&self.worker.lane)));
        }
        if let Some(omp) = self.worker.omp_threads {
            exports.push(format!("export OMP_NUM_THREADS={omp}"));
        }
        if request["bench"] == "gemm" {
            let mut args = vec![
                "python".into(),
                shell_quote(&format!("{scripts}/bench_gemm.py")),
                "--op".into(),
                shell_quote(request["op"].as_str().unwrap()),
                "--dtype".into(),
                shell_quote(request["dtype"].as_str().unwrap()),
                "--shapes".into(),
                shell_quote(request["shape"].as_str().unwrap()),
            ];
            if request["op"] == "bmm" {
                args.extend(["--batch".into(), request["batch"].to_string()]);
            }
            calls.push(args.join(" "));
        } else {
            let shapes = request["shape"]
                .as_array()
                .cloned()
                .unwrap_or(vec![request["shape"].clone()]);
            for shape in shapes {
                let mut args = vec![
                    "python".into(),
                    shell_quote(&format!("{scripts}/bench_op.py")),
                    "--op".into(),
                    shell_quote(request["op"].as_str().unwrap()),
                    "--shape".into(),
                    shell_quote(shape.as_str().unwrap()),
                    "--dtype".into(),
                    shell_quote(request["dtype"].as_str().unwrap()),
                    "--warmup".into(),
                    request["warmup"].to_string(),
                    "--rep".into(),
                    request["rep"].to_string(),
                ];
                if request["skip_torch"] == true {
                    args.push("--skip-torch".into());
                }
                if request.get("seed").is_some() {
                    args.extend(["--seed".into(), request["seed"].to_string()]);
                }
                calls.push(args.join(" "));
            }
        }
        let mut parts = vec![format!(
            "source {}",
            shell_quote(cfg["run_env"].as_str().unwrap())
        )];
        parts.extend(exports);
        parts.extend(calls);
        Ok(self.bash(&parts.join(" && ")))
    }
    fn subprocess(&self, request: &Value, timeout: Duration) -> Result<Execution> {
        let output = process::run(&mut self.benchmark(request)?, timeout)?;
        let mut execution = Execution::failed(if output.timed_out {
            "benchmark timeout".into()
        } else {
            format!("bench rc={}", output.code)
        });
        execution.stdout = output.stdout;
        execution.stderr = output.stderr;
        execution.exit_code = Some(output.code);
        if output.timed_out {
            execution.state = "timeout".into();
            return Ok(execution);
        }
        if output.code != 0 {
            return Ok(execution);
        }
        let rows = json_rows(&execution.stdout);
        if rows.is_empty() {
            execution.error = Some("no JSON line in stdout".into());
            return Ok(execution);
        }
        let (result, kope) = map_rows(request, &rows);
        execution.state = "done".into();
        execution.result = result;
        execution.kope_mem = kope;
        execution.error = None;
        Ok(execution)
    }
    fn warmfork(&mut self, id: &str, request: &Value, timeout: Duration) -> Result<Execution> {
        if self
            .warm
            .as_mut()
            .is_some_and(|warm| warm.child.try_wait().ok().flatten().is_some())
        {
            self.warm = None;
        }
        if self.warm.is_none() {
            let cfg = &self.cfg.value;
            let text = format!(
                "source {} && exec python {}",
                shell_quote(cfg["run_env"].as_str().unwrap()),
                shell_quote(cfg["warmd_path"].as_str().unwrap())
            );
            let mut command = self.bash(&text);
            command.env("WARMD_SCRIPTS_ROOT", cfg["scripts_root"].as_str().unwrap());
            if let Some(env) = cfg["warmd_env"].as_object() {
                for (key, value) in env {
                    command.env(
                        key,
                        value
                            .as_str()
                            .ok_or_else(|| anyhow::anyhow!("warmd_env values must be strings"))?,
                    );
                }
            }
            if cfg["warmd_output_selfcheck"] == true {
                command.env("WARMD_OUTPUT_SELFCHECK", "1");
            }
            self.warm = Some(WarmBridge::spawn(command)?);
        }
        let mut req = json!({});
        for key in [
            "bench",
            "op",
            "shape",
            "dtype",
            "warmup",
            "rep",
            "batch",
            "skip_torch",
            "seed",
        ] {
            if let Some(v) = request.get(key) {
                req[key] = v.clone();
            }
        }
        let bridge = self.warm.as_mut().unwrap();
        let response = bridge.send(
            &json!({"id":id,"timeout_s":timeout.as_secs_f64(),"req":req}),
            timeout + Duration::from_secs(30),
        )?;
        ensure!(response["id"] == id, "warmd response id mismatch");
        let state = response["state"]
            .as_str()
            .filter(|v| ["done", "failed", "timeout"].contains(v))
            .ok_or_else(|| anyhow::anyhow!("invalid warmd state"))?;
        let mut execution = Execution::failed(
            response["error"]
                .as_str()
                .unwrap_or("warmd execution failed"),
        );
        execution.state = state.into();
        execution.stdout = response["stdout"].as_str().unwrap_or("").into();
        execution.stderr = response["stderr"].as_str().unwrap_or("").into();
        execution.exit_code = response["rc"].as_i64().map(|n| n as i32);
        if state == "done" {
            let rows = response["result"]
                .as_array()
                .filter(|v| !v.is_empty())
                .ok_or_else(|| anyhow::anyhow!("warmd done but empty rows"))?;
            ensure!(
                rows.iter().all(Value::is_object),
                "invalid warmd result rows"
            );
            let (result, kope) = map_rows(request, rows);
            execution.result = result;
            execution.kope_mem = kope;
            execution.error = None;
        }
        Ok(execution)
    }
}
impl Executor for K3Executor {
    fn execute(&mut self, id: &str, request: &Value, timeout: Duration) -> Result<Execution> {
        let chosen = request["executor"]
            .as_str()
            .unwrap_or(&self.worker.executor);
        if chosen == "warmfork" {
            match self.warmfork(id, request, timeout) {
                Ok(v) => Ok(v),
                Err(error) => {
                    self.warm = None;
                    let mut result = self.subprocess(request, timeout)?;
                    result.fallback = true;
                    result.stderr = format!("[warmd fallback: {error}]\n{}", result.stderr);
                    Ok(result)
                }
            }
        } else {
            self.subprocess(request, timeout)
        }
    }
}
struct WarmBridge {
    child: Child,
    input: ChildStdin,
    lines: mpsc::Receiver<String>,
    _errors: Arc<Mutex<VecDeque<String>>>,
}
impl WarmBridge {
    fn spawn(mut command: Command) -> Result<Self> {
        process::isolate(&mut command);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn()?;
        let input = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (sender, lines) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if sender.send(line).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        let errors = Arc::new(Mutex::new(VecDeque::new()));
        let drain = errors.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr)
                .lines()
                .map_while(std::result::Result::ok)
            {
                let mut buffer = drain.lock().unwrap();
                buffer.push_back(line);
                if buffer.len() > 200 {
                    buffer.pop_front();
                }
            }
        });
        let bridge = Self {
            child,
            input,
            lines,
            _errors: errors,
        };
        let ready = parse_json(&bridge.lines.recv_timeout(Duration::from_secs(120))?)?;
        ensure!(ready["ready"] == true, "warmd did not become ready");
        Ok(bridge)
    }
    fn send(&mut self, request: &Value, timeout: Duration) -> Result<Value> {
        self.input.write_all(&serde_json::to_vec(request)?)?;
        self.input.write_all(b"\n")?;
        self.input.flush()?;
        parse_json(&self.lines.recv_timeout(timeout)?)
    }
}
impl Drop for WarmBridge {
    fn drop(&mut self) {
        process::kill_tree(&mut self.child);
    }
}
