//! The view of an event a notifier targets by: who it is for and what it is about, and nothing of what it carries.

use lakehouse_core::events::{AUTHOR_FACET, LANCE_FACET, SIGNATURE_FACET};
use lakehouse_core::naming::is_external_source_namespace;
use serde_json::{Map, Value};
use std::collections::BTreeSet;

const FIELDS: [&str; 3] = ["eventType", "eventTime", "producer"];
const TARGETING_FACETS: [&str; 3] = [AUTHOR_FACET, LANCE_FACET, SIGNATURE_FACET];
const RUN_FACETS: [&str; 4] = [AUTHOR_FACET, LANCE_FACET, SIGNATURE_FACET, "parent"];
const OUTPUT_FACETS: [&str; 1] = [LANCE_FACET];
const DATASET_FIELDS: [&str; 2] = ["namespace", "name"];

fn kept(holder: &Map<String, Value>, keys: &[&str]) -> Map<String, Value> {
    keys.iter().filter_map(|key| Some(((*key).to_owned(), holder.get(*key)?.clone()))).collect()
}

/// `bag` with only the facets in `keep`; a bag that is no object is passed as it is.
fn facets(bag: &Value, keep: &[&str]) -> Value {
    bag.as_object().map_or_else(|| bag.clone(), |bag| Value::Object(kept(bag, keep)))
}

/// A dataset's name and the facets in `keep`. An output with none of them carries no `facets` member.
fn dataset(item: &Value, keep: &[&str], always_facets: bool) -> Value {
    let Some(members) = item.as_object() else {
        return item.clone();
    };
    let mut named = kept(members, &DATASET_FIELDS);
    if let Some(bag) = members.get("facets") {
        let view = facets(bag, keep);
        let empty = match &view {
            Value::Object(view) => view.is_empty(),
            other => !truthy(other),
        };
        if !empty || always_facets {
            named.insert("facets".to_owned(), view);
        }
    }
    Value::Object(named)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(members) => !members.is_empty(),
    }
}

/// The targeting view of a stored `event`.
pub fn targeting_view(event: &Map<String, Value>) -> Map<String, Value> {
    let mut view = kept(event, &FIELDS);
    if let Some(run) = event.get("run") {
        let shown = run.as_object().map_or_else(
            || run.clone(),
            |run| {
                let mut shown = kept(run, &["runId"]);
                if let Some(bag) = run.get("facets") {
                    shown.insert("facets".to_owned(), facets(bag, &RUN_FACETS));
                }
                Value::Object(shown)
            },
        );
        view.insert("run".to_owned(), shown);
    }
    if let Some(outputs) = event.get("outputs") {
        let shown = outputs.as_array().map_or_else(
            || outputs.clone(),
            |outputs| outputs.iter().map(|item| dataset(item, &OUTPUT_FACETS, false)).collect(),
        );
        view.insert("outputs".to_owned(), shown);
    }
    if let Some(item) = event.get("dataset") {
        view.insert("dataset".to_owned(), dataset(item, &TARGETING_FACETS, true));
    }
    view
}

/// The catalog datasets the column lineage of `event`'s outputs reads from.
pub fn column_lineage_datasets(event: &Map<String, Value>, catalog: &str) -> BTreeSet<String> {
    let outputs = event.get("outputs").and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
    let fields = outputs
        .iter()
        .filter_map(|output| output.get("facets")?.get("columnLineage")?.get("fields")?.as_object());
    let inputs = fields
        .flat_map(Map::values)
        .filter_map(|field| field.as_object()?.get("inputFields")?.as_array())
        .flatten();
    let named = inputs.filter_map(|input| {
        let input = input.as_object()?;
        let name = input.get("name").filter(|name| truthy(name))?;
        let name = name.as_str().map_or_else(|| name.to_string(), str::to_owned);
        let namespace = input.get("namespace").and_then(Value::as_str).unwrap_or_default();
        (!is_external_source_namespace(namespace, &name, catalog)).then_some(name)
    });
    named.collect()
}
