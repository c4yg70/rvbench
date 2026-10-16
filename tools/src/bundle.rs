use anyhow::{Result, ensure};
use rvbench_core::json::sha256;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};
use walkdir::WalkDir;

pub fn build_bundle(root: &Path, output: &Path, binary: Option<&Path>) -> Result<Value> {
    ensure!(
        !output.exists(),
        "bundle output already exists; choose a new directory"
    );
    let mut files: Vec<(PathBuf, PathBuf)> = [
        "worker/config.example.json",
        "manager/config.example.json",
        "worker/ui.html",
        "manager/ui.html",
        "contract/openapi.json",
        "worker/contract/openapi.yaml",
        "manager/contract/openapi.yaml",
        "docs/manager-worker.md",
        "tools/deploy_cluster.py",
        "tools/cluster_smoke.py",
        "tools/cluster_recovery_smoke.py",
        "tools/verify_cluster_artifacts.py",
    ]
    .into_iter()
    .map(|p| (root.join(p), PathBuf::from(p)))
    .collect();
    let candidate = binary
        .map(Path::to_owned)
        .unwrap_or_else(|| root.join("target/riscv64gc-unknown-linux-gnu/release/rvbench-worker"));
    ensure!(
        candidate.is_file(),
        "Rust worker binary is missing; build the board target or pass --binary"
    );
    let header = fs::read(&candidate)?;
    ensure!(
        header.len() >= 64
            && header[..4] == *b"\x7fELF"
            && header[4] == 2
            && header[5] == 1
            && u16::from_le_bytes([header[18], header[19]]) == 243,
        "K3 bundle requires a 64-bit RISC-V ELF; host binaries are for local checks"
    );
    let manager = candidate.with_file_name("rvbench-manager");
    if manager.is_file() {
        let header = fs::read(&manager)?;
        ensure!(
            header.len() >= 64
                && header[..4] == *b"\x7fELF"
                && header[4] == 2
                && header[5] == 1
                && u16::from_le_bytes([header[18], header[19]]) == 243,
            "manager binary must be RISC-V ELF"
        );
        files.push((manager, "bin/rvbench-manager".into()));
    }
    files.push((candidate, "bin/rvbench-worker".into()));
    for (source, destination) in [(root.join("worker/adapters"), "worker/adapters")] {
        for entry in WalkDir::new(&source).follow_links(false) {
            let entry = entry?;
            if entry.file_type().is_file()
                && !entry
                    .path()
                    .components()
                    .any(|p| p.as_os_str() == "__pycache__")
                && entry.path().extension().is_none_or(|v| v != "pyc")
            {
                files.push((
                    entry.path().to_owned(),
                    Path::new(destination).join(entry.path().strip_prefix(&source)?),
                ));
            }
        }
    }
    for (source, _) in &files {
        ensure!(
            source.is_file(),
            "deployment source missing: {}",
            source.display()
        );
    }
    fs::create_dir_all(output)?;
    let mut manifest = Vec::new();
    for (source, relative) in files {
        let destination = output.join(&relative);
        fs::create_dir_all(destination.parent().unwrap())?;
        fs::copy(&source, &destination)?;
        let data = fs::read(&destination)?;
        manifest.push(json!({"path":relative.to_string_lossy().replace('\\',"/"),"source":source.strip_prefix(root).unwrap_or(&source).to_string_lossy().replace('\\',"/"),"sha256":sha256(&data),"bytes":data.len()}));
    }
    let value = json!({"backends":["k3","k1"],"protocol_version":"1.0.0","control_plane":"rust","target":"riscv64gc-unknown-linux-gnu","files":manifest});
    rvbench_runtime::files::atomic_json(&output.join("manifest.json"), &value)?;
    Ok(value)
}
