//! Lance filter literals and `IN` lists, spelled once for every reader of a catalog table.
//!
//! A filter is SQL that Lance parses (`lance_docs/lance/guide/read_and_write.md` § Filter push-down), so a value reaches
//! it only as a quoted literal: a quote inside one would end the literal and let the rest of the value read as SQL.

/// Values per `IN` list, so a wide set is read in bounded filters.
pub const IN_LIST_SIZE: usize = 500;

/// `value` as a SQL string literal for a Lance filter: quoted, with every quote doubled.
pub fn sql_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

/// `<column> IN (...)` filters over `values` in sorted order, at most [`IN_LIST_SIZE`] values each.
///
/// Sorted by UTF-8 bytes, which is code point order, so the lists are the ones a sort of the same strings in Python makes.
pub fn in_lists<S: AsRef<str>>(column: &str, values: impl IntoIterator<Item = S>) -> Vec<String> {
    let mut keys: Vec<String> = values.into_iter().map(|value| value.as_ref().to_owned()).collect();
    keys.sort_unstable();
    keys.chunks(IN_LIST_SIZE)
        .map(|chunk| format!("{column} IN ({})", chunk.iter().map(|key| sql_literal(key)).collect::<Vec<_>>().join(", ")))
        .collect()
}
