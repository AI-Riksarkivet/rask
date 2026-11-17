//! Authorization: the questions a governed door asks, and the two places they are answered.
//!
//! OpenFGA authenticates its callers and authorizes none of them, so the store has as few clients as the estate has
//! writers of grants. A service that writes none (lineage) asks the catalog's decision door instead
//! ([`catalog::CatalogDecider`]); one that holds a store client asks the store ([`openfga::OpenFga`]). Both fail closed
//! the same way: an answer that cannot be had is [`Unavailable`], never an allow and never a deny.

pub mod catalog;
pub mod openfga;

use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::path::Path;
use std::pin::Pin;

/// The most objects one question to the catalog's decision door may name; a longer question is asked in chunks.
pub const MAX_OBJECTS_PER_QUESTION: usize = 1000;
/// The checks one OpenFGA batch request carries, OpenFGA's default `maxChecksPerBatchCheck`.
pub const CHECKS_PER_BATCH: usize = 50;
/// The most batch requests one batch check sends at once: a whole question of 1,000 objects in one wave.
pub const MOST_PARALLEL_BATCHES: usize = 20;
/// What every outage is told as: a caller must not learn which authorization call failed.
pub const UNAVAILABLE: &str = "authorization service unavailable";

/// The authority could not answer. What went wrong is logged by the shell and never told to the caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unavailable(pub String);

impl Unavailable {
    pub fn new() -> Self {
        Unavailable(UNAVAILABLE.to_owned())
    }
}

impl Default for Unavailable {
    fn default() -> Self {
        Unavailable::new()
    }
}

/// The answer about one table asked about by name: whether the subject holds the relation on the table the name names
/// now, and, only when it does, that table's uuid and its namespace's.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct TableDecision {
    pub allowed: bool,
    #[serde(default)]
    pub table_uuid: Option<String>,
    #[serde(default)]
    pub namespace_uuid: Option<String>,
}

impl TableDecision {
    /// Whether the decision is one a door can give: a table's uuid exactly when it is allowed, a namespace's only then.
    pub fn is_whole(&self) -> bool {
        self.allowed == self.table_uuid.is_some() && (self.allowed || self.namespace_uuid.is_none())
    }
}

/// An answer a decider gives in time, or its outage.
pub type Answer<'a, T> = Pin<Box<dyn Future<Output = Result<T, Unavailable>> + Send + 'a>>;

/// The authorization questions a check-only door asks. `user` is a principal key, as a caller's subject.
pub trait AccessDecider: Send + Sync {
    /// Whether [`AccessDecider::check_tables`] answers a table by its name: then a refusal there is the answer a
    /// DescribeTable as that user gives.
    fn resolves_names(&self) -> bool;
    fn check<'a>(&'a self, user: &'a str, relation: &'a str, object: &'a str) -> Answer<'a, bool>;
    fn batch_check<'a>(&'a self, user: &'a str, relation: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>>;
    fn check_tables<'a>(&'a self, user: &'a str, relation: &'a str, tables: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>>;
    fn list_users<'a>(&'a self, relation: &'a str, object: &'a str) -> Answer<'a, Vec<String>>;
    /// Whether any tuple names each object: a denial on an object nothing governs can never be repaired by a grant.
    fn governed<'a>(&'a self, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>>;
    /// Which governed table each storage location holds, or `None` where it holds none.
    fn locate<'a>(&'a self, locations: &'a [String]) -> Answer<'a, BTreeMap<String, Option<String>>>;
}

/// The object a table's grants are held by. No name reaches OpenFGA: a name changes on a rename, a uuid never does.
pub fn table_object(uuid: &str) -> String {
    format!("table:{uuid}")
}

pub fn namespace_object(uuid: &str) -> String {
    format!("namespace:{uuid}")
}

/// Whether OpenFGA accepts `object` as an object id at all; one it refuses is answered "not granted" without asking,
/// since no tuple can name it and sending it would fail the request for every object beside it.
pub fn is_object_id(object: &str) -> bool {
    let (kind, identifier) = object.split_once(':').unwrap_or((object, ""));
    let runes = object.chars().count();
    (2..=256).contains(&runes)
        && !kind.is_empty()
        && !identifier.is_empty()
        && !identifier.contains(':')
        && !object.contains('#')
        && !object.contains(' ')
        && !object.chars().any(char::is_control)
}

/// `objects` without repeats, first seen first.
pub fn distinct(objects: &[String]) -> Vec<String> {
    let mut seen = BTreeSet::new();
    objects.iter().filter(|object| seen.insert(object.as_str())).cloned().collect()
}

/// `objects` without repeats, in chunks of `size`.
pub fn chunks(objects: &[String], size: usize) -> Vec<Vec<String>> {
    distinct(objects).chunks(size.max(1)).map(<[String]>::to_vec).collect()
}

/// `answer` when it answers exactly `asked`: one that leaves an object out, or names one nobody asked about, is an outage.
pub fn complete<V>(answer: BTreeMap<String, V>, asked: &[String]) -> Result<BTreeMap<String, V>, Unavailable> {
    let asked: BTreeSet<&str> = asked.iter().map(String::as_str).collect();
    if answer.len() != asked.len() || !answer.keys().all(|key| asked.contains(key.as_str())) {
        tracing::error!(asked = asked.len(), answered = answer.len(), "decision_answer_incomplete");
        return Err(Unavailable::new());
    }
    Ok(answer)
}

/// A projected token, read now: the kubelet rotates it under the running process, so a copy would stop authenticating.
pub fn read_token(path: &Path) -> Result<String, Unavailable> {
    let token = std::fs::read_to_string(path).map_err(|_| Unavailable(format!("the identity token at {} cannot be read", path.display())))?;
    let token = token.trim();
    if token.is_empty() {
        return Err(Unavailable(format!("the identity token at {} is empty", path.display())));
    }
    Ok(token.to_owned())
}

/// The bearer header for `token`, marked sensitive so no log or debug print shows it.
pub(crate) fn bearer(token: &str) -> Result<reqwest::header::HeaderValue, Unavailable> {
    let mut header =
        reqwest::header::HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| Unavailable("the identity token is no header value".to_owned()))?;
    header.set_sensitive(true);
    Ok(header)
}
