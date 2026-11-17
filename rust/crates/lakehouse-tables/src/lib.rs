//! A table the catalog holds, read and written only through the catalog's Lance Namespace REST doors, as the caller's
//! own identity, so the catalog authorizes, judges and audits every call as it does any other client's.
//!
//! What is decided is a pure function (a request, a filter, a refusal); [`client`] is the thin shell that sends it.

pub mod arrow;
pub mod bearer;
pub mod client;
pub mod errors;
pub mod filters;
pub mod requests;

pub use arrow::Rows;
pub use client::{Catalog, Described, Scan, Table};
pub use errors::CatalogError;
