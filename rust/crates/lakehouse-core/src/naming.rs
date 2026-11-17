//! How the catalog names tables, and which OpenLineage datasets are its own.

/// The separator between the segments of a table or namespace name.
pub const CATALOG_DELIMITER: &str = "$";

/// The scheme of the namespace a catalog names its tables' OpenLineage datasets under.
pub const CATALOG_DATASET_SCHEME: &str = "lance-namespace";

/// Namespaces that name a location outside every catalog, unless the name is a catalog name.
const EXTERNAL_SOURCE_NAMESPACES: [&str; 2] = ["file", "lance"];

/// Whether `namespace` is a catalog's dataset namespace.
pub fn is_catalog_dataset_namespace(namespace: &str) -> bool {
    namespace.strip_prefix(CATALOG_DATASET_SCHEME).is_some_and(|rest| rest.starts_with("://"))
}

/// Whether the dataset `namespace`/`name` lies outside the catalog whose dataset namespace is `catalog`.
///
/// Another catalog's tables are the outside world to this one, as is any namespace that is a URL.
pub fn is_external_source_namespace(namespace: &str, name: &str, catalog: &str) -> bool {
    if is_catalog_dataset_namespace(namespace) {
        return namespace.trim_end_matches('/') != catalog.trim_end_matches('/');
    }
    if namespace.contains("://") {
        return true;
    }
    EXTERNAL_SOURCE_NAMESPACES.contains(&namespace) && !name.contains(CATALOG_DELIMITER)
}
