//! The client sends what the Python client sends and makes of an answer what Python makes of it
//! (`rust/vectors/tables.json`, ADR 0222).
//!
//! Each recorded call is made by the Rust client over HTTP against a local server that answers the recorded answers in
//! order and keeps what it was sent, so the request on the wire is compared, not a value standing in for it.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use arrow_array::{Array, Int64Array, RecordBatch, StringArray, UInt64Array};
use arrow_schema::{DataType, Field, Schema};
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, Method, Uri};
use axum::response::IntoResponse;
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use lakehouse_core::service::problem::ErrorCode;
use lakehouse_tables::arrow::decode_answer;
use lakehouse_tables::errors::CatalogError;
use lakehouse_tables::filters::{in_lists, sql_literal};
use lakehouse_tables::{Catalog, Rows, Scan, bearer};
use serde_json::{Map, Value, json};
use std::sync::{Arc, Mutex};
use std::time::Duration;

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/tables.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

#[test]
fn a_literal_is_quoted_as_python_quotes_it() {
    for case in vectors("sql_literal") {
        assert_eq!(sql_literal(case["value"].as_str().unwrap()), case["literal"], "{case}");
    }
}

#[test]
fn a_set_of_values_is_split_into_the_filters_python_splits_it_into() {
    for case in vectors("in_lists") {
        let values: Vec<&str> = case["values"].as_array().unwrap().iter().map(|value| value.as_str().unwrap()).collect();
        assert_eq!(json!(in_lists(case["column"].as_str().unwrap(), values)), case["filters"], "{}", case["name"]);
    }
}

#[test]
fn a_catalog_answer_is_refused_as_python_refuses_it() {
    for case in vectors("errors") {
        let status = u16::try_from(case["status"].as_u64().unwrap()).unwrap();
        let refused = CatalogError::answered(status, case["body"].as_str().unwrap());
        assert_eq!(refused.status, Some(status), "{case}");
        assert_eq!(u64::from(refused.error.code.status()), case["raised_status"].as_u64().unwrap(), "{case}");
        assert_eq!(refused.error.message, case["message"].as_str().unwrap(), "{case}");
    }
}

#[test]
fn a_catalog_names_the_code_only_when_it_answers_with_that_codes_status() {
    let problem = |code: u8| json!({"code": code}).to_string();
    assert_eq!(CatalogError::answered(404, &problem(11)).error.code, ErrorCode::TableVersionNotFound);
    assert_eq!(CatalogError::answered(409, &problem(5)).error.code, ErrorCode::TableAlreadyExists);
    assert_eq!(
        CatalogError::answered(404, &problem(13)).error.code,
        ErrorCode::TableNotFound,
        "a code of another status is not taken"
    );
    assert_eq!(CatalogError::answered(409, "not json").error.code, ErrorCode::ConcurrentModification);
}

/// What the replaying catalog was sent.
#[derive(Debug)]
struct Sent {
    method: Method,
    target: String,
    headers: Map<String, Value>,
    body: Vec<u8>,
}

/// A recorded answer: its content type and its body.
type Answer = (String, Vec<u8>);

#[derive(Clone, Default)]
struct Replay {
    answers: Arc<Mutex<Vec<Answer>>>,
    sent: Arc<Mutex<Vec<Sent>>>,
}

async fn answer(State(replay): State<Replay>, method: Method, uri: Uri, headers: HeaderMap, body: Bytes) -> axum::response::Response {
    let kept = ["accept", "content-type", "authorization"]
        .into_iter()
        .filter_map(|name| headers.get(name).map(|value| (name.to_owned(), json!(value.to_str().unwrap()))))
        .collect();
    replay.sent.lock().unwrap().push(Sent {
        method,
        target: uri.to_string(),
        headers: kept,
        body: body.to_vec(),
    });
    let (content_type, bytes) = replay.answers.lock().unwrap().remove(0);
    ([(axum::http::header::CONTENT_TYPE, content_type)], bytes).into_response()
}

/// A catalog on loopback that answers `answers` in order and keeps each request.
async fn replaying(answers: Vec<Answer>) -> (String, Replay) {
    let replay = Replay {
        answers: Arc::new(Mutex::new(answers)),
        sent: Arc::default(),
    };
    let app = axum::Router::new().fallback(answer).with_state(replay.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (url, replay)
}

/// The rows the recorded writes carry.
fn recorded_rows() -> Rows {
    let schema = Arc::new(Schema::new(vec![
        Field::new("event_key", DataType::Utf8, true),
        Field::new("seq", DataType::Int64, true),
    ]));
    let keys = StringArray::from(vec!["a", "b'", "å"]);
    let seqs = Int64Array::from(vec![Some(1), Some(2), None]);
    RecordBatch::try_new(schema, vec![Arc::new(keys), Arc::new(seqs)]).unwrap().into()
}

/// Rows as the list of objects Python's `to_pylist` makes of them.
fn as_objects(rows: &Rows) -> Value {
    let mut objects = Vec::new();
    for batch in &rows.batches {
        for at in 0..batch.num_rows() {
            let mut object = Map::new();
            for (field, column) in batch.schema().fields().iter().zip(batch.columns()) {
                let value = if column.is_null(at) {
                    Value::Null
                } else if let Some(text) = column.as_any().downcast_ref::<StringArray>() {
                    json!(text.value(at))
                } else if let Some(number) = column.as_any().downcast_ref::<Int64Array>() {
                    json!(number.value(at))
                } else {
                    json!(column.as_any().downcast_ref::<UInt64Array>().unwrap().value(at))
                };
                object.insert(field.name().clone(), value);
            }
            objects.push(Value::Object(object));
        }
    }
    Value::Array(objects)
}

fn text(call: &Value, key: &str) -> Option<String> {
    call[key].as_str().map(str::to_owned)
}

fn number(call: &Value, key: &str) -> Option<u64> {
    call[key].as_u64()
}

/// Make the recorded call with the Rust client, and answer what it made of the catalog's answers.
async fn make(catalog: &Catalog, case: &Value) -> Value {
    let id = case["table_id"]
        .as_array()
        .unwrap()
        .iter()
        .map(|segment| segment.as_str().unwrap().to_owned())
        .collect();
    let table = catalog.table(id);
    let call = &case["call"];
    match call["op"].as_str().unwrap() {
        "create" => table.create(recorded_rows().schema).await.map(|()| Value::Null),
        "describe" => table.describe().await.map(|described| json!(described.properties)),
        "version" => table.version().await.map(|version| json!(version)),
        "count" => table
            .count_rows(text(call, "filter").as_deref(), number(call, "version"))
            .await
            .map(|count| json!(count)),
        "read" => {
            let columns = call["columns"]
                .as_array()
                .map(|columns| columns.iter().map(|column| column.as_str().unwrap().to_owned()).collect());
            let scan = Scan {
                columns,
                filter: text(call, "filter"),
                limit: number(call, "limit").map(|limit| usize::try_from(limit).unwrap()),
                offset: number(call, "offset").map(|offset| usize::try_from(offset).unwrap()),
                with_row_id: call["with_row_id"].as_bool().unwrap_or(false),
                version: number(call, "version"),
            };
            table.read(&scan).await.map(|rows| as_objects(&rows))
        }
        "merge_insert_only" => table
            .merge_insert_only(&recorded_rows(), call["on"].as_str().unwrap())
            .await
            .map(|()| Value::Null),
        "merge_upsert" => table
            .merge_upsert(&recorded_rows(), call["on"].as_str().unwrap(), text(call, "matched_when").as_deref())
            .await
            .map(|()| Value::Null),
        "delete" => table.delete(call["predicate"].as_str().unwrap()).await.map(|()| Value::Null),
        op => panic!("no such call: {op}"),
    }
    .unwrap()
}

fn token_file(token: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("lakehouse-tables-{}", uuid::Uuid::new_v4().simple()));
    std::fs::write(&path, format!("{token}\n")).unwrap();
    path
}

#[tokio::test]
async fn every_call_sends_the_request_python_sends_and_reads_its_answer_as_python_does() {
    let cases = vectors("requests");
    assert_eq!(cases.len(), 22, "each of the eleven calls, with a bearer and with none");
    for case in cases {
        let name = format!("{} (bearer: {})", case["name"], case["token"]);
        let exchanges = case["exchanges"].as_array().unwrap();
        let answers = exchanges
            .iter()
            .map(|exchange| {
                (
                    text(&exchange["answer"], "content_type").unwrap(),
                    STANDARD.decode(exchange["answer"]["body"].as_str().unwrap()).unwrap(),
                )
            })
            .collect();
        let (url, replay) = replaying(answers).await;
        let bearer = match case["token"].as_str() {
            Some(token) => bearer::token_file(token_file(token)),
            None => bearer::anonymous(),
        };
        let catalog = Catalog::new(&url, bearer, Duration::from_secs(10)).unwrap();

        let result = make(&catalog, &case).await;

        assert_eq!(result, case["result"], "{name}: what the answer is read as");
        let sent = replay.sent.lock().unwrap();
        assert_eq!(sent.len(), exchanges.len(), "{name}: one request for each Python sent");
        for (rust, python) in sent.iter().zip(exchanges) {
            assert_eq!(rust.method.as_str(), python["method"], "{name}");
            assert_eq!(rust.target, python["url"].as_str().unwrap(), "{name}: the door and its query");
            assert_eq!(Value::Object(rust.headers.clone()), python["headers"], "{name}: the headers");
            let theirs = STANDARD.decode(python["body"].as_str().unwrap()).unwrap();
            if python["headers"]["content-type"] == "application/vnd.apache.arrow.stream" {
                assert_eq!(decode_answer(&rust.body).unwrap(), decode_answer(&theirs).unwrap(), "{name}: the rows sent");
                continue;
            }
            let mut ours: Value = serde_json::from_slice(&rust.body).unwrap();
            let theirs: Value = serde_json::from_slice(&theirs).unwrap();
            if theirs.get("id").is_none() && python["url"].as_str().unwrap().contains("/describe") {
                // ADR 0222: the uuid describe names its table in the body as the version describe does.
                assert_eq!(ours.as_object_mut().unwrap().remove("id"), Some(case["table_id"].clone()), "{name}");
            }
            assert_eq!(ours, theirs, "{name}: the body");
        }
    }
}

#[tokio::test]
async fn the_bearer_is_read_from_its_file_for_every_request() {
    let path = token_file("first");
    let answers = (0..2).map(|_| ("application/json".to_owned(), b"7".to_vec())).collect();
    let (url, replay) = replaying(answers).await;
    let table = Catalog::new(&url, bearer::token_file(&path), Duration::from_secs(10))
        .unwrap()
        .table(vec!["a".into(), "b".into()]);

    table.count_rows(None, None).await.unwrap();
    std::fs::write(&path, "rotated").unwrap();
    table.count_rows(None, None).await.unwrap();

    let sent: Vec<Value> = replay.sent.lock().unwrap().iter().map(|sent| sent.headers["authorization"].clone()).collect();
    assert_eq!(sent, vec![json!("Bearer first"), json!("Bearer rotated")]);
}

#[tokio::test]
async fn a_call_whose_token_cannot_be_read_is_not_sent() {
    let (url, replay) = replaying(Vec::new()).await;
    let missing = std::env::temp_dir().join(format!("lakehouse-tables-absent-{}", uuid::Uuid::new_v4().simple()));
    let empty = token_file("  ");

    for path in [missing, empty] {
        let table = Catalog::new(&url, bearer::token_file(&path), Duration::from_secs(10))
            .unwrap()
            .table(vec!["a".into()]);
        let refused = table.delete("true").await.unwrap_err();
        assert_eq!((refused.status, refused.error.code), (None, ErrorCode::ServiceUnavailable), "{refused}");
    }
    assert!(replay.sent.lock().unwrap().is_empty(), "nothing was sent without its identity");
}
