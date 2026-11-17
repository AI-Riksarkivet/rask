//! The Rust outbox's half of the Python-and-Rust check (`scripts/port_outbox_fixture.py cross`, ADR 0234): it stages,
//! lists and drops on the outbox the Python outbox also uses, as the identity its environment names.
//!
//! ```text
//! outbox_cross list               prints [[key, text], ...], oldest first
//! outbox_cross stage <key> <text>
//! outbox_cross drop <key>
//! ```
//!
//! The store and the identity are read as lineage reads them (`LINEAGE_S3_*`, `LINEAGE_OUTBOX_URI`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::outbox::Outbox;
use lakehouse_core::storage::{S3, StoreSession, StoreSettings};
use serde_json::json;
use std::sync::Arc;

#[tokio::main]
async fn main() {
    let settings = StoreSettings::from_environment("LINEAGE", std::env::vars()).unwrap();
    let session = Arc::new(StoreSession::new(&settings, "port-outbox-cross").unwrap());
    session.exchange_at_boot().await.unwrap();
    let s3 = Arc::new(S3::new(&settings, session).unwrap());
    let outbox = Outbox::new(&std::env::var("LINEAGE_OUTBOX_URI").unwrap(), s3).unwrap();
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match arguments.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["list"] => {
            let listing = outbox.listing().await.unwrap();
            let staged = outbox.read_staged(&listing, None).await.unwrap();
            let pairs: Vec<_> = staged
                .into_iter()
                .map(|staged| json!([staged.key, String::from_utf8(staged.body).unwrap()]))
                .collect();
            println!("{}", json!(pairs));
        }
        ["stage", key, text] => {
            outbox.stage(key, text).await.unwrap();
            println!("{}", json!({"staged": key}));
        }
        ["drop", key] => {
            outbox.drop_staged(key).await.unwrap();
            println!("{}", json!({"dropped": key}));
        }
        _ => panic!("usage: outbox_cross list | stage <key> <text> | drop <key>"),
    }
}
