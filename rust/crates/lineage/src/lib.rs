//! The lineage service: the OpenLineage intake, the commit feed and the table-level edge list.

pub mod authz;
pub mod feed;
pub mod intake;
pub mod model;
pub mod paging;
pub mod relay;
pub mod signature;
pub mod store;
pub mod targeting;

/// The operations this crate serves, as `(method, path)` of `docs/operations.json`.
pub const SERVED: &[(&str, &str)] = &[];
