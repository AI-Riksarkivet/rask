//! An issuer's keys are fetched over HTTP and a bearer is judged by them, end to end through `Authenticator::caller_of`.
//!
//! The issuer is an in-process server on a loopback port; every key it signs with is generated when the test runs.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use jsonwebtoken::jwk::Jwk;
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use lakehouse_core::auth::identity::Caller;
use lakehouse_core::auth::issuer::{self, Clock, IssuerKeys};
use lakehouse_core::auth::settings::AuthSettings;
use lakehouse_core::auth::verifier::Authenticator;
use lakehouse_core::service::problem::{ErrorCode, NsError};
use rcgen::{BasicConstraints, CertificateParams, IsCa, KeyPair, PKCS_ECDSA_P256_SHA256, PKCS_RSA_SHA256, RsaKeySize};
use serde_json::{Value, json};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::net::{TcpListener, TcpStream};
use tokio_rustls::TlsAcceptor;
use tokio_rustls::server::TlsStream;

/// A key the issuer signs with: its private half, and its public half as the key set publishes it.
struct Key {
    algorithm: Algorithm,
    private: EncodingKey,
    public: Value,
}

fn key(kid: &str, algorithm: Algorithm) -> Key {
    let private = match algorithm {
        Algorithm::ES256 => EncodingKey::from_ec_pem(KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap().serialize_pem().as_bytes()).unwrap(),
        Algorithm::RS256 => EncodingKey::from_rsa_pem(
            KeyPair::generate_rsa_for(&PKCS_RSA_SHA256, RsaKeySize::_2048)
                .unwrap()
                .serialize_pem()
                .as_bytes(),
        )
        .unwrap(),
        other => panic!("no key is generated for {other:?}"),
    };
    let mut jwk = Jwk::from_encoding_key(&private, algorithm).unwrap();
    jwk.common.key_id = Some(kid.to_owned());
    Key {
        algorithm,
        private,
        public: serde_json::to_value(jwk).unwrap(),
    }
}

/// What the issuer serves, and what it was asked.
#[derive(Default)]
struct Served {
    names_itself: String,
    jwks_uri: String,
    algorithms: Vec<&'static str>,
    keys: Vec<Value>,
    down: bool,
    discoveries: usize,
    key_sets: usize,
    bearers: Vec<Option<String>>,
}

type Shared = Arc<Mutex<Served>>;

fn asked(served: &Shared, headers: &HeaderMap) -> bool {
    let mut served = served.lock().unwrap();
    let bearer = headers.get("authorization").map(|value| value.to_str().unwrap().to_owned());
    served.bearers.push(bearer);
    !served.down
}

async fn discovery(State(served): State<Shared>, headers: HeaderMap) -> Response {
    let up = asked(&served, &headers);
    let mut served = served.lock().unwrap();
    served.discoveries += 1;
    if !up {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let document = json!({
        "issuer": served.names_itself,
        "jwks_uri": served.jwks_uri,
        "id_token_signing_alg_values_supported": served.algorithms,
    });
    axum::Json(document).into_response()
}

async fn key_set(State(served): State<Shared>, headers: HeaderMap) -> Response {
    let up = asked(&served, &headers);
    let mut served = served.lock().unwrap();
    served.key_sets += 1;
    if !up {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    axum::Json(json!({"keys": served.keys})).into_response()
}

/// An issuer on a loopback port.
struct Issuer {
    base: String,
    served: Shared,
}

impl Issuer {
    fn router(served: &Shared) -> Router {
        Router::new()
            .route("/.well-known/openid-configuration", get(discovery))
            .route("/keys", get(key_set))
            .with_state(Arc::clone(served))
    }

    fn serving(base: String, keys: &[&Key]) -> Issuer {
        let served = Arc::new(Mutex::new(Served {
            names_itself: base.clone(),
            jwks_uri: format!("{base}/keys"),
            algorithms: vec!["RS256", "ES256"],
            keys: keys.iter().map(|key| key.public.clone()).collect(),
            ..Served::default()
        }));
        Issuer { base, served }
    }

    async fn plain(keys: &[&Key]) -> Issuer {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let issuer = Issuer::serving(format!("http://{}", listener.local_addr().unwrap()), keys);
        tokio::spawn(axum::serve(listener, Issuer::router(&issuer.served)).into_future());
        issuer
    }

    /// An issuer over TLS with a certificate from a CA of its own, as a cluster's service-account issuer is; the CA's
    /// certificate is written to `ca_file`.
    async fn private(keys: &[&Key], ca_file: &PathBuf) -> Issuer {
        let mut ca = CertificateParams::new(Vec::<String>::new()).unwrap();
        ca.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca = rcgen::CertifiedIssuer::self_signed(ca, KeyPair::generate().unwrap()).unwrap();
        std::fs::write(ca_file, ca.pem()).unwrap();
        let leaf_key = KeyPair::generate().unwrap();
        let leaf = CertificateParams::new(vec!["127.0.0.1".to_owned()]).unwrap().signed_by(&leaf_key, &ca).unwrap();
        let config = rustls::ServerConfig::builder_with_provider(Arc::new(rustls::crypto::aws_lc_rs::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(
                vec![leaf.der().clone()],
                rustls::pki_types::PrivateKeyDer::Pkcs8(leaf_key.serialize_der().into()),
            )
            .unwrap();
        let tcp = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let issuer = Issuer::serving(format!("https://{}", tcp.local_addr().unwrap()), keys);
        let listener = Tls {
            tcp,
            acceptor: TlsAcceptor::from(Arc::new(config)),
        };
        tokio::spawn(axum::serve(listener, Issuer::router(&issuer.served)).into_future());
        issuer
    }

    fn with<T>(&self, change: impl FnOnce(&mut Served) -> T) -> T {
        change(&mut self.served.lock().unwrap())
    }

    /// How often discovery and the key set were fetched.
    fn fetches(&self) -> (usize, usize) {
        self.with(|served| (served.discoveries, served.key_sets))
    }
}

struct Tls {
    tcp: TcpListener,
    acceptor: TlsAcceptor,
}

impl axum::serve::Listener for Tls {
    type Io = TlsStream<TcpStream>;
    type Addr = SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            let Ok((stream, address)) = self.tcp.accept().await else { continue };
            if let Ok(stream) = self.acceptor.accept(stream).await {
                return (stream, address);
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<SocketAddr> {
        self.tcp.local_addr()
    }
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64
}

fn token(key: &Key, kid: &str, issuer: &str, subject: &str, audience: &str) -> String {
    let mut header = Header::new(key.algorithm);
    header.kid = Some(kid.to_owned());
    let claims = json!({"iss": issuer, "sub": subject, "aud": audience, "iat": now(), "exp": now() + 600});
    jsonwebtoken::encode(&header, &claims, &key.private).unwrap()
}

fn settings(extra: &[(&str, &str)]) -> AuthSettings {
    let base = [
        ("RASK_OIDC_ENABLED", "true"),
        ("RASK_OIDC_AUDIENCE", "rask"),
        ("RASK_OIDC_IDP_ID", "dex"),
        ("RASK_OIDC_ALLOW_INSECURE", "true"),
    ];
    AuthSettings::from_environment(base.iter().chain(extra).map(|(name, value)| (name.to_string(), value.to_string()))).unwrap()
}

/// A clock the test moves by hand, so the 30-second pace is crossed without waiting for it.
fn clock() -> (Clock, Arc<Mutex<Duration>>) {
    let (start, moved) = (Instant::now(), Arc::new(Mutex::new(Duration::ZERO)));
    let read = Arc::clone(&moved);
    (Arc::new(move || start + *read.lock().unwrap()), moved)
}

fn people(issuer: &str, extra: &[(&str, &str)]) -> (Authenticator, Arc<IssuerKeys>, Arc<Mutex<Duration>>) {
    let settings = settings(&[&[("RASK_OIDC_ISSUER", issuer)], extra].concat());
    let (clock, moved) = clock();
    let keys = Arc::new(IssuerKeys::from_settings(&settings).unwrap().with_clock(clock));
    (Authenticator::new(&settings, Arc::clone(&keys) as _), keys, moved)
}

async fn caller(authenticator: &Authenticator, bearer: &str) -> Result<String, NsError> {
    match authenticator.caller_of(Some(bearer), now()).await? {
        Some(Caller::Person { principal, .. }) => Ok(format!("person {}", principal.key())),
        Some(Caller::Service { principal }) => Ok(format!("service {}", principal.key())),
        None => panic!("authentication is on"),
    }
}

fn refused(answer: Result<String, NsError>) -> ErrorCode {
    answer.expect_err("the bearer was taken").code
}

#[tokio::test]
async fn a_token_signed_by_its_issuers_key_names_its_caller_and_one_signed_by_another_key_is_refused() {
    let (ec, rsa) = (key("ec-1", Algorithm::ES256), key("rsa-1", Algorithm::RS256));
    let idp = Issuer::plain(&[&ec, &rsa]).await;
    let settings = settings(&[("RASK_OIDC_ISSUER", &idp.base)]);
    let (authenticator, keys) = issuer::authenticator(&settings).unwrap();
    let refreshing = issuer::start(&keys).await;
    assert_eq!(idp.fetches(), (1, 1), "the first refresh fetched discovery and the key set");

    assert_eq!(
        caller(&authenticator, &token(&ec, "ec-1", &idp.base, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
    assert_eq!(
        caller(&authenticator, &token(&rsa, "rsa-1", &idp.base, "bob", "rask")).await.unwrap(),
        "person dex~bob"
    );
    assert_eq!(idp.fetches(), (1, 1), "a request reads the keys held");

    let forger = key("ec-1", Algorithm::ES256);
    assert_eq!(
        refused(caller(&authenticator, &token(&forger, "ec-1", &idp.base, "alice", "rask")).await),
        ErrorCode::Unauthenticated
    );
    let another_audience = token(&ec, "ec-1", &idp.base, "alice", "elsewhere");
    assert_eq!(refused(caller(&authenticator, &another_audience).await), ErrorCode::Unauthenticated);
    refreshing.abort();
}

#[tokio::test]
async fn a_rotated_key_is_found_by_one_paced_reread_and_a_second_unknown_kid_inside_the_pace_costs_no_fetch() {
    let (first, second, third) = (key("k1", Algorithm::ES256), key("k2", Algorithm::ES256), key("k3", Algorithm::ES256));
    let idp = Issuer::plain(&[&first]).await;
    let (authenticator, keys, moved) = people(&idp.base, &[]);
    assert!(keys.refresh().await.is_empty());

    idp.with(|served| served.keys.push(second.public.clone()));
    assert_eq!(
        caller(&authenticator, &token(&second, "k2", &idp.base, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
    assert_eq!(idp.fetches(), (1, 2), "the unknown kid bought one re-read of the key set");

    idp.with(|served| served.keys.push(third.public.clone()));
    assert_eq!(
        refused(caller(&authenticator, &token(&third, "k3", &idp.base, "alice", "rask")).await),
        ErrorCode::Unauthenticated
    );
    assert_eq!(
        refused(caller(&authenticator, &token(&third, "novel", &idp.base, "alice", "rask")).await),
        ErrorCode::Unauthenticated
    );
    assert_eq!(idp.fetches(), (1, 2), "inside the pace an unknown kid is judged on the set held");

    *moved.lock().unwrap() = Duration::from_secs(31);
    assert_eq!(
        caller(&authenticator, &token(&third, "k3", &idp.base, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
    assert_eq!(idp.fetches(), (1, 3));
}

#[tokio::test]
async fn an_issuer_that_is_down_answers_503_until_it_answers_and_then_its_held_keys_outlive_its_next_outage() {
    let signer = key("k1", Algorithm::RS256);
    let idp = Issuer::plain(&[&signer]).await;
    idp.with(|served| served.down = true);
    let (authenticator, keys, moved) = people(&idp.base, &[]);
    let bearer = token(&signer, "k1", &idp.base, "alice", "rask");

    let failures = keys.refresh().await;
    assert_eq!(failures.len(), 1);
    assert!(failures[0].1.contains("could not be reached"), "{failures:?}");
    assert_eq!(refused(caller(&authenticator, &bearer).await), ErrorCode::ServiceUnavailable);
    assert_eq!(idp.fetches(), (2, 0), "the first request was admitted one discovery of its own");

    idp.with(|served| served.down = false);
    assert_eq!(refused(caller(&authenticator, &bearer).await), ErrorCode::ServiceUnavailable);
    assert_eq!(idp.fetches(), (2, 0), "inside the pace a request costs no fetch");
    *moved.lock().unwrap() = Duration::from_secs(31);
    assert_eq!(caller(&authenticator, &bearer).await.unwrap(), "person dex~alice");
    assert_eq!(idp.fetches(), (3, 1));

    idp.with(|served| served.down = true);
    assert_eq!(keys.refresh().await.len(), 1, "the periodic refresh reports the outage");
    assert_eq!(
        caller(&authenticator, &bearer).await.unwrap(),
        "person dex~alice",
        "and the keys held still serve"
    );
}

#[tokio::test]
async fn discovery_that_names_another_issuer_or_no_allowed_algorithm_leaves_the_issuer_unavailable() {
    let signer = key("k1", Algorithm::ES256);
    let idp = Issuer::plain(&[&signer]).await;
    let (authenticator, keys, moved) = people(&idp.base, &[]);
    let bearer = token(&signer, "k1", &idp.base, "alice", "rask");

    idp.with(|served| served.names_itself = format!("{}/someone-else", served.names_itself));
    assert!(keys.refresh().await[0].1.contains("different issuer"));
    assert_eq!(refused(caller(&authenticator, &bearer).await), ErrorCode::ServiceUnavailable);

    idp.with(|served| {
        served.names_itself = served.jwks_uri.trim_end_matches("/keys").to_owned();
        served.algorithms = vec!["HS256", "ES512"];
    });
    assert!(keys.refresh().await[0].1.contains("no mutually-supported"));
    *moved.lock().unwrap() = Duration::from_secs(31);
    assert_eq!(refused(caller(&authenticator, &bearer).await), ErrorCode::ServiceUnavailable);
    assert_eq!(idp.fetches().1, 0, "no key set was fetched for a document that cannot be used");
}

#[tokio::test]
async fn an_http_issuer_is_fetched_only_where_insecure_issuers_are_allowed() {
    let signer = key("k1", Algorithm::ES256);
    let idp = Issuer::plain(&[&signer]).await;
    let mut settings = settings(&[("RASK_OIDC_ISSUER", &idp.base)]);
    settings.allow_insecure = false;
    let (authenticator, keys) = issuer::authenticator(&settings).unwrap();

    assert!(keys.refresh().await[0].1.contains("must use HTTPS"));
    assert_eq!(
        refused(caller(&authenticator, &token(&signer, "k1", &idp.base, "alice", "rask")).await),
        ErrorCode::ServiceUnavailable
    );
    assert_eq!(idp.fetches(), (0, 0), "nothing was fetched over http");

    settings.allow_insecure = true;
    let (authenticator, keys) = issuer::authenticator(&settings).unwrap();
    assert!(keys.refresh().await.is_empty());
    assert_eq!(
        caller(&authenticator, &token(&signer, "k1", &idp.base, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
}

#[tokio::test]
async fn the_discovery_override_is_where_discovery_and_a_key_set_under_the_issuer_are_fetched() {
    let signer = key("k1", Algorithm::ES256);
    let idp = Issuer::plain(&[&signer]).await;
    let public = "https://idp.public.invalid";
    idp.with(|served| {
        served.names_itself = public.to_owned();
        served.jwks_uri = format!("{public}/keys");
    });
    let (authenticator, keys, _) = people(public, &[("RASK_OIDC_DISCOVERY_URL", &idp.base)]);

    assert!(keys.refresh().await.is_empty());
    assert_eq!(idp.fetches(), (1, 1), "the key set was fetched under the override, not at the public name");
    assert_eq!(
        caller(&authenticator, &token(&signer, "k1", public, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
}

#[tokio::test]
async fn a_service_account_issuer_is_fetched_over_its_own_ca_with_a_bearer_read_on_every_fetch() {
    let directory = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("issuer-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let (ca_file, token_file) = (directory.join("ca.crt"), directory.join("token"));
    std::fs::write(&token_file, "first-token\n").unwrap();
    let (person_key, account_key) = (key("p1", Algorithm::ES256), key("sa1", Algorithm::RS256));
    let idp = Issuer::plain(&[&person_key]).await;
    let cluster = Issuer::private(&[&account_key], &ca_file).await;
    let settings = settings(&[
        ("RASK_OIDC_ISSUER", &idp.base),
        ("RASK_SA_ISSUER", &cluster.base),
        ("RASK_SA_AUDIENCE", "rask-sa"),
        ("RASK_SA_FETCH_TOKEN_FILE", token_file.to_str().unwrap()),
        ("RASK_SA_CA_FILE", ca_file.to_str().unwrap()),
    ]);
    let (authenticator, keys) = issuer::authenticator(&settings).unwrap();

    assert_eq!(keys.refresh().await, vec![]);
    let account = token(&account_key, "sa1", &cluster.base, "system:serviceaccount:jobs:writer", "rask-sa");
    assert_eq!(
        caller(&authenticator, &account).await.unwrap(),
        "service kubernetes~system:serviceaccount:jobs:writer"
    );
    assert_eq!(
        caller(&authenticator, &token(&person_key, "p1", &idp.base, "alice", "rask")).await.unwrap(),
        "person dex~alice"
    );
    let bearer = |token: &str| Some(format!("Bearer {token}"));
    assert_eq!(cluster.with(|served| served.bearers.clone()), vec![bearer("first-token"); 2]);
    assert_eq!(idp.with(|served| served.bearers.clone()), vec![None, None], "a public issuer is sent no bearer");

    std::fs::write(&token_file, "rotated-token").unwrap();
    assert_eq!(keys.refresh().await, vec![]);
    assert_eq!(cluster.with(|served| served.bearers[2..].to_vec()), vec![bearer("rotated-token"); 2]);
}
