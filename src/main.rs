use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use tao::{
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoop},
    window::WindowBuilder,
};
use tiny_http::{Header, Response, Server, StatusCode};
use wry::WebViewBuilder;

fn main() -> wry::Result<()> {
    let server = Arc::new(Server::http("127.0.0.1:0").unwrap());
    let port = server.server_addr().to_ip().unwrap().port();
    let server_clone = server.clone();

    std::thread::spawn(move || {
        for request in server_clone.incoming_requests() {
            let path = request.url().trim_start_matches('/');
            let mut file_path = PathBuf::from(path);
            
            if file_path.as_os_str().is_empty() || request.url().contains("?") {
                file_path = PathBuf::from("index.html");
            }
            
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
                    if file_path.to_string_lossy() == "index.html" || file_path.to_string_lossy() == "" {
                        (include_bytes!("index.html").to_vec(), "text/html; charset=utf-8")
                    } else if file_path.to_string_lossy() == "idle.png" {
                        (include_bytes!("idle.png").to_vec(), "image/png")
                    } else if file_path.to_string_lossy() == "speaking.png" {
                        (include_bytes!("speaking.png").to_vec(), "image/png")
                    } else {
                        (b"Not found".to_vec(), "text/plain")
                    }
                }
            };

            let header = Header::from_bytes(&b"Content-Type"[..], mime.as_bytes()).unwrap();
            let mut response = Response::from_data(body).with_status_code(StatusCode(200));
            response.add_header(header);
            let _ = request.respond(response);
        }
    });

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
        .with_url(&format!("http://127.0.0.1:{}/index.html", port))
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
