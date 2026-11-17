//! The lineage service: the OpenLineage intake, the commit feed and the table-level edge list.

pub mod authz;
pub mod feed;
pub mod forward;
pub mod gate;
pub mod intake;
pub mod lane;
pub mod model;
pub mod paging;
pub mod relay;
pub mod settings;
pub mod signature;
pub mod store;
pub mod targeting;
pub mod tick;

/// The operations this crate serves, as `(method, path)` of `docs/operations.json`.
pub const SERVED: &[(&str, &str)] = &[];
