//! The shell answers what the Python app factory answers (`rust/vectors/shell_http.json`, ADR 0217 §5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::body::{Body, Bytes};
use axum::extract::Path;
use axum::http::Request;
use axum::routing::{get, post};
use axum::{Json, Router};
use http_body_util::{BodyExt, Full};
use lakehouse_core::service::health::{Dependency, HealthWatch};
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::problem::{ErrorCode, NsError};
use lakehouse_core::service::shell::Shell;
use serde_json::{Value, json};
use std::sync::Arc;
use std::time::Duration;
use tower::ServiceExt;

/// The doors the recorded exchanges were put to, beside the probes.
fn doors(deadline: Duration) -> Router {
    let refuse = |Path(name): Path<String>| async move { NsError::new(*ErrorCode::ALL.iter().find(|code| code.name() == name).unwrap(), "boom") };
    Router::new()
        .route("/raise/{name}", get(refuse))
        .route(
            "/raise-named",
            get(|| async { NsError::new(ErrorCode::PermissionDenied, "boom").named("UnauthoredRunError") }),
        )
        .route(
            "/slow",
            get(move || async move {
                tokio::time::sleep(5 * deadline).await;
                Json(json!({}))
            }),
        )
        .route(
            "/panic",
            get(|| async {
                if true {
                    panic!("boom")
                }
            }),
        )
        .route("/echo", post(|body: Bytes| async move { Json(json!({"bytes": body.len()})) }))
}

async fn shell_for(case: &Value, limits: &Value) -> Shell {
    let lifecycle = Lifecycle::default();
    match case["life"].as_str().unwrap() {
        "started" => lifecycle.mark_started(),
        "draining" => lifecycle.mark_draining(),
        _ => {}
    }
    let dependency = |name: &'static str, required: bool| {
        let answers = case["answering"][name].as_bool().unwrap();
        Dependency {
            name,
            required,
            read: Box::new(move || Box::pin(async move { if answers { Ok(()) } else { Err(format!("{name} is down")) } })),
        }
    };
    let health = HealthWatch::new(vec![dependency("catalog", true), dependency("bus", false)]);
    health.check(0).await;
    health.check(1).await;
    Shell {
        lifecycle: Arc::new(lifecycle),
        health: Arc::new(health),
        max_body_bytes: limits["max_body_bytes"].as_u64().unwrap() as usize,
        request_deadline: Duration::from_secs_f64(limits["deadline_seconds"].as_f64().unwrap()),
    }
}

/// What differs between the shell's answer to `case` and the Python factory's, as lines to print.
async fn differences(case: &Value, limits: &Value) -> Vec<String> {
    let shell = shell_for(case, limits).await;
    let mut request = Request::builder().method(case["method"].as_str().unwrap()).uri(case["path"].as_str().unwrap());
    for (name, value) in case["headers"].as_object().unwrap() {
        request = request.header(name, value.as_str().unwrap());
    }
    let size = case["body"].as_u64().unwrap() as usize;
    if size > 0 && case["declared"].as_bool().unwrap() {
        request = request.header("content-length", size);
    }
    let body = Body::new(Full::new(Bytes::from(vec![b'x'; size])));
    let answer = shell.router(doors(shell.request_deadline)).oneshot(request.body(body).unwrap()).await.unwrap();

    let mut differing = Vec::new();
    let mut check = |what: &str, ours: Value, theirs: &Value| {
        if &ours != theirs {
            differing.push(format!("{what}\n    rust:   {ours}\n    python: {theirs}"));
        }
    };
    check("status", json!(answer.status().as_u16()), &case["status"]);
    // A GET door answers HEAD here and its `Allow` says so; the Python doors refuse HEAD (ADR 0219).
    let header = |name: &str| Some(answer.headers().get(name)?.to_str().unwrap().replace(",HEAD", ""));
    let answered: serde_json::Map<String, Value> = ["content-type", "cache-control", "vary", "allow"]
        .iter()
        .filter_map(|name| Some(((*name).to_owned(), json!(header(name)?))))
        .collect();
    check("headers", Value::Object(answered), &case["answered"]);
    let mut ours: Value = serde_json::from_slice(&answer.into_body().collect().await.unwrap().to_bytes()).unwrap_or(Value::Null);
    let mut theirs = case["answer"].clone();
    let instance = ours.as_object_mut().and_then(|body| body.remove("instance"));
    match &case["instance"] {
        Value::String(sent) => check("instance", json!(instance), &json!(sent)),
        named => check("an instance is named", json!(instance.is_some()), named),
    }
    // What a panic is differs between the two (ADR 0219); its status, code and type do not.
    if case["path"] == "/panic" {
        for body in [&mut ours, &mut theirs] {
            body.as_object_mut().unwrap().retain(|member, _| member != "detail" && member != "error");
        }
    }
    check("body", ours, &theirs);
    differing
}

#[tokio::test]
async fn the_shell_answers_as_the_python_factory_does() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/shell_http.json");
    let vectors: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let cases = vectors["exchanges"].as_array().unwrap();
    let mut failures = Vec::new();
    for case in cases {
        let differing = differences(case, &vectors["limits"][0]).await;
        if !differing.is_empty() {
            failures.push(format!("{}:\n  {}", case["name"], differing.join("\n  ")));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} exchanges differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[tokio::test(start_paused = true)]
async fn an_answer_too_old_to_speak_for_now_reads_as_unavailable() {
    let health = HealthWatch::new(vec![Dependency {
        name: "catalog",
        required: true,
        read: Box::new(|| Box::pin(async { Ok(()) })),
    }]);
    health.check(0).await;
    tokio::time::advance(Duration::from_secs(20)).await;
    assert_eq!(serde_json::to_value(health.readiness()).unwrap()["status"], "ready");
    tokio::time::advance(Duration::from_secs(1)).await;
    assert_eq!(serde_json::to_value(health.readiness()).unwrap()["status"], "unhealthy");
}
