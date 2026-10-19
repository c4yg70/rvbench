use anyhow::{Result, ensure};
use rvbench_cli::json::sha256;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};
use walkdir::WalkDir;

fn elf_ok(bytes: &[u8]) -> bool {
    bytes.len() >= 64
        && bytes[..4] == *b"\x7fELF"
        && bytes[4] == 2
        && bytes[5] == 1
        && u16::from_le_bytes([bytes[18], bytes[19]]) == 243
}

/// Package the manager/worker pair for a RISC-V board.
///
/// Repository-owned files come from `root`; worker-side assets come from
/// `worker_root` and manager-side assets from `manager_root`, which are the
/// standalone checkouts of those services. Passing `None` for `manager_root`
/// produces a worker-only bundle. The protocol files are emitted from the
/// crate itself so the bundle cannot drift from the linked contract.
pub fn build_bundle(
    root: &Path,
    worker_root: &Path,
    manager_root: Option<&Path>,
    output: &Path,
    binary: Option<&Path>,
) -> Result<Value> {
    ensure!(
        !output.exists(),
        "bundle output already exists; choose a new directory"
    );
    ensure!(
        worker_root.is_dir(),
        "worker checkout not found: {}",
        worker_root.display()
    );
    if let Some(manager_root) = manager_root {
        ensure!(
            manager_root.is_dir(),
            "manager checkout not found: {}",
            manager_root.display()
        );
    }
    let mut files: Vec<(PathBuf, PathBuf)> = [
        "deploy/observability/contract.md",
        "deploy/observability/scrape.yml",
        "deploy/observability/metrics.json",
        "tools/deploy_observability.py",
        "tools/observability_smoke.py",
        "tools/build_monitoring.sh",
        "docs/manager-worker.md",
        "docs/observability.md",
        "tools/deploy_cluster.py",
        "tools/cluster_smoke.py",
        "tools/cluster_recovery_smoke.py",
        "tools/verify_cluster_artifacts.py",
    ]
    .into_iter()
    .map(|p| (root.join(p), PathBuf::from(p)))
    .collect();
    for (source, destination) in [
        ("config.example.json", "worker/config.example.json"),
        ("contract/openapi.yaml", "worker/contract/openapi.yaml"),
    ] {
        files.push((worker_root.join(source), PathBuf::from(destination)));
    }
    if let Some(manager_root) = manager_root {
        for (source, destination) in [
            ("config.example.json", "manager/config.example.json"),
            ("contract/openapi.yaml", "manager/contract/openapi.yaml"),
        ] {
            files.push((manager_root.join(source), PathBuf::from(destination)));
        }
    }
    let candidate = binary.map(Path::to_owned).unwrap_or_else(|| {
        worker_root.join("target/riscv64gc-unknown-linux-gnu/release/rvbench-worker")
    });
    ensure!(
        candidate.is_file(),
        "Rust worker binary is missing; build the board target or pass --binary"
    );
    ensure!(
        elf_ok(&fs::read(&candidate)?),
        "K3 bundle requires a 64-bit RISC-V ELF; host binaries are for local checks"
    );
    if let Some(manager_root) = manager_root {
        let manager =
            manager_root.join("target/riscv64gc-unknown-linux-gnu/release/rvbench-manager");
        ensure!(
            manager.is_file(),
            "manager binary is missing; build the board target in {}",
            manager_root.display()
        );
        ensure!(
            elf_ok(&fs::read(&manager)?),
            "manager binary must be RISC-V ELF"
        );
        files.push((manager, "bin/rvbench-manager".into()));
    }
    files.push((candidate, "bin/rvbench-worker".into()));
    for (source, destination) in [(worker_root.join("adapters"), "worker/adapters")] {
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
        let label = source
            .strip_prefix(root)
            .or_else(|_| source.strip_prefix(worker_root))
            .or_else(|_| source.strip_prefix(manager_root.unwrap_or(root)))
            .unwrap_or(&source)
            .to_string_lossy()
            .replace('\\', "/");
        manifest.push(json!({"path":relative.to_string_lossy().replace('\\',"/"),"source":label,"sha256":sha256(&data),"bytes":data.len()}));
    }
    for (relative, label, content) in [
        (
            "contract/openapi.json",
            "generated:rvbench-protocol/SPEC",
            rvbench_protocol::SPEC,
        ),
        (
            "webui/console.html",
            "generated:rvbench-protocol/CONSOLE_HTML",
            rvbench_protocol::CONSOLE_HTML,
        ),
    ] {
        let destination = output.join(relative);
        fs::create_dir_all(destination.parent().unwrap())?;
        fs::write(&destination, content)?;
        manifest.push(json!({"path":relative,"source":label,"sha256":sha256(content.as_bytes()),"bytes":content.len()}));
    }
    let value = json!({"backends":["k3","k1"],"protocol_version":"1.0.0","control_plane":"rust","target":"riscv64gc-unknown-linux-gnu","manager_included":manager_root.is_some(),"files":manifest});
    rvbench_cli::files::atomic_json(&output.join("manifest.json"), &value)?;
    Ok(value)
}
