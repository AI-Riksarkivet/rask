//! The OpenFGA decider against a real OpenFGA server holding the estate's authorization model (ADR 0226): what it
//! answers is the model's answer, a batch of any size is answered whole, and a store that cannot answer is an outage,
//! never a verdict.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[path = "support/openfga_server.rs"]
mod server;

use lakehouse_core::authz::openfga::{BREAKER_THRESHOLD, OpenFga, StoreSettings};
use lakehouse_core::authz::{AccessDecider, CHECKS_PER_BATCH};
use serde_json::json;
use server::{KEY, Server, tuple};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

const ADA: &str = "dex~ada";
const BOB: &str = "dex~bob";

fn decider(server: &Server) -> OpenFga {
    OpenFga::new(StoreSettings::new(&server.url, &server.store, &server.model, Some(server.token_file.clone()))).unwrap()
}

#[tokio::test]
async fn a_grant_allows_and_its_absence_denies_on_the_estate_model() {
    let server = Server::start().await;
    server
        .write(&[
            tuple("namespace:n1", "parent", "table:t1"),
            tuple("user:dex~ada", "reader", "namespace:n1"),
            tuple("user:dex~bob", "writer", "table:t2"),
            tuple("user:dex~bob", "maintainer", "table:t1"),
        ])
        .await;
    let decider = decider(&server);
    assert!(
        decider.check(ADA, "can_get_metadata", "table:t1").await.unwrap(),
        "a namespace's reader reads its tables"
    );
    assert!(!decider.check(ADA, "can_write_data", "table:t1").await.unwrap(), "a reader writes nothing");
    assert!(decider.check(BOB, "can_get_metadata", "table:t2").await.unwrap(), "a writer reads");
    assert!(decider.check(BOB, "can_maintain", "table:t1").await.unwrap());
    assert!(!decider.check(BOB, "can_write_data", "table:t1").await.unwrap(), "a maintainer writes no data");
    assert!(!decider.check(ADA, "can_read_event_feed", "estate:rask").await.unwrap());
    server.write(&[tuple("user:dex~ada", "event_reader", "estate:rask")]).await;
    assert!(
        decider.check(ADA, "can_read_event_feed", "estate:rask").await.unwrap(),
        "a revoke or a grant is read by the next check"
    );
}

#[tokio::test]
async fn a_time_boxed_grant_is_judged_on_the_checks_clock() {
    let server = Server::start().await;
    let boxed = |object: &str, duration: &str| {
        let mut granted = tuple("user:dex~ada", "writer", object);
        granted["condition"] = json!({"name": "non_expired_grant", "context": {"grant_time": "2020-01-01T00:00:00Z", "grant_duration": duration}});
        granted
    };
    server.write(&[boxed("table:lapsed", "1h"), boxed("table:live", "876000h")]).await;
    let decider = decider(&server);
    assert!(!decider.check(ADA, "can_write_data", "table:lapsed").await.unwrap());
    assert!(
        decider.check(ADA, "can_write_data", "table:live").await.unwrap(),
        "a live grant needs the clock the check sends"
    );
    let both = decider
        .batch_check(ADA, "can_write_data", &["table:lapsed".to_owned(), "table:live".to_owned()])
        .await
        .unwrap();
    assert_eq!(both, BTreeMap::from([("table:lapsed".to_owned(), false), ("table:live".to_owned(), true)]));
}

#[tokio::test]
async fn a_batch_larger_than_one_request_is_answered_whole() {
    let server = Server::start().await;
    let tables: Vec<String> = (0..3 * CHECKS_PER_BATCH + 7).map(|n| format!("table:t{n:03}")).collect();
    let granted: Vec<_> = tables.iter().step_by(2).map(|table| tuple("user:dex~ada", "reader", table)).collect();
    for chunk in granted.chunks(100) {
        server.write(chunk).await;
    }
    let mut asked = tables.clone();
    asked.extend(["table:a b".to_owned(), "table:x#y".to_owned(), tables[0].clone()]);
    let answered = decider(&server).batch_check(ADA, "can_get_metadata", &asked).await.unwrap();
    let mut expected: BTreeMap<String, bool> = tables.iter().enumerate().map(|(n, table)| (table.clone(), n % 2 == 0)).collect();
    expected.extend([("table:a b".to_owned(), false), ("table:x#y".to_owned(), false)]);
    assert_eq!(
        answered, expected,
        "every object answered, an id OpenFGA refuses answered no without failing the rest"
    );
}

#[tokio::test]
async fn a_listing_holds_what_the_principal_reaches() {
    let server = Server::start().await;
    server
        .write(&[
            tuple("namespace:n1", "parent", "table:t1"),
            tuple("namespace:n1", "parent", "table:t2"),
            tuple("user:dex~ada", "reader", "namespace:n1"),
            tuple("user:dex~ada", "writer", "table:t9"),
            tuple("user:dex~bob", "reader", "table:t3"),
        ])
        .await;
    let decider = decider(&server);
    let mut listed = decider.list_objects(ADA, "can_get_metadata", "table").await.unwrap();
    listed.objects.sort();
    assert_eq!(listed.objects, ["table:t1", "table:t2", "table:t9"]);
    assert!(!listed.truncated);
    assert_eq!(
        decider.list_users("can_get_metadata", "table:t1").await.unwrap(),
        [ADA],
        "a user's id is decoded to its principal key"
    );
    let objects = ["table:t3".to_owned(), "table:none".to_owned(), "not an id".to_owned()];
    let governed = decider.governed(&objects).await.unwrap();
    assert_eq!(
        governed,
        BTreeMap::from([("table:t3".to_owned(), true), ("table:none".to_owned(), false), ("not an id".to_owned(), false)])
    );
    let named = decider.check_tables(ADA, "can_get_metadata", &["wh$a$b".to_owned()]).await.unwrap();
    assert!(!named["wh$a$b"].allowed, "the store holds no names, so a name is refused");
    assert!(
        decider.locate(&["s3://b/k".to_owned()]).await.is_err(),
        "the store cannot say which table a location holds"
    );
}

#[tokio::test]
async fn the_token_is_read_on_every_request() {
    let server = Server::start().await;
    server.write(&[tuple("user:dex~ada", "reader", "table:t1")]).await;
    let decider = decider(&server);
    std::fs::write(&server.token_file, "not-the-key").unwrap();
    assert!(
        decider.check(ADA, "can_get_metadata", "table:t1").await.is_err(),
        "a refused credential is an outage"
    );
    std::fs::write(&server.token_file, KEY).unwrap();
    assert!(
        decider.check(ADA, "can_get_metadata", "table:t1").await.unwrap(),
        "the rotated token is read at once"
    );
    let tokenless = OpenFga::new(StoreSettings::new(&server.url, &server.store, &server.model, None)).unwrap();
    assert!(tokenless.check(ADA, "can_get_metadata", "table:t1").await.is_err());
}

#[tokio::test]
async fn a_stopped_store_is_unavailable_never_a_verdict() {
    let mut server = Server::start().await;
    server.write(&[tuple("user:dex~ada", "reader", "table:t1")]).await;
    let decider = decider(&server);
    assert!(decider.check(ADA, "can_get_metadata", "table:t1").await.unwrap());
    server.stop();
    let objects = ["table:t1".to_owned(), "table:t2".to_owned()];
    assert!(decider.check(ADA, "can_get_metadata", "table:t1").await.is_err());
    assert!(decider.batch_check(ADA, "can_get_metadata", &objects).await.is_err());
    assert!(decider.list_objects(ADA, "can_get_metadata", "table").await.is_err());
    assert!(decider.list_users("can_get_metadata", "table:t1").await.is_err());
    assert!(decider.governed(&objects).await.is_err());
    const { assert!(BREAKER_THRESHOLD <= 5, "the outages above open the circuit") };
    let began = Instant::now();
    assert!(decider.check(ADA, "can_get_metadata", "table:t1").await.is_err());
    assert!(
        began.elapsed() < Duration::from_millis(100),
        "an open circuit refuses without dialling or retrying"
    );
}
