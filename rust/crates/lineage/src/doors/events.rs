//! The commit feed (`/events`), each row shown only to a caller who may read every table it names, and its unfiltered
//! projection (`/events/projection`) on the estate's event-feed rung, cut to what deciding whom to tell reads.

use super::{Answer, Caller, Query, Refusal, answer, caller};
use crate::authz::gates::{DatasetFilter, require_event_feed_reader};
use crate::authz::scope::{Governed, governed_refs, output_refs};
use crate::model::Dataset;
use crate::paging::{forward_cursor, next_chunk};
use crate::state::State;
use crate::store::rows::EventRecord;
use crate::targeting::{column_lineage_datasets, targeting_view};
use axum::extract::{Request, State as Shared};
use axum::http::StatusCode;
use lakehouse_core::authz::table_object;
use lakehouse_core::naming::CATALOG_DELIMITER;
use lakehouse_core::service::problem::{ErrorCode, NsError};
use serde_json::{Value, json};

/// How many rows a governed or scoped page reads at most, since the filter may drop rows.
const EVENTS_FETCH: usize = 2000;
/// The most rows a page returns.
const EVENTS_RETURN: i128 = 500;

/// The paging read of both doors: a page older than `after`, or forward from `since`, never both.
struct Paging {
    after: Option<i64>,
    since: Option<i64>,
    limit: usize,
}

fn paging(query: &mut Query) -> Paging {
    let after = query.int("after", 1, None);
    let since = query.int("since", 0, None);
    Paging { after, since, limit: 0 }
}

impl Paging {
    fn one_direction(&self) -> Result<(), Refusal> {
        if self.after.is_some() && self.since.is_some() {
            return Err(NsError::new(ErrorCode::InvalidInput, "pass `after` (page older) or `since` (follow forward), not both").into());
        }
        Ok(())
    }

    /// The cursor a caller passes next: forward from `since` always one; older only while rows may remain below.
    fn next_cursor(&self, returned: &[EventRecord], read: &[EventRecord], fetch: usize, head: i64) -> Option<i64> {
        let seq = |records: &[EventRecord]| records.last().map(|record| record.seq);
        let unsigned = |seq: i64| u64::try_from(seq).unwrap_or_default();
        if let Some(since) = self.since {
            let cursor = forward_cursor(
                seq(returned).map(unsigned),
                returned.len(),
                seq(read).map(unsigned),
                self.limit,
                unsigned(since),
                unsigned(head),
            );
            return Some(i64::try_from(cursor).unwrap_or(i64::MAX));
        }
        if returned.len() == self.limit {
            seq(returned)
        } else if read.len() == fetch {
            seq(read)
        } else {
            None
        }
    }
}

/// What a scoped read keeps: rows naming a table, and rows naming a table directly in a namespace, each by the uuid its
/// name resolves to now, and by the name where it resolves to none.
struct Scope {
    table: Option<(String, Option<String>)>,
    namespace: Option<(String, Option<String>)>,
}

impl Scope {
    async fn resolve(state: &State, caller: &Caller, table: Option<String>, namespace: Option<String>) -> Result<Self, Refusal> {
        let bearer = caller.bearer.as_deref();
        let table = match table {
            Some(name) => {
                let uuid = state.names.table(&name, bearer).await?.table_uuid;
                Some((name, uuid))
            }
            None => None,
        };
        let namespace = match namespace {
            Some(name) => {
                let uuid = state.names.namespace(&name, bearer).await?;
                Some((name, uuid))
            }
            None => None,
        };
        Ok(Scope { table, namespace })
    }

    fn scoped(&self) -> bool {
        self.table.is_some() || self.namespace.is_some()
    }

    /// The names a row is matched by: the governed ones it recorded without a uuid where the scope has a uuid, else
    /// every table it names.
    fn by_name<'r>(uuid: &Option<String>, record: &'r EventRecord) -> Vec<&'r String> {
        if uuid.is_some() {
            record.unresolved_tables.iter().collect()
        } else {
            record.inputs.iter().chain(&record.outputs).collect()
        }
    }

    fn names(&self, record: &EventRecord) -> bool {
        if let Some((table, uuid)) = &self.table {
            let by_uuid = uuid.as_ref().is_some_and(|uuid| record.table_uuids.contains(uuid));
            if !by_uuid && !Self::by_name(uuid, record).contains(&table) {
                return false;
            }
        }
        let Some((namespace, uuid)) = &self.namespace else { return true };
        let prefix = format!("{namespace}{CATALOG_DELIMITER}");
        uuid.as_ref().is_some_and(|uuid| record.namespace_uuids.contains(uuid)) || Self::by_name(uuid, record).iter().any(|name| name.starts_with(&prefix))
    }
}

/// The datasets of a stored payload's list member, each read as the event model reads it.
fn datasets_in(payload: &serde_json::Map<String, Value>, member: &str) -> Vec<Dataset> {
    let items = payload.get(member).and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
    items.iter().filter_map(|item| serde_json::from_value(item.clone()).ok()).collect()
}

/// What a row is judged against: the tables it recorded by uuid, and those its payload names, the column upstreams of
/// its outputs among them. Its outputs are never exempted, as on the write path.
fn governed_by(record: &EventRecord, catalog: &str) -> Governed {
    let recorded = Governed {
        objects: record.table_uuids.iter().map(|uuid| table_object(uuid)).collect(),
        unresolved: record.unresolved_tables.iter().cloned().collect(),
    };
    let inputs = datasets_in(&record.event, "inputs");
    let mut outputs = datasets_in(&record.event, "outputs");
    if let Some(dataset) = record.event.get("dataset").filter(|dataset| dataset.is_object()) {
        outputs.extend(serde_json::from_value(dataset.clone()).ok());
    }
    if inputs.is_empty() && outputs.is_empty() {
        return recorded;
    }
    let read = governed_refs(inputs.iter().chain(&outputs), &column_lineage_datasets(&record.event, catalog), catalog);
    let written = output_refs(&outputs, catalog);
    recorded.joined(read.objects, read.unresolved).joined(written.objects, written.unresolved)
}

/// The ingested events, newest first or forward from `since`, each shown only where the caller may read every table
/// it names; a row naming no table is hidden under authorization.
pub async fn events(Shared(state): Shared<State>, request: Request) -> Answer {
    let caller = caller(&state, request.headers()).await?;
    let mut query = Query::of(request.uri());
    let mut paging = paging(&mut query);
    let (table, namespace) = (query.text("table"), query.text("namespace"));
    paging.limit = usize::try_from(query.int("limit", 1, Some(EVENTS_RETURN)).unwrap_or(500)).unwrap_or(500);
    let summary = query.flag("summary");
    query.checked()?;
    paging.one_direction()?;
    let scope = Scope::resolve(&state, &caller, table, namespace).await?;
    let limit = paging.limit;
    let fetch = if state.gate.fga_enabled || scope.scoped() { EVENTS_FETCH } else { limit };
    let mut window = state.gate.store.window(paging.after, paging.since, summary).await?;
    let mut filter = DatasetFilter::new(state.gate.authority(), caller.subject.as_deref());
    let (mut read, mut visible) = (Vec::new(), Vec::new());
    let (mut want, mut reads) = (limit.min(fetch), 1);
    loop {
        let chunk = window.take(want).await?;
        let exhausted = chunk.len() < want;
        if chunk.is_empty() {
            break;
        }
        read.extend(chunk.iter().cloned());
        let named = chunk.into_iter().filter(|record| scope.names(record)).collect();
        visible.extend(filter.governed(named, |record| governed_by(record, &state.gate.catalog)).await?);
        let left = if exhausted { 0 } else { fetch - read.len() };
        if visible.len() >= limit || left == 0 {
            break;
        }
        let wide = |count: usize| count as u64;
        want = usize::try_from(next_chunk(wide(limit), wide(read.len()), wide(visible.len()), wide(left), reads)).unwrap_or(left);
        reads += 1;
    }
    visible.truncate(limit);
    let bounds = window.bounds().await?;
    let next_cursor = paging.next_cursor(&visible, &read, fetch, bounds.map_or(0, |(_, head)| head));
    answer(
        StatusCode::OK,
        json!({"events": visible, "next_cursor": next_cursor, "oldest_seq": bounds.map(|(oldest, _)| oldest)}),
    )
}

/// The run feed with no per-table filter, for a notifier walking it, each row cut to its targeting view.
pub async fn projection(Shared(state): Shared<State>, request: Request) -> Answer {
    let caller = caller(&state, request.headers()).await?;
    let mut query = Query::of(request.uri());
    let mut paging = paging(&mut query);
    paging.limit = usize::try_from(query.int("limit", 1, Some(EVENTS_RETURN)).unwrap_or(500)).unwrap_or(500);
    query.checked()?;
    paging.one_direction()?;
    require_event_feed_reader(&state.gate.authority(), caller.subject.as_deref()).await?;
    let mut window = state.gate.store.window(paging.after, paging.since, false).await?;
    let records = window.take(paging.limit).await?;
    let bounds = window.bounds().await?;
    let next_cursor = paging.next_cursor(&records, &records, usize::MAX, bounds.map_or(0, |(_, head)| head));
    let events: Vec<Value> = records
        .iter()
        .map(|record| json!({"seq": record.seq, "event": targeting_view(&record.event)}))
        .collect();
    answer(
        StatusCode::OK,
        json!({"events": events, "next_cursor": next_cursor, "oldest_seq": bounds.map(|(oldest, _)| oldest)}),
    )
}
