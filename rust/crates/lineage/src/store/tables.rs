//! The five tables lineage keeps in the catalog's provenance namespace, and their schemas.
//!
//! The schemas are the ones the Python store creates, field for field: either implementation may create a table and
//! both write it during the mixed period (`rust/vectors/store.json`, `schemas`). Each declares an unenforced primary key,
//! which Lance reads from the field's metadata (`lance_docs/lance/format/table/index.md`, "Unenforced Primary Key"), and
//! every row is written by an insert-only merge on it, so a row is written once.

use arrow_schema::{DataType, Field, Schema, SchemaRef, TimeUnit};
use std::collections::HashMap;
use std::sync::Arc;

/// The commit feed: one row per terminal state of a run, per arrival of another state, and per static change.
pub const FEED: &str = "events";
/// One row per input and output of each successful run.
pub const EDGES: &str = "edges";
/// The outbox's governance refusals and the ingest lane's parked deliveries.
pub const REFUSALS: &str = "refusals";
/// Where each follower of the feed left it.
pub const CURSORS: &str = "cursors";
/// Who founded each run. Never pruned: a run id stays public in every backend the feed was forwarded to, so a binding
/// gone with the feed's retention would hand an old run to whoever posted its id next.
pub const RUNS: &str = "runs";

/// A parked delivery's key in `refusals` starts with this, beside the outbox refusals keyed by their staged object.
pub const PARKED_KEY_PREFIX: &str = "dead_letter:";

fn text(name: &str) -> Field {
    Field::new(name, DataType::Utf8, true)
}

fn key(name: &str) -> Field {
    Field::new(name, DataType::Utf8, false).with_metadata(HashMap::from([("lance-schema:unenforced-primary-key".to_owned(), "true".to_owned())]))
}

fn texts(name: &str) -> Field {
    Field::new(name, DataType::List(Arc::new(Field::new_list_field(DataType::Utf8, true))), true)
}

/// The estate's clock, never a producer's: retention reads it, and a skewed producer stamp would strand a row or drop a fresh one.
fn stamp(name: &str) -> Field {
    Field::new(name, DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into())), false)
}

fn int(name: &str, nullable: bool) -> Field {
    Field::new(name, DataType::Int64, nullable)
}

pub fn feed_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        key("event_key"),
        text("run_id"),
        text("event_type"),
        text("event_time"),
        text("job"),
        text("author"),
        texts("inputs"),
        texts("outputs"),
        texts("table_uuids"),
        texts("namespace_uuids"),
        texts("unresolved_tables"),
        Field::new("event", DataType::LargeUtf8, true),
        stamp("received_at"),
    ]))
}

pub fn edge_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        key("edge_key"),
        text("run_id"),
        text("writer_run_id"),
        text("author"),
        Field::new("in_table", DataType::Utf8, false),
        text("in_name"),
        int("in_version", true),
        text("in_branch"),
        text("in_branch_identifier"),
        Field::new("out_table", DataType::Utf8, false),
        text("out_name"),
        int("out_version", true),
        text("out_branch"),
        text("out_branch_identifier"),
        text("event_time"),
        stamp("received_at"),
    ]))
}

pub fn refusal_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        key("outbox_key"),
        text("run_id"),
        text("author"),
        Field::new("reason", DataType::Utf8, false),
        Field::new("event", DataType::LargeUtf8, false),
        stamp("refused_at"),
    ]))
}

pub fn cursor_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![key("follower"), int("seq", false), stamp("updated_at")]))
}

pub fn run_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![key("run_id"), text("author"), stamp("received_at")]))
}

/// Every table, in the order the store creates them.
pub fn all() -> [(&'static str, SchemaRef); 5] {
    [
        (FEED, feed_schema()),
        (EDGES, edge_schema()),
        (REFUSALS, refusal_schema()),
        (CURSORS, cursor_schema()),
        (RUNS, run_schema()),
    ]
}
