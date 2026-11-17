//! The client against the real Python catalog app on real pylance (`scripts/port_catalog_fixture.py`, ADR 0222).
//!
//! Skipped, with a message, when `RASK_PORT_CATALOG_URL` names no catalog. Run:
//!
//! ```text
//! uv run scripts/port_catalog_fixture.py --port 8765
//! cd rust && RASK_PORT_CATALOG_URL=http://127.0.0.1:8765 cargo test -p lakehouse-tables --test catalog
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use arrow_array::{Array, Int64Array, RecordBatch, StringArray, UInt64Array};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use lakehouse_core::service::problem::ErrorCode;
use lakehouse_tables::filters::{in_lists, sql_literal};
use lakehouse_tables::{Catalog, Rows, Scan, Table, bearer};
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::Duration;

fn catalog() -> Option<Catalog> {
    let Ok(url) = std::env::var("RASK_PORT_CATALOG_URL") else {
        eprintln!("skipped: RASK_PORT_CATALOG_URL names no catalog; start one with `uv run scripts/port_catalog_fixture.py`");
        return None;
    };
    Some(Catalog::new(&url, bearer::anonymous(), Duration::from_secs(60)).unwrap())
}

/// A table of its own for each test, in the namespace the fixture made, so a run never meets an earlier one's rows.
fn fresh(catalog: &Catalog) -> Table {
    catalog.table(vec!["lance_catalog".into(), "port".into(), format!("t{}", uuid::Uuid::new_v4().simple())])
}

/// The shape of lineage's cursor table: a key Lance holds as the table's primary key, and a sequence.
fn schema() -> SchemaRef {
    let key = Field::new("key", DataType::Utf8, false).with_metadata(HashMap::from([("lance-schema:unenforced-primary-key".to_owned(), "true".to_owned())]));
    Arc::new(Schema::new(vec![key, Field::new("seq", DataType::Int64, true)]))
}

fn rows(pairs: &[(&str, i64)]) -> Rows {
    let keys = StringArray::from_iter_values(pairs.iter().map(|(key, _)| *key));
    let seqs = Int64Array::from_iter_values(pairs.iter().map(|(_, seq)| *seq));
    RecordBatch::try_new(schema(), vec![Arc::new(keys), Arc::new(seqs)]).unwrap().into()
}

/// Each row's key and seq, by key.
fn by_key(rows: &Rows) -> BTreeMap<String, i64> {
    let mut found = BTreeMap::new();
    for batch in &rows.batches {
        let keys = batch.column_by_name("key").unwrap().as_any().downcast_ref::<StringArray>().unwrap();
        let seqs = batch.column_by_name("seq").unwrap().as_any().downcast_ref::<Int64Array>().unwrap();
        for at in 0..batch.num_rows() {
            found.insert(keys.value(at).to_owned(), seqs.value(at));
        }
    }
    found
}

fn row_ids(rows: &Rows) -> Vec<u64> {
    let mut ids: Vec<u64> = rows
        .batches
        .iter()
        .flat_map(|batch| {
            batch
                .column_by_name("_rowid")
                .unwrap()
                .as_any()
                .downcast_ref::<UInt64Array>()
                .unwrap()
                .values()
                .to_vec()
        })
        .collect();
    ids.sort_unstable();
    ids
}

async fn created(catalog: &Catalog) -> Table {
    let table = fresh(catalog);
    table.create(schema()).await.unwrap();
    table
}

#[tokio::test]
async fn a_create_keeps_a_table_that_exists() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    let version = table.version().await.unwrap();
    let uuid = table
        .describe()
        .await
        .unwrap()
        .uuid()
        .map(str::to_owned)
        .expect("the catalog mints a uuid for every table");
    table.merge_insert_only(&rows(&[("a", 1)]), "key").await.unwrap();

    table.create(schema()).await.unwrap();

    assert_eq!(table.count_rows(None, None).await.unwrap(), 1, "a create of a table that exists keeps its rows");
    assert_eq!(
        table.describe().await.unwrap().uuid(),
        Some(uuid.as_str()),
        "and the table it describes is the same one"
    );
    assert_eq!(table.version().await.unwrap(), version + 1, "only the write moved its version");
}

#[tokio::test]
async fn an_insert_only_merge_lands_a_key_once() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;

    table.merge_insert_only(&rows(&[("a", 1), ("b", 2)]), "key").await.unwrap();
    table.merge_insert_only(&rows(&[("b", 20), ("c", 3)]), "key").await.unwrap();
    table.merge_insert_only(&rows(&[("b", 200)]), "key").await.unwrap();

    let read = table.read(&Scan::default()).await.unwrap();
    assert_eq!(by_key(&read), BTreeMap::from([("a".into(), 1), ("b".into(), 2), ("c".into(), 3)]));
}

#[tokio::test]
async fn an_upsert_updates_a_row_only_where_its_condition_holds() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    let forward = Some("source.seq > target.seq");
    let seq = async || by_key(&table.read(&Scan::default()).await.unwrap())["f"];

    table.merge_upsert(&rows(&[("f", 5)]), "key", forward).await.unwrap();
    assert_eq!(seq().await, 5, "an unmatched row is inserted");
    table.merge_upsert(&rows(&[("f", 3)]), "key", forward).await.unwrap();
    assert_eq!(seq().await, 5, "a matched row whose condition fails is kept");
    table.merge_upsert(&rows(&[("f", 7)]), "key", forward).await.unwrap();
    assert_eq!(seq().await, 7, "a matched row whose condition holds is updated");
    table.merge_upsert(&rows(&[("f", 2)]), "key", None).await.unwrap();
    assert_eq!(seq().await, 2, "with no condition a matched row is updated whichever way it moves");
    assert_eq!(table.count_rows(None, None).await.unwrap(), 1);
}

#[tokio::test]
async fn a_delete_removes_the_rows_its_predicate_holds_for() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    table.merge_insert_only(&rows(&[("a", 1), ("it's", 2), ("c", 3)]), "key").await.unwrap();

    for predicate in in_lists("key", ["it's", "a"]) {
        table.delete(&predicate).await.unwrap();
    }

    assert_eq!(by_key(&table.read(&Scan::default()).await.unwrap()), BTreeMap::from([("c".into(), 3)]));
}

#[tokio::test]
async fn a_read_pinned_to_an_older_version_reads_that_version() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    table.merge_insert_only(&rows(&[("a", 1), ("b", 2)]), "key").await.unwrap();
    let before = table.version().await.unwrap();
    table.merge_insert_only(&rows(&[("c", 3)]), "key").await.unwrap();
    table.delete(&format!("key = {}", sql_literal("a"))).await.unwrap();

    let old = table
        .read(&Scan {
            version: Some(before),
            ..Scan::default()
        })
        .await
        .unwrap();
    let now = table.read(&Scan::default()).await.unwrap();

    assert_eq!(by_key(&old), BTreeMap::from([("a".into(), 1), ("b".into(), 2)]));
    assert_eq!(by_key(&now), BTreeMap::from([("b".into(), 2), ("c".into(), 3)]));
    assert_eq!(table.count_rows(None, Some(before)).await.unwrap(), 2);
    assert_eq!(table.count_rows(Some("seq >= 2"), Some(before)).await.unwrap(), 1);
    assert_eq!(table.count_rows(Some("seq >= 2"), None).await.unwrap(), 2);
}

#[tokio::test]
async fn row_ids_are_stable_and_filter_a_read() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    table.merge_insert_only(&rows(&[("a", 1), ("b", 2)]), "key").await.unwrap();
    table.merge_insert_only(&rows(&[("c", 3)]), "key").await.unwrap();
    let all = Scan {
        with_row_id: true,
        ..Scan::default()
    };

    let ids = row_ids(&table.read(&all).await.unwrap());
    table.delete("key = 'a'").await.unwrap();
    let kept = row_ids(&table.read(&all).await.unwrap());
    let ranged = table
        .read(&Scan {
            filter: Some(format!("_rowid >= {}", ids[1])),
            ..all.clone()
        })
        .await
        .unwrap();

    assert_eq!(ids, vec![0, 1, 2], "a table created with stable row ids numbers its rows in commit order");
    assert_eq!(kept, vec![1, 2], "a delete leaves the other rows their ids");
    assert_eq!(row_ids(&ranged), vec![1, 2]);
    assert_eq!(table.count_rows(Some("_rowid < 1 OR _rowid > 1"), None).await.unwrap(), 1);
}

#[tokio::test]
async fn a_read_pages_through_more_rows_than_a_page_holds() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    let keys: Vec<String> = (0..300).map(|n| format!("k{n:03}")).collect();
    let pairs: Vec<(&str, i64)> = keys.iter().enumerate().map(|(n, key)| (key.as_str(), n as i64)).collect();
    table.merge_insert_only(&rows(&pairs), "key").await.unwrap();

    let whole = table
        .read(&Scan {
            columns: Some(vec!["key".into(), "seq".into()]),
            ..Scan::default()
        })
        .await
        .unwrap();
    let window = table
        .read(&Scan {
            limit: Some(100),
            offset: Some(50),
            filter: Some("seq >= 0".into()),
            ..Scan::default()
        })
        .await
        .unwrap();
    let none = table
        .read(&Scan {
            limit: Some(0),
            ..Scan::default()
        })
        .await
        .unwrap();

    assert_eq!(whole.num_rows(), 300);
    assert_eq!(by_key(&whole).len(), 300, "no row is read twice across pages");
    assert_eq!(window.num_rows(), 100);
    assert_eq!(none.num_rows(), 0);
    assert_eq!(none.schema.fields().len(), 2, "a read of no rows still answers the table's columns");
}

#[tokio::test]
async fn the_filters_in_lists_builds_read_exactly_the_keys_they_name() {
    let Some(catalog) = catalog() else { return };
    let table = created(&catalog).await;
    let keys: Vec<String> = (0..1100).map(|n| format!("o'k{n:04}")).collect();
    let pairs: Vec<(&str, i64)> = keys.iter().map(|key| (key.as_str(), 0)).collect();
    table.merge_insert_only(&rows(&pairs), "key").await.unwrap();
    let wanted: Vec<&str> = keys.iter().step_by(2).map(String::as_str).chain(["absent"]).collect();

    let filters = in_lists("key", &wanted);
    let mut found = Vec::new();
    for filter in &filters {
        found.extend(
            by_key(
                &table
                    .read(&Scan {
                        columns: Some(vec!["key".into(), "seq".into()]),
                        filter: Some(filter.clone()),
                        ..Scan::default()
                    })
                    .await
                    .unwrap(),
            )
            .into_keys(),
        );
    }

    assert_eq!(filters.len(), 2, "551 values are two lists");
    found.sort();
    assert_eq!(found, keys.iter().step_by(2).cloned().collect::<Vec<_>>());
}

#[tokio::test]
async fn a_refusal_keeps_its_meaning_through_the_client() {
    let Some(catalog) = catalog() else { return };
    let missing = fresh(&catalog);
    let table = created(&catalog).await;

    let absent = missing.read(&Scan::default()).await.unwrap_err();
    let no_version = table
        .read(&Scan {
            version: Some(99),
            ..Scan::default()
        })
        .await
        .unwrap_err();
    let malformed = table
        .read(&Scan {
            filter: Some("key = = 'a'".into()),
            ..Scan::default()
        })
        .await
        .unwrap_err();
    let no_column = table
        .read(&Scan {
            filter: Some("nosuchcol = 1".into()),
            ..Scan::default()
        })
        .await
        .unwrap_err();

    assert_eq!((absent.status, absent.error.code), (Some(404), ErrorCode::TableNotFound), "{absent}");
    assert_eq!(no_version.status, Some(404), "{no_version}");
    assert_eq!((malformed.status, malformed.error.code), (Some(400), ErrorCode::InvalidInput), "{malformed}");
    assert!(malformed.error.message.starts_with("catalog rejected the request: "), "{malformed}");
    assert_eq!(
        (no_column.status, no_column.error.code),
        (Some(404), ErrorCode::TableColumnNotFound),
        "the code the catalog names is kept: {no_column}"
    );
}

#[tokio::test]
async fn a_catalog_that_does_not_answer_is_unavailable() {
    if catalog().is_none() {
        return;
    }
    let nothing = Catalog::new("http://127.0.0.1:9", bearer::anonymous(), Duration::from_secs(5)).unwrap();

    let refused = fresh(&nothing).version().await.unwrap_err();

    assert_eq!((refused.status, refused.error.code), (None, ErrorCode::ServiceUnavailable), "{refused}");
}
