//! The gate an event that reached lineage without a caller passes, by the bus or from the outbox: its signature over the
//! bytes that arrived, then what the author it is stamped for may record (`lineage.api.fga_deps.enforce_bus_authz`). The
//! bus lane and the relay pass this one gate, so neither admits what the other refuses.

use crate::authz::gates::{Authority, Denial, Feed, bus_authz};
use crate::feed::feed_key;
use crate::model::Event;
use crate::signature::{Admission, Signing, verify_arrived};
use crate::store::Store;
use lakehouse_core::authz::{AccessDecider, Answer, Unavailable};
use lakehouse_core::pywire::json::{self, Json};
use lakehouse_core::secrets::published::PublishedKeys;
use lakehouse_core::signing::KeysUnavailable;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

/// The `data` of an envelope's text exactly as it arrived, which a signature covers; an empty object where there is none.
pub fn arrived_data(envelope: &[u8]) -> Json {
    let parsed = std::str::from_utf8(envelope).ok().and_then(|text| json::parse(text).ok());
    match parsed.as_ref().and_then(|envelope| envelope.get("data")) {
        Some(data @ Json::Object(_)) => data.clone(),
        _ => Json::Object(BTreeMap::new()),
    }
}

/// The key id the event's signature names, which paces the reads a key id met unknown may force.
fn kid_of(arrived: &Json) -> Option<String> {
    let holder = ["run", "dataset"].into_iter().find_map(|holder| match arrived.get(holder) {
        Some(found @ Json::Object(_)) => Some(found),
        _ => None,
    })?;
    holder.get("facets")?.get("rask_signature")?.get("kid")?.as_str().map(str::to_owned)
}

/// The feed's two lookups, read through the catalog.
struct Recorded<'s>(&'s Store);

fn unavailable(error: impl std::fmt::Display) -> Unavailable {
    Unavailable(error.to_string())
}

impl Feed for Recorded<'_> {
    fn run_author<'a>(&'a self, run_id: &'a str) -> Answer<'a, Option<String>> {
        Box::pin(async move { self.0.run_author(run_id).await.map_err(unavailable) })
    }

    /// Byte for byte as the feed stores it: one that differs in any member is a new assertion, never a redelivery.
    fn is_recorded<'a>(&'a self, event: &'a Event) -> Answer<'a, bool> {
        Box::pin(async move {
            let stored = self.0.recorded_event(&feed_key(event)).await.map_err(unavailable)?;
            Ok(stored.is_some_and(|stored| serde_json::from_str::<Value>(&event.to_json()).ok() == Some(Value::Object(stored))))
        })
    }
}

pub struct Gate {
    pub signing: Signing,
    keys: Arc<PublishedKeys>,
    /// Whether OpenFGA is consulted (`RASK_FGA_ENABLED`), and the decider that answers it.
    fga_enabled: bool,
    decider: Option<Arc<dyn AccessDecider>>,
    catalog: String,
    root: String,
    store: Store,
}

impl Gate {
    /// `catalog` is the dataset namespace this estate's tables are named under, `root` the object estate privileges are
    /// checked on (`RASK_FGA_ROOT_OBJECT`).
    pub fn new(
        signing: Signing,
        keys: Arc<PublishedKeys>,
        fga_enabled: bool,
        decider: Option<Arc<dyn AccessDecider>>,
        catalog: &str,
        root: &str,
        store: Store,
    ) -> Self {
        Gate {
            signing,
            keys,
            fga_enabled,
            decider,
            catalog: catalog.to_owned(),
            root: root.to_owned(),
            store,
        }
    }

    /// The uuids of the tables `event`'s datasets alias where its author may record it, or why not. `arrived` is the
    /// event as it arrived (`arrived_data`), never the parsed model, which grows members the producer never signed.
    pub async fn authorize(&self, event: &Event, arrived: Json) -> Result<BTreeSet<String>, Denial> {
        let admission = if self.signing.enforced() {
            // The published keys are read through a blocking client.
            let (keys, signing) = (Arc::clone(&self.keys), self.signing.clone());
            let verified = tokio::task::spawn_blocking(move || verify_arrived(&arrived, &keys.for_event(kid_of(&arrived).as_deref()), &signing)).await;
            verified.unwrap_or_else(|_| Admission::Unavailable(KeysUnavailable { identity: String::new() }))
        } else {
            Admission::Unenforced
        };
        let authority = Authority {
            enabled: self.fga_enabled,
            decider: self.decider.as_deref(),
            catalog: &self.catalog,
            root: &self.root,
        };
        bus_authz(event, &admission, &self.signing, &authority, Some(&Recorded(&self.store))).await
    }
}
