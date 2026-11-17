//! The facets the lakehouse's OpenLineage events carry, and what is read from them.

use serde_json::{Map, Value};
use uuid::Uuid;

/// The facet a catalog event names its table, operation and ref in.
pub const LANCE_FACET: &str = "rask_lance";
/// The facet naming who an event is on behalf of.
pub const AUTHOR_FACET: &str = "rask_author";
/// The facet holding a producer's signature over the event.
pub const SIGNATURE_FACET: &str = "rask_signature";
/// The member of the lance facet that tells one branch of a name from a later branch of the same name.
pub const BRANCH_IDENTIFIER: &str = "branchIdentifier";
/// OpenLineage's facet of other names a dataset is known by.
pub const SYMLINKS_FACET: &str = "symlinks";

const TABLE_UUID: &str = "tableUuid";
const NAMESPACE_UUID: &str = "namespaceUuid";

/// The namespace every static event's id is derived in, so one change always has one id.
const STATIC_EVENT_NAMESPACE: Uuid = Uuid::from_u128(0x6f0d_9bb2_3c47_5f1a_9b8e_4a2d_1c7e_5f30);

/// The uuids a dataset's lance facet names its table and namespace by.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DatasetUuids {
    pub table_uuid: Option<String>,
    pub namespace_uuid: Option<String>,
}

/// The uuids `facets` names, each absent unless it is a non-empty string.
pub fn dataset_uuids(facets: &Map<String, Value>) -> DatasetUuids {
    let lance = facets.get(LANCE_FACET).and_then(Value::as_object);
    let text = |key: &str| {
        lance
            .and_then(|lance| lance.get(key))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
    };
    DatasetUuids {
        table_uuid: text(TABLE_UUID),
        namespace_uuid: text(NAMESPACE_UUID),
    }
}

/// The id of an event no run performed: the same change described twice has the same id.
pub fn static_event_id(producer: &str, namespace: &str, name: &str, operation: &str, event_time: &str) -> String {
    let described = [producer, namespace, name, operation, event_time].join("|");
    Uuid::new_v5(&STATIC_EVENT_NAMESPACE, described.as_bytes()).to_string()
}
