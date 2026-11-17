//! canon-1: the bytes a signature is computed over and checked against.
//!
//! canon-1 is the binary64 (I-JSON) profile of `json.dumps(value, sort_keys=True, separators=(",", ":"),
//! ensure_ascii=False, allow_nan=False)` in CPython, after a hop that reads numbers as binary64 is made harmless: an
//! integral float below 2^53 is written as an int, an int beyond 2^53-1 is refused, and a non-finite number has no form.
//! Keys sort by code point, strings are UTF-8 with only `"`, `\` and the control characters escaped, and a float is
//! written as CPython's `repr` writes it.

use super::json::{Json, PyStr};
use std::fmt::Write;

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
    let mut out = Vec::new();
    write(&mut out, value, 0)?;
    Ok(out)
}

fn write(out: &mut Vec<u8>, value: &Json, depth: usize) -> Result<(), CanonError> {
    match value {
        Json::Null => out.extend_from_slice(b"null"),
        Json::Bool(true) => out.extend_from_slice(b"true"),
        Json::Bool(false) => out.extend_from_slice(b"false"),
        Json::Int(digits) => write_int(out, digits)?,
        Json::Float(number) => write_float(out, *number)?,
        Json::Str(text) => write_string(out, text)?,
        Json::Array(items) => {
            if depth >= MAX_NESTING {
                return Err(CanonError::TooDeep);
            }
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write(out, item, depth + 1)?;
            }
            out.push(b']');
        }
        Json::Object(members) => {
            if depth >= MAX_NESTING {
                return Err(CanonError::TooDeep);
            }
            out.push(b'{');
            // A BTreeMap of generalized UTF-8 iterates in code point order, which is `sort_keys`.
            for (index, (key, member)) in members.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_string(out, key)?;
                out.push(b':');
                write(out, member, depth + 1)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

fn write_int(out: &mut Vec<u8>, digits: &str) -> Result<(), CanonError> {
    let (negative, magnitude) = digits.strip_prefix('-').map_or((false, digits), |rest| (true, rest));
    if magnitude.len() > MAX_SAFE_INTEGER.len() || (magnitude.len() == MAX_SAFE_INTEGER.len() && magnitude > MAX_SAFE_INTEGER) {
        return Err(CanonError::UnsafeInteger);
    }
    if negative && magnitude != "0" {
        out.push(b'-');
    }
    out.extend_from_slice(magnitude.as_bytes());
    Ok(())
}

fn write_float(out: &mut Vec<u8>, number: f64) -> Result<(), CanonError> {
    if !number.is_finite() {
        return Err(CanonError::NonFinite);
    }
    if number.fract() == 0.0 && number.abs() < TWO_TO_53 {
        // Exact: an integral binary64 below 2^53 is an integer an i64 holds, and `-0.0` is written as `0`.
        let whole = number as i64;
        out.extend_from_slice(whole.to_string().as_bytes());
        return Ok(());
    }
    out.extend_from_slice(python_repr(number).as_bytes());
    Ok(())
}

/// A finite float as CPython's `repr` writes it: the shortest digits that read back to it, positioned by its rule.
pub fn python_repr(number: f64) -> String {
    // `{:e}` gives the shortest round-trip digits as `d[.ddd]e<exp>`, the digits CPython's `repr` chooses too.
    let scientific = format!("{:e}", number.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((scientific.as_str(), "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let point = exponent + 1;
    let count = digits.len() as i32;
    let mut text = String::new();
    if number.is_sign_negative() {
        text.push('-');
    }
    if !(-4 < point && point <= 16) {
        text.push_str(&digits[..1]);
        if digits.len() > 1 {
            text.push('.');
            text.push_str(&digits[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        let _ = write!(text, "e{sign}{:02}", exponent.abs());
    } else if point <= 0 {
        text.push_str("0.");
        text.push_str(&"0".repeat((-point) as usize));
        text.push_str(&digits);
    } else if point >= count {
        text.push_str(&digits);
        text.push_str(&"0".repeat((point - count) as usize));
        text.push_str(".0");
    } else {
        text.push_str(&digits[..point as usize]);
        text.push('.');
        text.push_str(&digits[point as usize..]);
    }
    text
}

fn write_string(out: &mut Vec<u8>, text: &PyStr) -> Result<(), CanonError> {
    let text = text.as_str().ok_or(CanonError::LoneSurrogate)?;
    out.push(b'"');
    for character in text.chars() {
        match character {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{8}' => out.extend_from_slice(b"\\b"),
            '\u{c}' => out.extend_from_slice(b"\\f"),
            '\u{0}'..='\u{1f}' => out.extend_from_slice(format!("\\u{:04x}", character as u32).as_bytes()),
            other => {
                let mut buffer = [0u8; 4];
                out.extend_from_slice(other.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    out.push(b'"');
    Ok(())
}
