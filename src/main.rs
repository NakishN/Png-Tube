use tao::{
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoop},
    window::WindowBuilder,
};
use wry::{http::Response, WebViewBuilder};
use std::fs;
use std::path::PathBuf;

fn main() -> wry::Result<()> {
    let event_loop = EventLoop::new();
    let window = WindowBuilder::new()
        .with_title("PNGTuber (Rust)")
        .with_inner_size(tao::dpi::LogicalSize::new(500.0, 600.0))
        .with_transparent(true)
        .with_decorations(true)
        .build(&event_loop)
        .unwrap();

    let _webview = WebViewBuilder::new(&window)
        .with_transparent(true)
        .with_custom_protocol("app".into(), move |request| {
            let path = request.uri().path().trim_start_matches('/');
            let mut file_path = PathBuf::from(path);
            
            if file_path.as_os_str().is_empty() {
                file_path = PathBuf::from("index.html");
            }

            // Try reading from current directory
            let (body, mime) = match fs::read(&file_path) {
                Ok(content) => {
                    let mime = if file_path.extension().unwrap_or_default() == "png" {
                        "image/png"
                    } else if file_path.extension().unwrap_or_default() == "jpg" || file_path.extension().unwrap_or_default() == "jpeg" {
                        "image/jpeg"
                    } else {
                        "text/html; charset=utf-8"
                    };
                    (content, mime)
                },
                Err(_) => {
                    if path == "index.html" || path == "" {
                        (include_bytes!("index.html").to_vec(), "text/html; charset=utf-8")
                    } else if path == "idle.png" {
                        (include_bytes!("idle.png").to_vec(), "image/png")
                    } else if path == "speaking.png" {
                        (include_bytes!("speaking.png").to_vec(), "image/png")
                    } else {
                        (b"Not found".to_vec(), "text/plain")
                    }
                }
            };

            Response::builder()
                .header("Content-Type", mime)
                .header("Access-Control-Allow-Origin", "*")
                .body(body.into())
                .unwrap()
        })
        .with_url("app://localhost/index.html")
        .build()?;

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;

        if let Event::WindowEvent {
            event: WindowEvent::CloseRequested,
            ..
        } = event
        {
            *control_flow = ControlFlow::Exit;
        }
    });
}
