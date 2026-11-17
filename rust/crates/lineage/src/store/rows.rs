//! Rows as Arrow, both ways: the batches a write sends, built from the feed's and the edges' rows, and the columns a
//! read answers, taken out of the catalog's batches.

use crate::feed::{EdgeRow, FeedRow};
use crate::store::tables;
use arrow_array::builder::{ListBuilder, StringBuilder};
use arrow_array::cast::AsArray;
use arrow_array::types::{Int64Type, TimestampMicrosecondType, UInt64Type};
use arrow_array::{Array, ArrayRef, Int64Array, LargeStringArray, RecordBatch, StringArray, TimestampMicrosecondArray};
use arrow_schema::ArrowError;
use lakehouse_core::service::problem::ErrorCode;
use lakehouse_tables::{CatalogError, Rows};
use serde::Serialize;
use serde_json::{Map, Value};
use std::sync::Arc;

/// A row the store keeps, with the time the store received it, in microseconds since the epoch.
pub type Stamped<'a, R> = (&'a R, i64);

fn texts<'a>(values: impl Iterator<Item = Option<&'a str>>) -> ArrayRef {
    Arc::new(values.collect::<StringArray>())
}

fn lists<'a>(values: impl Iterator<Item = &'a Vec<String>>) -> ArrayRef {
    let mut lists = ListBuilder::new(StringBuilder::new());
    for list in values {
        for value in list {
            lists.values().append_value(value);
        }
        lists.append(true);
    }
    Arc::new(lists.finish())
}

fn stamps(values: impl Iterator<Item = i64>) -> ArrayRef {
    Arc::new(TimestampMicrosecondArray::from_iter_values(values).with_timezone("UTC"))
}

fn ints(values: impl Iterator<Item = Option<i64>>) -> ArrayRef {
    Arc::new(values.collect::<Int64Array>())
}

pub fn feed_batch(rows: &[Stamped<FeedRow>]) -> Result<RecordBatch, ArrowError> {
    let text = |pick: fn(&FeedRow) -> Option<&str>| texts(rows.iter().map(|(row, _)| pick(row)));
    let list = |pick: fn(&FeedRow) -> &Vec<String>| lists(rows.iter().map(|(row, _)| pick(row)));
    RecordBatch::try_new(
        tables::feed_schema(),
        vec![
            text(|row| Some(&row.event_key)),
            text(|row| Some(&row.run_id)),
            text(|row| Some(&row.event_type)),
            text(|row| Some(&row.event_time)),
            text(|row| row.job.as_deref()),
            text(|row| row.author.as_deref()),
            list(|row| &row.inputs),
            list(|row| &row.outputs),
            list(|row| &row.scope.table_uuids),
            list(|row| &row.scope.namespace_uuids),
            list(|row| &row.scope.unresolved_tables),
            Arc::new(rows.iter().map(|(row, _)| Some(row.event.as_str())).collect::<LargeStringArray>()),
            stamps(rows.iter().map(|(_, at)| *at)),
        ],
    )
}

pub fn edge_batch(rows: &[Stamped<EdgeRow>]) -> Result<RecordBatch, ArrowError> {
    let text = |pick: fn(&EdgeRow) -> Option<&str>| texts(rows.iter().map(|(row, _)| pick(row)));
    let int = |pick: fn(&EdgeRow) -> Option<i64>| ints(rows.iter().map(|(row, _)| pick(row)));
    RecordBatch::try_new(
        tables::edge_schema(),
        vec![
            text(|row| Some(&row.edge_key)),
            text(|row| Some(&row.run_id)),
            text(|row| row.writer_run_id.as_deref()),
            text(|row| row.author.as_deref()),
            text(|row| Some(&row.in_table)),
            text(|row| Some(&row.in_name)),
            int(|row| row.in_version),
            text(|row| row.in_branch.as_deref()),
            text(|row| row.in_branch_identifier.as_deref()),
            text(|row| Some(&row.out_table)),
            text(|row| Some(&row.out_name)),
            int(|row| row.out_version),
            text(|row| row.out_branch.as_deref()),
            text(|row| row.out_branch_identifier.as_deref()),
            text(|row| Some(&row.event_time)),
            stamps(rows.iter().map(|(_, at)| *at)),
        ],
    )
}

/// Run bindings: each run with the author it is bound to, and when it was received.
pub fn run_batch(rows: &[(&str, Option<&str>, i64)]) -> Result<RecordBatch, ArrowError> {
    RecordBatch::try_new(
        tables::run_schema(),
        vec![
            texts(rows.iter().map(|(run, _, _)| Some(*run))),
            texts(rows.iter().map(|(_, author, _)| *author)),
            stamps(rows.iter().map(|(_, _, at)| *at)),
        ],
    )
}

/// One kept refusal: its key, run, author, reason, the event as JSON, and when it was refused.
pub fn refusal_batch(key: &str, run_id: Option<&str>, author: Option<&str>, reason: &str, event: &str, at: i64) -> Result<RecordBatch, ArrowError> {
    RecordBatch::try_new(
        tables::refusal_schema(),
        vec![
            texts([Some(key)].into_iter()),
            texts([run_id].into_iter()),
            texts([author].into_iter()),
            texts([Some(reason)].into_iter()),
            Arc::new(LargeStringArray::from(vec![event])),
            stamps([at].into_iter()),
        ],
    )
}

pub fn cursor_batch(follower: &str, seq: i64, at: i64) -> Result<RecordBatch, ArrowError> {
    RecordBatch::try_new(
        tables::cursor_schema(),
        vec![texts([Some(follower)].into_iter()), ints([Some(seq)].into_iter()), stamps([at].into_iter())],
    )
}

fn unreadable(column: &str) -> CatalogError {
    CatalogError::local(ErrorCode::Internal, format!("the catalog answered no readable column {column}"))
}

/// A column of every batch of `rows`, by name.
fn columns<'a>(rows: &'a Rows, name: &str) -> Result<Vec<&'a ArrayRef>, CatalogError> {
    rows.batches
        .iter()
        .map(|batch| batch.column_by_name(name).ok_or_else(|| unreadable(name)))
        .collect()
}

/// A text column, small or large.
pub fn text_column(rows: &Rows, name: &str) -> Result<Vec<Option<String>>, CatalogError> {
    let mut values = Vec::new();
    for column in columns(rows, name)? {
        let owned = |value: Option<&str>| value.map(str::to_owned);
        match (column.as_string_opt::<i32>(), column.as_string_opt::<i64>()) {
            (Some(small), _) => values.extend(small.iter().map(owned)),
            (_, Some(large)) => values.extend(large.iter().map(owned)),
            _ => return Err(unreadable(name)),
        }
    }
    Ok(values)
}

/// A list-of-text column, a null list read as empty.
pub fn list_column(rows: &Rows, name: &str) -> Result<Vec<Vec<String>>, CatalogError> {
    let mut values = Vec::new();
    for column in columns(rows, name)? {
        let list = column.as_list_opt::<i32>().ok_or_else(|| unreadable(name))?;
        for at in 0..list.len() {
            if list.is_null(at) {
                values.push(Vec::new());
                continue;
            }
            let items = list.value(at);
            let items = items.as_string_opt::<i32>().ok_or_else(|| unreadable(name))?;
            values.push(items.iter().map(|item| item.unwrap_or_default().to_owned()).collect());
        }
    }
    Ok(values)
}

/// A whole-number column: Int64, UInt64 (the row id) or a microsecond timestamp.
pub fn int_column(rows: &Rows, name: &str) -> Result<Vec<Option<i64>>, CatalogError> {
    let mut values = Vec::new();
    for column in columns(rows, name)? {
        if let Some(ints) = column.as_primitive_opt::<Int64Type>() {
            values.extend(ints.iter());
        } else if let Some(stamps) = column.as_primitive_opt::<TimestampMicrosecondType>() {
            values.extend(stamps.iter());
        } else if let Some(ids) = column.as_primitive_opt::<UInt64Type>() {
            values.extend(ids.iter().map(|id| id.and_then(|id| i64::try_from(id).ok())));
        } else {
            return Err(unreadable(name));
        }
    }
    Ok(values)
}

/// The row ids a read answered, one per row.
pub fn row_ids(rows: &Rows) -> Result<Vec<i64>, CatalogError> {
    int_column(rows, "_rowid")?
        .into_iter()
        .map(|id| id.ok_or_else(|| unreadable("_rowid")))
        .collect()
}

/// One row of the feed as a read answers it: its seq is its row id plus one, so the first row is 1.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EventRecord {
    pub seq: i64,
    pub event_type: Option<String>,
    pub event_time: Option<String>,
    pub job: Option<String>,
    pub author: Option<String>,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
    pub table_uuids: Vec<String>,
    pub namespace_uuids: Vec<String>,
    pub unresolved_tables: Vec<String>,
    /// The event as recorded; empty in a summary, or where the stored text is no JSON object.
    pub event: Map<String, Value>,
}

/// The records a read of the feed answered, its payload read unless `summary`.
pub fn records(rows: &Rows, summary: bool) -> Result<Vec<EventRecord>, CatalogError> {
    let ids = row_ids(rows)?;
    let mut texts = [
        text_column(rows, "event_type")?,
        text_column(rows, "event_time")?,
        text_column(rows, "job")?,
        text_column(rows, "author")?,
    ];
    let mut events = if summary { Vec::new() } else { text_column(rows, "event")? };
    let names = ["inputs", "outputs", "table_uuids", "namespace_uuids", "unresolved_tables"];
    let mut lists = names.iter().map(|name| list_column(rows, name)).collect::<Result<Vec<_>, _>>()?;
    let mut text = |column: usize, at: usize| texts.get_mut(column).and_then(|values| values.get_mut(at)).and_then(Option::take);
    let mut records = Vec::with_capacity(ids.len());
    for (at, id) in ids.into_iter().enumerate() {
        let mut list = |column: usize| {
            lists
                .get_mut(column)
                .and_then(|values| values.get_mut(at))
                .map(std::mem::take)
                .unwrap_or_default()
        };
        let event = events
            .get_mut(at)
            .and_then(Option::take)
            .and_then(|text| serde_json::from_str::<Value>(&text).ok());
        records.push(EventRecord {
            seq: id + 1,
            event_type: text(0, at),
            event_time: text(1, at),
            job: text(2, at),
            author: text(3, at),
            inputs: list(0),
            outputs: list(1),
            table_uuids: list(2),
            namespace_uuids: list(3),
            unresolved_tables: list(4),
            event: match event {
                Some(Value::Object(event)) => event,
                _ => Map::new(),
            },
        });
    }
    Ok(records)
}
