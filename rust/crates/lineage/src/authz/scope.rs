//! What an event names in the governed estate, decided from the event and the answers already had. No function here
//! asks anything: which questions to ask is a value, and asking is the gates' (`super::gates`).

use crate::model::Dataset;
use lakehouse_core::authz::{TableDecision, table_object};
use lakehouse_core::events::dataset_uuids;
use lakehouse_core::naming::{is_catalog_dataset_namespace, is_external_source_namespace};
use serde_json::Map;
use std::collections::{BTreeMap, BTreeSet};

/// The rung that sees a table, the one the catalog's DescribeTable asks.
pub const READ: &str = "can_get_metadata";
pub const WRITE_RELATIONS: &[&str] = &["can_write_data"];
/// A maintenance act accepts either rung: the sweep holds the maintainer's and never the writer's, and the catalog gates
/// an index commit at the writer's, so either alone would refuse an honest producer.
pub const MAINTENANCE_RELATIONS: &[&str] = &["can_write_data", "can_maintain"];
/// The operations that rewrite how a table is stored and change none of its rows.
pub const MAINTENANCE_OPERATIONS: [&str; 3] = ["expire_versions", "compact_table", "create_index"];

/// Which rungs may record `operation`, any one sufficing. The relation follows the act, never who performed it.
pub fn relations_for_operation(operation: Option<&str>) -> &'static [&'static str] {
    if operation.is_some_and(|operation| MAINTENANCE_OPERATIONS.contains(&operation)) {
        MAINTENANCE_RELATIONS
    } else {
        WRITE_RELATIONS
    }
}

/// What an item names: the objects its tables' grants are held by, and the governed tables it names without a uuid,
/// which no tuple can name.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Governed {
    pub objects: BTreeSet<String>,
    pub unresolved: BTreeSet<String>,
}

impl Governed {
    pub fn names_nothing(&self) -> bool {
        self.objects.is_empty() && self.unresolved.is_empty()
    }

    pub fn joined(mut self, objects: impl IntoIterator<Item = String>, unresolved: impl IntoIterator<Item = String>) -> Self {
        self.objects.extend(objects);
        self.unresolved.extend(unresolved);
        self
    }
}

fn uuid_of(dataset: &Dataset) -> Option<String> {
    dataset_uuids(&dataset.facets).table_uuid
}

/// What `datasets` and the column upstreams named by a governed table's name govern. An external source governs nothing;
/// a column upstream is the table a dataset of the event by that name is, else unresolved.
pub fn governed_refs<'a>(datasets: impl IntoIterator<Item = &'a Dataset>, column_upstreams: &BTreeSet<String>, catalog: &str) -> Governed {
    let mut governed = Governed::default();
    let mut by_name = BTreeMap::new();
    for dataset in datasets {
        if dataset.name.is_empty() || is_external_source_namespace(&dataset.namespace, &dataset.name, catalog) {
            continue;
        }
        match uuid_of(dataset) {
            Some(uuid) => {
                governed.objects.insert(table_object(&uuid));
                by_name.insert(dataset.name.clone(), table_object(&uuid));
            }
            None => {
                governed.unresolved.insert(dataset.name.clone());
            }
        }
    }
    for name in column_upstreams {
        match by_name.get(name) {
            Some(object) => governed.objects.insert(object.clone()),
            None => governed.unresolved.insert(name.clone()),
        };
    }
    governed.unresolved.retain(|name| !by_name.contains_key(name));
    governed
}

/// What `outputs` govern, none exempted: an output without a uuid, an external one included, is unresolved, since
/// writing is the direction that changes the estate.
pub fn output_refs<'a>(outputs: impl IntoIterator<Item = &'a Dataset>, catalog: &str) -> Governed {
    let mut governed = Governed::default();
    for dataset in outputs.into_iter().filter(|dataset| !dataset.name.is_empty()) {
        let external = is_external_source_namespace(&dataset.namespace, &dataset.name, catalog);
        match uuid_of(dataset).filter(|_| !external) {
            Some(uuid) => governed.objects.insert(table_object(&uuid)),
            None => governed.unresolved.insert(dataset.name.clone()),
        };
    }
    governed
}

/// The storage location an OpenLineage `(namespace, name)` names, or `None` where the namespace names no store.
pub fn identifier_location(namespace: &str, name: &str) -> Option<String> {
    if namespace == "file" {
        return Some(name.to_owned());
    }
    if !namespace.contains("://") || is_catalog_dataset_namespace(namespace) {
        return None;
    }
    Some(format!("{}/{}", namespace.trim_end_matches('/'), name.trim_start_matches('/')))
}

/// The governed tables datasets are named as besides their own names, by the index of each dataset asked about:
/// `objects` by a storage location or a symlinked name, `located` the datasets whose own name is a location a table
/// holds, `hidden` the symlinked names the reader may not see, and `tables` the uuid of every table `objects` names.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Aliases {
    pub objects: BTreeMap<usize, BTreeSet<String>>,
    pub located: BTreeSet<usize>,
    pub hidden: BTreeMap<usize, BTreeSet<String>>,
    pub tables: BTreeSet<String>,
}

impl Aliases {
    pub fn hidden_names(&self) -> BTreeSet<String> {
        self.hidden.values().flatten().cloned().collect()
    }

    pub fn all_objects(&self) -> BTreeSet<String> {
        self.objects.values().flatten().cloned().collect()
    }
}

/// What resolving datasets' aliases asks: the catalog which table each location holds, and which table each symlinked
/// name names for the reader. A graph backend joins a dataset by every identifier it carries, so each is decided.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AliasQuestions {
    pub locations: BTreeMap<String, Vec<usize>>,
    pub names: BTreeMap<String, Vec<usize>>,
    own: BTreeMap<usize, String>,
}

pub fn alias_questions(datasets: &[Dataset], catalog: &str) -> AliasQuestions {
    let mut questions = AliasQuestions::default();
    for (index, dataset) in datasets.iter().enumerate() {
        if is_external_source_namespace(&dataset.namespace, &dataset.name, catalog)
            && let Some(location) = identifier_location(&dataset.namespace, &dataset.name)
        {
            questions.own.insert(index, location.clone());
            questions.locations.entry(location).or_default().push(index);
        }
        for (namespace, name) in dataset.symlink_identifiers() {
            if !is_external_source_namespace(&namespace, &name, catalog) {
                questions.names.entry(name).or_default().push(index);
            } else if let Some(location) = identifier_location(&namespace, &name) {
                questions.locations.entry(location).or_default().push(index);
            }
        }
    }
    questions
}

impl AliasQuestions {
    pub fn location_list(&self) -> Vec<String> {
        self.locations.keys().cloned().collect()
    }

    pub fn name_list(&self) -> Vec<String> {
        self.names.keys().cloned().collect()
    }

    /// The aliases the answers make of the datasets asked about.
    pub fn answered(&self, located: &BTreeMap<String, Option<String>>, decided: &BTreeMap<String, TableDecision>) -> Aliases {
        let mut aliases = Aliases::default();
        let mut held = BTreeSet::new();
        for (location, uuid) in located {
            let (Some(uuid), Some(indices)) = (uuid, self.locations.get(location)) else {
                continue;
            };
            held.insert(location.clone());
            aliases.tables.insert(uuid.clone());
            for index in indices {
                aliases.objects.entry(*index).or_default().insert(table_object(uuid));
            }
        }
        for (name, decision) in decided {
            let Some(indices) = self.names.get(name) else { continue };
            for index in indices {
                match &decision.table_uuid {
                    Some(uuid) => aliases.objects.entry(*index).or_default().insert(table_object(uuid)),
                    None => aliases.hidden.entry(*index).or_default().insert(name.clone()),
                };
            }
            if let Some(uuid) = &decision.table_uuid {
                aliases.tables.insert(uuid.clone());
            }
        }
        aliases.located = self
            .own
            .iter()
            .filter(|(_, location)| held.contains(*location))
            .map(|(index, _)| *index)
            .collect();
        aliases
    }
}

/// The column upstreams `outputs` name outside the catalog's own names, each as a dataset of its own: one a location
/// a governed table holds is read as that table, as an input is.
pub fn upstreams_named_by_a_location(outputs: &[Dataset], catalog: &str) -> Vec<Dataset> {
    let upstreams = outputs.iter().flat_map(Dataset::column_upstreams);
    upstreams
        .filter(|(namespace, name)| is_external_source_namespace(namespace, name, catalog))
        .map(|(namespace, name)| Dataset {
            namespace,
            name,
            facets: Map::new(),
            extra: Map::new(),
        })
        .collect()
}

/// The column upstreams `outputs` name by a governed table's own name.
pub fn upstreams_named_as_tables(outputs: &[Dataset], catalog: &str) -> BTreeSet<String> {
    let upstreams = outputs.iter().flat_map(Dataset::column_upstreams);
    upstreams
        .filter(|(namespace, name)| !is_external_source_namespace(namespace, name, catalog))
        .map(|(_, name)| name)
        .collect()
}

/// The names `datasets` give the tables `objects` are, for a refusal: a caller reads names, never uuids. A dataset an
/// alias makes one of them is named too, by what the caller sent.
pub fn names_of(datasets: &[Dataset], objects: &BTreeSet<String>, aliases: &Aliases) -> BTreeSet<String> {
    let named = |(index, dataset): &(usize, &Dataset)| {
        let by_uuid = uuid_of(dataset).is_some_and(|uuid| objects.contains(&table_object(&uuid)));
        by_uuid || aliases.objects.get(index).is_some_and(|aliased| !aliased.is_disjoint(objects))
    };
    datasets.iter().enumerate().filter(named).map(|(_, dataset)| dataset.name.clone()).collect()
}

/// What a staged event names for a reader, under every identifier it uses: beside the tables its datasets carry the
/// uuids of, every table an input, an output or a column upstream is aliased to, and every hidden name as unresolved.
/// `aliases` are the answers for `named_by(events)` asked about in one list.
pub fn scoped(events: &[(&[Dataset], &[Dataset])], aliases: &Aliases, catalog: &str) -> Vec<Governed> {
    let mut index = 0;
    let mut scopes = Vec::with_capacity(events.len());
    for (inputs, outputs) in events {
        let count = inputs.len() + outputs.len() + upstreams_named_by_a_location(outputs, catalog).len();
        let (mut aliased, mut hidden) = (BTreeSet::new(), BTreeSet::new());
        for at in index..index + count {
            aliased.extend(aliases.objects.get(&at).into_iter().flatten().cloned());
            hidden.extend(aliases.hidden.get(&at).into_iter().flatten().cloned());
        }
        index += count;
        let named = governed_refs(inputs.iter().chain(outputs.iter()), &upstreams_named_as_tables(outputs, catalog), catalog);
        scopes.push(named.joined(aliased, hidden));
    }
    scopes
}

/// The datasets `scoped` asks about, in the one list it is answered for.
pub fn named_by(events: &[(&[Dataset], &[Dataset])], catalog: &str) -> Vec<Dataset> {
    let each = events.iter().flat_map(|(inputs, outputs)| {
        let located = upstreams_named_by_a_location(outputs, catalog);
        inputs.iter().chain(outputs.iter()).cloned().chain(located).collect::<Vec<_>>()
    });
    each.collect()
}
