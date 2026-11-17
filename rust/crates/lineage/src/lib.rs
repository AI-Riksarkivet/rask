//! The lineage service: the OpenLineage intake, the commit feed and the table-level edge list.

pub mod feed;
pub mod model;
pub mod paging;
pub mod targeting;

/// The operations this crate serves, as `(method, path)` of `docs/operations.json`.
pub const SERVED: &[(&str, &str)] = &[];
