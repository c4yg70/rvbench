use anyhow::{Result, ensure};
use clap::{Parser, Subcommand};
use rvbench_cli::control;
use rvbench_cli::{
    hidden_readiness_blockers, load_json, readiness_blockers, spec_digest,
    validate_baseline_results, validate_eval_results, validate_hidden_spec, validate_spec,
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Parser)]
#[command(
    name = "rvbench",
    version,
    about = "RISC-V benchmark contracts and evaluation client"
)]
pub struct Options {
    /// Manager or standalone worker base URL.
    #[arg(long, global = true)]
    base: Option<String>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Manager / standalone worker contract v1 (no authentication).
    Control {
        #[command(subcommand)]
        action: control::Action,
    },
    Validate {
        #[command(subcommand)]
        kind: Validation,
    },
    Digest {
        file: PathBuf,
        /// Report implementation and covered case digests for a baseline entry.
        #[arg(long)]
        baseline: Option<String>,
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
    BaselineResults {
        file: PathBuf,
        #[arg(long)]
        spec: PathBuf,
    },
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
            let relative = rvbench_cli::hidden::public_relative_path(&value)?;
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
        Validation::BaselineResults { file, spec } => {
            checked(validate_baseline_results(
                &load_json(file)?,
                Some(&load_json(spec)?),
                spec.parent(),
                file.parent(),
            ))?;
            println!(
                "VALID: historical baseline evidence (does not attest execution or comparability)"
            );
            Ok(0)
        }
    }
}
fn execute(options: &Options) -> Result<i32> {
    match &options.command {
        Command::Control { action } => control::run(options.base.as_deref(), action)?,
        Command::Validate { kind } => return validate(kind),
        Command::Digest { file, baseline } => {
            let spec = load_json(file)?;
            if let Some(id) = baseline {
                checked(validate_spec(&spec, file.parent()))?;
                let entry = spec["baseline"]["entries"]
                    .as_array()
                    .and_then(|entries| entries.iter().find(|entry| entry["id"] == *id))
                    .ok_or_else(|| anyhow::anyhow!("unknown baseline id"))?;
                let op = spec["operators"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|op| op["id"] == entry["operator_id"])
                    .unwrap();
                let cases: Vec<_> = op["cases"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|case| entry["case_ids"].as_array().unwrap().contains(&case["id"]))
                    .map(|case| json!({"case_id":case["id"],"case_contract_sha256":rvbench_cli::baseline::case_contract_digest(&spec,op,case)}))
                    .collect();
                print(
                    &json!({"baseline_id":id,"implementation_sha256":rvbench_cli::baseline::implementation_digest(entry),"cases":cases}),
                );
            } else {
                println!("{}", spec_digest(&spec));
            }
        }
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
