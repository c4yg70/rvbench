//! Compatibility rules for legacy campaign registries and nullable outcomes.
use super::profiles::{base_label, comparable};
use serde_json::{Value, json};
pub const STOCHASTIC: [&str; 12] = [
    "bernoulli",
    "rand",
    "rand_like",
    "randn",
    "randn_like",
    "dropout",
    "rrelu_with_noise_functional",
    "multinomial",
    "exponential",
    "normal",
    "poisson",
    "uniform",
];

pub fn registry_key(cond: &str, op: &str, backend: &str) -> String {
    if backend != "k3" {
        return format!("b:{backend}/{cond}/{op}");
    }
    if ["r1/E", "r1/H", "base"].contains(&cond) {
        return format!("expand_late/{cond}/{op}");
    }
    if [
        "r1/E", "r1/H", "r2/E", "r2/H", "r3/E", "r3/H", "base", "retime",
    ]
    .contains(&cond)
    {
        return format!("astra7/{cond}/{op}");
    }
    if [
        "r1/F", "r1/R", "r1/S", "r2/F", "r2/R", "r2/S", "_base", "_heldout",
    ]
    .contains(&cond)
    {
        return format!("astra6/{cond}/{op}");
    }
    if [
        "r1/E", "r1/H", "r1/T", "r2/E", "r2/H", "r2/T", "r3/E", "r3/H", "r3/T", "r4/E", "r4/H",
        "r4/T", "r5/H", "r5/full",
    ]
    .contains(&cond)
    {
        return format!("astra5/{cond}/{op}");
    }
    if [
        "full",
        "kope",
        "seed_fixed",
        "zero_fixed",
        "zero_adaptive",
        "strategy_only",
        "full_adaptive",
        "mask_on",
        "mask_off",
        "mask_random",
        "transfer_on",
        "target_local",
    ]
    .contains(&cond)
    {
        return format!("astra/{cond}/{op}");
    }
    format!("KOPE/{op}")
}
pub fn judge(
    ticket: &Value,
    job: &Value,
    registry: &mut Value,
    base: &str,
    key_override: Option<&str>,
) -> (String, String) {
    let state = job["state"].as_str().unwrap_or("unknown");
    let kind = ticket["kind"].as_str().unwrap_or("correctness");
    let op = ticket["op"].as_str().unwrap_or("unknown");
    let reply = |outcome: &str, note: &str| (outcome.to_owned(), note.to_owned());
    if state == "timeout" {
        return reply("timeout", "evaluation timeout");
    }
    if kind == "func" {
        return if state == "done" {
            reply("func_pass", "function smoke test completed")
        } else {
            reply("func_fail", "function smoke test failed")
        };
    }
    if state != "done" {
        return reply(
            "crash",
            "evaluation failed; inspect infrastructure evidence",
        );
    }
    let result = &job["result"];
    if kind != "correctness" {
        return if result["correct"] == false {
            reply("timing_failed_correctness", "timing failed correctness")
        } else {
            reply("timing_recorded", "timing result recorded")
        };
    }
    if result["correct"] == false {
        return reply("rejected_correctness", "numeric comparison failed");
    }
    if result["correct"] == true {
        let backend = ticket["backend"].as_str().unwrap_or("k3");
        let key = key_override.map(str::to_owned).unwrap_or_else(|| {
            registry_key(ticket["cond"].as_str().unwrap_or("KOPE"), op, backend)
        });
        let host = base_label(base);
        let existing = &registry[&key];
        let measured = result["gems_ms"].as_f64();
        if existing.is_object()
            && comparable(existing["host"].as_str(), Some(&host))
            && let Some(reference) = existing["ref_ms"].as_f64().filter(|v| *v > 0.0)
        {
            let Some(g) = measured else {
                return reply(
                    "needs_verifier_attention",
                    "correct result lacks candidate timing",
                );
            };
            if g >= reference * 0.95 {
                return reply(
                    "rejected_slower",
                    "candidate did not improve the same-host reference by more than 5%",
                );
            }
            registry[&key] = json!({"host":host,"ref_ms":g,"ref_job":job["job_id"],"ref_stem":format!("r{}",ticket["attempt"].as_u64().unwrap_or(1)),"ts":chrono::Utc::now().to_rfc3339()});
            if backend != "k3" {
                registry[&key]["backend"] = json!(backend);
            }
            return reply(
                "accepted_correctness_and_faster",
                "same-host reference improved by more than 5%",
            );
        }
        registry[&key] = json!({"host":host,"ref_ms":result["gems_ms"],"ref_job":job["job_id"],"ref_stem":format!("r{}",ticket["attempt"].as_u64().unwrap_or(1)),"ts":chrono::Utc::now().to_rfc3339(),"rebaselined":true});
        if backend != "k3" {
            registry[&key]["backend"] = json!(backend);
        }
        return reply(
            "accepted_correctness",
            "established a reference without cross-host comparison",
        );
    }
    if STOCHASTIC.contains(&op) {
        reply("needs_stats_check", "statistical validation required")
    } else if ticket["attempt"].as_u64().unwrap_or(1) >= 2 {
        reply(
            "suspended_unverifiable",
            "repeated attempt lacks a verifiable contract",
        )
    } else {
        reply(
            "unverifiable_retry",
            "retry may provide verification evidence",
        )
    }
}
