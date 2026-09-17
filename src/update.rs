use std::env;
use std::fs::{self, DirBuilder, File, Permissions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const RELEASES: &str = "https://github.com/storozhenko98/bailout/releases";
const MAX_SIZE: u64 = 6_000_000;
const RESTART: &str = "BAILOUT_UPDATE_RESTART";

fn version(value: &str) -> Option<[u64; 3]> {
    let fields: Vec<_> = value
        .strip_prefix('v')
        .unwrap_or(value)
        .split('.')
        .collect();
    if fields.len() != 3 {
        return None;
    }
    let mut result = [0; 3];
    for (index, field) in fields.iter().enumerate() {
        if field.is_empty()
            || !field.bytes().all(|b| b.is_ascii_digit())
            || (field.len() > 1 && field.starts_with('0'))
        {
            return None;
        }
        result[index] = field.parse().ok()?;
    }
    Some(result)
}

fn platform() -> super::Result<&'static str> {
    match (env::consts::OS, env::consts::ARCH) {
        ("macos", "aarch64") => Ok("macos-arm64"),
        ("linux", "x86_64") => Ok("linux-x64"),
        ("linux", "aarch64") => Ok("linux-arm64"),
        _ => Err("No release for this platform.".into()),
    }
}

struct Staging(PathBuf);
impl Staging {
    fn new(parent: &Path) -> super::Result<Self> {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_nanos();
        let path = parent.join(format!(".bailout-update-{}-{nonce}", std::process::id()));
        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|_| "Install directory is not writable; run the installer as its owner.")?;
        Ok(Self(path))
    }
}
impl Drop for Staging {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn command(command: Command, seconds: u64, limit: usize) -> super::Result<String> {
    let result = super::execute(command, None, Duration::from_secs(seconds), false, limit)?;
    if result.code != 0 || result.timed_out || result.truncated {
        return Err("Update command failed or exceeded its limit.".into());
    }
    Ok(result.output)
}

fn curl(seconds: u64) -> Command {
    let mut command = Command::new("curl");
    command
        .args([
            "-q",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "3",
            "--max-time",
        ])
        .arg(seconds.to_string());
    command
}

fn download(url: &str, path: &Path, max_size: u64) -> super::Result<()> {
    let mut request = curl(60);
    request
        .arg("--max-filesize")
        .arg(max_size.to_string())
        .arg("--output")
        .arg(path)
        .arg(url);
    command(request, 62, 2048)
        .map_err(|_| "Release download failed; existing version retained.")?;
    if fs::metadata(path).map_err(|e| e.to_string())?.len() > max_size {
        return Err("Release download exceeds its size limit.".into());
    }
    Ok(())
}

fn checksum(manifest: &str, asset: &str) -> super::Result<String> {
    let mut matches = manifest.lines().filter_map(|line| {
        let fields: Vec<_> = line.split_whitespace().collect();
        (fields.len() == 2 && fields[1] == asset).then(|| fields[0])
    });
    let digest = matches.next().ok_or("Release checksum missing.")?;
    if matches.next().is_some()
        || digest.len() != 64
        || !digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("Invalid release checksum.".into());
    }
    Ok(digest.into())
}

fn digest(path: &Path) -> super::Result<String> {
    let mut sha = Command::new("sha256sum");
    sha.arg(path);
    let output = command(sha, 10, 2048).or_else(|_| {
        let mut sha = Command::new("shasum");
        sha.args(["-a", "256"]).arg(path);
        command(sha, 10, 2048)
    })?;
    Ok(output.split_whitespace().next().unwrap_or("").to_string())
}

pub fn startup() -> super::Result<()> {
    if env::var(RESTART).ok().as_deref()
        == Some(&format!("{}:{}", std::process::id(), super::VERSION))
    {
        env::remove_var(RESTART);
        return Ok(());
    }
    env::remove_var(RESTART);
    if cfg!(debug_assertions)
        || option_env!("BAILOUT_DEFAULT_API").is_some()
        || env::var("BAILOUT_NO_UPDATE").as_deref() == Ok("1")
    {
        return Ok(());
    }
    if let Err(error) = update(false) {
        if super::CANCELLED.load(std::sync::atomic::Ordering::SeqCst) {
            return Err("Interrupted.".into());
        }
        super::ui::note(&format!(
            "Update unavailable; continuing with v{}. {}",
            super::VERSION,
            error
        ));
    }
    Ok(())
}

pub fn update(manual: bool) -> super::Result<()> {
    if manual
        && env::var(RESTART).ok().as_deref()
            == Some(&format!("{}:{}", std::process::id(), super::VERSION))
    {
        env::remove_var(RESTART);
        super::ui::note(&format!("Now running v{}", super::VERSION));
        return Ok(());
    }
    if option_env!("BAILOUT_DEFAULT_API").is_some() {
        return Err("This custom build manages its own updates.".into());
    }
    let path = env::current_exe()
        .and_then(fs::canonicalize)
        .map_err(|e| e.to_string())?;
    let mut original = File::open(&path).map_err(|e| e.to_string())?;
    let metadata = original.metadata().map_err(|e| e.to_string())?;
    if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o6000 != 0 {
        return Err("Update this installation as its owner.".into());
    }
    // Lock the original inode: concurrent starts can keep running this version.
    if unsafe { libc::flock(original.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        if manual {
            return Err("Another startup is checking for updates.".into());
        }
        return Ok(());
    }
    let mut request = curl(5);
    request
        .args([
            "--head",
            "--output",
            "/dev/null",
            "--write-out",
            "%{url_effective}",
        ])
        .arg(format!("{RELEASES}/latest"));
    let resolved = command(request, 6, 2048).map_err(|_| "Could not check GitHub releases.")?;
    let tag = resolved
        .trim()
        .strip_prefix(&format!("{RELEASES}/tag/"))
        .filter(|tag| tag.starts_with('v'))
        .ok_or("Unexpected release URL.")?;
    let latest = version(tag).ok_or("Invalid stable release version.")?;
    if latest <= version(super::VERSION).ok_or("Invalid local version.")? {
        if manual {
            super::ui::note(&format!("Already current · v{}", super::VERSION));
        }
        return Ok(());
    }
    let parent = path.parent().ok_or("Missing install directory.")?;
    let staging = Staging::new(parent)?;
    let asset = format!("bailout-{}.tar.gz", platform()?);
    let archive = staging.0.join(&asset);
    let manifest = staging.0.join("SHA256SUMS");
    super::ui::note(&format!("Updating v{} → {tag}…", super::VERSION));
    download(
        &format!("{RELEASES}/download/{tag}/{asset}"),
        &archive,
        MAX_SIZE,
    )?;
    download(
        &format!("{RELEASES}/download/{tag}/SHA256SUMS"),
        &manifest,
        16_000,
    )?;
    let expected = checksum(
        &fs::read_to_string(manifest).map_err(|e| e.to_string())?,
        &asset,
    )?;
    if digest(&archive)? != expected {
        return Err("Checksum mismatch; existing version retained.".into());
    }
    let mut list = Command::new("tar");
    list.arg("-tzf").arg(&archive);
    if command(list, 10, 2048)?.trim() != "bailout" {
        return Err("Unexpected release archive contents.".into());
    }
    // Reject links and special files before extraction, including hard links.
    let mut types = Command::new("tar");
    types.arg("-tvzf").arg(&archive);
    let listing = command(types, 10, 2048)?;
    if listing.lines().count() != 1 || !listing.starts_with('-') {
        return Err("Release archive must contain one regular file.".into());
    }
    let mut extract = Command::new("tar");
    extract.arg("-xzf").arg(&archive).arg("-C").arg(&staging.0);
    command(extract, 10, 2048)?;
    let candidate = staging.0.join("bailout");
    let candidate_metadata = fs::symlink_metadata(&candidate).map_err(|e| e.to_string())?;
    if !candidate_metadata.is_file()
        || candidate_metadata.len() == 0
        || candidate_metadata.len() >= MAX_SIZE
    {
        return Err("Invalid release binary.".into());
    }
    fs::set_permissions(&candidate, Permissions::from_mode(metadata.mode() & 0o777))
        .map_err(|e| e.to_string())?;
    let mut probe = Command::new(&candidate);
    probe.arg("--version");
    if command(probe, 5, 2048)?.trim() != format!("bailout {}", &tag[1..]) {
        return Err("Downloaded binary failed its version check.".into());
    }
    let current = fs::metadata(&path).map_err(|e| e.to_string())?;
    if current.ino() != metadata.ino() || current.dev() != metadata.dev() {
        return Err("Installation changed during update; retry on the next start.".into());
    }
    // Retain a bounded copy for rollback if exec itself fails. No backup files
    // or updater state are left behind after a successful restart.
    let mut backup = Vec::new();
    (&mut original)
        .take(MAX_SIZE + 1)
        .read_to_end(&mut backup)
        .map_err(|e| e.to_string())?;
    if backup.len() as u64 > MAX_SIZE {
        return Err("Existing binary exceeds the update limit.".into());
    }
    File::open(&candidate)
        .and_then(|f| f.sync_all())
        .map_err(|e| e.to_string())?;
    fs::rename(&candidate, &path).map_err(|e| e.to_string())?;
    drop(staging);
    super::ui::note(&format!("Updated to {tag}. Restarting…"));
    let error = Command::new(&path)
        .args(env::args_os().skip(1))
        .env(RESTART, format!("{}:{}", std::process::id(), &tag[1..]))
        .exec();
    let rollback = Staging::new(parent)?;
    let saved = rollback.0.join("bailout");
    let mut file =
        File::create(&saved).map_err(|e| format!("Restart failed and rollback failed: {e}"))?;
    file.write_all(&backup)
        .and_then(|_| file.set_permissions(metadata.permissions()))
        .and_then(|_| file.sync_all())
        .and_then(|_| fs::rename(saved, &path))
        .map_err(|e| format!("Restart failed and rollback failed: {e}"))?;
    Err(format!(
        "Restart failed; restored the previous binary: {error}"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stable_versions_only_and_numeric_order() {
        assert!(version("v0.10.0") > version("v0.9.9"));
        for invalid in ["v1.2", "v1.2.3-beta", "v01.2.3", "v1.2.3/evil", "v1.-2.3"] {
            assert_eq!(version(invalid), None);
        }
    }
    #[test]
    fn exactly_one_checksum_for_the_asset() {
        let line = format!("{}  bailout-macos-arm64.tar.gz\n", "a".repeat(64));
        assert!(checksum(&line, "bailout-macos-arm64.tar.gz").is_ok());
        assert!(checksum(&(line.clone() + &line), "bailout-macos-arm64.tar.gz").is_err());
        assert!(checksum(&line, "bailout-linux-x64.tar.gz").is_err());
    }
}
