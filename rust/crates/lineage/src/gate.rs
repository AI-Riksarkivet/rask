//! The one gate every event passes before it is recorded, whatever way it came (the model's Judge): by the bus or from
//! the outbox, its signature over the bytes that arrived and then what the author it is stamped for may record
//! (`lineage.api.fga_deps.enforce_bus_authz`); by the HTTP door, what the caller may record as itself
//! (`enforce_output_authz`); replayed by an operator, both. The lane, the relay and the doors hold this one gate, so none
//! admits what another refuses.

use crate::authz::gates::Denial;
use crate::authz::gates::{Authority, Feed, bus_authz, ingest_authz};
use crate::feed::feed_key;
use crate::model::Event;
use crate::signals::Signals;
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

fn unavailable(error: impl std::fmt::Display) -> Unavailable {
    Unavailable(error.to_string())
}

/// The feed's two lookups a gate makes, read through the catalog.
impl Feed for Store {
    fn run_author<'a>(&'a self, run_id: &'a str) -> Answer<'a, Option<String>> {
        Box::pin(async move { Store::run_author(self, run_id).await.map_err(unavailable) })
    }

    /// Byte for byte as the feed stores it: one that differs in any member is a new assertion, never a redelivery.
    fn is_recorded<'a>(&'a self, event: &'a Event) -> Answer<'a, bool> {
        Box::pin(async move {
            let stored = self.recorded_event(&feed_key(event)).await.map_err(unavailable)?;
            Ok(stored.is_some_and(|stored| serde_json::from_str::<Value>(&event.to_json()).ok() == Some(Value::Object(stored))))
        })
    }
}

pub struct Gate {
    pub signing: Signing,
    pub keys: Arc<PublishedKeys>,
    /// Whether OpenFGA is consulted (`RASK_FGA_ENABLED`), and the decider that answers it.
    pub fga_enabled: bool,
    pub decider: Option<Arc<dyn AccessDecider>>,
    /// The dataset namespace this estate's tables are named under, and the object estate privileges are checked on
    /// (`RASK_FGA_ROOT_OBJECT`).
    pub catalog: String,
    pub root: String,
    pub store: Store,
    pub signals: Arc<Signals>,
}

impl Gate {
    pub fn authority(&self) -> Authority<'_> {
        Authority {
            enabled: self.fga_enabled,
            decider: self.decider.as_deref(),
            catalog: &self.catalog,
            root: &self.root,
        }
    }

    /// The signature's verdict over `arrived`, counted; nothing is verified where signing is not enforced.
    async fn admission(&self, arrived: Json) -> Admission {
        if !self.signing.enforced() {
            return Admission::Unenforced;
        }
        // The published keys are read through a blocking client.
        let (keys, signing) = (Arc::clone(&self.keys), self.signing.clone());
        let verified = tokio::task::spawn_blocking(move || verify_arrived(&arrived, &keys.for_event(kid_of(&arrived).as_deref()), &signing)).await;
        let admission = verified.unwrap_or_else(|_| Admission::Unavailable(KeysUnavailable { identity: String::new() }));
        match &admission {
            Admission::Verified(verified) => self.signals.lineage.record_signature_verified(&verified.identity),
            Admission::Refused(refusal) => {
                self.signals.lineage.record_signature_refused(refusal.reason);
                tracing::warn!(reason = refusal.reason.label(), "lineage_signature_refused");
            }
            Admission::Unavailable(_) => tracing::warn!("lineage_signature_keys_unavailable"),
            Admission::Unenforced => {}
        }
        admission
    }

    /// The uuids of the tables `event`'s datasets alias where the author it is stamped for may record it, or why not.
    /// `arrived` is the event as it arrived (`arrived_data`), never the parsed model, which grows members the producer
    /// never signed.
    pub async fn authorize(&self, event: &Event, arrived: Json) -> Result<BTreeSet<String>, Denial> {
        let admission = self.admission(arrived).await;
        bus_authz(event, &admission, &self.signing, &self.authority(), Some(&self.store)).await
    }

    /// What `subject`, the caller of the HTTP door, may record as itself.
    pub async fn ingest(&self, event: &Event, subject: Option<&str>) -> Result<BTreeSet<String>, Denial> {
        ingest_authz(event, subject, &self.authority(), Some(&self.store)).await
    }

    /// A staged event an operator replays: the gate of its arrival over the bytes staged, and the caller's own.
    pub async fn replay(&self, event: &Event, arrived: Json, subject: Option<&str>) -> Result<BTreeSet<String>, Denial> {
        let mut aliased = self.authorize(event, arrived).await?;
        aliased.extend(self.ingest(event, subject).await?);
        Ok(aliased)
    }
}
