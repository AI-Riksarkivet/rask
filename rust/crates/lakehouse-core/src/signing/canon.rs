//! canon-1: the bytes a signature is computed over and checked against.
//!
//! canon-1 is the binary64 (I-JSON) profile of `json.dumps(value, sort_keys=True, separators=(",", ":"),
//! ensure_ascii=False, allow_nan=False)` in CPython, after a hop that reads numbers as binary64 is made harmless: an
//! integral float below 2^53 is written as an int, an int beyond 2^53-1 is refused, and a non-finite number has no form.
//! Keys sort by code point, strings are UTF-8 with only `"`, `\` and the control characters escaped, and a float is
//! written as CPython's `repr` writes it.

use crate::pywire::json::{Json, PyStr};
use crate::pywire::{self, COMPACT_UTF8};

/// Containers nested deeper than this have no canonical form.
pub const MAX_NESTING: usize = 64;

const MAX_SAFE_INTEGER: &str = "9007199254740991";
const TWO_TO_53: f64 = 9_007_199_254_740_992.0;

/// Why a value has no canon-1 form.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonError {
    UnsafeInteger,
    NonFinite,
    TooDeep,
    LoneSurrogate,
}

/// The canon-1 bytes of `value`.
pub fn canon_1(value: &Json) -> Result<Vec<u8>, CanonError> {
    let mut out = String::new();
    write(&mut out, value, 0)?;
    Ok(out.into_bytes())
}

fn write(out: &mut String, value: &Json, depth: usize) -> Result<(), CanonError> {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Json::Int(digits) => write_int(out, digits)?,
        Json::Float(number) => write_float(out, *number)?,
        Json::Str(text) => pywire::write_string(out, text_of(text)?, COMPACT_UTF8.ensure_ascii),
        Json::Array(_) | Json::Object(_) if depth >= MAX_NESTING => return Err(CanonError::TooDeep),
        Json::Array(items) => pywire::sequence(out, ('[', ']'), COMPACT_UTF8, items, |out, item| write(out, item, depth + 1))?,
        // A BTreeMap of generalized UTF-8 iterates in code point order, which is `sort_keys`.
        Json::Object(members) => pywire::sequence(out, ('{', '}'), COMPACT_UTF8, members, |out, (key, member)| {
            pywire::write_string(out, text_of(key)?, COMPACT_UTF8.ensure_ascii);
            out.push_str(COMPACT_UTF8.key_separator);
            write(out, member, depth + 1)
        })?,
    }
    Ok(())
}

/// A string's text; a lone surrogate has no UTF-8 form.
fn text_of(text: &PyStr) -> Result<&str, CanonError> {
    text.as_str().ok_or(CanonError::LoneSurrogate)
}

fn write_int(out: &mut String, digits: &str) -> Result<(), CanonError> {
    let (negative, magnitude) = digits.strip_prefix('-').map_or((false, digits), |rest| (true, rest));
    if magnitude.len() > MAX_SAFE_INTEGER.len() || (magnitude.len() == MAX_SAFE_INTEGER.len() && magnitude > MAX_SAFE_INTEGER) {
        return Err(CanonError::UnsafeInteger);
    }
    if negative && magnitude != "0" {
        out.push('-');
    }
    out.push_str(magnitude);
    Ok(())
}

fn write_float(out: &mut String, number: f64) -> Result<(), CanonError> {
    if !number.is_finite() {
        return Err(CanonError::NonFinite);
    }
    if number.fract() == 0.0 && number.abs() < TWO_TO_53 {
        // Exact: an integral binary64 below 2^53 is an integer an i64 holds, and `-0.0` is written as `0`.
        out.push_str(&(number as i64).to_string());
        return Ok(());
    }
    out.push_str(&pywire::float_repr(number));
    Ok(())
}
