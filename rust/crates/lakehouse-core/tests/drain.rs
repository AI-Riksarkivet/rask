//! A served shell told to stop: SIGTERM turns the probe to shutting down, and a request already taken is answered.
//!
//! In a test binary of its own, because the signal is sent to this process.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::routing::get;
use lakehouse_core::service::health::HealthWatch;
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// The status and body of `GET path`, asked over a connection of its own.
async fn ask(address: SocketAddr, path: &str) -> (u16, String) {
    let mut connection = TcpStream::connect(address).await.unwrap();
    connection
        .write_all(format!("GET {path} HTTP/1.1\r\nhost: shell\r\nconnection: close\r\n\r\n").as_bytes())
        .await
        .unwrap();
    let mut answer = String::new();
    connection.read_to_string(&mut answer).await.unwrap();
    let status = answer.split(' ').nth(1).unwrap().parse().unwrap();
    (status, answer.split("\r\n\r\n").nth(1).unwrap_or_default().to_owned())
}

#[tokio::test(flavor = "multi_thread")]
async fn sigterm_drains_the_shell_and_an_open_request_is_answered() {
    let shell = Shell {
        lifecycle: Arc::new(Lifecycle::default()),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: 1024,
        request_deadline: Duration::from_secs(30),
    };
    shell.lifecycle.mark_started();
    let doors = Router::new().route(
        "/slow",
        get(|| async {
            tokio::time::sleep(Duration::from_millis(600)).await;
            "done"
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let served = tokio::spawn({
        let (shell, router) = (shell.clone(), shell.router(doors));
        async move { shell.serve(listener, router).await }
    });

    assert_eq!(ask(address, "/readyz").await.0, 200);
    let open = tokio::spawn(ask(address, "/slow"));
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert!(
        std::process::Command::new("kill")
            .args(["-TERM", &std::process::id().to_string()])
            .status()
            .unwrap()
            .success()
    );

    assert_eq!(open.await.unwrap(), (200, "done".to_owned()));
    served.await.unwrap().unwrap();
    assert!(shell.lifecycle.is_draining() && !shell.lifecycle.is_started());
    assert!(TcpStream::connect(address).await.is_err(), "a drained shell takes no new connection");
}
