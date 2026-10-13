use crate::Backend;
use anyhow::{Result, ensure};
use rvbench_core::{json::sha256, load_json};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::Instant,
};

pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("file has no parent"))?;
    fs::create_dir_all(parent)?;
    let temp = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap().to_string_lossy(),
        uuid::Uuid::new_v4()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        Ok(())
    })();
    if temp.exists() {
        let _ = fs::remove_file(temp);
    }
    result
}
pub fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    atomic_write(path, &serde_json::to_vec_pretty(value)?)
}

pub struct FileLocks {
    files: Vec<File>,
    pub waited_s: f64,
}
impl FileLocks {
    pub fn acquire(
        targets: &[(PathBuf, bool)],
        directory: &Path,
        timing: Option<&Path>,
    ) -> Result<Self> {
        fs::create_dir_all(directory)?;
        let mut sorted = BTreeMap::new();
        for (path, write) in targets {
            sorted
                .entry(path.clone())
                .and_modify(|v| *v |= *write)
                .or_insert(*write);
        }
        let start = Instant::now();
        let mut result = Self {
            files: Vec::new(),
            waited_s: 0.0,
        };
        for (target, write) in sorted {
            let key = sha256(target.to_string_lossy().as_bytes());
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(directory.join(format!("path.{}.lock", &key[..16])))?;
            if write {
                fs2::FileExt::lock_exclusive(&file)?;
            } else {
                fs2::FileExt::lock_shared(&file)?;
            }
            result.files.push(file);
        }
        result.waited_s = start.elapsed().as_secs_f64();
        if let Some(path) = timing {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
            }
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)?;
            fs2::FileExt::lock_exclusive(&file)?;
            result.files.push(file);
        }
        Ok(result)
    }
}
impl Drop for FileLocks {
    fn drop(&mut self) {
        for file in self.files.iter().rev() {
            let _ = fs2::FileExt::unlock(file);
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ManifestEntry {
    pub path: String,
    pub abs: PathBuf,
    pub backup: PathBuf,
    pub existed: bool,
    pub created_dirs: Vec<PathBuf>,
    pub sha256: String,
    pub orig_sha256: Option<String>,
    pub deployed_sha256: Option<String>,
    pub deployed_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub restored_sha256: Option<Value>,
}
pub struct Deployment {
    pub job_dir: PathBuf,
    pub manifest: Vec<ManifestEntry>,
    staged: Vec<PathBuf>,
    committed: bool,
}
impl Deployment {
    pub fn new(job_dir: &Path) -> Self {
        Self {
            job_dir: job_dir.to_owned(),
            manifest: Vec::new(),
            staged: Vec::new(),
            committed: false,
        }
    }
    fn persist(&self) -> Result<()> {
        atomic_json(
            &self.job_dir.join("manifest.json"),
            &serde_json::to_value(&self.manifest)?,
        )
    }
    pub fn deploy(&mut self, backend: &dyn Backend, files: &Value) -> Result<()> {
        self.persist()?;
        for ent in files
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("invalid deployment files"))?
        {
            let relative = ent["path"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("missing deployment path"))?;
            let target = backend.resolve_path(relative)?;
            let bytes = ent["content"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("missing content"))?
                .as_bytes();
            let existed = target.exists();
            let original = if existed {
                Some(fs::read(&target)?)
            } else {
                None
            };
            if let Some(expected) = ent.get("expect_sha256").and_then(Value::as_str) {
                ensure!(
                    original.as_ref().is_some_and(|v| sha256(v) == expected),
                    "slot_poisoned: deployment identity mismatch"
                );
            }
            let backup = self.job_dir.join("backup").join(relative);
            let mut created_dirs = Vec::new();
            let mut parent = target.parent();
            while let Some(p) = parent {
                if p.exists() {
                    break;
                }
                created_dirs.push(p.to_owned());
                parent = p.parent();
            }
            if existed {
                fs::create_dir_all(backup.parent().unwrap())?;
                fs::copy(&target, &backup)?;
            }
            let stage = target.with_file_name(format!(
                "{}.{}.tmp",
                target.file_name().unwrap().to_string_lossy(),
                self.job_dir.file_name().unwrap().to_string_lossy()
            ));
            self.manifest.push(ManifestEntry {
                path: relative.into(),
                abs: target.clone(),
                backup,
                existed,
                created_dirs,
                sha256: sha256(bytes),
                orig_sha256: original.as_ref().map(|v| sha256(v)),
                deployed_sha256: None,
                deployed_bytes: None,
                restored_sha256: None,
            });
            self.persist()?;
            self.staged.push(stage.clone());
            atomic_write(&stage, bytes)?;
            if existed {
                fs::set_permissions(&stage, fs::metadata(&target)?.permissions())?;
            }
        }
        for index in 0..self.manifest.len() {
            let ent = &mut self.manifest[index];
            fs::rename(&self.staged[index], &ent.abs)?;
            let bytes = fs::read(&ent.abs)?;
            let actual = sha256(&bytes);
            ent.deployed_sha256 = Some(actual.clone());
            ent.deployed_bytes = Some(bytes.len() as u64);
            let matches = actual == ent.sha256;
            self.persist()?;
            ensure!(
                matches,
                "loaded_mismatch: deployment readback differs from request"
            );
        }
        self.committed = true;
        Ok(())
    }
    pub fn loaded_files(&self) -> Value {
        json!(
            self.manifest
                .iter()
                .map(|m| json!({"path":m.path,"sha256":m.deployed_sha256,"bytes":m.deployed_bytes}))
                .collect::<Vec<_>>()
        )
    }
    pub fn restore(&mut self) -> Result<()> {
        for ent in self.manifest.iter_mut().rev() {
            if ent.restored_sha256.is_some() {
                continue;
            }
            if ent.existed {
                let original = fs::read(&ent.backup)?;
                ensure!(
                    Some(sha256(&original)) == ent.orig_sha256,
                    "backup identity mismatch"
                );
                atomic_write(&ent.abs, &original)?;
                fs::set_permissions(&ent.abs, fs::metadata(&ent.backup)?.permissions())?;
            } else if ent.abs.exists() {
                fs::remove_file(&ent.abs)?;
            }
            let digest = if ent.abs.exists() {
                Some(sha256(&fs::read(&ent.abs)?))
            } else {
                None
            };
            ensure!(digest == ent.orig_sha256, "restore readback mismatch");
            ent.restored_sha256 = Some(json!(digest));
            for dir in &ent.created_dirs {
                let _ = fs::remove_dir(dir);
            }
        }
        self.persist()?;
        self.committed = true;
        Ok(())
    }
    pub fn from_manifest(path: &Path, backend: &dyn Backend) -> Result<Self> {
        let value = load_json(path.join("manifest.json"))?;
        let manifest: Vec<ManifestEntry> = serde_json::from_value(value)?;
        for ent in &manifest {
            ensure!(
                backend.resolve_path(&ent.path)? == ent.abs,
                "recovery manifest path differs from backend whitelist"
            );
            ensure!(
                ent.backup == path.join("backup").join(&ent.path),
                "recovery backup escaped job directory"
            );
            if ent.restored_sha256.is_none() {
                let current = if ent.abs.exists() {
                    Some(sha256(&fs::read(&ent.abs)?))
                } else {
                    None
                };
                ensure!(
                    current == ent.orig_sha256
                        || current == ent.deployed_sha256
                        || current.as_ref() == Some(&ent.sha256),
                    "recovery found foreign slot content; refusing overwrite"
                );
            }
        }
        Ok(Self {
            job_dir: path.to_owned(),
            manifest,
            staged: Vec::new(),
            committed: false,
        })
    }
}
impl Drop for Deployment {
    fn drop(&mut self) {
        for path in &self.staged {
            if path.exists() {
                let _ = fs::remove_file(path);
            }
        }
    }
}
