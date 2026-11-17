//! Lineage's doors as one router, which the service hands to the shell (`lakehouse_core::service::shell::Shell::router`)
//! to be served behind its probes and limits.

use crate::doors::{dlq, events, ingest, tables};
use crate::state::State;
use axum::Router;
use axum::routing::{get, post};

pub fn router(state: State) -> Router {
    Router::new()
        .route("/events", get(events::events))
        .route("/events/projection", get(events::projection))
        .route("/tables/{name}/derived", get(tables::derived))
        .route("/admin/dlq", get(dlq::list))
        .route("/admin/dlq/{run_id}/replay", post(dlq::replay))
        .route("/admin/dlq/parked", get(dlq::parked))
        .route("/admin/dlq/parked/{run_id}/replay", post(dlq::replay_parked))
        .route("/api/v1/lineage", post(ingest::ingest))
        .with_state(state)
}
