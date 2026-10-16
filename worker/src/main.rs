use clap::Parser;
#[derive(Parser)]
struct Args {
    #[arg(long)]
    config: std::path::PathBuf,
}
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let config: rvbench_worker::Config = serde_json::from_slice(&std::fs::read(args.config)?)?;
    let listener = tokio::net::TcpListener::bind(&config.listen).await?;
    let worker = rvbench_worker::Worker::open(config).await?;
    worker.start();
    axum::serve(listener, rvbench_worker::router(worker)).await?;
    Ok(())
}
