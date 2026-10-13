use anyhow::Result;
use clap::Parser;
use rvbench_k3::K3Config;
use rvbench_runtime::Service;
use std::{path::PathBuf, sync::Arc};

#[derive(Parser)]
#[command(
    name = "rvbench-evald",
    about = "Rust evaluation service with a Python tensor bridge"
)]
struct Args {
    #[arg(long, env = "RVBENCH_CONFIG")]
    config: Option<PathBuf>,
    #[arg(long)]
    gen_token: bool,
    #[arg(long)]
    check_config: bool,
    #[arg(long)]
    print_port: bool,
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if args.gen_token {
        println!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        return Ok(());
    }
    let path = args.config.unwrap_or_else(|| {
        if PathBuf::from("config.json").is_file() {
            PathBuf::from("config.json")
        } else {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("evald/config.json")
        }
    });
    let cfg = K3Config::load(&path)?;
    if args.print_port {
        println!("{}", cfg.settings.port);
        return Ok(());
    }
    if args.check_config {
        println!("VALID: {} workers configured", cfg.workers.len());
        return Ok(());
    }
    let service = Service::start(Arc::new(cfg))?;
    service.serve().await
}
