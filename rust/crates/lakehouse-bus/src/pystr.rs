//! Python's `str()` and truthiness of a JSON value: an envelope's id is built from `str()` of what the event holds, so
//! the same event gets the same id from either implementation.

use lakehouse_core::pywire;
use serde_json::Value;

/// Python's `str()` of a JSON value as `json.loads` reads it. An array or an object is spelled as its JSON text, where
/// Python spells its `repr` (ADR 0225).
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
        Value::Number(number) => pywire::number_text(number),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Whether Python reads `value` as false: `None`, `False`, a zero, an empty string, list or dict.
pub fn falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(members) => members.is_empty(),
    }
}

/// Python's `str(value or default)`: the default for a value Python reads as false, absent included.
pub fn str_or(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(value) if !falsy(value) => py_str(value),
        _ => default.to_owned(),
    }
}
