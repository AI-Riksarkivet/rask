//! What was derived from a governed table, read off the edge list, gated on reading the table's metadata. A derived
//! table the caller may not see is left out, and the answer then says it is incomplete.

use super::{Answer, Query, answer, caller, segment};
use crate::authz::gates::{DatasetFilter, require_metadata_access};
use crate::state::State;
use crate::store::walk::MAX_DERIVED_DEPTH;
use axum::extract::{Path, Request, State as Shared};
use axum::http::StatusCode;
use lakehouse_core::authz::table_object;
use serde_json::json;
use std::collections::BTreeSet;

/// Every table derived from `name` the caller may see, nearest first, to `depth` hops; walked from the uuid `name`
/// resolves to and from the name itself, which an outside producer's edges may key the table by.
pub async fn derived(Shared(state): Shared<State>, Path(name): Path<String>, request: Request) -> Answer {
    let name = segment(name)?;
    let caller = caller(&state, request.headers()).await?;
    let (subject, bearer) = (caller.subject.as_deref(), caller.bearer.as_deref());
    require_metadata_access(&name, &state.gate.authority(), subject, state.names.as_ref(), bearer).await?;
    let mut query = Query::of(request.uri());
    let depth = query.int("depth", 1, Some(i128::from(MAX_DERIVED_DEPTH)));
    query.checked()?;
    let resolved = state.names.table(&name, bearer).await?.table_uuid;
    let sources: BTreeSet<String> = [Some(name.clone()), resolved.clone()].into_iter().flatten().collect();
    let depth = depth.and_then(|depth| u32::try_from(depth).ok()).unwrap_or(MAX_DERIVED_DEPTH);
    let (tables, walked) = state.gate.store.derived(&sources, depth).await?;
    let objects = tables.iter().filter_map(|derived| derived.uuid.as_deref().map(table_object)).collect();
    let visible = DatasetFilter::new(state.gate.authority(), subject).visible(&objects).await?;
    let seen = |uuid: &Option<String>| uuid.as_deref().is_some_and(|uuid| visible.contains(&table_object(uuid)));
    let shown: Vec<_> = tables.iter().filter(|derived| !state.gate.fga_enabled || seen(&derived.uuid)).collect();
    let complete = walked && shown.len() == tables.len();
    answer(StatusCode::OK, json!({"table": name, "uuid": resolved, "derived": shown, "complete": complete}))
}
