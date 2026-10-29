use rand::TryRng;
use rynhult_desktop_spike::{supervisor::{self, Companion}, transport};
use std::{io::{Read, Write}, net::TcpStream, path::PathBuf, sync::{Arc, Mutex}, time::{Duration, Instant}};
use tauri::{RunEvent, WebviewUrl, WebviewWindowBuilder};

fn main() {
    if let Err(error) = run() {
        eprintln!("Desktop spike could not start: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let started = Instant::now();
    let context = tauri::generate_context!();
    // Tauri resolves Linux package resources or macOS Contents/Resources; development uses the checkout.
    let resources = tauri::utils::platform::resource_dir(context.package_info(), &tauri::Env::default())
        .ok().filter(|dir| dir.join("desktop/main.py").is_file())
        .map_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize(), Ok)?;
    // The one-time macOS Installer owns the coherent execution runtime/UI in Library.
    // Before setup (or after full uninstall), the bundled window still opens normally.
    #[cfg(target_os = "macos")]
    let resources = {
        let installed = PathBuf::from("/Library/Application Support/Rynhult Desktop/Runtime");
        if installed.join("desktop/main.py").is_file() { installed } else { resources }
    };
    let home = PathBuf::from(std::env::var("HOME")?);
    let config_home = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from)
        .filter(|p| p.is_absolute()).unwrap_or_else(|| home.join(".config"));
    let profile = config_home.join("rynhult-desktop-spike");
    // Create private state before constructing either process or WebView.
    use std::os::unix::fs::DirBuilderExt;
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(&profile)?;
    use std::os::unix::fs::PermissionsExt;
    if std::fs::metadata(&profile)?.permissions().mode() & 0o077 != 0 {
        return Err("Spike profile must have mode 0700".into());
    }
    let mut bytes = [0u8; 32];
    rand::rngs::SysRng.try_fill_bytes(&mut bytes)?;  // the OS RNG; an entropy failure aborts startup
    let token: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    let config = serde_json::json!({"token": token, "data_dir": profile,
        "static_dir": resources.join("desktop/web-dist")}).to_string();
    // The bundled runtime, never the system Python; RYNHULT_SPIKE_PYTHON overrides for development.
    let python = std::env::var_os("RYNHULT_SPIKE_PYTHON").map(PathBuf::from)
        .unwrap_or_else(|| resources.join("desktop/runtime/python/bin/python3"));
    if !python.is_file() {
        return Err(format!("No Python runtime at {}; run desktop/build-runtime.sh", python.display()).into());
    }
    let child = Companion::start(&python, &resources.join("desktop/main.py"), &profile,
                                 config, Duration::from_secs(30))?;
    let url = transport::verified_origin(&child.ready, &token)?;
    // Authenticate readiness over the listener; stdout alone is insufficient.
    let mut connection = TcpStream::connect(("127.0.0.1", url.port().unwrap()))?;
    connection.set_read_timeout(Some(Duration::from_secs(5)))?;
    write!(connection, "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nCookie: rynhult_desktop={}\r\nConnection: close\r\n\r\n", url.port().unwrap(), token)?;
    let mut response = String::new();
    connection.take(65536).read_to_string(&mut response)?;
    if !response.starts_with("HTTP/1.1 200 ") { return Err("Main failed authenticated health check".into()); }
    eprintln!("desktop_main_ready_ms={} child_pid={}", started.elapsed().as_millis(), child.id());
    let state = Arc::new(Mutex::new(Some(child)));
    let watched = state.clone();
    let app = tauri::Builder::default().setup(move |app| {
        let allowed_origin = url.origin();
        let platform = if cfg!(target_os = "macos") { "macOS" } else { "Linux" };
        let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
            .title(format!("Rynhult · {platform} · Standalone"))
            .inner_size(1440.0, 900.0)
            .data_directory(profile.join("webview"))
            .on_navigation(move |target| target.scheme() == "tauri" || target.origin() == allowed_origin)
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .build()?;
        let cookie = tauri::webview::Cookie::build(("rynhult_desktop", token))
            .domain("127.0.0.1").path("/").http_only(true)
            .same_site(tauri::webview::cookie::SameSite::Strict).build();
        window.set_cookie(cookie)?;
        window.navigate(url)?;
        // Close after detecting child death. Polling does not authenticate a replacement listener between checks.
        let handle = app.handle().clone();
        supervisor::watch(watched, Duration::from_millis(200), move || {
            eprintln!("Main exited unexpectedly; closing the window");
            handle.exit(1);
        });
        Ok(())
    }).build(context)?;
    app.run(move |_app, event| {
        if matches!(event, RunEvent::Exit) {
            if let Some(mut child) = state.lock().unwrap().take() {
                if let Err(error) = child.stop() { eprintln!("Main shutdown: {error}"); }
            }
        }
    });
    Ok(())
}
