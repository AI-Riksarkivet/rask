//! A governed table's name resolved to the uuids the catalog keys it by, as the caller, and stamped on the event.
//!
//! A table's identity across a rename is its uuid, so lineage records a governed table by it. The catalog stamps the
//! uuids on every event it emits; an outside producer's event names its tables only, and a uuid in its body is a claim,
//! so each is replaced by what the catalog answers the caller. Never as this service: a name reaches exactly the tables
//! the caller may see, and lineage holds no grant to describe the estate.

use super::gates::Names;
use super::scope::{READ, identifier_location};
use crate::model::{Dataset, Event};
use lakehouse_core::authz::{AccessDecider, Answer, Unavailable};
use lakehouse_core::events::{AUTHOR_FACET, DatasetUuids, LANCE_FACET};
use lakehouse_core::naming::{CATALOG_DELIMITER, is_external_source_namespace};
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

/// The producer named on the uuids stamped on an outside producer's datasets.
pub const NAMES_PRODUCER: &str = "https://github.com/AI-Riksarkivet/rask/tree/main/services/lineage/src/lineage/services/names.py";
/// The producer named on the author facet the HTTP door stamps.
pub const AUTHOR_PRODUCER: &str = "https://github.com/AI-Riksarkivet/rask/tree/main/services/lineage/src/lineage/api/fga_deps.py";
const FACET_SCHEMAS: &str = "https://raw.githubusercontent.com/AI-Riksarkivet/rask/facets-1.0.0/spec/facets/rask";
/// The property a catalog object keeps its uuid under.
const UUID_PROPERTY: &str = "rask.uuid";

/// Resolves names through the catalog's DescribeTable and DescribeNamespace, under the caller's own bearer.
pub struct CatalogNames {
    base: String,
    client: reqwest::Client,
}

impl CatalogNames {
    pub fn new(catalog_url: &str, timeout: Duration) -> Result<Self, String> {
        // No redirect is followed, so the caller's bearer reaches no host but the catalog.
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| format!("the HTTP client for the catalog cannot be built: {error}"))?;
        Ok(CatalogNames {
            base: catalog_url.trim_end_matches('/').to_owned(),
            client,
        })
    }

    /// The uuid the catalog keeps `name` under, or `None` where it names nothing the caller may see. Any other answer
    /// is an outage: a table recorded under a name it should have been keyed by a uuid would split its lineage.
    async fn describe(&self, kind: &str, name: &str, body: Value, bearer: Option<&str>) -> Result<Option<String>, Unavailable> {
        let id = utf8_percent_encode(name, NON_ALPHANUMERIC);
        let delimiter = utf8_percent_encode(CATALOG_DELIMITER, NON_ALPHANUMERIC);
        let url = format!("{}/v1/{kind}/{id}/describe?delimiter={delimiter}", self.base);
        let mut request = self.client.post(url).header(CONTENT_TYPE, "application/json");
        if let Some(bearer) = bearer.filter(|bearer| !bearer.is_empty()) {
            let mut header = reqwest::header::HeaderValue::from_str(&format!("Bearer {bearer}")).map_err(|_| Unavailable::new())?;
            header.set_sensitive(true);
            request = request.header(AUTHORIZATION, header);
        }
        let response = request.body(body.to_string()).send().await.map_err(|error| {
            tracing::error!(error = %error.without_url(), "catalog_unavailable");
            Unavailable("the catalog is unavailable".to_owned())
        })?;
        match response.status().as_u16() {
            200 => {}
            401 | 403 | 404 => return Ok(None),
            status => {
                tracing::error!(status, "catalog_unavailable");
                return Err(Unavailable("the catalog is unavailable".to_owned()));
            }
        }
        let described: Value = serde_json::from_slice(&response.bytes().await.map_err(|_| Unavailable::new())?).map_err(|_| Unavailable::new())?;
        let uuid = described
            .get("properties")
            .and_then(|properties| properties.get(UUID_PROPERTY))
            .and_then(Value::as_str);
        Ok(uuid.filter(|uuid| !uuid.is_empty()).map(str::to_owned))
    }
}

impl Names for CatalogNames {
    fn table<'a>(&'a self, name: &'a str, bearer: Option<&'a str>) -> Answer<'a, DatasetUuids> {
        Box::pin(async move {
            // The uuid is all this reads, so no credential is minted for the caller's location.
            let Some(table_uuid) = self.describe("table", name, json!({"vend_credentials": false}), bearer).await? else {
                return Ok(DatasetUuids::default());
            };
            let parent = name
                .rsplit_once(CATALOG_DELIMITER)
                .map(|(parent, _)| parent)
                .filter(|parent| !parent.is_empty());
            let namespace_uuid = match parent {
                Some(parent) => self.describe("namespace", parent, json!({}), bearer).await?,
                None => None,
            };
            Ok(DatasetUuids {
                table_uuid: Some(table_uuid),
                namespace_uuid,
            })
        })
    }
}

/// `facets` with the lance facet naming exactly `uuids`: one set where it has one and removed where it has none, so a
/// stamp is the whole answer and no unchecked claim survives it. The facet's other members stay.
pub fn with_dataset_uuids(facets: &Map<String, Value>, uuids: &DatasetUuids, producer: &str) -> Map<String, Value> {
    let mut facets = facets.clone();
    let mut lance = facets.get(LANCE_FACET).and_then(Value::as_object).cloned().unwrap_or_default();
    for (key, value) in [("tableUuid", &uuids.table_uuid), ("namespaceUuid", &uuids.namespace_uuid)] {
        match value.as_ref().filter(|value| !value.is_empty()) {
            Some(value) => lance.insert(key.to_owned(), Value::String(value.clone())),
            None => lance.shift_remove(key),
        };
    }
    if lance.keys().any(|key| key != "_producer" && key != "_schemaURL") {
        lance.insert("_producer".to_owned(), Value::String(producer.to_owned()));
        lance.insert(
            "_schemaURL".to_owned(),
            Value::String(format!("{FACET_SCHEMAS}/RaskLanceFacet.json#/$defs/RaskLanceFacet")),
        );
        facets.insert(LANCE_FACET.to_owned(), Value::Object(lance));
    } else {
        facets.shift_remove(LANCE_FACET);
    }
    facets
}

fn datasets_of(event: &mut Event) -> Vec<&mut Dataset> {
    match event {
        Event::Run(run) => {
            let run = &mut **run;
            run.inputs.iter_mut().chain(run.outputs.iter_mut()).collect()
        }
        Event::Dataset(dataset) => vec![&mut dataset.dataset],
    }
}

/// Name every governed dataset `event` names by the uuids its name resolves to for the caller, and no other. Where the
/// decider resolves names and the caller's subject is known, the names are asked about in one question; otherwise each
/// is described under the caller's bearer. A dataset named by a storage location is named by the uuid of the table the
/// catalog says holds it.
pub async fn stamp_resolved_uuids(
    event: &mut Event,
    names: &dyn Names,
    bearer: Option<&str>,
    decider: Option<&dyn AccessDecider>,
    subject: Option<&str>,
    catalog: &str,
) -> Result<(), Unavailable> {
    let external = |dataset: &Dataset| is_external_source_namespace(&dataset.namespace, &dataset.name, catalog);
    let datasets = datasets_of(event);
    let governed: Vec<String> = datasets
        .iter()
        .filter(|dataset| !external(dataset))
        .map(|dataset| dataset.name.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut resolved: BTreeMap<String, DatasetUuids> = BTreeMap::new();
    match (decider, subject) {
        (Some(decider), Some(subject)) if decider.resolves_names() => {
            if !governed.is_empty() {
                for (name, decision) in decider.check_tables(subject, READ, &governed).await? {
                    let uuids = DatasetUuids {
                        table_uuid: decision.table_uuid,
                        namespace_uuid: decision.namespace_uuid,
                    };
                    resolved.insert(name, uuids);
                }
            }
        }
        _ => {
            for name in &governed {
                resolved.insert(name.clone(), names.table(name, bearer).await?);
            }
        }
    }
    let locations: Vec<String> = datasets
        .iter()
        .filter(|dataset| external(dataset))
        .filter_map(|dataset| identifier_location(&dataset.namespace, &dataset.name))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let located = match decider {
        Some(decider) if !locations.is_empty() => decider.locate(&locations).await?,
        _ => BTreeMap::new(),
    };
    for dataset in datasets {
        let uuids = if external(dataset) {
            let location = identifier_location(&dataset.namespace, &dataset.name);
            DatasetUuids {
                table_uuid: location.and_then(|location| located.get(&location).cloned().flatten()),
                namespace_uuid: None,
            }
        } else {
            resolved.get(&dataset.name).cloned().unwrap_or_default()
        };
        dataset.facets = with_dataset_uuids(&dataset.facets, &uuids, NAMES_PRODUCER);
    }
    Ok(())
}

/// Bind the author to the verified caller, never to what the body claims. A dataset event has no run, so its author
/// rides the dataset's facet, which is where it is read from.
pub fn enforce_author(event: &mut Event, subject: Option<&str>) {
    let Some(subject) = subject else { return };
    let stamp = json!({
        "_producer": AUTHOR_PRODUCER,
        "_schemaURL": format!("{FACET_SCHEMAS}/RaskAuthorFacet.json#/$defs/RaskAuthorFacet"),
        "name": subject,
        "sub": subject,
    });
    let facets = match event {
        Event::Run(run) => &mut run.run.facets,
        Event::Dataset(dataset) => &mut dataset.dataset.facets,
    };
    facets.insert(AUTHOR_FACET.to_owned(), stamp);
}
