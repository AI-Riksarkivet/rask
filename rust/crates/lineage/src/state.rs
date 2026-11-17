//! What lineage's doors are served with: each collaborator a value the service builds once, so a test builds the same
//! state against real ones or stand-ins.

use crate::authz::gates::{Authority, Feed, Names};
use crate::authz::names::CatalogNames;
use crate::feed::feed_key;
use crate::model::Event;
use crate::signature::Signing;
use crate::store::Store;
use lakehouse_bus::Bus;
use lakehouse_core::auth::verifier::Authenticator;
use lakehouse_core::authz::{AccessDecider, Answer, Unavailable};
use lakehouse_core::outbox::Outbox;
use lakehouse_core::signing::PublicKeySource;
use serde_json::{Value, json};
use std::sync::Arc;
use std::time::Duration;

/// The catalog's names as the doors resolve them: a table's uuids, and a namespace's uuid, as the caller sees them.
pub trait Resolver: Names {
    fn namespace<'a>(&'a self, name: &'a str, bearer: Option<&'a str>) -> Answer<'a, Option<String>>;
}

impl Resolver for CatalogNames {
    fn namespace<'a>(&'a self, name: &'a str, bearer: Option<&'a str>) -> Answer<'a, Option<String>> {
        Box::pin(self.describe("namespace", name, json!({}), bearer))
    }
}

/// Where a re-announced event is published: the bus this process holds, the topic, and how long a publish may take.
#[derive(Clone)]
pub struct Publisher {
    pub bus: Arc<Bus>,
    pub topic: String,
    pub within: Duration,
}

/// Everything a door needs. Cloning shares every collaborator.
#[derive(Clone)]
pub struct State {
    /// Who a bearer names; authentication off answers no caller.
    pub authenticator: Arc<Authenticator>,
    /// Whether OpenFGA is consulted (`RASK_FGA_ENABLED`), and the decider asked when it is.
    pub fga_enabled: bool,
    pub decider: Option<Arc<dyn AccessDecider>>,
    /// The dataset namespace the catalog names its tables under, and the object every estate privilege is checked on.
    pub catalog: String,
    pub root: String,
    pub store: Store,
    pub names: Arc<dyn Resolver>,
    /// The outbox the admin doors read and replay; `None` where the deployment keeps none.
    pub outbox: Option<Arc<Outbox>>,
    /// Where a replayed event is announced again; `None` where the process holds no bus.
    pub publisher: Option<Publisher>,
    /// The signers' published keys and who may sign, for an event that arrived signed.
    pub keys: Arc<dyn PublicKeySource + Send + Sync>,
    pub signing: Signing,
}

impl State {
    pub fn authority(&self) -> Authority<'_> {
        Authority {
            enabled: self.fga_enabled,
            decider: self.decider.as_deref(),
            catalog: &self.catalog,
            root: &self.root,
        }
    }
}

/// The feed's two lookups the gates make, read from the store.
pub struct StoreFeed<'a>(pub &'a Store);

impl Feed for StoreFeed<'_> {
    fn run_author<'a>(&'a self, run_id: &'a str) -> Answer<'a, Option<String>> {
        Box::pin(async move { self.0.run_author(run_id).await.map_err(|error| Unavailable(error.to_string())) })
    }

    fn is_recorded<'a>(&'a self, event: &'a Event) -> Answer<'a, bool> {
        Box::pin(async move {
            let stored = self.0.recorded_event(&feed_key(event)).await.map_err(|error| Unavailable(error.to_string()))?;
            let payload: Value = serde_json::from_str(&event.to_json()).unwrap_or_default();
            Ok(stored.is_some_and(|stored| payload.as_object() == Some(&stored)))
        })
    }
}
