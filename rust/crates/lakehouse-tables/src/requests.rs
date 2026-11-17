//! The requests the client puts to the catalog's doors, built as values: which door, what query, what body.
//!
//! Every door is a `POST /v1/table/{id}/<action>` of the Lance Namespace REST spec (`lance_docs/ns_catalog/spec.yaml`),
//! the table named by its segments joined with the `$` delimiter and passed again as the `delimiter` query parameter.

use lakehouse_core::naming::CATALOG_DELIMITER;
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use serde_json::{Value, json};

/// The media type of an Arrow IPC stream, the body every write door of the spec takes.
pub const ARROW_STREAM: &str = "application/vnd.apache.arrow.stream";
const JSON: &str = "application/json";

/// What stays unescaped in a path segment or a query value: the URI's unreserved characters, so `$` travels as `%24`.
const ESCAPED: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');

/// When a request may be sent again after it failed with no answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resend {
    /// A read: whatever cut it off, sending it again changes nothing, so a connection the catalog closed for idleness
    /// does not fail the read.
    UntilAnswered,
    /// A write: only when it never left, because a write the catalog may have applied is never sent twice.
    OnlyUnsent,
}

impl Resend {
    /// Whether a request that failed unanswered is sent again; `unsent` is whether it failed before leaving.
    pub fn allows(self, unsent: bool) -> bool {
        unsent || self == Resend::UntilAnswered
    }
}

/// One request to a door of the catalog.
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    /// The path and query, relative to the catalog's base URL.
    pub target: String,
    pub content_type: &'static str,
    pub body: Vec<u8>,
    pub resend: Resend,
}

/// The headers a request carries beside its body's type: what it takes back, and whose identity it is sent as.
pub fn headers(bearer: Option<&str>) -> Vec<(&'static str, String)> {
    let bearer = bearer
        .filter(|token| !token.is_empty())
        .map(|token| ("authorization", format!("Bearer {token}")));
    [("accept", JSON.to_owned())].into_iter().chain(bearer).collect()
}

/// How a merge treats a row whose key the table already holds.
#[derive(Debug, Clone, PartialEq)]
pub enum Matched {
    /// Keep the table's row: a row whose key is held lands nothing.
    Keep,
    /// Update it, only where the condition (`when_matched_update_all_filt`, over `source.<col>` and `target.<col>`)
    /// holds when one is given.
    Update(Option<String>),
}

/// One page of a scan: the `/query` door has no plain-scan verb, so a scan is an empty vector, a bounded `k` and the filter.
#[derive(Debug, Clone, PartialEq)]
pub struct Page<'a> {
    pub columns: Option<&'a [String]>,
    pub filter: Option<&'a str>,
    pub k: usize,
    pub offset: usize,
    pub with_row_id: bool,
    pub version: u64,
}

fn target(id: &[String], action: &str, query: &[(&str, &str)]) -> String {
    let escape = |text: &str| utf8_percent_encode(text, ESCAPED).to_string();
    let query: String = query.iter().map(|(key, value)| format!("&{key}={}", escape(value))).collect();
    format!(
        "/v1/table/{}/{action}?delimiter={}{query}",
        escape(&id.join(CATALOG_DELIMITER)),
        escape(CATALOG_DELIMITER)
    )
}

/// A JSON request; a member that is null is left out, as an unset optional field of the spec's request is.
fn json(target: String, mut body: Value, resend: Resend) -> Request {
    if let Some(members) = body.as_object_mut() {
        members.retain(|_, value| !value.is_null());
    }
    let body = serde_json::to_vec(&body).unwrap_or_default();
    Request {
        target,
        content_type: JSON,
        body,
        resend,
    }
}

fn arrow(target: String, stream: Vec<u8>) -> Request {
    Request {
        target,
        content_type: ARROW_STREAM,
        body: stream,
        resend: Resend::OnlyUnsent,
    }
}

/// `CreateTable` with `mode=exist_ok`: a table that already exists is kept, whatever schema `stream` declares.
pub fn create(id: &[String], stream: Vec<u8>) -> Request {
    arrow(target(id, "create", &[("mode", "exist_ok")]), stream)
}

/// `DescribeTable`, asking for no vended credential: nothing read through it reaches the store.
///
/// `detailed` asks the catalog to open the table for its current version (`load_detailed_metadata`); without it the
/// catalog may answer from its record alone, which holds the table's properties.
pub fn describe(id: &[String], detailed: bool) -> Request {
    let query: &[(&str, &str)] = if detailed { &[("load_detailed_metadata", "true")] } else { &[] };
    let body = json!({"id": id, "with_table_uri": false, "check_declared": false, "vend_credentials": false});
    json(target(id, "describe", query), body, Resend::UntilAnswered)
}

/// `CountTableRows`, under `predicate` when one is given, at `version` when one is given.
pub fn count_rows(id: &[String], predicate: Option<&str>, version: Option<u64>) -> Request {
    json(
        target(id, "count_rows", &[]),
        json!({"id": id, "version": version, "predicate": predicate}),
        Resend::UntilAnswered,
    )
}

/// `QueryTable` for one page of a scan, pinned to its version.
pub fn query(id: &[String], page: &Page<'_>) -> Request {
    let columns = page
        .columns
        .filter(|columns| !columns.is_empty())
        .map(|columns| json!({"column_names": columns}));
    let body = json!({
        "id": id, "columns": columns, "filter": page.filter, "k": page.k, "offset": (page.offset > 0).then_some(page.offset),
        "vector": {"single_vector": []}, "version": page.version, "with_row_id": page.with_row_id.then_some(true),
    });
    json(target(id, "query", &[]), body, Resend::UntilAnswered)
}

/// `MergeInsertIntoTable` on the key `on`: a row whose key the table does not hold is inserted, and a held one is
/// treated as `matched` says.
pub fn merge_insert(id: &[String], on: &str, matched: &Matched, stream: Vec<u8>) -> Request {
    let (update, condition) = match matched {
        Matched::Keep => ("false", None),
        Matched::Update(condition) => ("true", condition.as_deref()),
    };
    let condition = condition.map(|condition| ("when_matched_update_all_filt", condition));
    let query: Vec<_> = [("on", on), ("when_matched_update_all", update)]
        .into_iter()
        .chain(condition)
        .chain([("when_not_matched_insert_all", "true")])
        .collect();
    arrow(target(id, "merge_insert", &query), stream)
}

/// `DeleteFromTable`: the rows `predicate` holds for.
pub fn delete(id: &[String], predicate: &str) -> Request {
    json(target(id, "delete", &[]), json!({"predicate": predicate}), Resend::OnlyUnsent)
}
