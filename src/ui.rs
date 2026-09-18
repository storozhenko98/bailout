use rustyline::error::ReadlineError;
use rustyline::{
    Cmd, ConditionalEventHandler, DefaultEditor, Event, EventContext, EventHandler, KeyEvent,
    RepeatCount,
};
use std::env;
use std::io::{self, IsTerminal, Write};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub fn terminal() -> bool {
    io::stderr().is_terminal() && env::var("TERM").unwrap_or_default() != "dumb"
}
pub fn paint(text: &str, code: &str) -> String {
    if terminal() && env::var_os("NO_COLOR").is_none() {
        format!("\x1b[{code}m{text}\x1b[0m")
    } else {
        text.into()
    }
}
pub fn dim(text: &str) -> String {
    paint(text, "90")
}
pub fn accent(text: &str) -> String {
    paint(text, "1;32")
}

pub fn welcome(version: &str, model: &str) {
    let cwd = env::current_dir()
        .map(|p| p.display().to_string())
        .unwrap_or_default();
    let home = env::var("HOME").unwrap_or_default();
    let cwd = if !home.is_empty() && cwd.starts_with(&home) {
        cwd.replacen(&home, "~", 1)
    } else {
        cwd
    };
    eprintln!(
        "\n  {} {}\n  {}\n",
        paint("bailout", "1"),
        dim(&format!("v{version}")),
        dim(&super::clean(&cwd))
    );
    eprintln!(
        "  {} {}",
        accent("›"),
        if model == "auto" {
            "auto · free models · full access"
        } else {
            model
        }
    );
    eprintln!(
        "  {}",
        dim("Fresh machine? Broken setup? Tell me what needs to work.")
    );
    eprintln!(
        "  {}\n",
        dim("/shell local terminal   /new fresh conversation   /help")
    );
}

pub fn note(text: &str) {
    eprintln!("  {}", dim(&super::clean(text)));
}
pub fn error(text: &str) {
    eprintln!("\n  {} {}\n", paint("!", "1;33"), super::clean(text));
}

struct ClearOrExit;
impl ConditionalEventHandler for ClearOrExit {
    fn handle(&self, _: &Event, _: RepeatCount, _: bool, context: &EventContext) -> Option<Cmd> {
        Some(if context.line().is_empty() {
            Cmd::EndOfFile
        } else {
            Cmd::Interrupt
        })
    }
}

pub fn editor() -> super::Result<DefaultEditor> {
    let config = rustyline::Config::builder().auto_add_history(true).build();
    let mut editor = DefaultEditor::with_config(config).map_err(|e| e.to_string())?;
    editor.bind_sequence(
        KeyEvent::ctrl('C'),
        EventHandler::Conditional(Box::new(ClearOrExit)),
    );
    editor.bind_sequence(KeyEvent::ctrl('J'), Cmd::Newline);
    editor.bind_sequence(KeyEvent::alt('\r'), Cmd::Newline);
    Ok(editor)
}

pub enum Input {
    Text(String),
    Cleared,
    Exit,
}

// Keep a signal arriving just before read() from trapping a plain terminal in a
// blocking read. Restore the descriptor before handing it to Bash or an editor.
struct NonblockingInput(i32);
impl NonblockingInput {
    fn new() -> super::Result<Self> {
        let flags = unsafe { libc::fcntl(libc::STDIN_FILENO, libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(libc::STDIN_FILENO, libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            return Err(io::Error::last_os_error().to_string());
        }
        Ok(Self(flags))
    }
}
impl Drop for NonblockingInput {
    fn drop(&mut self) {
        unsafe { libc::fcntl(libc::STDIN_FILENO, libc::F_SETFL, self.0) };
    }
}

pub fn read(editor: &mut DefaultEditor, selecting: bool) -> super::Result<Input> {
    let plain = if selecting { "  model › " } else { "  › " };
    if env::var("TERM").unwrap_or_default() == "dumb" {
        let _input = NonblockingInput::new()?;
        eprint!("{plain}");
        let _ = io::stderr().flush();
        let mut bytes = Vec::new();
        loop {
            if super::CANCELLED.load(Ordering::SeqCst) {
                return Ok(Input::Exit);
            }
            let mut c = 0u8;
            let count = unsafe { libc::read(libc::STDIN_FILENO, (&mut c as *mut u8).cast(), 1) };
            if count == 0 {
                return Ok(Input::Exit);
            }
            if count < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    return Ok(Input::Exit);
                }
                if error.kind() == io::ErrorKind::WouldBlock {
                    let mut poll = libc::pollfd {
                        fd: libc::STDIN_FILENO,
                        events: libc::POLLIN,
                        revents: 0,
                    };
                    unsafe { libc::poll(&mut poll, 1, 50) };
                    continue;
                }
                return Err(error.to_string());
            }
            if c == b'\n' {
                return Ok(Input::Text(String::from_utf8_lossy(&bytes).into_owned()));
            }
            bytes.push(c);
            if bytes.len() > super::CONTEXT_LIMIT {
                return Err("Input is too long.".into());
            }
        }
    }
    let colored = if selecting {
        format!("  model {} ", accent("›"))
    } else {
        format!("  {} ", accent("›"))
    };
    match editor.readline(&(plain, colored.as_str())) {
        Ok(line) => Ok(Input::Text(line)),
        Err(ReadlineError::Interrupted) => Ok(Input::Cleared),
        Err(ReadlineError::Eof) => Ok(Input::Exit),
        Err(e) => Err(e.to_string()),
    }
}

pub struct Spinner {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}
impl Spinner {
    pub fn new(label: &str) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let task = if terminal() {
            let done = stop.clone();
            let label = label.to_owned();
            Some(thread::spawn(move || {
                let started = Instant::now();
                let frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
                let mut i = 0;
                while !done.load(Ordering::Relaxed) {
                    eprint!(
                        "\r\x1b[2K  {} {} {}",
                        accent(frames[i % frames.len()]),
                        label,
                        dim(&format!(
                            "{:.0}s · ctrl-c to stop",
                            started.elapsed().as_secs_f64()
                        ))
                    );
                    let _ = io::stderr().flush();
                    thread::sleep(Duration::from_millis(80));
                    i += 1;
                }
                eprint!("\r\x1b[2K");
            }))
        } else {
            None
        };
        Self { stop, thread: task }
    }
}
impl Drop for Spinner {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(task) = self.thread.take() {
            let _ = task.join();
        }
    }
}

pub fn tool_start(command: &str) {
    eprintln!("\n  {}", paint("bash", "1"));
    for line in super::clean(command).lines().take(12) {
        eprintln!("  {} {}", accent("$"), line);
    }
    if command.lines().count() > 12 {
        note("… command continues");
    }
}
pub fn tool_finish(output: &str, code: i32, timeout: bool, seconds: f64, verbose: bool) {
    let text = super::clean(output);
    let lines: Vec<_> = text.lines().collect();
    let count = if verbose {
        lines.len()
    } else {
        10.min(lines.len())
    };
    for line in &lines[..count] {
        eprintln!("  {} {}", dim("│"), line);
    }
    if count < lines.len() {
        note(&format!(
            "… {} more lines · /last to expand",
            lines.len() - count
        ));
    }
    let state = if timeout {
        "timed out".into()
    } else if code == 0 {
        "done".into()
    } else {
        format!("exit {code}")
    };
    note(&format!("{state} · {seconds:.1}s"));
    eprintln!();
}

pub struct StreamAnswer {
    line_start: bool,
    printed: bool,
}
impl StreamAnswer {
    pub fn new() -> Self {
        Self {
            line_start: true,
            printed: false,
        }
    }
    pub fn push(&mut self, text: &str) {
        for c in super::clean(text).chars() {
            if self.line_start && io::stdout().is_terminal() {
                print!("  ");
            }
            print!("{c}");
            self.line_start = c == '\n';
            self.printed = true;
        }
        let _ = io::stdout().flush();
    }
    pub fn finish(&self) {
        if self.printed && !self.line_start {
            println!();
        }
    }
}
