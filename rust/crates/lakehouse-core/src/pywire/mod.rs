//! JSON text and float spellings as CPython reads and writes them, for the bytes a Python service and a Rust one must
//! agree on: an edge's key (ADR 0218), the canon-1 bytes a signature covers (ADR 0223) and the bus's wire form (ADR 0225).
//!
//! What differs between those uses is a parameter here, not a copy: the separators, whether a string escapes every
//! character outside printable ASCII (`ensure_ascii`), and the key order, which is the value's own (a `serde_json`
//! object keeps the order it was built in, a [`json::Json`] object is sorted, which is `sort_keys`).

pub mod json;

use serde_json::{Number, Value};
use std::convert::Infallible;
use std::fmt::Write;

/// How `json.dumps` lays a document out: what follows an item and a key, and whether `ensure_ascii` is on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Style {
    pub item_separator: &'static str,
    pub key_separator: &'static str,
    pub ensure_ascii: bool,
}

/// `json.dumps(value)`.
pub const DEFAULT: Style = Style {
    item_separator: ", ",
    key_separator: ": ",
    ensure_ascii: true,
};

/// `json.dumps(value, separators=(",", ":"))`.
pub const COMPACT: Style = Style {
    item_separator: ",",
    key_separator: ":",
    ensure_ascii: true,
};

/// `json.dumps(value, separators=(",", ":"), ensure_ascii=False)`.
pub const COMPACT_UTF8: Style = Style {
    item_separator: ",",
    key_separator: ":",
    ensure_ascii: false,
};

/// `value` as `json.dumps` writes it in `style`, members in the order the value holds them.
pub fn dumps(value: &Value, style: Style) -> String {
    let mut out = String::new();
    let Ok(()) = write_value(&mut out, value, style);
    out
}

fn write_value(out: &mut String, value: &Value, style: Style) -> Result<(), Infallible> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => out.push_str(&number_text(number)),
        Value::String(text) => write_string(out, text, style.ensure_ascii),
        Value::Array(items) => sequence(out, ('[', ']'), style, items, |out, item| write_value(out, item, style))?,
        Value::Object(members) => sequence(out, ('{', '}'), style, members, |out, (key, member)| {
            write_string(out, key, style.ensure_ascii);
            out.push_str(style.key_separator);
            write_value(out, member, style)
        })?,
    }
    Ok(())
}

/// `items` between `brackets`, each written by `each` and joined by the style's item separator.
pub fn sequence<T, E>(
    out: &mut String,
    brackets: (char, char),
    style: Style,
    items: impl IntoIterator<Item = T>,
    mut each: impl FnMut(&mut String, T) -> Result<(), E>,
) -> Result<(), E> {
    out.push(brackets.0);
    for (index, item) in items.into_iter().enumerate() {
        if index > 0 {
            out.push_str(style.item_separator);
        }
        each(out, item)?;
    }
    out.push(brackets.1);
    Ok(())
}

/// `text` as a JSON string. `"`, `\` and the control characters are always escaped; with `ensure_ascii` so is every
/// character outside printable ASCII, as `\uXXXX` UTF-16 units, and without it those are written as UTF-8.
pub fn write_string(out: &mut String, text: &str, ensure_ascii: bool) {
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
            // A control character is one UTF-16 unit, its own code point.
            other if ensure_ascii || other < ' ' => {
                for unit in other.encode_utf16(&mut [0u16; 2]) {
                    // Writing to a String cannot fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

/// A number as Python writes it: an integer as its digits, a float as its `repr`.
pub fn number_text(number: &Number) -> String {
    number.as_f64().filter(|_| number.is_f64()).map_or_else(|| number.to_string(), float_repr)
}

/// A finite float as CPython's `repr` spells it: the shortest digits that read back as it, fixed between 1e-4 and 1e16
/// and in exponent form outside, with `.0` on a whole number.
pub fn float_repr(float: f64) -> String {
    // Rust writes the same shortest digits in exponent form: `d.ddde±x`.
    let scientific = format!("{:e}", float.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((scientific.as_str(), "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let point = exponent.parse::<i64>().unwrap_or(0) + 1;
    let sign = if float.is_sign_negative() { "-" } else { "" };
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
