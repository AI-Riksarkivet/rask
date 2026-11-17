//! A real OpenFGA server for a test: the pinned release (`scripts/openfga_server.sh`) as a subprocess on the in-memory
//! datastore, on free loopback ports, requiring a preshared key, with the estate's authorization model written to a
//! store of its own. No container: the binary is the one the chart deploys.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub const KEY: &str = "test-preshared-key";

pub struct Server {
    child: Child,
    pub url: String,
    pub store: String,
    pub model: String,
    pub token_file: PathBuf,
}

fn repository() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

/// The pinned server binary, fetched once.
fn binary() -> PathBuf {
    let script = repository().join("scripts/openfga_server.sh");
    let output = Command::new("bash").arg(&script).stderr(Stdio::inherit()).output();
    let output = output.unwrap_or_else(|error| panic!("cannot run {}: {error}", script.display()));
    assert!(
        output.status.success(),
        "the pinned OpenFGA server could not be fetched by {}; the test needs it (and network access to github.com once)",
        script.display()
    );
    PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

async fn post(client: &reqwest::Client, url: String, body: Value) -> Value {
    let response = client
        .post(url)
        .bearer_auth(KEY)
        .header("content-type", "application/json")
        .body(body.to_string())
        .send()
        .await
        .unwrap();
    let status = response.status();
    let text = response.text().await.unwrap();
    assert!(status.is_success(), "OpenFGA answered {status}: {text}");
    serde_json::from_str(&text).unwrap()
}

impl Server {
    pub async fn start() -> Server {
        let (http, grpc) = (free_port(), free_port());
        let child = Command::new(binary())
            .args([
                "run",
                "--datastore-engine",
                "memory",
                "--playground-enabled=false",
                "--metrics-enabled=false",
                "--log-level",
                "error",
            ])
            .args(["--http-addr", &format!("127.0.0.1:{http}"), "--grpc-addr", &format!("127.0.0.1:{grpc}")])
            .args(["--authn-method", "preshared", "--authn-preshared-keys", KEY])
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let url = format!("http://127.0.0.1:{http}");
        let client = reqwest::Client::new();
        let began = Instant::now();
        while client
            .get(format!("{url}/healthz"))
            .send()
            .await
            .map(|answer| !answer.status().is_success())
            .unwrap_or(true)
        {
            assert!(began.elapsed() < Duration::from_secs(30), "OpenFGA did not become healthy on {url}");
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let store = post(&client, format!("{url}/stores"), json!({"name": "rask-test"})).await["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let model_path = repository().join("packages/lakehouse/src/lakehouse/governed/auth/model.json");
        let model: Value = serde_json::from_str(&std::fs::read_to_string(model_path).unwrap()).unwrap();
        let model = post(&client, format!("{url}/stores/{store}/authorization-models"), model).await["authorization_model_id"]
            .as_str()
            .unwrap()
            .to_owned();
        let token_file = std::env::temp_dir().join(format!("openfga-token-{}-{http}", std::process::id()));
        std::fs::write(&token_file, format!("{KEY}\n")).unwrap();
        Server {
            child,
            url,
            store,
            model,
            token_file,
        }
    }

    /// Write tuples, each `(user, relation, object)` with an optional condition.
    pub async fn write(&self, tuples: &[Value]) {
        let body = json!({"writes": {"tuple_keys": tuples}, "authorization_model_id": self.model});
        post(&reqwest::Client::new(), format!("{}/stores/{}/write", self.url, self.store), body).await;
    }

    pub fn stop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop();
        let _ = std::fs::remove_file(&self.token_file);
    }
}

pub fn tuple(user: &str, relation: &str, object: &str) -> Value {
    json!({"user": user, "relation": relation, "object": object})
}
