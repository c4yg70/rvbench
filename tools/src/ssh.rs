//! Native SSH transport; no Python or system ssh.exe dependency.
use anyhow::{Result, ensure};
use regex::Regex;
use russh::{
    ChannelMsg, Disconnect, client,
    keys::{self, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
};
use russh_sftp::client::SftpSession;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::io::AsyncWriteExt;

#[derive(Clone, Debug)]
pub struct Target {
    pub host: String,
    pub user: String,
    pub port: u16,
    pub keys: Vec<PathBuf>,
}
fn home() -> Result<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .ok_or_else(|| anyhow::anyhow!("home directory is unavailable"))
}
fn expand(path: &str, home: &Path) -> PathBuf {
    if path == "~" {
        home.to_owned()
    } else if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        home.join(rest)
    } else {
        PathBuf::from(path)
    }
}
fn pattern(pattern: &str, target: &str) -> bool {
    Regex::new(&format!(
        "(?i)^{}$",
        regex::escape(pattern)
            .replace("\\*", ".*")
            .replace("\\?", ".")
    ))
    .is_ok_and(|re| re.is_match(target))
}
pub fn parse_config(config: &str, target: &str) -> BTreeMap<String, Vec<String>> {
    let mut active = true;
    let mut values: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for line in config.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once(char::is_whitespace) else {
            continue;
        };
        let key = key.to_ascii_lowercase();
        let value = value.trim().trim_matches('"').trim_matches('\'');
        if key == "host" {
            let patterns: Vec<_> = value.split_whitespace().collect();
            active = patterns
                .iter()
                .any(|p| !p.starts_with('!') && pattern(p, target))
                && !patterns
                    .iter()
                    .any(|p| p.strip_prefix('!').is_some_and(|p| pattern(p, target)));
        } else if active {
            if key == "identityfile" {
                values.entry(key).or_default().push(value.into());
            } else {
                values.entry(key).or_insert(vec![value.into()]);
            }
        }
    }
    values
}
pub fn resolve(
    alias: &str,
    user: Option<&str>,
    key: Option<&Path>,
    no_config: bool,
) -> Result<Target> {
    let home = home()?;
    let values = if no_config {
        BTreeMap::new()
    } else {
        parse_config(
            &fs::read_to_string(home.join(".ssh/config")).unwrap_or_default(),
            alias,
        )
    };
    let get = |key: &str| values.get(key).and_then(|v| v.first()).map(String::as_str);
    let host = get("hostname").unwrap_or(alias).to_owned();
    let user = user
        .or_else(|| get("user"))
        .ok_or_else(|| anyhow::anyhow!("pass --user or configure User in ~/.ssh/config"))?
        .to_owned();
    let port = get("port").unwrap_or("22").parse()?;
    let keys = if let Some(key) = key {
        vec![key.to_owned()]
    } else {
        values
            .get("identityfile")
            .map(|files| {
                files
                    .iter()
                    .map(|v| expand(&v.replace("%d", &home.to_string_lossy()), &home))
                    .collect()
            })
            .unwrap_or_else(|| vec![home.join(".ssh/id_ed25519"), home.join(".ssh/id_rsa")])
    };
    Ok(Target {
        host,
        user,
        port,
        keys,
    })
}
struct Handler {
    host: String,
    port: u16,
    accept_new: bool,
}
impl client::Handler for Handler {
    type Error = anyhow::Error;
    async fn check_server_key(
        &mut self,
        key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        let public = key.public_key();
        if keys::check_known_hosts(&self.host, self.port, &public)? {
            return Ok(true);
        }
        ensure!(
            self.accept_new,
            "SSH host key is unknown; verify it and use --accept-new once"
        );
        keys::known_hosts::learn_known_hosts(&self.host, self.port, &public)?;
        Ok(true)
    }
}
pub struct Session {
    handle: client::Handle<Handler>,
}
impl Session {
    pub async fn connect(target: &Target, accept_new: bool, timeout: Duration) -> Result<Self> {
        let config = Arc::new(client::Config {
            inactivity_timeout: None,
            ..Default::default()
        });
        let handler = Handler {
            host: target.host.clone(),
            port: target.port,
            accept_new,
        };
        let mut handle = tokio::time::timeout(
            timeout,
            client::connect(config, (target.host.as_str(), target.port), handler),
        )
        .await??;
        let passphrase = std::env::var("RVBENCH_SSH_KEY_PASSPHRASE").ok();
        let mut authenticated = false;
        for path in target.keys.iter().filter(|path| path.is_file()) {
            let key = keys::load_secret_key(path, passphrase.as_deref())?;
            let hash = handle.best_supported_rsa_hash().await?.flatten();
            if handle
                .authenticate_publickey(
                    &target.user,
                    PrivateKeyWithHashAlg::new(Arc::new(key), hash),
                )
                .await?
                .success()
            {
                authenticated = true;
                break;
            }
        }
        if !authenticated && let Ok(password) = std::env::var("RVBENCH_SSH_PASSWORD") {
            authenticated = handle
                .authenticate_password(&target.user, password)
                .await?
                .success();
        }
        ensure!(
            authenticated,
            "SSH authentication failed; provide a configured key or credential environment"
        );
        Ok(Self { handle })
    }
    pub async fn exec(&self, command: &str, timeout: Duration) -> Result<(u32, Vec<u8>, Vec<u8>)> {
        let channel = self.handle.channel_open_session().await?;
        tokio::time::timeout(timeout, async move {
            let mut channel = channel;
            channel.exec(true, command).await?;
            channel.eof().await?;
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            let mut status = None;
            while let Some(message) = channel.wait().await {
                match message {
                    ChannelMsg::Data { data } => stdout.extend_from_slice(&data),
                    ChannelMsg::ExtendedData { data, .. } => stderr.extend_from_slice(&data),
                    ChannelMsg::ExitStatus { exit_status } => status = Some(exit_status),
                    _ => {}
                }
            }
            Ok::<_, anyhow::Error>((
                status.ok_or_else(|| anyhow::anyhow!("SSH channel closed without exit status"))?,
                stdout,
                stderr,
            ))
        })
        .await?
    }
    pub async fn sftp(&self) -> Result<SftpSession> {
        let channel = self.handle.channel_open_session().await?;
        channel.request_subsystem(true, "sftp").await?;
        Ok(SftpSession::new(channel.into_stream()).await?)
    }
    pub async fn put(&self, local: &Path, remote: &str) -> Result<()> {
        let sftp = self.sftp().await?;
        let mut file = sftp.create(remote).await?;
        file.write_all(&fs::read(local)?).await?;
        file.shutdown().await?;
        sftp.close().await?;
        Ok(())
    }
    pub async fn get(&self, remote: &str, local: &Path) -> Result<()> {
        let sftp = self.sftp().await?;
        let bytes = sftp.read(remote).await?;
        if let Some(parent) = local.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        rvbench_runtime::files::atomic_write(local, &bytes)?;
        sftp.close().await?;
        Ok(())
    }
    pub async fn close(self) -> Result<()> {
        self.handle
            .disconnect(Disconnect::ByApplication, "", "en")
            .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn host_patterns_and_first_value_semantics() {
        let cfg = "Host board\n HostName 10.0.0.1\n User alice\n IdentityFile ~/.ssh/board\nHost * !ignored\n User fallback\n Port 2222\n";
        let values = parse_config(cfg, "board");
        assert_eq!(values["hostname"], vec!["10.0.0.1"]);
        assert_eq!(values["user"], vec!["alice"]);
        assert_eq!(values["port"], vec!["2222"]);
        assert!(!parse_config(cfg, "ignored").contains_key("user"));
    }
}
