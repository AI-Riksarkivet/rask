//! What one group commit writes, decided from values: the run bindings, the edges and the feed rows of a batch of
//! admitted events, and which of them are refused because their run is another author's.

use crate::feed::{EdgeRow, FeedRow, edges_of, feed_row};
use crate::model::{Event, author_sub_from_payload};
use crate::store::rows;
use arrow_array::RecordBatch;
use arrow_schema::ArrowError;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};

/// One admitted event's rows, waiting for the group commit that lands them.
#[derive(Debug, Clone, PartialEq)]
pub struct Admitted {
    /// The run a run event names: what its first event binds to `author`.
    pub run_id: Option<String>,
    /// The verified subject the event is recorded for, read from its stored payload.
    pub author: Option<String>,
    pub received_at: i64,
    pub row: FeedRow,
    pub edges: Vec<EdgeRow>,
    /// The bytes of its rows as JSON, what a batch is bounded by.
    pub weight: usize,
}

/// `event`'s rows, received at `received_at` (microseconds since the epoch).
pub fn admitted(event: &Event, aliased: &[String], catalog: &str, received_at: i64) -> Admitted {
    let row = feed_row(event, aliased, catalog);
    let edges = edges_of(event, catalog);
    let run_id = match event {
        Event::Run(run) if !run.run.run_id.is_empty() => Some(run.run.run_id.clone()),
        _ => None,
    };
    let author = run_id
        .as_ref()
        .and_then(|_| serde_json::from_str::<Value>(&row.event).ok())
        .and_then(|stored| author_sub_from_payload(&stored).map(str::to_owned));
    let weight = serde_json::to_vec(&(&row, &edges)).map_or(row.event.len(), |json| json.len());
    Admitted {
        run_id,
        author,
        received_at,
        row,
        edges,
        weight,
    }
}

/// The runs a batch names, whose bindings are read before it commits.
pub fn runs_of(batch: &[Admitted]) -> BTreeSet<String> {
    batch.iter().filter_map(|admitted| admitted.run_id.clone()).collect()
}

/// Whether an event of `run_id` by `author` extends a run bound to someone else. An unauthored side compares to nothing.
pub fn extends_another(run_id: Option<&str>, author: Option<&str>, authors: &HashMap<String, Option<String>>) -> bool {
    let (Some(run_id), Some(author)) = (run_id, author) else {
        return false;
    };
    matches!(authors.get(run_id), Some(Some(owner)) if owner != author)
}

/// The position of the first of each key, in order: the catalog refuses a write that repeats a key.
pub fn first_of_each<'a>(keys: impl IntoIterator<Item = &'a str>) -> Vec<usize> {
    let mut seen = HashSet::new();
    keys.into_iter().enumerate().filter(|(_, key)| seen.insert(*key)).map(|(at, _)| at).collect()
}

/// The writes of one commit, in the order they are made, and the events refused.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub runs: Option<RecordBatch>,
    pub edges: Option<RecordBatch>,
    pub feed: Option<RecordBatch>,
    /// The positions of the events whose run is another author's; nothing of theirs is written.
    pub foreign: Vec<usize>,
}

/// What committing `batch` writes, given the authors the table binds its runs to.
///
/// A run is bound to the author of its first event: the table's binding, else the batch's first. Events delivered together
/// were each authorized before any of them bound its run, so the commit asks again, where `refuse` says the doors ask at
/// all: an event of a run bound to another author is refused.
pub fn plan(batch: &[Admitted], bound: &HashMap<String, Option<String>>, refuse: bool) -> Result<Plan, ArrowError> {
    let mut authors = bound.clone();
    let mut founders = Vec::new();
    for admitted in batch {
        if let Some(run_id) = &admitted.run_id
            && !authors.contains_key(run_id)
        {
            authors.insert(run_id.clone(), admitted.author.clone());
            founders.push((run_id.as_str(), admitted.author.as_deref(), admitted.received_at));
        }
    }
    let foreign: Vec<usize> = (0..batch.len())
        .filter(|at| {
            refuse
                && batch
                    .get(*at)
                    .is_some_and(|admitted| extends_another(admitted.run_id.as_deref(), admitted.author.as_deref(), &authors))
        })
        .collect();
    let landing: Vec<&Admitted> = batch
        .iter()
        .enumerate()
        .filter(|(at, _)| !foreign.contains(at))
        .map(|(_, admitted)| admitted)
        .collect();
    let runs = (!founders.is_empty()).then(|| rows::run_batch(&founders)).transpose()?;
    let edges: Vec<(&EdgeRow, i64)> = landing
        .iter()
        .flat_map(|admitted| admitted.edges.iter().map(|edge| (edge, admitted.received_at)))
        .collect();
    let edges = firsts(&edges, |(edge, _)| &edge.edge_key);
    let feed: Vec<(&FeedRow, i64)> = landing.iter().map(|admitted| (&admitted.row, admitted.received_at)).collect();
    let feed = firsts(&feed, |(row, _)| &row.event_key);
    Ok(Plan {
        runs,
        edges: (!edges.is_empty()).then(|| rows::edge_batch(&edges)).transpose()?,
        feed: (!landing.is_empty()).then(|| rows::feed_batch(&feed)).transpose()?,
        foreign,
    })
}

fn firsts<T: Copy>(rows: &[T], key: impl Fn(&T) -> &String) -> Vec<T> {
    let kept = first_of_each(rows.iter().map(|row| key(row).as_str()));
    kept.into_iter().filter_map(|at| rows.get(at).copied()).collect()
}
