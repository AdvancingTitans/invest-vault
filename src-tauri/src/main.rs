#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct ServiceProcess(Mutex<Option<CommandChild>>);

enum StartupOutcome {
    Ready(u16),
    Error(String),
}

const API_PROTOCOL: &str = "holding-notebook-v1";

fn safe_filename(value: &str, extension: &str, fallback: &str) -> String {
    let stem: String = value
        .chars()
        .filter(|character| character.is_alphanumeric() || matches!(character, '-' | '_' | ' '))
        .take(80)
        .collect();
    format!("{}.{}", if stem.trim().is_empty() { fallback } else { stem.trim() }, extension)
}

#[tauri::command]
fn save_export(bytes: Vec<u8>, suggested_name: String, kind: String) -> Result<Option<String>, String> {
    let (extension, fallback, title, filter, max_size, requires_zip) = match kind.as_str() {
        "xlsx" => ("xlsx", "我的持仓", "选择 Excel 保存位置", "Excel 工作簿", 10 * 1024 * 1024, true),
        "markdown" => ("md", "投资笔记", "选择笔记保存位置", "Markdown 文档", 20 * 1024 * 1024, false),
        "backup" => ("zip", "投资札记完整备份", "选择备份保存位置", "ZIP 备份", 500 * 1024 * 1024, true),
        _ => return Err("不支持的导出类型".into()),
    };
    if bytes.len() > max_size || (requires_zip && !bytes.starts_with(b"PK")) {
        return Err("导出文件校验失败".into());
    }
    if kind == "markdown" && std::str::from_utf8(&bytes).is_err() {
        return Err("笔记内容不是有效文本".into());
    }
    let Some(path) = rfd::FileDialog::new()
        .set_title(title)
        .set_file_name(safe_filename(&suggested_name, extension, fallback))
        .add_filter(filter, &[extension])
        .save_file()
    else {
        return Ok(None);
    };
    std::fs::write(&path, bytes).map_err(|error| format!("保存文件失败：{error}"))?;
    Ok(Some(path.to_string_lossy().into_owned()))
}

fn runtime_matches(response: &str, runtime_token: &str) -> bool {
    response.contains("200 OK")
        && response.contains(API_PROTOCOL)
        && response.contains(runtime_token)
}

fn reserve_loopback_port() -> std::io::Result<u16> {
    // ponytail: releasing this ephemeral listener before sidecar bind has a tiny local race;
    // pass an inherited socket only if desktop startup becomes multi-process orchestration.
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    Ok(listener.local_addr()?.port())
}

fn new_runtime_token() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{}-{nanos}", std::process::id())
}

fn service_is_ready(port: u16, runtime_token: &str) -> bool {
    let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    for _ in 0..80 {
        if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(100)) {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(300)));
            let _ = stream.write_all(b"GET /api/diagnostics HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");
            let mut response = String::new();
            if stream.read_to_string(&mut response).is_ok()
                && runtime_matches(&response, runtime_token)
            {
                return true;
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

fn startup_outcome(result: Result<u16, String>) -> StartupOutcome {
    match result {
        Ok(port) => StartupOutcome::Ready(port),
        Err(message) => StartupOutcome::Error(message),
    }
}

#[cfg(test)]
mod tests {
    use super::runtime_matches;

    #[test]
    fn readiness_rejects_an_unrelated_or_stale_service() {
        let current = "HTTP/1.0 200 OK\r\n\r\n{\"api_protocol\":\"holding-notebook-v1\",\"runtime_token\":\"run-2\"}";
        let stale = "HTTP/1.0 200 OK\r\n\r\n{\"api_protocol\":\"holding-notebook-v1\",\"runtime_token\":\"run-1\"}";

        assert!(runtime_matches(current, "run-2"));
        assert!(!runtime_matches(stale, "run-2"));
        assert!(!runtime_matches("HTTP/1.0 200 OK\r\n\r\n{}", "run-2"));
    }

    #[test]
    fn startup_failure_is_rendered_instead_of_returned_from_setup() {
        assert!(matches!(
            super::startup_outcome(Err("数据服务被旧进程占用".into())),
            super::StartupOutcome::Error(message) if message.contains("旧进程")
        ));
    }

    #[test]
    fn export_filename_cannot_escape_the_user_selected_directory() {
        assert_eq!(super::safe_filename("../../持仓.csv", "xlsx", "我的持仓"), "持仓csv.xlsx");
        assert_eq!(super::safe_filename("", "zip", "完整备份"), "完整备份.zip");
    }
}

fn stop_service(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<ServiceProcess>() {
        if let Ok(mut process) = state.0.lock() {
            if let Some(child) = process.take() {
                let _ = child.kill();
            }
        }
    }
}

fn start_service(app: &tauri::App) -> Result<u16, String> {
    let port = reserve_loopback_port().map_err(|error| error.to_string())?;
    let runtime_token = new_runtime_token();
    let command = app
        .shell()
        .sidecar("invest-vault-service")
        .map_err(|error| error.to_string())?
        .env("INVEST_VAULT_PORT", port.to_string())
        .env("INVEST_VAULT_RUNTIME_TOKEN", runtime_token.clone())
        .env("INVEST_VAULT_PARENT_PID", std::process::id().to_string());
    let (mut events, child) = command.spawn().map_err(|error| error.to_string())?;
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(line) => eprintln!("service: {}", String::from_utf8_lossy(&line)),
                CommandEvent::Stderr(line) => eprintln!("service error: {}", String::from_utf8_lossy(&line)),
                _ => {}
            }
        }
    });

    app.manage(ServiceProcess(Mutex::new(Some(child))));
    if !service_is_ready(port, &runtime_token) {
        stop_service(&app.handle());
        return Err("本地数据服务未能启动，可能仍有旧版本在后台运行".into());
    }
    Ok(port)
}

fn show_startup_error(app: &tauri::App, message: &str) {
    eprintln!("投资札记启动失败: {message}");
    stop_service(&app.handle());
    if let Err(error) = WebviewWindowBuilder::new(
        app,
        "startup-error",
        WebviewUrl::App("startup-error.html".into()),
    )
    .title("投资札记无法启动")
    .inner_size(520.0, 360.0)
    .resizable(false)
    .build()
    {
        eprintln!("无法显示启动错误窗口: {error}");
        app.handle().exit(1);
    }
}

fn main() {
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![save_export])
        .setup(|app| {
            match startup_outcome(start_service(app)) {
                StartupOutcome::Ready(port) => {
                    let url: Result<tauri::Url, _> = format!("http://127.0.0.1:{port}").parse();
                    let window = url.map_err(|error| error.to_string()).and_then(|url| {
                        WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                            .title("投资札记")
                            .inner_size(1280.0, 820.0)
                            .min_inner_size(390.0, 620.0)
                            .build()
                            .map_err(|error| error.to_string())
                    });
                    if let Err(error) = window {
                        show_startup_error(app, &error);
                    }
                }
                StartupOutcome::Error(message) => show_startup_error(app, &message),
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("投资札记启动失败");

    application.run(|app, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_service(app);
        }
    });
}
