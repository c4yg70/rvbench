use anyhow::{Result, ensure};
use rvbench_core::{load_json, parse_json};
use rvbench_runtime::{Settings, WorkerSpec};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};

#[derive(Clone)]
pub struct K3Config {
    pub value: Value,
    pub settings: Settings,
    pub workers: Vec<WorkerSpec>,
}
pub const LANES: [(&str, &str); 3] = [("timing", "perf"), ("correctness", "acc"), ("func", "func")];
fn integer(v: &Value, min: u64, name: &str) -> Result<u64> {
    let n = v
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("{name} must be an integer"))?;
    ensure!(n >= min && n <= 1000000, "{name} is out of range");
    Ok(n)
}
pub fn cpu_ids(v: &Value) -> Result<Vec<u32>> {
    let mut ids = BTreeSet::new();
    if let Some(list) = v.as_array() {
        for n in list {
            ids.insert(u32::try_from(
                n.as_u64()
                    .ok_or_else(|| anyhow::anyhow!("invalid CPU id"))?,
            )?);
        }
    } else if let Some(text) = v.as_str() {
        for part in text.split(',') {
            if let Some((a, b)) = part.trim().split_once('-') {
                let a: u32 = a.parse()?;
                let b: u32 = b.parse()?;
                ensure!(a <= b && b - a < 65536, "invalid CPU range");
                ids.extend(a..=b);
            } else {
                ids.insert(part.trim().parse()?);
            }
        }
    } else {
        anyhow::bail!("CPU ids must be a string or list");
    }
    ensure!(!ids.is_empty(), "empty CPU set");
    Ok(ids.into_iter().collect())
}
pub fn cpu_string(ids: &[u32]) -> String {
    let mut out = Vec::new();
    let mut index = 0;
    while index < ids.len() {
        let start = ids[index];
        let mut end = start;
        index += 1;
        while index < ids.len() && ids[index] == end + 1 {
            end = ids[index];
            index += 1;
        }
        out.push(if start == end {
            start.to_string()
        } else {
            format!("{start}-{end}")
        });
    }
    out.join(",")
}
fn known_keys(value: &Value, keys: &[&str], name: &str) -> Result<()> {
    ensure!(value.is_object(), "{name} must be an object");
    ensure!(
        value
            .as_object()
            .unwrap()
            .keys()
            .all(|k| keys.contains(&k.as_str())),
        "unknown {name} key"
    );
    Ok(())
}
fn default(v: &mut Value, key: &str, value: Value) {
    if v.get(key).is_none() {
        v[key] = value;
    }
}

impl K3Config {
    pub fn load(path: &Path) -> Result<Self> {
        Self::from_value(load_json(path)?, path.parent().unwrap_or(Path::new(".")))
    }
    pub fn from_value(mut cfg: Value, base: &Path) -> Result<Self> {
        ensure!(cfg.is_object(), "config must be an object");
        for name in ["token", "flag_gems_root", "scripts_root", "run_env"] {
            ensure!(
                cfg[name].as_str().is_some_and(|s| !s.is_empty()),
                "missing config field {name}"
            );
        }
        let defaults = json!({"host":"0.0.0.0","port":8788,"data_dir":base,"lock_file":"/tmp/k3_bench.lock","path_lock_dir":"/tmp/evald-pathlocks","queue_max":64,"timeout_default_min":15,"timeout_max_min":30,"body_max_bytes":8388608,"history_mem":1000,"longpoll_max_s":60,"executor":"warmfork","warmd_env":{},"warmd_output_selfcheck":false,"require_verified_base":false,"correctness_workers":3,"correctness_cpus":"4-6","correctness_omp_threads":1,"correctness_executor":null});
        for (key, value) in defaults.as_object().unwrap() {
            default(&mut cfg, key, value.clone());
        }
        let data = PathBuf::from(
            cfg["data_dir"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("invalid data_dir"))?,
        );
        default(&mut cfg, "warmd_path", json!(data.join("warmd.py")));
        for key in ["warmd_output_selfcheck", "require_verified_base"] {
            ensure!(cfg[key].is_boolean(), "{key} must be boolean");
        }
        ensure!(
            ["subprocess", "warmfork"].contains(&cfg["executor"].as_str().unwrap_or("")),
            "invalid executor"
        );
        let max_timeout = cfg["timeout_max_min"]
            .as_f64()
            .filter(|n| n.is_finite() && *n >= 0.1)
            .ok_or_else(|| anyhow::anyhow!("invalid timeout_max_min"))?;
        let legacy_acc = cfg["correctness_workers"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("invalid correctness_workers"))?;
        default(
            &mut cfg,
            "evaluators",
            json!({"perf":{"enabled":true},"acc":{"enabled":legacy_acc>0},"func":{"enabled":false}}),
        );
        known_keys(&cfg["evaluators"], &["perf", "acc", "func"], "evaluators")?;
        let mut normalized = json!({});
        for name in ["perf", "acc", "func"] {
            let mut sec = cfg["evaluators"]
                .get(name)
                .filter(|v| !v.is_null())
                .cloned()
                .unwrap_or(json!({}));
            known_keys(
                &sec,
                &[
                    "enabled",
                    "warmup_times",
                    "repeat_times",
                    "workers",
                    "cpus",
                    "omp_threads",
                    "executor",
                    "timeout_min",
                    "split_cpus",
                ],
                "evaluator",
            )?;
            default(
                &mut sec,
                "enabled",
                json!(name == "perf" || (name == "acc" && legacy_acc > 0)),
            );
            ensure!(sec["enabled"].is_boolean(), "enabled must be boolean");
            default(
                &mut sec,
                "warmup_times",
                json!(if name == "perf" { 25 } else { 0 }),
            );
            default(
                &mut sec,
                "repeat_times",
                json!(if name == "perf" { 100 } else { 1 }),
            );
            for (key, min) in [("warmup_times", 0), ("repeat_times", 1)] {
                ensure!(
                    integer(&sec[key], min, key)? <= 10000,
                    "invalid timing rounds"
                );
            }
            if name != "perf" {
                ensure!(
                    sec["warmup_times"] == 0 && sec["repeat_times"] == 1,
                    "acc/func rounds must be 0/1"
                );
            }
            default(
                &mut sec,
                "workers",
                json!(if name == "perf" {
                    1
                } else if name == "acc" {
                    legacy_acc
                } else {
                    2
                }),
            );
            let cpus = if name == "perf" {
                Value::Null
            } else if name == "acc" {
                cfg["correctness_cpus"].clone()
            } else {
                normalized["acc"]["cpus"].clone()
            };
            default(&mut sec, "cpus", cpus);
            let omp = if name == "perf" {
                Value::Null
            } else if name == "acc" {
                cfg["correctness_omp_threads"].clone()
            } else {
                normalized["acc"]["omp_threads"].clone()
            };
            default(&mut sec, "omp_threads", omp);
            if sec["enabled"] == true {
                integer(&sec["workers"], 1, "workers")?;
                if name != "perf" {
                    cpu_ids(&sec["cpus"])?;
                }
            }
            if !sec["cpus"].is_null() {
                cpu_ids(&sec["cpus"])?;
            }
            if !sec["omp_threads"].is_null() {
                integer(&sec["omp_threads"], 1, "omp_threads")?;
            }
            if let Some(executor) = sec.get("executor") {
                ensure!(
                    ["subprocess", "warmfork"].contains(&executor.as_str().unwrap_or("")),
                    "invalid lane executor"
                );
            }
            if let Some(timeout) = sec.get("timeout_min") {
                ensure!(
                    timeout
                        .as_f64()
                        .is_some_and(|n| n >= 0.1 && n <= max_timeout),
                    "invalid lane timeout"
                );
            }
            if let Some(split) = sec.get("split_cpus") {
                ensure!(split.is_boolean(), "split_cpus must be boolean");
            }
            normalized[name] = sec;
        }
        ensure!(
            ["perf", "acc", "func"]
                .iter()
                .any(|n| normalized[*n]["enabled"] == true),
            "at least one evaluator must be enabled"
        );
        if let Some(force) = cfg.get("force_lane").filter(|v| !v.is_null()) {
            let name = LANES
                .iter()
                .find(|(lane, _)| Some(*lane) == force.as_str())
                .map(|(_, n)| *n)
                .ok_or_else(|| anyhow::anyhow!("invalid force_lane"))?;
            ensure!(
                normalized[name]["enabled"] == true,
                "force_lane evaluator is disabled"
            );
        }
        let mut limits = cfg.get("lane_queue_max").cloned().unwrap_or(json!({}));
        for (lane, _) in LANES {
            default(&mut limits, lane, Value::Null);
        }
        if let Some(raw) = cfg.get("perf").filter(|v| !v.is_null()) {
            let overrides = if let Some(text) = raw.as_str() {
                parse_json(text)?
            } else {
                raw.clone()
            };
            known_keys(&overrides, &["perf", "acc", "func"], "perf")?;
            for (lane, name) in LANES {
                let Some(sec) = overrides.get(name).filter(|v| !v.is_null()) else {
                    continue;
                };
                known_keys(
                    sec,
                    &[
                        "cores",
                        "cpu_ids",
                        "max_concurrency",
                        "max_queue",
                        "omp_threads",
                        "executor",
                        "timeout_min",
                        "split_cpus",
                    ],
                    "perf section",
                )?;
                let target = &mut normalized[name];
                if let Some(ids) = sec.get("cpu_ids").filter(|v| !v.is_null()) {
                    let ids = cpu_ids(ids)?;
                    if let Some(cores) = sec.get("cores").filter(|v| !v.is_null()) {
                        ensure!(
                            integer(cores, 1, "cores")? == ids.len() as u64,
                            "cores/cpu_ids mismatch"
                        );
                    }
                    target["cpus"] = json!(cpu_string(&ids));
                } else if let Some(cores) = sec.get("cores").filter(|v| !v.is_null()) {
                    let n = integer(cores, 1, "cores")? as usize;
                    let pool = cpu_ids(&target["cpus"])?;
                    ensure!(n <= pool.len(), "cores exceed existing CPU pool");
                    target["cpus"] = json!(cpu_string(&pool[..n]));
                }
                for (from, to) in [
                    ("max_concurrency", "workers"),
                    ("omp_threads", "omp_threads"),
                ] {
                    if let Some(v) = sec.get(from) {
                        integer(v, 1, from)?;
                        target[to] = v.clone();
                    }
                }
                if let Some(v) = sec.get("split_cpus") {
                    ensure!(v.is_boolean(), "split_cpus must be boolean");
                    target["split_cpus"] = v.clone();
                }
                if let Some(v) = sec.get("executor") {
                    ensure!(
                        ["warmfork", "subprocess"].contains(&v.as_str().unwrap_or("")),
                        "invalid executor"
                    );
                    target["executor"] = v.clone();
                }
                if let Some(v) = sec.get("timeout_min") {
                    ensure!(
                        v.as_f64().is_some_and(|n| n >= 0.1 && n <= max_timeout),
                        "invalid timeout_min"
                    );
                    target["timeout_min"] = v.clone();
                }
                if let Some(v) = sec.get("max_queue") {
                    integer(v, 1, "max_queue")?;
                    limits[lane] = v.clone();
                }
            }
        }
        cfg["evaluators"] = normalized;
        cfg["lane_queue_max"] = limits;
        let settings = Settings::from_value(&cfg)?;
        let mut workers = Vec::new();
        for (lane, name) in LANES {
            let sec = &cfg["evaluators"][name];
            if sec["enabled"] != true {
                continue;
            }
            let total = sec["workers"].as_u64().unwrap() as usize;
            ensure!(total <= 1024, "worker count is too large");
            for index in 0..total {
                let cpus = if sec["cpus"].is_null() {
                    None
                } else {
                    let pool = cpu_ids(&sec["cpus"])?;
                    if total > 1
                        && pool.len() >= total
                        && sec
                            .get("split_cpus")
                            .and_then(Value::as_bool)
                            .unwrap_or(true)
                    {
                        let basic = pool.len() / total;
                        let extra = pool.len() % total;
                        let start = index * basic + index.min(extra);
                        let size = basic + usize::from(index < extra);
                        Some(cpu_string(&pool[start..start + size]))
                    } else {
                        Some(cpu_string(&pool))
                    }
                };
                let lock = if lane == "timing" {
                    let path = cfg["lock_file"]
                        .as_str()
                        .ok_or_else(|| anyhow::anyhow!("invalid lock_file"))?;
                    Some(PathBuf::from(if total > 1 {
                        format!("{path}.{index}")
                    } else {
                        path.to_owned()
                    }))
                } else {
                    None
                };
                let executor = sec
                    .get("executor")
                    .or_else(|| {
                        if lane != "timing" {
                            cfg.get("correctness_executor").filter(|v| !v.is_null())
                        } else {
                            None
                        }
                    })
                    .unwrap_or(&cfg["executor"])
                    .as_str()
                    .unwrap()
                    .to_owned();
                workers.push(WorkerSpec {
                    lane: lane.into(),
                    index,
                    total,
                    cpus,
                    omp_threads: sec["omp_threads"].as_u64().map(|n| n as u32),
                    timing_lock: lock,
                    executor,
                });
            }
        }
        Ok(Self {
            value: cfg,
            settings,
            workers,
        })
    }
    pub fn resolve_path(&self, relative: &str) -> Result<PathBuf> {
        ensure!(
            !relative.contains(['\\', ':'])
                && !relative.starts_with('/')
                && relative
                    .split('/')
                    .all(|p| !p.is_empty() && p != "." && p != ".."),
            "unsafe deployment path"
        );
        let parts: Vec<_> = relative.split('/').collect();
        let (root, rest) = if parts.len() >= 3
            && parts[0] == "flag_gems"
            && (parts[1] == "ops" || parts[1..].starts_with(&["runtime", "backend", "_spacemit"]))
        {
            (
                PathBuf::from(self.value["flag_gems_root"].as_str().unwrap()),
                parts[1..].join("/"),
            )
        } else if parts.len() == 2
            && parts[0] == "scripts"
            && ["bench_op.py", "bench_gemm.py"].contains(&parts[1])
        {
            (
                PathBuf::from(self.value["scripts_root"].as_str().unwrap()),
                parts[1].to_owned(),
            )
        } else {
            anyhow::bail!("path is outside deployment whitelist");
        };
        let absolute = std::path::absolute(root)?;
        let mut existing = absolute.as_path();
        while !existing.exists() {
            existing = existing
                .parent()
                .ok_or_else(|| anyhow::anyhow!("invalid deployment root"))?;
        }
        let root = existing
            .canonicalize()?
            .join(absolute.strip_prefix(existing)?);
        let target = root.join(rest);
        let mut ancestor = Some(target.as_path());
        while let Some(path) = ancestor {
            if !path.starts_with(&root) {
                break;
            }
            if path.exists() {
                ensure!(
                    path.canonicalize()?.starts_with(&root),
                    "deployment path escapes through a symlink"
                );
                break;
            }
            ancestor = path.parent();
        }
        Ok(target)
    }
}
