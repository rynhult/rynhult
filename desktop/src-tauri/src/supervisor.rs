#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, sync::{mpsc, Arc, Mutex}, time::Duration};

    // A real fixture process proves EOF shutdown and reaping, without any live agent tools.
    #[test]
    fn companion_reaps_on_drop() {
        let root = std::env::temp_dir().join(format!("desktop-supervisor-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let script = root.join("child.py");
        fs::write(&script, "import sys,json\nc=json.loads(sys.stdin.readline())\nprint(json.dumps({'origin':'http://127.0.0.1:12345'}),flush=True)\nsys.stdin.read()\n").unwrap();
        let mut child = Companion::start("/usr/bin/python3".as_ref(), &script, &root,
                                         "{}".to_string(), Duration::from_secs(2)).unwrap();
        assert_eq!(child.ready.trim(), "{\"origin\": \"http://127.0.0.1:12345\"}");
        child.stop().unwrap();
        assert!(child.process.try_wait().unwrap().unwrap().success());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn watch_reports_a_companion_that_exits_on_its_own() {
        let root = std::env::temp_dir().join(format!("desktop-watch-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let script = root.join("child.py");
        // Ready, then crash: the port it held is free for any local user to bind.
        fs::write(&script, "print('{}', flush=True)\nraise SystemExit(3)\n").unwrap();
        let child = Companion::start("/usr/bin/python3".as_ref(), &script, &root,
                                     "{}".to_string(), Duration::from_secs(2)).unwrap();
        let state = Arc::new(Mutex::new(Some(child)));
        let (send, recv) = mpsc::channel();
        watch(state.clone(), Duration::from_millis(20), move || send.send(()).unwrap());
        recv.recv_timeout(Duration::from_secs(5)).expect("the host learns the main ended");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn watch_stays_quiet_after_the_host_stops_the_companion() {
        let root = std::env::temp_dir().join(format!("desktop-watch-stop-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let script = root.join("child.py");
        fs::write(&script, "import sys\nprint('{}', flush=True)\nsys.stdin.read()\n").unwrap();
        let child = Companion::start("/usr/bin/python3".as_ref(), &script, &root,
                                     "{}".to_string(), Duration::from_secs(2)).unwrap();
        let state = Arc::new(Mutex::new(Some(child)));
        let (send, recv) = mpsc::channel();
        let watcher = watch(state.clone(), Duration::from_millis(20), move || send.send(()).unwrap());
        state.lock().unwrap().take().unwrap().stop().unwrap();
        watcher.join().unwrap();
        assert!(recv.try_recv().is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn readiness_timeout_reaps_child() {
        let root = std::env::temp_dir().join(format!("desktop-timeout-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let script = root.join("child.py");
        fs::write(&script, "import time\ntime.sleep(60)\n").unwrap();
        assert!(Companion::start("/usr/bin/python3".as_ref(), &script, &root,
                                "{}".to_string(), Duration::from_millis(100)).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn dropping_companion_requests_graceful_eof_shutdown() {
        let root = std::env::temp_dir().join(format!("desktop-eof-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let script = root.join("child.py");
        fs::write(&script, "import sys,pathlib\nsys.stdin.readline()\nprint('ready',flush=True)\nsys.stdin.read()\npathlib.Path('stopped').write_text('graceful')\n").unwrap();
        let child = Companion::start("/usr/bin/python3".as_ref(), &script, &root,
                                     "{}".to_string(), Duration::from_secs(2)).unwrap();
        drop(child);
        assert_eq!(fs::read_to_string(root.join("stopped")).unwrap(), "graceful");
        fs::remove_dir_all(root).unwrap();
    }
}

use std::{
    io::{self, BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

pub struct Companion {
    process: Child,
    input: Option<ChildStdin>,
    pub ready: String,
}

impl Companion {
    pub fn id(&self) -> u32 {
        self.process.id()
    }
    pub fn start(python: &Path, script: &Path, home: &Path, config: String, timeout: Duration) -> io::Result<Self> {
        let process = Command::new(python).arg(script)
            .env_clear().env("HOME", home).env("PATH", "/usr/bin:/bin")
            .env("PYTHONPATH", script.parent().unwrap().join("../src"))
            // No user site-packages; a packaged install is read-only and ships bytecode.
            .env("PYTHONNOUSERSITE", "1").env("PYTHONDONTWRITEBYTECODE", "1")
            .current_dir(home).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn()?;
        let mut companion = Self { process, input: None, ready: String::new() };
        companion.input = companion.process.stdin.take();
        writeln!(companion.input.as_mut().unwrap(), "{config}")?;
        let stdout = companion.process.stdout.take().unwrap();
        let (send, recv) = mpsc::channel();
        thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = send.send(result);
        });
        companion.ready = recv.recv_timeout(timeout)
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "Main readiness timed out"))??;
        Ok(companion)
    }

    pub fn stop(&mut self) -> io::Result<()> {
        self.input.take(); // EOF: uvicorn performs lifespan cleanup.
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            if self.process.try_wait()?.is_some() { return Ok(()); }
            if Instant::now() >= deadline {
                self.process.kill()?;
                self.process.wait()?;
                return Err(io::Error::new(io::ErrorKind::TimedOut, "Main required forced termination"));
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
}

/// Calls `on_exit` when the main ends on its own (a crash, not the host's stop). Its loopback port is then free
/// for any local user to bind, and the window would hand that listener the capability and session cookies.
pub fn watch(state: Arc<Mutex<Option<Companion>>>, every: Duration,
             on_exit: impl FnOnce() + Send + 'static) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        loop {
            thread::sleep(every);
            match state.lock().unwrap().as_mut() {
                None => return, // the host stopped it
                Some(child) => if !matches!(child.process.try_wait(), Ok(None)) { break },
            }
        }
        on_exit();
    })
}

impl Drop for Companion {
    fn drop(&mut self) { let _ = self.stop(); }
}
