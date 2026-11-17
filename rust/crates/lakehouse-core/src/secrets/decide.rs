//! What the session with the secret store decides, as functions of values: the settings, a login's lease, a login's and
//! a read's answer, which names are secret names, how long a retry waits, and the loop's row of unanswered logins.

use reqwest::header::HeaderValue;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::path::PathBuf;
use std::time::Duration;

/// A login is made again once this share of its lease is used, so a login the store does not admit at once has the
/// rest of the lease to be asked again in.
pub const RENEWED_AT: f64 = 0.5;
/// The share of its lease a login is used for; the rest is left for a read in flight and for clocks that differ.
pub const LEASE_USED: f64 = 0.9;
/// The shortest lease a login is taken with: the loop that wakes every 15 s is then sure of one pass that asks the store
/// and one that logs in before the token is used up.
pub const SHORTEST_LEASE_SECONDS: i64 = 60;
/// The longest lease a login is taken with, the hour a store token lives at most: a longer one, or one that never
/// expires, is a credential that does not rotate.
pub const LONGEST_LEASE_SECONDS: i64 = 3600;
/// The longest row of logins the loop counts: the passes sat out double (none, 1, 3, 7, 15) and stay at fifteen.
pub const LONGEST_UNANSWERED_ROW: u32 = 5;
/// The longest wait between two attempts, in seconds.
pub const LONGEST_BACKOFF_SECONDS: f64 = 15.0;

/// Where the session's store is, and who the process logs in to it as.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretStoreSettings {
    /// Empty for a process with no secret store: it reads no secret, signs nothing and keys nothing.
    pub address: String,
    pub role: String,
    pub auth_mount: String,
    pub kv_mount: String,
    /// The projected ServiceAccount token a login presents, read at each login because the kubelet rotates it.
    pub token_file: Option<PathBuf>,
    /// The only CA the store's certificate is checked against, where it is under a private CA.
    pub ca_file: Option<PathBuf>,
}

impl SecretStoreSettings {
    /// The settings `environment` holds, its names read without regard to case, or why they cannot be taken.
    pub fn from_environment(environment: impl IntoIterator<Item = (String, String)>) -> Result<Self, String> {
        let environment: HashMap<String, String> = environment.into_iter().map(|(name, value)| (name.to_uppercase(), value)).collect();
        let text = |name: &str, default: &str| environment.get(name).map_or_else(|| default.to_owned(), Clone::clone);
        let settings = SecretStoreSettings {
            address: text("RASK_SECRET_STORE_ADDR", ""),
            role: text("RASK_SECRET_STORE_ROLE", ""),
            auth_mount: text("RASK_SECRET_STORE_AUTH_MOUNT", "kubernetes"),
            kv_mount: text("RASK_SECRET_STORE_KV_MOUNT", "secret"),
            token_file: environment.get("RASK_SECRET_STORE_TOKEN_FILE").map(PathBuf::from),
            ca_file: environment.get("RASK_SECRET_STORE_CA_FILE").map(PathBuf::from),
        };
        if settings.configured() && (settings.role.is_empty() || settings.token_file.is_none()) {
            return Err(
                "RASK_SECRET_STORE_ADDR names a store this process logs in to as itself: set RASK_SECRET_STORE_ROLE to its role there \
                 and RASK_SECRET_STORE_TOKEN_FILE to its projected token"
                    .to_owned(),
            );
        }
        Ok(settings)
    }

    /// The settings this process's environment holds; a variable that is not text is not one of them.
    pub fn from_process() -> Result<Self, String> {
        Self::from_environment(std::env::vars_os().filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?))))
    }

    /// Whether these settings name a store.
    pub fn configured(&self) -> bool {
        !self.address.is_empty()
    }
}

/// When a login asked at some instant is made again, and when it stops being used, as offsets from that instant.
pub fn lease_offsets(lease_seconds: i64) -> (Duration, Duration) {
    let lease = lease_seconds as f64;
    (Duration::from_secs_f64(lease * RENEWED_AT), Duration::from_secs_f64(lease * LEASE_USED))
}

/// Whether `credential` is visible ASCII, which an HTTP header carries as it stands. It is checked before it is sent,
/// because a client that refuses a header value quotes it in its error, and the error reaches a log.
pub fn fits_a_header(credential: &str) -> bool {
    !credential.is_empty() && credential.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

/// Whether `name` is a secret name: it becomes a segment of the store's path, so only what the chart's names are made of.
pub fn is_secret_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    bytes.next().is_some_and(|first| first.is_ascii_lowercase() || first.is_ascii_digit())
        && name.len() <= 253
        && bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

/// A token the store issued. It shows nothing of itself when printed, and leaves only as a header marked sensitive.
#[derive(Clone, PartialEq, Eq)]
pub struct StoreToken(String);

impl StoreToken {
    pub fn new(token: &str) -> Self {
        StoreToken(token.to_owned())
    }

    /// The header a call under this token carries; a token is held only once it fits one, so this cannot fail.
    pub fn header(&self) -> Option<HeaderValue> {
        let mut header = HeaderValue::from_str(&self.0).ok()?;
        header.set_sensitive(true);
        Some(header)
    }
}

impl fmt::Debug for StoreToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("StoreToken(..)")
    }
}

/// What a login the store admitted issued: its token and its lease in whole seconds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issued {
    pub token: StoreToken,
    pub lease_seconds: i64,
}

/// The token and lease a login's answer issues, or nothing where it names no token this process takes: one an HTTP
/// header carries, with a lease of a minute to an hour written as a whole JSON number.
pub fn parse_login(body: &[u8]) -> Option<Issued> {
    let answer: Value = serde_json::from_slice(body).ok()?;
    let auth = answer.get("auth")?;
    let token = auth.get("client_token")?.as_str().filter(|token| fits_a_header(token))?;
    let lease = match auth.get("lease_duration")? {
        Value::Number(number) => match number.as_i64() {
            Some(whole) => whole,
            None => number.as_f64().filter(|float| float.fract() == 0.0 && float.abs() < 1e15)? as i64,
        },
        _ => return None,
    };
    (SHORTEST_LEASE_SECONDS..=LONGEST_LEASE_SECONDS).contains(&lease).then(|| Issued {
        token: StoreToken::new(token),
        lease_seconds: lease,
    })
}

/// The fields a KV v2 read answered, or nothing where the answer is not one: every field rask reads is a string, so an
/// answer holding any other value is refused whole rather than read as text it never was.
pub fn parse_read(body: &[u8]) -> Option<BTreeMap<String, String>> {
    let answer: Value = serde_json::from_slice(body).ok()?;
    answer
        .get("data")?
        .get("data")?
        .as_object()?
        .iter()
        .map(|(name, value)| Some((name.clone(), value.as_str()?.to_owned())))
        .collect()
}

/// How long the wait after failed attempt `attempt` (from 1) is: `initial` doubled per attempt, plus a jitter of up to
/// a second (`draw` in [0, 1)), at most `LONGEST_BACKOFF_SECONDS`.
pub fn backoff_seconds(attempt: u32, initial: f64, draw: f64) -> f64 {
    let exponent = i32::try_from(attempt.saturating_sub(1)).unwrap_or(i32::MAX);
    (initial * 2f64.powi(exponent) + draw).clamp(0.0, LONGEST_BACKOFF_SECONDS)
}

/// What the loop says of an outage, once until the store next answers a question about its token.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Said {
    /// The store could not be asked about the held token.
    TokenNotChecked,
    /// A login made on a refusal was refused the question too: no dropped token explains that.
    TokenCannotAskAboutItself,
    /// The store refuses the held token the question: it dropped it.
    TokenDropped,
    /// A login the loop asked for was not made, and the held one is still in use.
    LoginNotRenewed,
    /// A login the loop asked for was not made, and the held one has run out.
    LoginLapsed,
}

impl Said {
    pub fn event(self) -> &'static str {
        match self {
            Said::TokenNotChecked => "secret_store_token_not_checked",
            Said::TokenCannotAskAboutItself => "secret_store_token_cannot_ask_about_itself",
            Said::TokenDropped => "secret_store_token_dropped",
            Said::LoginNotRenewed => "secret_store_login_not_renewed",
            Said::LoginLapsed => "secret_store_login_lapsed",
        }
    }

    /// An error where reads have no login, or where no dropped token explains a refusal; else a warning.
    pub fn is_error(self) -> bool {
        matches!(self, Said::TokenCannotAskAboutItself | Said::LoginLapsed)
    }
}

/// The loop's row of logins the store has not answered, the passes it sits out before the next, and what it has said.
/// Only the store's answer ends the row.
#[derive(Debug, Default)]
pub struct Row {
    unanswered: u32,
    sitting_out: u32,
    said: BTreeSet<Said>,
}

impl Row {
    fn say(&mut self, said: Said) -> Option<Said> {
        self.said.insert(said).then_some(said)
    }

    /// The store could not be asked: a pass sat out is a pass, answered or not.
    pub fn unchecked(&mut self) -> Option<Said> {
        self.sitting_out = self.sitting_out.saturating_sub(1);
        self.say(Said::TokenNotChecked)
    }

    /// The store answered the held token: the row ends, and the next outage is said again.
    pub fn honoured(&mut self) {
        *self = Row::default();
    }

    /// The store refused the held token the question: whether this pass logs in, and what it says.
    pub fn refused(&mut self) -> (bool, Option<Said>) {
        if self.unanswered == 0 {
            return (true, self.say(Said::TokenDropped));
        }
        let said = self.say(Said::TokenCannotAskAboutItself);
        if self.sitting_out > 0 {
            self.sitting_out -= 1;
            return (false, said);
        }
        (true, said)
    }

    /// A login the pass asked for was not made; `usable` is whether the held one is still in use.
    pub fn not_renewed(&mut self, usable: bool) -> Option<Said> {
        self.say(if usable { Said::LoginNotRenewed } else { Said::LoginLapsed })
    }

    /// A login was made, on a refusal or not: one made in a row the store has not answered counts in it.
    pub fn logged_in(&mut self, on_refusal: bool) {
        if on_refusal || self.unanswered > 0 {
            self.unanswered = (self.unanswered + 1).min(LONGEST_UNANSWERED_ROW);
            self.sitting_out = (1 << (self.unanswered - 1)) - 1;
        }
    }
}
