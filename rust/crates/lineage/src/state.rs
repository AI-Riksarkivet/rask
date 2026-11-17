//! What lineage's doors are served with: each collaborator a value the service builds once, so a test builds the same
//! state against real ones or stand-ins.

use crate::authz::gates::Names;
use crate::authz::names::CatalogNames;
use crate::gate::Gate;
use lakehouse_bus::Bus;
use lakehouse_core::auth::verifier::Authenticator;
use lakehouse_core::authz::Answer;
use lakehouse_core::outbox::Outbox;
use serde_json::json;
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
    /// Who may write an event, the store it is recorded in and what is counted of it: the lanes' own gate.
    pub gate: Arc<Gate>,
    pub names: Arc<dyn Resolver>,
    /// The outbox the admin doors read and replay; `None` where the deployment keeps none.
    pub outbox: Option<Arc<Outbox>>,
    /// Where a replayed event is announced again; `None` where the process holds no bus.
    pub publisher: Option<Publisher>,
}
