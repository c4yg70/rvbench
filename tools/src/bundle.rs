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
    let k3 = root.join("backends/spacemit/k3");
    let mut files: Vec<(PathBuf, PathBuf)> = vec![
        (k3.join("evald/evaldctl.sh"), "evald/evaldctl.sh".into()),
        (
            k3.join("evald/config.example.json"),
            "evald/config.example.json".into(),
        ),
        (k3.join("adapters/python/warmd.py"), "evald/warmd.py".into()),
        (
            root.join("webui/dashboard.html"),
            "evald/dashboard.html".into(),
        ),
    ];
    let candidate = binary
        .map(Path::to_owned)
        .unwrap_or_else(|| root.join("target/riscv64gc-unknown-linux-gnu/release/rvbench-evald"));
    ensure!(
        candidate.is_file(),
        "Rust evald binary is missing; build the board target or pass --binary"
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
    files.push((candidate, "evald/rvbench-evald".into()));
    for (source, destination) in [
        (k3.join("monitor"), "evald/monitor"),
        (k3.join("adapters/board"), "scripts"),
    ] {
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
    let value = json!({"backend":"spacemit-k3","control_plane":"rust","target":"riscv64gc-unknown-linux-gnu","files":manifest});
    rvbench_runtime::files::atomic_json(&output.join("manifest.json"), &value)?;
    Ok(value)
}
