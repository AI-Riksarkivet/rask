//! A table's name is resolved through the catalog's own describe doors, as the caller (ADR 0226). The catalog is an
//! in-process server on a loopback port that answers per caller and records every request.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use lakehouse_core::events::DatasetUuids;
use lineage::authz::gates::Names;
use lineage::authz::names::CatalogNames;
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const UA: &str = "0190a1b2-0000-7000-8000-0000000000a1";
const UNS: &str = "0190a1b2-0000-7000-8000-0000000000ff";

async fn catalog(seen: Arc<Mutex<Vec<Value>>>) -> String {
    let app = Router::new().fallback(move |uri: Uri, headers: HeaderMap, body: Bytes| {
        let seen = Arc::clone(&seen);
        async move {
            let path = percent_decode_str(uri.path()).decode_utf8_lossy().into_owned();
            let bearer = headers.get("authorization").map(|value| value.to_str().unwrap().to_owned());
            let body: Value = serde_json::from_slice(&body).unwrap();
            seen.lock()
                .unwrap()
                .push(json!({"path": path, "query": uri.query(), "bearer": bearer, "body": body}));
            let described = |uuid: &str| axum::Json(json!({"properties": {"rask.uuid": uuid}})).into_response();
            let response: Response = match (path.as_str(), bearer.as_deref()) {
                ("/v1/table/wh$boom$t/describe", _) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
                (_, None) => StatusCode::UNAUTHORIZED.into_response(),
                (_, Some(bearer)) if bearer != "Bearer ada" => StatusCode::FORBIDDEN.into_response(),
                ("/v1/table/wh$raw$pages/describe" | "/v1/table/solo/describe", _) => described(UA),
                ("/v1/namespace/wh$raw/describe", _) => described(UNS),
                _ => StatusCode::NOT_FOUND.into_response(),
            };
            response
        }
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{address}/")
}

#[tokio::test]
async fn a_name_resolves_to_what_the_catalog_answers_its_caller() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let names = CatalogNames::new(&catalog(Arc::clone(&seen)).await, Duration::from_secs(5)).unwrap();
    let both = DatasetUuids {
        table_uuid: Some(UA.to_owned()),
        namespace_uuid: Some(UNS.to_owned()),
    };
    assert_eq!(names.table("wh$raw$pages", Some("ada")).await.unwrap(), both);
    let asked = seen.lock().unwrap().clone();
    assert_eq!(asked.len(), 2, "the table, then its namespace");
    assert_eq!(asked[0]["bearer"], "Bearer ada", "the caller's own bearer, never the service's");
    assert_eq!(asked[0]["query"], "delimiter=%24");
    assert_eq!(asked[0]["body"], json!({"vend_credentials": false}), "no credential is minted for a resolution");
    assert_eq!(asked[1]["path"], "/v1/namespace/wh$raw/describe");

    let unseen = DatasetUuids::default();
    assert_eq!(
        names.table("wh$raw$pages", Some("bob")).await.unwrap(),
        unseen,
        "a table the caller may not see resolves to nothing"
    );
    assert_eq!(names.table("wh$raw$pages", None).await.unwrap(), unseen);
    assert_eq!(names.table("wh$none$t", Some("ada")).await.unwrap(), unseen);
    let solo = names.table("solo", Some("ada")).await.unwrap();
    assert_eq!(
        (solo.table_uuid.as_deref(), solo.namespace_uuid),
        (Some(UA), None),
        "a name with no parent asks no namespace"
    );
    assert!(
        names.table("wh$boom$t", Some("ada")).await.is_err(),
        "a catalog that fails is an outage, not an unknown name"
    );
}
