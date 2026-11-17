//! Lineage's gates over the OpenFGA decider and a real OpenFGA server holding the estate's model (ADR 0226): a page is
//! filtered to what its reader sees, and an event is admitted on exactly the rungs its operation demands.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[path = "../../lakehouse-core/tests/support/openfga_server.rs"]
mod server;

use lakehouse_core::authz::AccessDecider;
use lakehouse_core::authz::openfga::{OpenFga, StoreSettings};
use lineage::authz::gates::{Authority, DatasetFilter, Kind, authorizing, bus_authz, ingest_authz};
use lineage::authz::scope::Governed;
use lineage::intake::{Authorizing, Refusal};
use lineage::model::parse_event;
use lineage::signature::{Admission, Signing};
use serde_json::{Value, json};
use server::{Server, tuple};

const CATALOG: &str = "lance-namespace://localhost/api/catalog";

fn table(name: &str, uuid: &str) -> Value {
    json!({"namespace": CATALOG, "name": name, "facets": {"rask_lance": {"tableUuid": uuid}}})
}

fn run(inputs: Vec<Value>, outputs: Vec<Value>, author: &str, operation: &str) -> Value {
    json!({
        "eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z",
        "run": {"runId": "r1", "facets": {"rask_author": {"sub": author}, "rask_lance": {"operation": operation}}},
        "job": {"namespace": "jobs", "name": "copy"}, "inputs": inputs, "outputs": outputs,
    })
}

async fn estate() -> (Server, OpenFga) {
    let server = Server::start().await;
    server
        .write(&[
            tuple("user:dex~ada", "reader", "table:raw"),
            tuple("user:dex~ada", "writer", "table:clean"),
            tuple("user:dex~bob", "reader", "table:clean"),
            tuple("user:kubernetes~system%3Aserviceaccount%3Arask%3Asweep", "maintainer", "table:clean"),
        ])
        .await;
    let decider = OpenFga::new(StoreSettings::new(&server.url, &server.store, &server.model, Some(server.token_file.clone()))).unwrap();
    (server, decider)
}

fn authority(decider: &dyn AccessDecider) -> Authority<'_> {
    Authority {
        enabled: true,
        decider: Some(decider),
        catalog: CATALOG,
        root: "estate:rask",
    }
}

#[tokio::test]
async fn a_page_is_filtered_to_what_its_reader_sees() {
    let (_server, decider) = estate().await;
    let governs = |objects: &[&str]| Governed {
        objects: objects.iter().map(|object| (*object).to_owned()).collect(),
        unresolved: Default::default(),
    };
    let page = [
        governs(&["table:raw"]),
        governs(&["table:raw", "table:clean"]),
        governs(&["table:secret"]),
        governs(&[]),
    ];
    let mut filter = DatasetFilter::new(authority(&decider), Some("dex~bob"));
    assert_eq!(
        filter.governed((0..4).collect(), |index: &usize| page[*index].clone()).await.unwrap(),
        Vec::<usize>::new()
    );
    let mut filter = DatasetFilter::new(authority(&decider), Some("dex~ada"));
    assert_eq!(filter.governed((0..4).collect(), |index: &usize| page[*index].clone()).await.unwrap(), [0, 1]);
}

#[tokio::test]
async fn an_event_is_admitted_on_the_rungs_its_operation_demands() {
    let (_server, decider) = estate().await;
    let authority = authority(&decider);
    let (raw, clean) = (table("wh$raw$pages", "raw"), table("wh$clean$pages", "clean"));
    let written = parse_event(&run(vec![raw.clone()], vec![clean.clone()], "dex~ada", "append")).unwrap();
    assert_eq!(ingest_authz(&written, Some("dex~ada"), &authority, None).await.unwrap().len(), 0);
    let refused = ingest_authz(&written, Some("dex~bob"), &authority, None).await.unwrap_err();
    assert_eq!(
        (refused.kind, refused.message.as_str()),
        (Kind::PermissionDenied, "can_write_data required on outputs: wh$clean$pages")
    );
    let nowhere = parse_event(&run(vec![], vec![table("wh$gone$t", "gone")], "dex~ada", "append")).unwrap();
    assert_eq!(
        ingest_authz(&nowhere, Some("dex~ada"), &authority, None).await.unwrap_err().kind,
        Kind::Ungoverned,
        "no grant names it"
    );

    let sweep = "kubernetes~system:serviceaccount:rask:sweep";
    let (signing, unsigned) = (Signing::default(), Admission::Unenforced);
    let compaction = parse_event(&run(vec![], vec![clean.clone()], sweep, "compact_table")).unwrap();
    let judged = authorizing(bus_authz(&compaction, &unsigned, &signing, &authority, None).await);
    assert_eq!(judged, Authorizing::Allowed(Vec::new()), "the maintainer's rung records a compaction");
    let append = parse_event(&run(vec![], vec![clean], sweep, "append")).unwrap();
    let judged = authorizing(bus_authz(&append, &unsigned, &signing, &authority, None).await);
    assert_eq!(judged, Authorizing::Refused(Refusal::Denied), "and no write of data");
}
