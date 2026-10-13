use serde_json::Value;
use std::collections::{BTreeMap, VecDeque};

#[derive(Default)]
pub struct Metrics {
    pub submissions: u64,
    pub jobs: BTreeMap<String, u64>,
    pub completed: BTreeMap<(String, String), u64>,
    pub last_submission: f64,
    pub last_completion: f64,
    pub recent: VecDeque<Value>,
    pub hist: BTreeMap<(String, String), Vec<f64>>,
    pub http: BTreeMap<(String, u16), u64>,
    pub fallback: u64,
    pub restore_fail: u64,
    pub slot_drift: u64,
    pub loaded_mismatch: u64,
}
impl Metrics {
    pub fn observe(&mut self, kind: &str, lane: &str, value: f64) {
        if value.is_finite() && value >= 0.0 {
            let stats = self
                .hist
                .entry((kind.into(), lane.into()))
                .or_insert_with(|| vec![0.0; 2 + buckets(kind).len()]);
            stats[0] += 1.0;
            stats[1] += value;
            for (index, bound) in buckets(kind).iter().enumerate() {
                if value <= *bound {
                    stats[index + 2] += 1.0;
                }
            }
        }
    }
    pub fn finished(&mut self, job: &Value, epoch: f64) {
        let state = job["state"].as_str().unwrap_or("failed");
        let lane = job["request"]["lane"].as_str().unwrap_or("timing");
        *self.jobs.entry(state.into()).or_default() += 1;
        *self
            .completed
            .entry((lane.into(), state.into()))
            .or_default() += 1;
        self.last_completion = epoch;
        self.recent.push_front(job.clone());
        self.recent.truncate(3);
        if let Some(duration) = job["duration_s"].as_f64() {
            self.observe("job_duration", lane, duration);
        }
        if let Some(psr) = job["result"]["psr"]
            .as_f64()
            .or_else(|| job["kope_mem"]["psr"].as_f64())
        {
            self.observe("psr", lane, psr);
        }
    }
    pub fn render(
        &self,
        lanes: &Value,
        limits: &Value,
        queue_max: usize,
        longpoll: usize,
        oldest: &Value,
    ) -> String {
        let mut lines = vec![
            "# TYPE evald_up gauge".into(),
            "evald_up 1".into(),
            "# TYPE evald_queue_max gauge".into(),
            format!("evald_queue_max {queue_max}"),
            "# TYPE evald_submissions_total counter".into(),
            format!("evald_submissions_total {}", self.submissions),
            "# TYPE evald_jobs_total counter".into(),
            "# TYPE evald_queued gauge".into(),
            "# TYPE evald_running gauge".into(),
        ];
        for lane in ["timing", "correctness", "func"] {
            for (metric, key) in [("evald_queued", "queued"), ("evald_running", "running")] {
                lines.push(format!(
                    "{metric}{{lane=\"{lane}\"}} {}",
                    lanes[lane][key].as_u64().unwrap_or(0)
                ));
            }
            if let Some(max) = limits[lane].as_u64() {
                lines.push(format!("evald_lane_queue_max{{lane=\"{lane}\"}} {max}"));
            }
            for state in ["done", "failed", "timeout", "canceled"] {
                lines.push(format!(
                    "evald_jobs_completed_total{{lane=\"{lane}\",state=\"{state}\"}} {}",
                    self.completed
                        .get(&(lane.into(), state.into()))
                        .copied()
                        .unwrap_or(0)
                ));
            }
            lines.push(format!(
                "evald_oldest_queued_age_seconds{{lane=\"{lane}\"}} {}",
                oldest[lane].as_f64().unwrap_or(0.0)
            ));
        }
        for state in ["done", "failed", "timeout"] {
            lines.push(format!(
                "evald_jobs_total{{state=\"{state}\"}} {}",
                self.jobs.get(state).copied().unwrap_or(0)
            ));
        }
        lines.push(format!(
            "evald_last_submission_timestamp_seconds {}",
            self.last_submission
        ));
        lines.push(format!(
            "evald_last_completion_timestamp_seconds {}",
            self.last_completion
        ));
        if let Some(job) = self.recent.front() {
            lines.push(format!(
                "evald_last_job_info{{op=\"{}\",state=\"{}\"}} 1",
                escape(job["request"]["op"].as_str().unwrap_or("unknown")),
                escape(job["state"].as_str().unwrap_or("unknown"))
            ));
        }
        for (index, job) in self.recent.iter().enumerate() {
            let labels = format!(
                "idx=\"{index}\",op=\"{}\",state=\"{}\"",
                escape(job["request"]["op"].as_str().unwrap_or("unknown")),
                escape(job["state"].as_str().unwrap_or("unknown"))
            );
            for (metric, key) in [
                ("evald_recent_job_duration_seconds", "duration_s"),
                ("evald_recent_job_queue_wait_seconds", "queue_wait_s"),
                (
                    "evald_recent_job_finished_timestamp_seconds",
                    "finished_epoch",
                ),
            ] {
                if let Some(n) = job[key].as_f64() {
                    lines.push(format!("{metric}{{{labels}}} {n}"));
                }
            }
        }
        for kind in [
            "queue_wait",
            "job_duration",
            "psr",
            "deploy",
            "restore",
            "pathlock_wait",
        ] {
            let metric = match kind {
                "psr" => "evald_psr".to_owned(),
                _ => format!("evald_{kind}_seconds"),
            };
            lines.push(format!(
                "# HELP {metric} Process-local histogram; resets on restart."
            ));
            lines.push(format!("# TYPE {metric} histogram"));
            let global = ["deploy", "restore", "pathlock_wait"].contains(&kind);
            for lane in if global {
                vec!["all"]
            } else {
                vec!["timing", "correctness", "func"]
            } {
                let stats = self.hist.get(&(kind.into(), lane.into()));
                let labels = if global {
                    String::new()
                } else {
                    format!("lane=\"{lane}\",")
                };
                for (index, bound) in buckets(kind).iter().enumerate() {
                    lines.push(format!(
                        "{metric}_bucket{{{labels}le=\"{bound}\"}} {}",
                        stats.map(|v| v[index + 2] as u64).unwrap_or(0)
                    ));
                }
                lines.push(format!(
                    "{metric}_bucket{{{labels}le=\"+Inf\"}} {}",
                    stats.map(|v| v[0] as u64).unwrap_or(0)
                ));
                let labels = if global {
                    String::new()
                } else {
                    format!("{{lane=\"{lane}\"}}")
                };
                lines.push(format!(
                    "{metric}_sum{labels} {}",
                    stats.map(|v| v[1]).unwrap_or(0.0)
                ));
                lines.push(format!(
                    "{metric}_count{labels} {}",
                    stats.map(|v| v[0] as u64).unwrap_or(0)
                ));
            }
        }
        for (metric, value) in [
            ("evald_restore_failures_total", self.restore_fail),
            ("evald_slot_drift_total", self.slot_drift),
            ("evald_loaded_mismatch_total", self.loaded_mismatch),
        ] {
            lines.push(format!("# TYPE {metric} counter"));
            lines.push(format!("{metric} {value}"));
        }
        lines.push(format!(
            "evald_executor_fallback_total{{reason=\"warmd_transport\"}} {}",
            self.fallback
        ));
        lines.push(format!("evald_longpoll_active {longpoll}"));
        lines.push(format!("evald_spool_jobs_total {}", self.submissions));
        for ((path, code), count) in &self.http {
            lines.push(format!(
                "evald_http_requests_total{{path=\"{}\",code=\"{code}\"}} {count}",
                escape(path)
            ));
        }
        lines.join("\n") + "\n"
    }
}
fn escape(v: &str) -> String {
    v.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}
fn buckets(kind: &str) -> &'static [f64] {
    match kind {
        "queue_wait" => &[1., 5., 15., 30., 60., 120., 300., 600.],
        "job_duration" => &[5., 15., 30., 60., 90., 120., 300., 600.],
        "psr" => &[1., 2., 4., 8., 16.],
        "pathlock_wait" => &[0.001, 0.01, 0.05, 0.1, 0.5, 1., 5., 30.],
        _ => &[0.1, 0.5, 1., 2., 5., 10., 30., 60.],
    }
}
