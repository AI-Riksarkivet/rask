//! The Rust half of `scripts/port_doors_check.sh`: lineage's doors served from the environment the Python lineage app is
//! started with, on the port named first, until it is stopped. It builds the state from real collaborators (the token
//! issuer, OpenFGA, the catalog and, where `LINEAGE_OUTBOX_URI` names one, the outbox on the store under this process's
//! own exchanged session) and is test code: the service's own wiring arrives with its settings.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::auth::issuer;
use lakehouse_core::auth::settings::AuthSettings;
use lakehouse_core::authz::AccessDecider;
use lakehouse_core::authz::openfga::{OpenFga, StoreSettings as FgaSettings};
use lakehouse_core::outbox::Outbox;
use lakehouse_core::service::health::HealthWatch;
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use lakehouse_core::storage::{S3, StoreSession, StoreSettings};
use lakehouse_tables::Catalog;
use lakehouse_tables::bearer::anonymous;
use lineage::app::router;
use lineage::authz::names::CatalogNames;
use lineage::signature::Signing;
use lineage::state::State;
use lineage::store::Store;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

#[tokio::main]
async fn main() {
    let port: u16 = std::env::args().nth(1).expect("the port to serve on").parse().unwrap();
    let auth = AuthSettings::from_environment(std::env::vars()).unwrap();
    let (authenticator, keys) = issuer::authenticator(&auth).unwrap();
    issuer::start(&keys).await;
    let fga_enabled = env("RASK_FGA_ENABLED").is_some_and(|enabled| enabled == "true");
    let decider: Option<Arc<dyn AccessDecider>> = fga_enabled.then(|| {
        let settings = FgaSettings::new(
            &env("RASK_FGA_API_URL").unwrap(),
            &env("RASK_FGA_STORE_ID").unwrap(),
            &env("RASK_FGA_MODEL_ID").unwrap(),
            env("RASK_FGA_TOKEN_FILE").map(Into::into),
        );
        Arc::new(OpenFga::new(settings).unwrap()) as Arc<dyn AccessDecider>
    });
    let catalog_url = env("LINEAGE_CATALOG_URL").unwrap();
    let dataset_namespace = env("LINEAGE_CATALOG_DATASET_NAMESPACE").unwrap_or_else(|| "lance-namespace://localhost/api/catalog".to_owned());
    let namespace = env("LINEAGE_PROVENANCE_NAMESPACE").unwrap_or_else(|| "lance_catalog$provenance".to_owned());
    let tables = Catalog::new(&catalog_url, anonymous(), Duration::from_secs(30)).unwrap();
    let store = Store::new(tables, namespace.split('$').map(str::to_owned).collect(), &dataset_namespace, fga_enabled).unwrap();
    store.ensure().await.unwrap();
    let outbox = match env("LINEAGE_OUTBOX_URI") {
        Some(uri) => {
            let settings = StoreSettings::from_environment("LINEAGE", std::env::vars()).unwrap();
            let session = Arc::new(StoreSession::new(&settings, "lineage").unwrap());
            session.exchange_at_boot().await.unwrap();
            Some(Arc::new(Outbox::new(&uri, Arc::new(S3::new(&settings, session).unwrap())).unwrap()))
        }
        None => None,
    };
    let state = State {
        authenticator: Arc::new(authenticator),
        fga_enabled,
        decider,
        catalog: dataset_namespace,
        root: env("RASK_FGA_ROOT_OBJECT").unwrap_or_else(|| "estate:rask".to_owned()),
        store,
        names: Arc::new(CatalogNames::new(&catalog_url, Duration::from_secs(30)).unwrap()),
        outbox,
        publisher: None,
        keys: Arc::new(BTreeMap::<String, Vec<String>>::new()),
        signing: Signing::default(),
    };
    let lifecycle = Lifecycle::default();
    lifecycle.mark_started();
    let shell = Shell {
        lifecycle: Arc::new(lifecycle),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: env("LINEAGE_MAX_BODY_BYTES").map_or(8 * 1024 * 1024, |bytes| bytes.parse().unwrap()),
        request_deadline: Duration::from_secs(300),
    };
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await.unwrap();
    println!("serving on {port}");
    shell.serve(listener, shell.router(router(state))).await.unwrap();
}
