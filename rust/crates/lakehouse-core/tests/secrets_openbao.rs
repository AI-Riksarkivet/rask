//! The session with the secret store against a real OpenBao, pinned in `scripts/toolchain.sha256` and fetched into
//! `.localbin/bao`, run in dev mode with a JWT auth method mounted where the estate mounts its Kubernetes one, so the
//! login, the role's policy, `auth/token/lookup-self` and the KV v2 read are the store's own. The key the store
//! verifies the projected token with is generated when the test runs.
//!
//! Run with `cargo test -p lakehouse-core --test secrets_openbao -- --ignored` after fetching the binary:
//! `scripts/fetch_pinned.sh https://github.com/openbao/openbao/releases/download/v2.2.0/bao_2.2.0_Linux_x86_64.tar.gz <tar>`
//! and `tar -xzf <tar> -C .localbin bao`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use jsonwebtoken::{Algorithm, EncodingKey, Header};
use lakehouse_core::secrets::{Budget, Refused, SecretStore, SecretStoreSettings};
use rcgen::{KeyPair, PKCS_ECDSA_P256_SHA256};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const ROOT: &str = "root";
const ROLE: &str = "lineage";
const ACCOUNT: &str = "system:serviceaccount:rask:lineage";

/// The dev server, stopped when the test ends however it ends.
struct Bao(Child);

impl Drop for Bao {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

async fn as_root(client: &reqwest::Client, base: &str, path: &str, body: Value) {
    let response = client
        .post(format!("{base}/v1/{path}"))
        .header("X-Vault-Token", ROOT)
        .json(&body)
        .send()
        .await
        .unwrap();
    assert!(response.status().is_success(), "{path}: {}", response.text().await.unwrap());
}

#[tokio::test]
#[ignore = "needs the pinned OpenBao binary in .localbin/bao; see the module comment"]
async fn a_real_openbao_admits_the_role_and_serves_what_its_policy_names() {
    let binary = PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../.localbin/bao"));
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let home = std::env::temp_dir().join(format!("rask-openbao-{}-{port}", std::process::id()));
    std::fs::create_dir_all(&home).unwrap();
    let _bao = Bao(Command::new(&binary)
        .args([
            "server",
            "-dev",
            &format!("-dev-root-token-id={ROOT}"),
            &format!("-dev-listen-address=127.0.0.1:{port}"),
        ])
        .env_clear()
        .env("HOME", &home)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap());
    let base = format!("http://127.0.0.1:{port}");
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    for _ in 0..100 {
        if client
            .get(format!("{base}/v1/sys/health"))
            .send()
            .await
            .is_ok_and(|answer| answer.status().is_success())
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
    as_root(&client, &base, "sys/auth/kubernetes", json!({"type": "jwt"})).await;
    as_root(
        &client,
        &base,
        "auth/kubernetes/config",
        json!({"jwt_validation_pubkeys": [key.public_key_pem()]}),
    )
    .await;
    let policy = "path \"secret/data/signing-public-*\" { capabilities = [\"read\"] }\npath \"auth/token/lookup-self\" { capabilities = [\"read\"] }";
    as_root(&client, &base, "sys/policies/acl/lineage", json!({"policy": policy})).await;
    let role = json!({
        "role_type": "jwt", "bound_audiences": ["openbao"], "user_claim": "sub", "bound_subject": ACCOUNT,
        "token_policies": [ROLE], "token_no_default_policy": true, "token_ttl": "15m", "token_max_ttl": "1h",
    });
    as_root(&client, &base, &format!("auth/kubernetes/role/{ROLE}"), role).await;
    as_root(&client, &base, "secret/data/signing-public-catalog", json!({"data": {"keys": "UPUBLISHED"}})).await;
    as_root(&client, &base, "secret/data/signing-key-catalog", json!({"data": {"seed": "SPRIVATE"}})).await;

    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    let claims = json!({"iss": "https://kubernetes.default.svc", "sub": ACCOUNT, "aud": ["openbao"], "iat": now, "nbf": now, "exp": now + 600});
    let projected = jsonwebtoken::encode(
        &Header::new(Algorithm::ES256),
        &claims,
        &EncodingKey::from_ec_pem(key.serialize_pem().as_bytes()).unwrap(),
    )
    .unwrap();
    let token = home.join("token");
    std::fs::write(&token, projected).unwrap();
    let settings = SecretStoreSettings::from_environment([
        ("RASK_SECRET_STORE_ADDR".to_owned(), base),
        ("RASK_SECRET_STORE_ROLE".to_owned(), ROLE.to_owned()),
        ("RASK_SECRET_STORE_TOKEN_FILE".to_owned(), token.display().to_string()),
    ])
    .unwrap();
    let store = SecretStore::new(settings).unwrap();
    let budget = Budget {
        timeout: Duration::from_secs(5),
        retries: 3,
        backoff: Duration::from_millis(200),
    };

    store.log_in(budget).await.unwrap();

    let published = store.read("signing-public-catalog", budget).await.unwrap();
    assert_eq!(published, BTreeMap::from([("keys".to_owned(), "UPUBLISHED".to_owned())]));
    let one = Budget::one_attempt(Duration::from_secs(5));
    assert!(
        matches!(store.read_required("signing-key-catalog", "seed", one).await, Err(Refused::NotServed { .. })),
        "a secret the role's policy does not name is not served"
    );
    assert_eq!(store.answers().await, Ok(()));
    std::fs::remove_dir_all(&home).unwrap();
}
