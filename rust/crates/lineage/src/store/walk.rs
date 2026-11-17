//! What a table fed: every table the edge list records a run writing from it, then from those.
//!
//! One walk answers lineage's `derived` door and the catalog's erasure verification, which must not disagree. An edge
//! is followed whatever version of its input the run read: a table derived from any version of the source may hold what
//! an erasure removes from it.

use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

/// How many derivation hops `derived` follows by default; a longer chain is a cycle the walk already stops, or an
/// estate whose answer says it stopped short.
pub const MAX_DERIVED_DEPTH: u32 = 20;

/// The edge columns a walk reads.
pub const COLUMNS: [&str; 3] = ["out_table", "out_name", "received_at"];

/// One edge out of a table the walk reached: the derived table's key, the name its event used, and when it was recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge {
    pub out_table: String,
    pub out_name: Option<String>,
    pub received_at: i64,
}

/// What a walk reached.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Walked {
    /// Each derived table's key, with its hops from the source: 1 for a table a run wrote from the source.
    pub depths: BTreeMap<String, u32>,
    /// The name each derived table's newest edge was recorded under.
    pub names: BTreeMap<String, String>,
    /// False when the walk stopped at its depth with tables left to follow.
    pub complete: bool,
}

/// A derived table as the door answers it: by its newest name, its uuid when its key is one, and its depth.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DerivedTable {
    pub table: String,
    pub uuid: Option<String>,
    pub depth: u32,
}

/// Every table derived from the table `sources` key, to `max_depth` hops, or to the last one with `None`.
///
/// `rows` answers the edges whose input is one of the keys it is given, every call at one version of the edge list. A
/// table among `sources` is never its own derived table, whatever cycle the edges hold.
pub async fn walk<E>(
    mut rows: impl AsyncFnMut(&BTreeSet<String>) -> Result<Vec<Edge>, E>,
    sources: &BTreeSet<String>,
    max_depth: Option<u32>,
) -> Result<Walked, E> {
    let mut depths: BTreeMap<String, u32> = BTreeMap::new();
    let mut newest: BTreeMap<String, (i64, String)> = BTreeMap::new();
    let (mut frontier, mut depth) = (sources.clone(), 0);
    while !frontier.is_empty() {
        let mut found = BTreeSet::new();
        for edge in rows(&frontier).await? {
            let name = edge.out_name.filter(|name| !name.is_empty()).unwrap_or_else(|| edge.out_table.clone());
            if newest.get(&edge.out_table).is_none_or(|(at, _)| edge.received_at >= *at) {
                newest.insert(edge.out_table.clone(), (edge.received_at, name));
            }
            found.insert(edge.out_table);
        }
        found.retain(|key| !depths.contains_key(key) && !sources.contains(key));
        if Some(depth) == max_depth {
            return Ok(walked(depths, &newest, found.is_empty()));
        }
        depth += 1;
        depths.extend(found.iter().map(|key| (key.clone(), depth)));
        frontier = found;
    }
    Ok(walked(depths, &newest, true))
}

fn walked(depths: BTreeMap<String, u32>, newest: &BTreeMap<String, (i64, String)>, complete: bool) -> Walked {
    let names = depths.keys().filter_map(|key| Some((key.clone(), newest.get(key)?.1.clone()))).collect();
    Walked { depths, names, complete }
}

/// Whether an edge key is a governed table's uuid, in its canonical hyphenated form, rather than a name.
pub fn is_table_uuid(key: &str) -> bool {
    key.len() == 36
        && key.char_indices().all(|(at, char)| {
            if matches!(at, 8 | 13 | 18 | 23) {
                char == '-'
            } else {
                char.is_ascii_hexdigit()
            }
        })
}

/// The derived tables a walk reached, nearest first, then by name.
pub fn derived_tables(walked: &Walked) -> Vec<DerivedTable> {
    let mut found: Vec<DerivedTable> = walked
        .depths
        .iter()
        .map(|(key, depth)| DerivedTable {
            table: walked.names.get(key).unwrap_or(key).clone(),
            uuid: is_table_uuid(key).then(|| key.clone()),
            depth: *depth,
        })
        .collect();
    found.sort_by(|one, other| (one.depth, &one.table).cmp(&(other.depth, &other.table)));
    found
}
