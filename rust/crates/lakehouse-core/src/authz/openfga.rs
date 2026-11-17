//! The OpenFGA store, asked over its HTTP API: for a service that holds a client of its own.
//!
//! Every read skips OpenFGA's caches (`HIGHER_CONSISTENCY`), so a revoke is answered by the next read, and carries the
//! check's clock (`current_time`), which a time-boxed grant's condition is evaluated against. Transient failures (the
//! transport, 429, 5xx, a batch item the server could not answer) are retried within a budget; a definitive answer
//! never is. An outage after the retries fails closed, and after [`BREAKER_THRESHOLD`] outages in a row every call is
//! refused at once for [`BREAKER_RESET`], so a request does not pay the retry budget against a store that is down.

use super::{AccessDecider, Answer, CHECKS_PER_BATCH, MOST_PARALLEL_BATCHES, TableDecision, Unavailable, bearer, is_object_id, read_token};
use crate::auth::principal::Principal;
use percent_encoding::percent_decode_str;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const BREAKER_THRESHOLD: u32 = 5;
pub const BREAKER_RESET: Duration = Duration::from_secs(15);
/// OpenFGA's default `listObjectsMaxResults`: a listing that reaches it is likely cut there.
pub const LIST_OBJECTS_SERVER_CAP: usize = 1000;
const CONSISTENCY: &str = "HIGHER_CONSISTENCY";

/// Where the store is and how it is asked.
#[derive(Debug, Clone)]
pub struct StoreSettings {
    pub api_url: String,
    pub store_id: String,
    pub model_id: String,
    /// The pod's projected token for OpenFGA, read on every request; `None` sends no bearer.
    pub token_file: Option<PathBuf>,
    pub timeout: Duration,
    pub attempts: u32,
    pub backoff: Duration,
    pub max_backoff: Duration,
    /// One call's retries never outlast this, so a flapping store cannot pin a request.
    pub budget: Duration,
}

impl StoreSettings {
    pub fn new(api_url: &str, store_id: &str, model_id: &str, token_file: Option<PathBuf>) -> Self {
        StoreSettings {
            api_url: api_url.trim_end_matches('/').to_owned(),
            store_id: store_id.to_owned(),
            model_id: model_id.to_owned(),
            token_file,
            timeout: Duration::from_secs(5),
            attempts: 3,
            backoff: Duration::from_millis(200),
            max_backoff: Duration::from_secs(2),
            budget: Duration::from_secs(15),
        }
    }
}

/// The circuit over the store: closed, it counts outages in a row; open, it refuses every call until it may ask again,
/// and the first call to end then decides.
#[derive(Debug, Default)]
pub struct Circuit {
    outages: u32,
    opened_at: Option<Instant>,
}

impl Circuit {
    pub fn refuses(&self, now: Instant) -> bool {
        self.opened_at.is_some_and(|opened| now.duration_since(opened) < BREAKER_RESET)
    }

    pub fn answered(&mut self) {
        self.outages = 0;
        self.opened_at = None;
    }

    pub fn outage(&mut self, now: Instant) {
        self.outages += 1;
        if self.opened_at.is_some() || self.outages >= BREAKER_THRESHOLD {
            self.opened_at = Some(now);
        }
    }
}

/// Why one request was not answered: `transient` is worth asking again.
struct Failure {
    transient: bool,
    detail: String,
}

impl Failure {
    fn permanent(detail: impl Into<String>) -> Self {
        Failure {
            transient: false,
            detail: detail.into(),
        }
    }
}

/// What a listing found, and whether the server may have cut it at its cap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectListing {
    pub objects: Vec<String>,
    pub truncated: bool,
}

struct Wire {
    settings: StoreSettings,
    client: reqwest::Client,
}

impl Wire {
    /// POST `body` to `route`, pinned to the model where `pinned`: a read of tuples names no model.
    async fn post(&self, route: &str, mut body: Map<String, Value>, pinned: bool) -> Result<Value, Failure> {
        if pinned {
            body.insert("authorization_model_id".to_owned(), Value::String(self.settings.model_id.clone()));
        }
        let url = format!("{}/stores/{}{route}", self.settings.api_url, self.settings.store_id);
        let mut request = self.client.post(url).header(CONTENT_TYPE, "application/json");
        if let Some(path) = &self.settings.token_file {
            let token = read_token(path).map_err(|error| Failure::permanent(error.0))?;
            request = request.header(AUTHORIZATION, bearer(&token).map_err(|error| Failure::permanent(error.0))?);
        }
        let response = request.body(Value::Object(body).to_string()).send().await.map_err(|error| Failure {
            transient: true,
            detail: error.without_url().to_string(),
        })?;
        let status = response.status().as_u16();
        let bytes = response.bytes().await.map_err(|error| Failure {
            transient: true,
            detail: error.without_url().to_string(),
        })?;
        if status != 200 {
            return Err(Failure {
                transient: status == 429 || status >= 500,
                detail: format!("it answered {status}"),
            });
        }
        serde_json::from_slice(&bytes).map_err(|_| Failure::permanent("its answer is no JSON"))
    }
}

/// An OpenFGA client pinned to a store and an authorization model.
pub struct OpenFga {
    wire: Arc<Wire>,
    circuit: Mutex<Circuit>,
}

/// The check's clock, RFC 3339 in UTC: OpenFGA cannot evaluate a condition whose parameters are missing, so a read
/// without it would deny every time-boxed grant.
fn current_time() -> String {
    let since = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let (days, seconds) = ((since.as_secs() / 86_400) as i64, since.as_secs() % 86_400);
    // Days since 1970-01-01 to a civil date (Hinnant's algorithm).
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era = (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 { month_index + 3 } else { month_index - 9 };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    let (hour, minute, second) = (seconds / 3600, seconds % 3600 / 60, seconds % 60);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{:06}Z", since.subsec_micros())
}

fn read_options() -> Map<String, Value> {
    let mut options = Map::new();
    options.insert("context".to_owned(), json!({"current_time": current_time()}));
    options.insert("consistency".to_owned(), Value::String(CONSISTENCY.to_owned()));
    options
}

fn with(mut base: Map<String, Value>, members: Value) -> Map<String, Value> {
    if let Value::Object(members) = members {
        base.extend(members);
    }
    base
}

/// The OpenFGA user a principal key is; a key that names no principal is answered as an outage, since no grant names it
/// and nothing a caller sends reaches here unverified.
fn user_of(subject: &str) -> Result<String, Failure> {
    Principal::parse(subject)
        .map(|principal| principal.fga_user())
        .map_err(|_| Failure::permanent("the subject is no principal"))
}

/// A batch's answers, or why they are not all answers: an item the server could not decide is no denial.
fn batch_answers(answer: &Value, asked: &[(String, String)]) -> Result<Vec<(String, bool)>, Failure> {
    let results = answer
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(|| Failure::permanent("a batch answer holds no result"))?;
    let mut decided = Vec::with_capacity(asked.len());
    let (mut unanswered, mut input_fault) = (0, false);
    for (id, object) in asked {
        let item = results.get(id).and_then(Value::as_object);
        match item.map(|item| (item.get("error").filter(|error| !error.is_null()), item.get("allowed"))) {
            Some((None, allowed)) => decided.push((object.clone(), allowed.and_then(Value::as_bool).unwrap_or(false))),
            Some((Some(error), _)) => {
                unanswered += 1;
                input_fault |= error.get("input_error").is_some_and(|fault| !fault.is_null());
            }
            None => unanswered += 1,
        }
    }
    if unanswered > 0 {
        return Err(Failure {
            transient: !input_fault,
            detail: format!("OpenFGA could not answer {unanswered} batch item(s)"),
        });
    }
    Ok(decided)
}

impl OpenFga {
    pub fn new(settings: StoreSettings) -> Result<Self, String> {
        // No redirect is followed, so the bearer reaches no host but the store's.
        let client = reqwest::Client::builder()
            .timeout(settings.timeout)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| format!("the HTTP client for OpenFGA cannot be built: {error}"))?;
        Ok(OpenFga {
            wire: Arc::new(Wire { settings, client }),
            circuit: Mutex::new(Circuit::default()),
        })
    }

    fn circuit<T>(&self, change: impl FnOnce(&mut Circuit) -> T) -> T {
        let mut circuit = self.circuit.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        change(&mut circuit)
    }

    /// One logical call under the one posture every call shares: retry what is transient, then fail closed.
    async fn guarded<T, F, Fut>(&self, event: &'static str, call: F) -> Result<T, Unavailable>
    where
        F: Fn() -> Fut,
        Fut: Future<Output = Result<T, Failure>>,
    {
        if self.circuit(|circuit| circuit.refuses(Instant::now())) {
            tracing::warn!(event, "openfga_circuit_open");
            return Err(Unavailable::new());
        }
        let settings = &self.wire.settings;
        let began = Instant::now();
        let mut attempt = 1;
        loop {
            match call().await {
                Ok(answer) => {
                    self.circuit(Circuit::answered);
                    return Ok(answer);
                }
                Err(failure) => {
                    let pause = settings.backoff.saturating_mul(2u32.saturating_pow(attempt - 1)).min(settings.max_backoff);
                    if failure.transient && attempt < settings.attempts.max(1) && began.elapsed() + pause < settings.budget {
                        tracing::warn!(event, attempt, "openfga_call_retry");
                        tokio::time::sleep(pause).await;
                        attempt += 1;
                        continue;
                    }
                    if failure.transient {
                        self.circuit(|circuit| circuit.outage(Instant::now()));
                    } else {
                        self.circuit(Circuit::answered);
                    }
                    tracing::error!(event, error = failure.detail, "openfga_unavailable");
                    return Err(Unavailable::new());
                }
            }
        }
    }

    async fn checked(&self, user: &str, relation: &str, object: &str) -> Result<bool, Unavailable> {
        self.guarded("openfga_check_unavailable", || async move {
            let tuple = json!({"tuple_key": {"user": user_of(user)?, "relation": relation, "object": object}});
            let answer = self.wire.post("/check", with(read_options(), tuple), true).await?;
            Ok(answer.get("allowed").and_then(Value::as_bool).unwrap_or(false))
        })
        .await
    }

    /// Every object in batches of [`CHECKS_PER_BATCH`], up to [`MOST_PARALLEL_BATCHES`] at once.
    async fn batch(&self, user: &str, relation: &str, objects: &[String]) -> Result<BTreeMap<String, bool>, Unavailable> {
        let mut allowed: BTreeMap<String, bool> = objects
            .iter()
            .filter(|object| !is_object_id(object))
            .map(|object| (object.clone(), false))
            .collect();
        let askable: Vec<String> = super::distinct(objects).into_iter().filter(|object| is_object_id(object)).collect();
        if askable.is_empty() {
            return Ok(allowed);
        }
        let askable = &askable;
        let answers = self
            .guarded("openfga_batch_check_unavailable", || async move {
                let fga_user = user_of(user)?;
                let mut decided = Vec::with_capacity(askable.len());
                let batches: Vec<Vec<(String, String)>> = askable
                    .iter()
                    .enumerate()
                    .map(|(index, object)| (index.to_string(), object.clone()))
                    .collect::<Vec<_>>()
                    .chunks(CHECKS_PER_BATCH)
                    .map(<[(String, String)]>::to_vec)
                    .collect();
                for wave in batches.chunks(MOST_PARALLEL_BATCHES) {
                    let mut sent = tokio::task::JoinSet::new();
                    for batch in wave {
                        let checks: Vec<Value> = batch
                            .iter()
                            .map(|(id, object)| {
                                json!({"tuple_key": {"user": fga_user, "relation": relation, "object": object}, "context": {"current_time": current_time()}, "correlation_id": id})
                            })
                            .collect();
                        let body = with(Map::new(), json!({"checks": checks, "consistency": CONSISTENCY}));
                        let (wire, batch) = (Arc::clone(&self.wire), batch.clone());
                        sent.spawn(async move { batch_answers(&wire.post("/batch-check", body, true).await?, &batch) });
                    }
                    while let Some(joined) = sent.join_next().await {
                        decided.extend(joined.map_err(|_| Failure::permanent("a batch request was lost"))??);
                    }
                }
                Ok(decided)
            })
            .await?;
        allowed.extend(answers);
        Ok(allowed)
    }

    /// The objects of `object_type` the principal `user` holds `relation` on.
    pub async fn list_objects(&self, user: &str, relation: &str, object_type: &str) -> Result<ObjectListing, Unavailable> {
        self.guarded("openfga_list_objects_unavailable", || async move {
            let question = json!({"user": user_of(user)?, "relation": relation, "type": object_type});
            let answer = self.wire.post("/list-objects", with(read_options(), question), true).await?;
            let objects: Vec<String> = answer
                .get("objects")
                .and_then(Value::as_array)
                .map(|objects| objects.iter().filter_map(Value::as_str).map(str::to_owned).collect())
                .unwrap_or_default();
            let truncated = objects.len() >= LIST_OBJECTS_SERVER_CAP;
            if truncated {
                tracing::warn!(relation, object_type, objects = objects.len(), "openfga_list_objects_possibly_truncated");
            }
            Ok(ObjectListing { objects, truncated })
        })
        .await
    }

    async fn users(&self, relation: &str, object: &str) -> Result<Vec<String>, Unavailable> {
        let (kind, id) = object.split_once(':').unwrap_or((object, ""));
        self.guarded("openfga_list_users_unavailable", || async move {
            let question = json!({"object": {"type": kind, "id": id}, "relation": relation, "user_filters": [{"type": "user"}]});
            let answer = self.wire.post("/list-users", with(read_options(), question), true).await?;
            let subject = |user: &Value| {
                if let Some(found) = user.get("object") {
                    let id = found.get("id").and_then(Value::as_str).unwrap_or_default();
                    let is_user = found.get("type").and_then(Value::as_str) == Some("user");
                    Some(if is_user {
                        percent_decode_str(id).decode_utf8_lossy().into_owned()
                    } else {
                        id.to_owned()
                    })
                } else if let Some(userset) = user.get("userset") {
                    userset.get("id").and_then(Value::as_str).map(str::to_owned)
                } else {
                    user.get("wildcard").map(|_| "*".to_owned())
                }
            };
            let mut subjects: Vec<String> = answer
                .get("users")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(subject)
                .collect();
            subjects.sort();
            Ok(subjects)
        })
        .await
    }

    async fn holds_a_tuple(&self, object: &str) -> Result<bool, Unavailable> {
        if !is_object_id(object) {
            return Ok(false);
        }
        self.guarded("openfga_read_unavailable", || async move {
            let question = json!({"tuple_key": {"object": object}, "page_size": 1, "consistency": CONSISTENCY});
            let answer = self.wire.post("/read", with(Map::new(), question), false).await?;
            Ok(answer.get("tuples").and_then(Value::as_array).is_some_and(|tuples| !tuples.is_empty()))
        })
        .await
    }
}

impl AccessDecider for OpenFga {
    /// No: the store holds uuids and no names.
    fn resolves_names(&self) -> bool {
        false
    }

    fn check<'a>(&'a self, user: &'a str, relation: &'a str, object: &'a str) -> Answer<'a, bool> {
        Box::pin(self.checked(user, relation, object))
    }

    fn batch_check<'a>(&'a self, user: &'a str, relation: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(self.batch(user, relation, objects))
    }

    /// Refused, every one: only the catalog resolves a name.
    fn check_tables<'a>(&'a self, _: &'a str, _: &'a str, tables: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>> {
        Box::pin(async move { Ok(tables.iter().map(|name| (name.clone(), TableDecision::default())).collect()) })
    }

    fn list_users<'a>(&'a self, relation: &'a str, object: &'a str) -> Answer<'a, Vec<String>> {
        Box::pin(self.users(relation, object))
    }

    fn governed<'a>(&'a self, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move {
            let mut governed = BTreeMap::new();
            for object in objects {
                governed.insert(object.clone(), self.holds_a_tuple(object).await?);
            }
            Ok(governed)
        })
    }

    /// An outage, never "holds none": that answer exempts a location from every check, and the store holds no locations.
    fn locate<'a>(&'a self, _: &'a [String]) -> Answer<'a, BTreeMap<String, Option<String>>> {
        Box::pin(async { Err(Unavailable("only the catalog answers which table a location holds".to_owned())) })
    }
}
