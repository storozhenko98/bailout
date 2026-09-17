use std::io::{self, IsTerminal};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

// Give an interactive command the real terminal, then restore our foreground
// group and terminal settings even if it is interrupted while reading a password.
struct Terminal {
    group: libc::pid_t,
    mode: libc::termios,
}
fn foreground(group: libc::pid_t) -> i32 {
    unsafe {
        let previous = libc::signal(libc::SIGTTOU, libc::SIG_IGN);
        let result = libc::tcsetpgrp(libc::STDIN_FILENO, group);
        libc::signal(libc::SIGTTOU, previous);
        result
    }
}
impl Drop for Terminal {
    fn drop(&mut self) {
        foreground(self.group);
        unsafe {
            libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &self.mode);
        }
    }
}

pub fn interactive(mut command: Command, timeout: Duration) -> super::Result<super::Execution> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(
            "This command needs your terminal. Run bailout interactively to complete sign-in."
                .into(),
        );
    }
    let mut mode = unsafe { std::mem::zeroed() };
    let group = unsafe { libc::tcgetpgrp(libc::STDIN_FILENO) };
    if group < 0 || unsafe { libc::tcgetattr(libc::STDIN_FILENO, &mut mode) } != 0 {
        return Err("Cannot access the foreground terminal.".into());
    }
    let terminal = Terminal { group, mode };
    let mut child = command
        .process_group(0)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| e.to_string())?;
    let pid = child.id() as i32;
    if foreground(pid) != 0 {
        let _ = child.kill();
        let _ = child.wait();
        return Err("Cannot hand over the terminal.".into());
    }
    unsafe {
        libc::kill(-pid, libc::SIGCONT);
    }
    let started = Instant::now();
    let mut timed_out = false;
    let result = loop {
        if super::CANCELLED.load(Ordering::SeqCst) || started.elapsed() >= timeout {
            timed_out = !super::CANCELLED.load(Ordering::SeqCst);
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            break child.wait();
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(e) => {
                unsafe {
                    libc::kill(-pid, libc::SIGKILL);
                }
                let _ = child.wait();
                break Err(e);
            }
        }
    };
    drop(terminal);
    let status = result.map_err(|e| e.to_string())?;
    if status.signal() == Some(libc::SIGINT) || status.code() == Some(130) {
        super::CANCELLED.store(true, Ordering::SeqCst);
    }
    if super::CANCELLED.load(Ordering::SeqCst) {
        return Err("Interrupted.".into());
    }
    Ok(super::Execution {
        output: "Interactive command finished. Terminal input and output were not captured or sent to the model.".into(),
        code: status.code().unwrap_or(128), timed_out, truncated: false,
    })
}

pub fn shell() -> super::Result<()> {
    super::ui::note("Local Bash. Type exit to return. This shell is not sent to the model.");
    let mut command = Command::new("bash");
    command
        .args(["--noprofile", "--norc", "-i"])
        .env_remove("BASH_ENV")
        .env("HISTFILE", "/dev/null")
        .env("HISTSIZE", "0");
    let result = interactive(command, Duration::from_secs(1800))?;
    super::ui::note(&format!("Back in bailout · shell exit {}", result.code));
    Ok(())
}

pub fn uninstall() -> super::Result<()> {
    let path = std::env::current_exe().map_err(|e| e.to_string())?;
    std::fs::remove_file(&path).map_err(|e| format!("Could not remove {}: {e}", path.display()))?;
    println!(
        "Removed {}. Your tools, files, and configuration stay where they are.",
        path.display()
    );
    Ok(())
}
