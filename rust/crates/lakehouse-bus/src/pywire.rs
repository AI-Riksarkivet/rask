//! JSON text and scalar spellings as Python writes them, for what the bus carries byte for byte.
//!
//! A publish's body is Python's `json.dumps(envelope)` with its default separators (`", "` and `": "`) and every
//! character outside printable ASCII escaped; a float is spelled as Python's `repr` spells it. An envelope's id is built
//! from `str()` of what the event holds, so the same event gets the same id from either implementation.

use serde_json::{Number, Value};
use std::fmt::Write;

/// `value` as Python's `json.dumps(value)` writes it.
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => out.push_str(&number_text(number)),
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(members) => {
            out.push('{');
            for (index, (key, member)) in members.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_string(out, key);
                out.push_str(": ");
                write_value(out, member);
            }
            out.push('}');
        }
    }
}

fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            other => {
                let mut units = [0u16; 2];
                for unit in other.encode_utf16(&mut units) {
                    // Writing to a String cannot fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

fn number_text(number: &Number) -> String {
    match number.as_f64() {
        Some(float) if !number.is_i64() && !number.is_u64() => float_repr(float),
        _ => number.to_string(),
    }
}

/// A float as Python's `repr` spells it: the shortest digits that read back as it, fixed between 1e-4 and 1e16 and in
/// exponent form outside, with `.0` on a whole number.
pub fn float_repr(float: f64) -> String {
    if float == 0.0 {
        return if float.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    if !float.is_finite() {
        return if float.is_nan() {
            "NaN"
        } else if float > 0.0 {
            "Infinity"
        } else {
            "-Infinity"
        }
        .to_owned();
    }
    // Rust writes the same shortest digits in exponent form: `d.ddde±x`.
    let scientific = format!("{:e}", float.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((scientific.as_str(), "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let point = exponent.parse::<i64>().unwrap_or(0) + 1;
    let sign = if float < 0.0 { "-" } else { "" };
    let count = i64::try_from(digits.len()).unwrap_or(i64::MAX);
    if -4 < point && point <= 16 {
        let text = if point <= 0 {
            format!("0.{}{digits}", "0".repeat(usize::try_from(-point).unwrap_or(0)))
        } else if point >= count {
            format!("{digits}{}.0", "0".repeat(usize::try_from(point - count).unwrap_or(0)))
        } else {
            let (whole, fraction) = digits.split_at(usize::try_from(point).unwrap_or(0));
            format!("{whole}.{fraction}")
        };
        return format!("{sign}{text}");
    }
    let (first, rest) = digits.split_at(1);
    let mantissa = if rest.is_empty() { first.to_owned() } else { format!("{first}.{rest}") };
    let exponent = point - 1;
    format!("{sign}{mantissa}e{}{:02}", if exponent < 0 { '-' } else { '+' }, exponent.abs())
}

/// Python's `str()` of a JSON value as `json.loads` reads it. An array or an object is spelled as its JSON text, where
/// Python spells its `repr` (ADR 0225).
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
        Value::Number(number) => number_text(number),
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
