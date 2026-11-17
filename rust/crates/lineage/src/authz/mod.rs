//! Who may read lineage's records and who may write an event into them, decided on OpenFGA's grants.
//!
//! `scope` decides what an event or a record names; `gates` asks the decider about it, one gate per door and route;
//! `names` resolves an outside producer's table names to the uuids grants are held by, as the caller.

pub mod gates;
pub mod names;
pub mod scope;
