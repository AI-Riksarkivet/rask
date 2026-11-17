//! An OpenLineage event as lineage takes it: what it must hold, what it may not exceed, and what is read from it.
//!
//! An event keeps every member it arrived with. The members lineage reads are typed; the rest ride along in `extra`
//! and are stored as sent.

use lakehouse_core::events::{AUTHOR_FACET, BRANCH_IDENTIFIER, LANCE_FACET, SIGNATURE_FACET, SYMLINKS_FACET, static_event_id};
use lakehouse_core::naming::is_external_source_namespace;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;

/// The most datasets a run event may name as inputs, and as outputs.
pub const MAX_DATASETS: usize = 64;
/// The most bytes an event may weigh as JSON.
pub const MAX_EVENT_BYTES: usize = 1024 * 1024;
/// The most distinct datasets, symlinks and column upstreams an event may name: one authorization question's worth.
pub const MAX_NAMED_IDENTIFIERS: usize = 1000;

type Facets = Map<String, Value>;

/// Why an event is refused before anything is asked about its caller.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum EventError {
    #[error("{0}")]
    Malformed(String),
    #[error("a run event names at most {MAX_DATASETS} {0}")]
    TooManyDatasets(&'static str),
    #[error("a DatasetEvent may not carry {0}: the spec forbids both on a static metadata event")]
    RunOrJobOnDatasetEvent(String),
    #[error("the event weighs {0} bytes as JSON; lineage keeps at most {MAX_EVENT_BYTES}")]
    TooHeavy(usize),
    #[error("the event names {0} distinct datasets, symlinks and column upstreams; lineage takes at most {MAX_NAMED_IDENTIFIERS}")]
    TooManyNames(usize),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Dataset {
    pub namespace: String,
    pub name: String,
    #[serde(default)]
    pub facets: Facets,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Job {
    pub namespace: String,
    pub name: String,
    #[serde(default)]
    pub facets: Facets,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Run {
    #[serde(rename = "runId", alias = "run_id")]
    pub run_id: String,
    #[serde(default)]
    pub facets: Facets,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunEvent {
    #[serde(rename = "eventType", alias = "event_type")]
    pub event_type: String,
    #[serde(rename = "eventTime", alias = "event_time")]
    pub event_time: String,
    #[serde(default)]
    pub producer: Option<String>,
    pub run: Run,
    pub job: Job,
    #[serde(default)]
    pub inputs: Vec<Dataset>,
    #[serde(default)]
    pub outputs: Vec<Dataset>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// A change to a dataset that no run performed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DatasetEvent {
    #[serde(rename = "eventTime", alias = "event_time")]
    pub event_time: String,
    #[serde(default)]
    pub producer: Option<String>,
    pub dataset: Dataset,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Run(Box<RunEvent>),
    Dataset(Box<DatasetEvent>),
}

/// A JSON value Python would take as true in a condition.
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

/// A member that is a non-empty string.
fn text<'a>(holder: &'a Facets, key: &str) -> Option<&'a str> {
    holder.get(key).and_then(Value::as_str).filter(|value| !value.is_empty())
}

/// The first owner an `ownership` facet names.
fn first_owner(facets: &Facets, only_first: bool) -> Option<String> {
    let owners = facets.get("ownership")?.as_object()?.get("owners")?.as_array()?;
    let named = |owner: &Value| owner.as_object().and_then(|owner| text(owner, "name")).map(str::to_owned);
    if only_first {
        owners.first().and_then(named)
    } else {
        owners.iter().find_map(named)
    }
}

/// Who an author facet names: its `name`, else its `sub`.
fn authored(facet: &Facets) -> Option<String> {
    text(facet, "name").or_else(|| text(facet, "sub")).map(str::to_owned)
}

impl Dataset {
    /// The name lineage's edges know the dataset by: its catalog name, or `namespace/name` outside the catalog.
    pub fn lineage_name(&self, catalog: &str) -> String {
        if is_external_source_namespace(&self.namespace, &self.name, catalog) {
            format!("{}/{}", self.namespace, self.name)
        } else {
            self.name.clone()
        }
    }

    /// The facet `name`, from the dataset's facets or its output or input facets, where it is an object.
    pub fn facet(&self, name: &str) -> Option<&Facets> {
        let slots = [
            Some(&self.facets),
            self.extra.get("outputFacets").and_then(Value::as_object),
            self.extra.get("inputFacets").and_then(Value::as_object),
        ];
        slots.into_iter().flatten().find_map(|slot| slot.get(name).and_then(Value::as_object))
    }

    /// The other names the dataset is known by.
    pub fn symlink_identifiers(&self) -> Vec<(String, String)> {
        let Some(identifiers) = self.facet(SYMLINKS_FACET).and_then(|facet| facet.get("identifiers")).and_then(Value::as_array) else {
            return Vec::new();
        };
        let named = |entry: &Value| {
            let entry = entry.as_object()?;
            Some((entry.get("namespace")?.as_str()?.to_owned(), text(entry, "name")?.to_owned()))
        };
        identifiers.iter().filter_map(named).collect()
    }

    /// The datasets the dataset's column lineage reads from.
    pub fn column_upstreams(&self) -> Vec<(String, String)> {
        let Some(fields) = self.facet("columnLineage").and_then(|facet| facet.get("fields")).and_then(Value::as_object) else {
            return Vec::new();
        };
        let upstream = |input: &Value| {
            let input = input.as_object()?;
            let name = input.get("name").filter(|name| truthy(name))?;
            input.get("field").filter(|field| truthy(field))?;
            let namespace = input.get("namespace").filter(|namespace| truthy(namespace)).map(plain).unwrap_or_default();
            Some((namespace, plain(name)))
        };
        let inputs = fields.values().filter_map(|spec| spec.as_object()?.get("inputFields")?.as_array());
        inputs.flatten().filter_map(upstream).collect()
    }
}

/// A scalar as text: a string as it is, anything else as its JSON.
fn plain(value: &Value) -> String {
    value.as_str().map_or_else(|| value.to_string(), str::to_owned)
}

impl RunEvent {
    fn lance_facet(&self) -> Option<&Facets> {
        self.run.facets.get(LANCE_FACET).and_then(Value::as_object)
    }

    pub fn author(&self) -> Option<String> {
        match self.run.facets.get(AUTHOR_FACET) {
            Some(Value::Object(facet)) => authored(facet),
            Some(Value::String(author)) if !author.is_empty() => Some(author.clone()),
            _ => None,
        }
        .or_else(|| first_owner(&self.job.facets, false))
    }

    pub fn is_success(&self) -> bool {
        self.event_type.eq_ignore_ascii_case("COMPLETE")
    }

    /// The branch the run wrote, where it wrote one.
    pub fn write_ref(&self) -> Option<&str> {
        text(self.lance_facet()?, "ref")
    }

    pub fn write_ref_identifier(&self) -> Option<&str> {
        self.write_ref()?;
        text(self.lance_facet()?, BRANCH_IDENTIFIER)
    }
}

impl DatasetEvent {
    fn operation(&self) -> &str {
        self.dataset
            .facets
            .get(LANCE_FACET)
            .and_then(Value::as_object)
            .and_then(|lance| text(lance, "operation"))
            .unwrap_or_default()
    }

    /// The id the feed keeps the event under: derived from what it describes, so a repeat is the same event.
    pub fn feed_id(&self) -> String {
        static_event_id(
            self.producer.as_deref().unwrap_or_default(),
            &self.dataset.namespace,
            &self.dataset.name,
            self.operation(),
            &self.event_time,
        )
    }

    pub fn author(&self) -> Option<String> {
        let facets = &self.dataset.facets;
        facets
            .get(AUTHOR_FACET)
            .and_then(Value::as_object)
            .and_then(authored)
            .or_else(|| first_owner(facets, true))
    }
}

impl Event {
    pub fn inputs(&self) -> &[Dataset] {
        match self {
            Event::Run(event) => &event.inputs,
            Event::Dataset(_) => &[],
        }
    }

    pub fn outputs(&self) -> &[Dataset] {
        match self {
            Event::Run(event) => &event.outputs,
            Event::Dataset(event) => std::slice::from_ref(&event.dataset),
        }
    }

    pub fn author(&self) -> Option<String> {
        match self {
            Event::Run(event) => event.author(),
            Event::Dataset(event) => event.author(),
        }
    }

    pub fn event_time(&self) -> &str {
        match self {
            Event::Run(event) => &event.event_time,
            Event::Dataset(event) => &event.event_time,
        }
    }

    /// The event as the JSON the feed stores.
    pub fn to_json(&self) -> String {
        let written = match self {
            Event::Run(event) => serde_json::to_string(event),
            Event::Dataset(event) => serde_json::to_string(event),
        };
        // Every member is a string, a list or a JSON value, so writing cannot fail.
        written.unwrap_or_default()
    }

    fn bounded(self) -> Result<Self, EventError> {
        let weight = self.to_json().len();
        if weight > MAX_EVENT_BYTES {
            return Err(EventError::TooHeavy(weight));
        }
        let mut named = HashSet::new();
        for dataset in self.inputs().iter().chain(self.outputs()) {
            named.insert((dataset.namespace.clone(), dataset.name.clone()));
            named.extend(dataset.symlink_identifiers());
            named.extend(dataset.column_upstreams());
        }
        if named.len() > MAX_NAMED_IDENTIFIERS {
            return Err(EventError::TooManyNames(named.len()));
        }
        Ok(self)
    }
}

/// The event `data` is, or why lineage does not take it.
///
/// An object with a `dataset` and no `run` is a dataset event; anything else is read as a run event.
pub fn parse_event(data: &Value) -> Result<Event, EventError> {
    let malformed = |error: serde_json::Error| EventError::Malformed(error.to_string());
    let members = data.as_object();
    if members.is_some_and(|members| members.contains_key("dataset") && !members.contains_key("run")) {
        let named: Vec<&str> = ["job", "run"]
            .into_iter()
            .filter(|member| data.get(member).is_some_and(|value| !value.is_null()))
            .collect();
        if !named.is_empty() {
            return Err(EventError::RunOrJobOnDatasetEvent(named.join(" or ")));
        }
        return Event::Dataset(Box::new(DatasetEvent::deserialize(data).map_err(malformed)?)).bounded();
    }
    let event = RunEvent::deserialize(data).map_err(malformed)?;
    if event.inputs.len() > MAX_DATASETS {
        return Err(EventError::TooManyDatasets("inputs"));
    }
    if event.outputs.len() > MAX_DATASETS {
        return Err(EventError::TooManyDatasets("outputs"));
    }
    Event::Run(Box::new(event)).bounded()
}

/// `raw` with the signature facet removed from its run and its dataset.
///
/// A signature arrives on the bus, where it is verified. At the HTTP door it is a claim nobody checked.
pub fn without_signature_facet(raw: &Value) -> Value {
    let mut cleared = raw.clone();
    for holder in ["run", "dataset"] {
        if let Some(facets) = cleared
            .get_mut(holder)
            .and_then(Value::as_object_mut)
            .and_then(|section| section.get_mut("facets"))
            .and_then(Value::as_object_mut)
        {
            facets.shift_remove(SIGNATURE_FACET);
        }
    }
    cleared
}

/// The subject the author facet of `raw`'s run or dataset names, where it names one.
pub fn author_sub_from_payload(raw: &Value) -> Option<&str> {
    ["run", "dataset"].into_iter().find_map(|holder| {
        let sub = raw
            .get(holder)?
            .as_object()?
            .get("facets")?
            .as_object()?
            .get(AUTHOR_FACET)?
            .as_object()?
            .get("sub")?
            .as_str()?;
        (!sub.trim().is_empty()).then_some(sub)
    })
}

/// The run id `raw` carries, where it carries one.
pub fn run_id_from_payload(raw: &Value) -> Option<&str> {
    let run_id = raw.get("run")?.as_object()?.get("runId")?.as_str()?;
    (!run_id.trim().is_empty()).then_some(run_id)
}
