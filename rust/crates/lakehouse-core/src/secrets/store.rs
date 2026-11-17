//! The session with the secret store: a login held for its lease, reads under it, and the loop that keeps it.

use super::decide::{self, Issued, Row, Said, SecretStoreSettings};
use crate::auth::issuer::Clock;
use reqwest::StatusCode;
use std::collections::BTreeMap;
use std::collections::hash_map::RandomState;
use std::fmt;
use std::future::Future;
use std::hash::BuildHasher;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tokio::time::Instant as Deadline;

/// Where a token asks the store about itself, under `/v1/`: a 403 there is a token the store no longer honours.
pub const ASKS_ABOUT_ITSELF: &str = "auth/token/lookup-self";
/// How often the loop asks the store about its token and looks whether its login is due.
pub const RENEWAL_WAKE: Duration = Duration::from_secs(15);
/// How long after the loop's last pass began a read still leaves the login to it: four passes.
pub const LOOP_SILENT: Duration = Duration::from_secs(60);
/// The bound on the readiness report's one question to the store.
pub const ANSWER_TIMEOUT: Duration = Duration::from_secs(2);
const TOKEN_HEADER: &str = "X-Vault-Token";

/// How long a call may ask: each wait of one attempt is bounded by `timeout`, `retries` attempts in all, the first wait
/// between two `backoff`, doubling.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Budget {
    pub timeout: Duration,
    pub retries: u32,
    pub backoff: Duration,
}

impl Budget {
    /// Ten attempts of five seconds from a backoff of three, about two minutes: a store still being seeded is waited out.
    pub const BOOT: Budget = Budget {
        timeout: Duration::from_secs(5),
        retries: 10,
        backoff: Duration::from_secs(3),
    };

    /// One attempt, for a caller on a request path.
    pub fn one_attempt(timeout: Duration) -> Budget {
        Budget {
            timeout,
            retries: 1,
            backoff: Duration::ZERO,
        }
    }
}

/// An attempt the store did not serve: it answered a status other than success, did not answer, or this module found
/// its answer, the projected token or the time left wanting. None of them quotes a token or a secret.
#[derive(Debug, Clone, PartialEq, Eq)]
enum NotServed {
    Status(StatusCode),
    Transport(String),
    Own(&'static str),
}

impl fmt::Display for NotServed {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NotServed::Status(status) => write!(formatter, "the secret store answered {status}"),
            NotServed::Transport(reason) => write!(formatter, "the secret store did not answer: {reason}"),
            NotServed::Own(reason) => formatter.write_str(reason),
        }
    }
}

impl From<reqwest::Error> for NotServed {
    fn from(error: reqwest::Error) -> Self {
        NotServed::Transport(error.without_url().to_string())
    }
}

/// Why the readiness report does not count the store as serving this process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Unanswered {
    /// The store answered this status to the login or the question.
    Status(u16),
    /// The store did not answer.
    NoAnswer(String),
    /// The session holds no usable login: the projected token, the login's answer or the time fell short, or the loop
    /// that makes the next login is passing.
    NoUsableLogin(String),
    /// The store refuses the token the question about itself: it dropped the login, or the policy lacks the grant.
    TokenRefused,
}

impl From<NotServed> for Unanswered {
    fn from(not_served: NotServed) -> Self {
        match not_served {
            NotServed::Status(status) => Unanswered::Status(status.as_u16()),
            NotServed::Transport(reason) => Unanswered::NoAnswer(reason),
            NotServed::Own(reason) => Unanswered::NoUsableLogin(reason.to_owned()),
        }
    }
}

/// Why a required secret was refused: this process has no store, the store did not serve it or it lacks the field,
/// or the name is no secret name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refused {
    NoStore,
    NotServed { secret: String, require: String },
    NoSecretName(String),
}

impl fmt::Display for Refused {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Refused::NoStore => formatter.write_str("this process is configured with no secret store (RASK_SECRET_STORE_ADDR is unset)"),
            Refused::NotServed { secret, require } => write!(
                formatter,
                "secret {secret:?} gave this process no {require:?}: failing closed (the secret store is the sole source)"
            ),
            Refused::NoSecretName(name) => write!(formatter, "{name:?} is no secret name: only [a-z0-9-] is read"),
        }
    }
}

/// What one pass of the loop did: whether it logged in, and what it said of an outage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pass {
    pub logged_in: bool,
    pub said: Vec<Said>,
}

/// A login the store admitted, with when the loop makes the next one and when this process stops using it.
#[derive(Debug)]
struct Login {
    issued: Issued,
    renew_after: Instant,
    expires: Instant,
}

/// The source of the backoff's jitter, a draw in [0, 1); injectable, so a test waits the same on every run.
pub type Jitter = Arc<dyn Fn() -> f64 + Send + Sync>;

/// One process's session with its secret store.
pub struct SecretStore {
    settings: SecretStoreSettings,
    base: String,
    client: reqwest::Client,
    clock: Clock,
    jitter: Jitter,
    held: Mutex<Option<Arc<Login>>>,
    /// The one login in flight: a reader that holds no usable login waits for it, no longer than its own seconds.
    one_login: tokio::sync::Mutex<()>,
    closed: watch::Sender<bool>,
    last_pass: Mutex<Option<Instant>>,
    row: Mutex<Row>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

fn random_draw() -> f64 {
    (RandomState::new().hash_one(Instant::now()) >> 11) as f64 / (1u64 << 53) as f64
}

impl SecretStore {
    /// A session that has logged in to nothing yet, or why `settings` cannot open one.
    pub fn new(settings: SecretStoreSettings) -> Result<Self, String> {
        if !settings.configured() || settings.token_file.is_none() {
            return Err("no secret store is configured: RASK_SECRET_STORE_ADDR is unset".to_owned());
        }
        // No proxy and no redirect: a login carries the pod's identity token, so only the declared address receives it.
        let mut client = reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).no_proxy();
        if let Some(ca_file) = &settings.ca_file {
            let shown = ca_file.display();
            let pem = std::fs::read(ca_file).map_err(|error| format!("the CA file {shown} cannot be read: {error}"))?;
            let certificates = reqwest::Certificate::from_pem_bundle(&pem).map_err(|error| format!("the CA file {shown} holds no certificate: {error}"))?;
            client = client.tls_certs_only(certificates);
        }
        Ok(SecretStore {
            base: settings.address.trim_end_matches('/').to_owned(),
            client: client
                .build()
                .map_err(|error| format!("the secret store's HTTP client cannot be built: {error}"))?,
            settings,
            clock: Arc::new(Instant::now),
            jitter: Arc::new(random_draw),
            held: Mutex::default(),
            one_login: tokio::sync::Mutex::default(),
            closed: watch::Sender::new(false),
            last_pass: Mutex::default(),
            row: Mutex::default(),
        })
    }

    /// The clock a login's lease and the loop's passes are measured on.
    pub fn with_clock(self, clock: Clock) -> Self {
        SecretStore { clock, ..self }
    }

    pub fn with_jitter(self, jitter: Jitter) -> Self {
        SecretStore { jitter, ..self }
    }

    /// Log in, asking again within `budget` until the store has admitted this process and answered its token about
    /// itself. The login is held for the reads that follow.
    pub async fn log_in(&self, budget: Budget) -> Result<(), String> {
        self.asking_again(budget, || self.log_in_once(budget.timeout)).await.map_err(|error| {
            format!(
                "the secret store gave this process no usable login as its role {:?}: {error}",
                self.settings.role
            )
        })
    }

    /// One pass of the loop: log in again once half the held login's lease is used, or on the pass that finds the store
    /// refusing it, each login the store then refuses in turn waiting twice the passes the last did.
    pub async fn keep_logged_in(&self, timeout: Duration) -> Pass {
        let now = (self.clock)();
        *locked(&self.last_pass) = Some(now);
        let held = locked(&self.held).clone();
        let mut said = Vec::new();
        let mut deadline = Deadline::now() + timeout;
        let mut on_refusal = false;
        if let Some(login) = held.as_ref().filter(|login| now < login.renew_after) {
            let answer = self.honoured(login, deadline).await;
            let mut row = locked(&self.row);
            match answer {
                Err(error) => {
                    said.extend(row.unchecked().inspect(|said| say(*said, &error)));
                    return Pass { logged_in: false, said };
                }
                Ok(true) => {
                    row.honoured();
                    return Pass { logged_in: false, said };
                }
                Ok(false) => {
                    let (logs_in, refusal) = row.refused();
                    said.extend(refusal.inspect(|said| say(*said, &self.settings.role)));
                    if !logs_in {
                        return Pass { logged_in: false, said };
                    }
                }
            }
            on_refusal = true;
            deadline = Deadline::now() + timeout;
        }
        match self.renew(held.as_ref(), deadline).await {
            Ok(logged_in) => {
                if logged_in {
                    locked(&self.row).logged_in(on_refusal);
                }
                Pass { logged_in, said }
            }
            Err(error) => {
                let usable = held.is_some_and(|login| (self.clock)() < login.expires);
                said.extend(locked(&self.row).not_renewed(usable).inspect(|said| say(*said, &error)));
                Pass { logged_in: false, said }
            }
        }
    }

    /// The fields of secret `name`, or an empty map (logged) when the store did not serve it within `budget`. What a
    /// miss costs is the caller's to say: a start that requires the secret fails on it (`read_required`).
    pub async fn read(&self, name: &str, budget: Budget) -> Result<BTreeMap<String, String>, Refused> {
        if !decide::is_secret_name(name) {
            return Err(Refused::NoSecretName(name.to_owned()));
        }
        match self.asking_again(budget, || self.read_once(name, budget.timeout)).await {
            Ok(fields) => Ok(fields),
            Err(error) => {
                tracing::warn!(secret = name, error = %error, "secret_store_read_failed");
                Ok(BTreeMap::new())
            }
        }
    }

    /// The fields of secret `name`, refused unless the store served it with a non-empty `require`: the store is the sole
    /// source, so a miss must not start a process on an empty key.
    pub async fn read_required(&self, name: &str, require: &str, budget: Budget) -> Result<BTreeMap<String, String>, Refused> {
        let fields = self.read(name, budget).await?;
        if fields.get(require).is_none_or(String::is_empty) {
            return Err(Refused::NotServed {
                secret: name.to_owned(),
                require: require.to_owned(),
            });
        }
        Ok(fields)
    }

    /// Whether the store admits this process and answers its token about itself: what the readiness report shows.
    pub async fn answers(&self) -> Result<(), Unanswered> {
        let deadline = Deadline::now() + ANSWER_TIMEOUT;
        let login = self.login(deadline).await?;
        match self.honoured(&login, deadline).await? {
            true => Ok(()),
            false => Err(Unanswered::TokenRefused),
        }
    }

    /// End every wait of an attempt in flight; no attempt starts after it. The store token expires by itself.
    pub fn close(&self) {
        self.closed.send_replace(true);
    }

    async fn log_in_once(&self, timeout: Duration) -> Result<(), NotServed> {
        let deadline = Deadline::now() + timeout;
        let login = self.login(deadline).await?;
        if self.honoured(&login, deadline).await? {
            return Ok(());
        }
        // Dropped without waiting: a login in flight holds the guard, and its outcome is the next attempt's to judge.
        if let Ok(_guard) = self.one_login.try_lock() {
            let mut held = locked(&self.held);
            if held.as_ref().is_some_and(|current| Arc::ptr_eq(current, &login)) {
                *held = None;
            }
        }
        Err(NotServed::Own(
            "the store admitted the login and refuses its token the question about itself: it was replaced since, or the role's policy \
             does not grant read on auth/token/lookup-self",
        ))
    }

    async fn read_once(&self, name: &str, timeout: Duration) -> Result<BTreeMap<String, String>, NotServed> {
        let deadline = Deadline::now() + timeout;
        let login = self.login(deadline).await?;
        let path = format!("/v1/{}/data/{name}", self.settings.kv_mount);
        let body = self.get(&path, &login, deadline).await?;
        decide::parse_read(&body).ok_or(NotServed::Own("the secret store's answer is not a KV v2 read of string fields"))
    }

    async fn get(&self, path: &str, login: &Login, deadline: Deadline) -> Result<Vec<u8>, NotServed> {
        let response = self.call(path, login, deadline).await?;
        if !response.status().is_success() {
            return Err(NotServed::Status(response.status()));
        }
        Ok(response.bytes().await?.to_vec())
    }

    async fn call(&self, path: &str, login: &Login, deadline: Deadline) -> Result<reqwest::Response, NotServed> {
        let header = login
            .issued
            .token
            .header()
            .ok_or(NotServed::Own("the held token is not one an HTTP header carries"))?;
        let request = self.client.get(format!("{}{path}", self.base)).header(TOKEN_HEADER, header);
        Ok(request.timeout(self.left(deadline)?).send().await?)
    }

    /// Whether the store answers `login` about itself: 403 for a token it revoked, one a replaced store never issued, and
    /// one whose policy does not grant the question.
    async fn honoured(&self, login: &Login, deadline: Deadline) -> Result<bool, NotServed> {
        let response = self.call(&format!("/v1/{ASKS_ABOUT_ITSELF}"), login, deadline).await?;
        match response.status() {
            StatusCode::FORBIDDEN => Ok(false),
            status if status.is_success() => Ok(true),
            status => Err(NotServed::Status(status)),
        }
    }

    fn usable(&self) -> Option<Arc<Login>> {
        locked(&self.held).clone().filter(|login| (self.clock)() < login.expires)
    }

    /// The login to read under: a usable one at once, else the outcome of the one in flight, else one made now, unless
    /// the loop is passing, whose next pass then makes it.
    async fn login(&self, deadline: Deadline) -> Result<Arc<Login>, NotServed> {
        if let Some(login) = self.usable() {
            return Ok(login);
        }
        let _guard = self.one_login_within(deadline).await?;
        if let Some(login) = self.usable() {
            return Ok(login);
        }
        let now = (self.clock)();
        if locked(&self.last_pass).is_some_and(|last| now.saturating_duration_since(last) < LOOP_SILENT) {
            return Err(NotServed::Own(
                "this process holds no usable login to the secret store: its loop makes the next one, at its next pass",
            ));
        }
        let login = Arc::new(self.log_in_now(deadline).await?);
        *locked(&self.held) = Some(Arc::clone(&login));
        Ok(login)
    }

    /// The loop's login, made unless a reader replaced `held` while the pass waited for the one in flight.
    async fn renew(&self, held: Option<&Arc<Login>>, deadline: Deadline) -> Result<bool, NotServed> {
        let _guard = self.one_login_within(deadline).await?;
        let current = locked(&self.held).clone();
        if current.as_ref().map(Arc::as_ptr) != held.map(Arc::as_ptr) {
            return Ok(false);
        }
        let login = self.log_in_now(deadline).await?;
        *locked(&self.held) = Some(Arc::new(login));
        Ok(true)
    }

    async fn one_login_within(&self, deadline: Deadline) -> Result<tokio::sync::MutexGuard<'_, ()>, NotServed> {
        tokio::time::timeout(self.left(deadline)?, self.one_login.lock())
            .await
            .map_err(|_| NotServed::Own("another reader's login to the secret store was still in flight when this attempt ran out of time"))
    }

    async fn log_in_now(&self, deadline: Deadline) -> Result<Login, NotServed> {
        let settings = &self.settings;
        // The kubelet rotates the projected token under the process, so it is read at every login and kept nowhere.
        let projected = settings
            .token_file
            .as_ref()
            .and_then(|path| std::fs::read_to_string(path).ok())
            .ok_or(NotServed::Own("the projected token is unreadable"))?;
        let asked = (self.clock)();
        let url = format!("{}/v1/auth/{}/login", self.base, settings.auth_mount);
        let body = serde_json::json!({ "role": settings.role, "jwt": projected.trim_matches(python_whitespace) });
        let response = self.client.post(url).json(&body).timeout(self.left(deadline)?).send().await?;
        if !response.status().is_success() {
            return Err(NotServed::Status(response.status()));
        }
        let issued = decide::parse_login(&response.bytes().await?).ok_or(NotServed::Own(
            "the secret store's login answer names no token this process takes: one an HTTP header carries, with a lease of 60 to 3600 s \
             (the role's token_ttl)",
        ))?;
        let (renew_after, expires) = decide::lease_offsets(issued.lease_seconds);
        tracing::info!(role = settings.role, lease_seconds = issued.lease_seconds, "secret_store_logged_in");
        Ok(Login {
            issued,
            renew_after: asked + renew_after,
            expires: asked + expires,
        })
    }

    /// `attempt`, asked again on whatever the store did not serve, `budget.retries` attempts in all, with jittered
    /// exponential backoff. Closing the session wakes a call from its wait between two attempts and ends its asking.
    async fn asking_again<T, Attempt>(&self, budget: Budget, attempt: impl Fn() -> Attempt) -> Result<T, NotServed>
    where
        Attempt: Future<Output = Result<T, NotServed>>,
    {
        let mut closed = self.closed.subscribe();
        let mut made = 0;
        loop {
            made += 1;
            let error = match attempt().await {
                Ok(value) => return Ok(value),
                Err(error) => error,
            };
            if made >= budget.retries || *closed.borrow() {
                return Err(error);
            }
            let wait = decide::backoff_seconds(made, budget.backoff.as_secs_f64(), (self.jitter)());
            tracing::warn!(attempt = made, wait, error = %error, "secret_store_asking_again");
            tokio::select! {
                () = tokio::time::sleep(Duration::from_secs_f64(wait)) => {}
                _ = closed.wait_for(|closed| *closed) => return Err(error),
            }
        }
    }

    /// The seconds an attempt has left before `deadline`: none once the session is closed.
    fn left(&self, deadline: Deadline) -> Result<Duration, NotServed> {
        if *self.closed.borrow() {
            return Err(NotServed::Own("the session with the secret store is closed"));
        }
        Some(deadline.saturating_duration_since(Deadline::now()))
            .filter(|left| !left.is_zero())
            .ok_or(NotServed::Own("the attempt ran out of time before the secret store answered"))
    }
}

fn say(said: Said, detail: &dyn fmt::Display) {
    if said.is_error() {
        tracing::error!(detail = %detail, "{}", said.event());
    } else {
        tracing::warn!(detail = %detail, "{}", said.event());
    }
}

/// The fields of secret `name` from this process's session, refused where the process has no store, the store did not
/// serve it, or it holds no non-empty `require`.
pub async fn read_required_secret(store: Option<&SecretStore>, name: &str, require: &str, budget: Budget) -> Result<BTreeMap<String, String>, Refused> {
    store.ok_or(Refused::NoStore)?.read_required(name, require, budget).await
}

/// A process's session kept logged in by its loop for as long as the value lives.
pub struct Session {
    pub store: Arc<SecretStore>,
    keeper: JoinHandle<()>,
}

impl Session {
    /// Log in with the boot budget and keep the login on a loop that passes every `wake`, or refuse: a wrong role, a
    /// token the role's audience refuses, a CA that does not sign the store's certificate, a lease outside the minute to
    /// the hour, a policy without the question and a store that never comes up all fail the start closed.
    pub async fn open(store: Arc<SecretStore>, wake: Duration) -> Result<Session, String> {
        if let Err(error) = store.log_in(Budget::BOOT).await {
            store.close();
            return Err(error);
        }
        let kept = Arc::clone(&store);
        let keeper = tokio::spawn(async move {
            loop {
                tokio::time::sleep(wake).await;
                kept.keep_logged_in(Budget::BOOT.timeout).await;
            }
        });
        Ok(Session { store, keeper })
    }

    /// The session `settings` name, or none for a process the chart named no store.
    pub async fn from_settings(settings: SecretStoreSettings) -> Result<Option<Session>, String> {
        if !settings.configured() {
            return Ok(None);
        }
        Session::open(Arc::new(SecretStore::new(settings)?), RENEWAL_WAKE).await.map(Some)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.keeper.abort();
        self.store.close();
    }
}
