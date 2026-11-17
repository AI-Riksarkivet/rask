//! What lineage records for an event: its row in the feed and the table-level edges it adds.
//!
//! Every function here takes an event and returns rows. Nothing reads a clock or a table: the store stamps the time a
//! row was received when it writes it.

use crate::model::{Dataset, Event, RunEvent};
use lakehouse_core::events::{AUTHOR_FACET, BRANCH_IDENTIFIER, LANCE_FACET, SIGNATURE_FACET, dataset_uuids};
use lakehouse_core::naming::is_external_source_namespace;
use lakehouse_core::pywire;
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// The states that end a run. A run reaches each at most once, so the state alone keys the event.
const TERMINAL_STATES: [&str; 3] = ["COMPLETE", "FAIL", "ABORT"];

/// The tables and namespaces an event is about, by uuid where its facets name one and by name where they do not.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Scope {
    pub table_uuids: Vec<String>,
    pub namespace_uuids: Vec<String>,
    pub unresolved_tables: Vec<String>,
}

/// An event's row in the feed. `event` is the event as stored JSON.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FeedRow {
    pub event_key: String,
    pub run_id: String,
    pub event_type: String,
    pub event_time: String,
    pub job: Option<String>,
    pub author: Option<String>,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
    #[serde(flatten)]
    pub scope: Scope,
    pub event: String,
}

/// One input table a successful run read to write one output table.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EdgeRow {
    pub edge_key: String,
    pub run_id: String,
    pub writer_run_id: Option<String>,
    pub author: Option<String>,
    pub in_table: String,
    pub in_name: String,
    pub in_version: Option<i64>,
    pub in_branch: Option<String>,
    pub in_branch_identifier: Option<String>,
    pub out_table: String,
    pub out_name: String,
    pub out_version: Option<i64>,
    pub out_branch: Option<String>,
    pub out_branch_identifier: Option<String>,
    pub event_time: String,
}

fn push_new(seen: &mut Vec<String>, value: &str) {
    if !seen.iter().any(|held| held == value) {
        seen.push(value.to_owned());
    }
}

/// The key the feed holds `event` under, so the same event delivered twice is one row.
///
/// A dataset event nobody signed is keyed by who posted it too: a caller cannot overwrite another's by describing
/// the same change.
pub fn feed_key(event: &Event) -> String {
    match event {
        Event::Dataset(change) => {
            let key = format!("{}@COMPLETE", change.feed_id());
            if change.dataset.facets.contains_key(SIGNATURE_FACET) {
                return key;
            }
            let author = change
                .dataset
                .facets
                .get(AUTHOR_FACET)
                .and_then(|author| author.get("sub"))
                .and_then(Value::as_str);
            format!("{key}@posted:{}", author.filter(|sub| !sub.trim().is_empty()).unwrap_or_default())
        }
        Event::Run(run) if TERMINAL_STATES.contains(&run.event_type.as_str()) => {
            format!("{}@{}", run.run.run_id, run.event_type)
        }
        Event::Run(run) => format!("{}@{}@{}", run.run.run_id, run.event_type, run.event_time),
    }
}

/// The scope of `datasets`: those the catalog governs, in the order named, each once.
pub fn scope_of<'a>(datasets: impl IntoIterator<Item = &'a Dataset>, catalog: &str) -> Scope {
    let mut scope = Scope::default();
    for dataset in datasets {
        let uuids = dataset_uuids(&dataset.facets);
        if is_external_source_namespace(&dataset.namespace, &dataset.name, catalog) && uuids.table_uuid.is_none() {
            continue;
        }
        match &uuids.table_uuid {
            Some(uuid) => push_new(&mut scope.table_uuids, uuid),
            None => push_new(&mut scope.unresolved_tables, &dataset.name),
        }
        if let Some(uuid) = &uuids.namespace_uuid {
            push_new(&mut scope.namespace_uuids, uuid);
        }
    }
    scope
}

/// The row the feed keeps for `event`. `aliased` are tables the event's outputs resolved to under another name.
pub fn feed_row(event: &Event, aliased: &[String], catalog: &str) -> FeedRow {
    let mut scope = scope_of(event.inputs().iter().chain(event.outputs()), catalog);
    let mut sorted: Vec<&String> = aliased.iter().collect();
    sorted.sort();
    for uuid in sorted {
        push_new(&mut scope.table_uuids, uuid);
    }
    let names = |datasets: &[Dataset]| datasets.iter().map(|dataset| dataset.name.clone()).collect();
    let (run_id, event_type, job, inputs) = match event {
        Event::Dataset(change) => (change.feed_id(), "COMPLETE".to_owned(), None, Vec::new()),
        Event::Run(run) => (
            run.run.run_id.clone(),
            run.event_type.clone(),
            Some(format!("{}/{}", run.job.namespace, run.job.name)),
            names(&run.inputs),
        ),
    };
    FeedRow {
        event_key: feed_key(event),
        run_id,
        event_type,
        event_time: event.event_time().to_owned(),
        job,
        author: event.author(),
        inputs,
        outputs: names(event.outputs()),
        scope,
        event: event.to_json(),
    }
}

/// The table an edge names `dataset` by: its uuid where its facets carry one, else its lineage name.
fn edge_identity(dataset: &Dataset, catalog: &str) -> String {
    dataset_uuids(&dataset.facets).table_uuid.unwrap_or_else(|| dataset.lineage_name(catalog))
}

/// The table version a dataset's `version` facet names: a whole number, as a number or as text.
fn version_of(dataset: &Dataset) -> Option<i64> {
    let version = match dataset.facet("version")?.get("datasetVersion")? {
        Value::Number(number) => number.as_i64()?,
        Value::String(text) => text.trim().parse().ok()?,
        _ => return None,
    };
    (version >= 0).then_some(version)
}

fn lance_text(dataset: &Dataset, key: &str) -> Option<String> {
    dataset
        .facet(LANCE_FACET)?
        .get(key)?
        .as_str()
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

/// The run id of a `{run: {runId}, job: {namespace, name}}` reference, where the reference is a well-formed one.
fn referenced_run(reference: &Value) -> Option<&str> {
    let (job, run) = (reference.get("job")?, reference.get("run")?);
    job.get("namespace")?.as_str()?;
    job.get("name")?.as_str()?;
    run.get("runId").or_else(|| run.get("run_id"))?.as_str()
}

/// The run a `parent` facet names, where the facet is a well-formed one.
fn writer_run(event: &RunEvent) -> Option<String> {
    let parent = event.run.facets.get("parent")?;
    let text_or_absent = |key: &str| parent.get(key).is_none_or(Value::is_string);
    let rooted = parent.get("root").is_none_or(|root| root.is_null() || referenced_run(root).is_some());
    if !(text_or_absent("_producer") && text_or_absent("_schemaURL") && rooted) {
        return None;
    }
    referenced_run(parent).filter(|run_id| !run_id.is_empty()).map(str::to_owned)
}

/// The edges `event` adds: one for each input a successful run read into each output, never a table into itself.
pub fn edges_of(event: &Event, catalog: &str) -> Vec<EdgeRow> {
    let Event::Run(run) = event else {
        return Vec::new();
    };
    if !run.is_success() {
        return Vec::new();
    }
    let writer = writer_run(run);
    let mut rows = Vec::new();
    for output in &run.outputs {
        let (out_table, out_version) = (edge_identity(output, catalog), version_of(output));
        for source in &run.inputs {
            let in_table = edge_identity(source, catalog);
            if in_table == out_table {
                continue;
            }
            let (in_version, in_branch) = (version_of(source), lance_text(source, "ref"));
            let key = json!([run.run.run_id, in_table, in_version, in_branch, out_table, out_version, run.write_ref()]);
            rows.push(EdgeRow {
                edge_key: format!("{:x}", Sha256::digest(pywire::dumps(&key, pywire::COMPACT))),
                run_id: run.run.run_id.clone(),
                writer_run_id: writer.clone(),
                author: run.author(),
                in_table,
                in_name: source.lineage_name(catalog),
                in_version,
                in_branch_identifier: in_branch.as_ref().and_then(|_| lance_text(source, BRANCH_IDENTIFIER)),
                in_branch,
                out_table: out_table.clone(),
                out_name: output.lineage_name(catalog),
                out_version,
                out_branch: run.write_ref().map(str::to_owned),
                out_branch_identifier: run.write_ref_identifier().map(str::to_owned),
                event_time: run.event_time.clone(),
            });
        }
    }
    rows
}
