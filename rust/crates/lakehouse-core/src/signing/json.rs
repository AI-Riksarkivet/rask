//! JSON read as CPython's `json.loads` reads it, which is the document a signature is checked over.
//!
//! A signature covers the canonical bytes of the value a producer wrote, so the reader keeps what a general JSON
//! library loses: an integer of any length as its digits (a whole number past 2^53-1 has no canonical form, and only
//! the digits say so), the `NaN` and `Infinity` literals CPython accepts, and a lone surrogate escape, which CPython
//! keeps in a string and which has no UTF-8 form.

use std::collections::BTreeMap;

/// How deep a document may nest and still be read: deeper than any delivery the bus hands on (300 levels).
pub const MAX_PARSE_DEPTH: usize = 512;

/// CPython refuses to read an integer longer than this many digits (`sys.int_info.str_digits_check_threshold`).
const MAX_INT_DIGITS: usize = 4300;

/// A string as CPython holds it: code points, any of which may be a lone surrogate.
///
/// Held as generalized UTF-8, in which a surrogate is the three bytes UTF-8 would give it, so byte order is code point
/// order (CPython's key order) and a string is valid UTF-8 exactly when it holds no surrogate.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct PyStr(Vec<u8>);

impl PyStr {
    /// The text, unless the string holds a lone surrogate.
    pub fn as_str(&self) -> Option<&str> {
        std::str::from_utf8(&self.0).ok()
    }

    /// Whether this begins with `prefix`, whatever follows.
    pub fn starts_with(&self, prefix: &str) -> bool {
        self.0.starts_with(prefix.as_bytes())
    }

    /// Whether this is exactly `text`.
    pub fn is(&self, text: &str) -> bool {
        self.0 == text.as_bytes()
    }
}

impl From<&str> for PyStr {
    fn from(text: &str) -> Self {
        PyStr(text.as_bytes().to_vec())
    }
}

/// A JSON value as CPython's reader gives it.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// A number written without a fraction or an exponent: an `int`, kept as the digits written.
    Int(String),
    /// A number written with a fraction or an exponent, or a `NaN` or `Infinity` literal: a `float`.
    Float(f64),
    Str(PyStr),
    Array(Vec<Json>),
    /// Members by key. A key written twice holds the value written last, as CPython's `dict` does.
    Object(BTreeMap<PyStr, Json>),
}

impl Json {
    pub fn as_object(&self) -> Option<&BTreeMap<PyStr, Json>> {
        match self {
            Json::Object(members) => Some(members),
            _ => None,
        }
    }

    /// The member `key` of an object; nothing for any other value.
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object()?.get(&PyStr::from(key))
    }

    /// The text of a string that holds no lone surrogate.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(text) => text.as_str(),
            _ => None,
        }
    }
}

/// Why a text is not a JSON document CPython reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub position: usize,
    pub message: &'static str,
}

/// The document `text` holds, read as `json.loads(text)` reads it.
pub fn parse(text: &str) -> Result<Json, ParseError> {
    let mut reader = Reader { bytes: text.as_bytes(), at: 0 };
    reader.whitespace();
    let value = reader.value(0)?;
    reader.whitespace();
    if reader.at != reader.bytes.len() {
        return Err(reader.error("extra data"));
    }
    Ok(value)
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn error(&self, message: &'static str) -> ParseError {
        ParseError { position: self.at, message }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn literal(&mut self, word: &str, value: Json) -> Result<Json, ParseError> {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            Ok(value)
        } else {
            Err(self.error("expecting value"))
        }
    }

    fn value(&mut self, depth: usize) -> Result<Json, ParseError> {
        match self.peek() {
            Some(b'{') => self.object(depth + 1),
            Some(b'[') => self.array(depth + 1),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b'n') => self.literal("null", Json::Null),
            Some(b't') => self.literal("true", Json::Bool(true)),
            Some(b'f') => self.literal("false", Json::Bool(false)),
            Some(b'N') => self.literal("NaN", Json::Float(f64::NAN)),
            Some(b'I') => self.literal("Infinity", Json::Float(f64::INFINITY)),
            Some(b'-') if self.bytes[self.at..].starts_with(b"-Infinity") => self.literal("-Infinity", Json::Float(f64::NEG_INFINITY)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.error("expecting value")),
        }
    }

    fn nested(&self, depth: usize) -> Result<(), ParseError> {
        if depth > MAX_PARSE_DEPTH {
            Err(self.error("nested too deep"))
        } else {
            Ok(())
        }
    }

    fn array(&mut self, depth: usize) -> Result<Json, ParseError> {
        self.nested(depth)?;
        self.at += 1;
        let mut items = Vec::new();
        self.whitespace();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(Json::Array(items));
        }
        loop {
            self.whitespace();
            items.push(self.value(depth)?);
            self.whitespace();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(Json::Array(items));
                }
                _ => return Err(self.error("expecting ',' delimiter")),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<Json, ParseError> {
        self.nested(depth)?;
        self.at += 1;
        let mut members = BTreeMap::new();
        self.whitespace();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Json::Object(members));
        }
        loop {
            self.whitespace();
            if self.peek() != Some(b'"') {
                return Err(self.error("expecting property name enclosed in double quotes"));
            }
            let key = self.string()?;
            self.whitespace();
            if self.peek() != Some(b':') {
                return Err(self.error("expecting ':' delimiter"));
            }
            self.at += 1;
            self.whitespace();
            let value = self.value(depth)?;
            members.insert(key, value);
            self.whitespace();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Json::Object(members));
                }
                _ => return Err(self.error("expecting ',' delimiter")),
            }
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.at += 1;
        }
        self.at - start
    }

    /// `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`: a fraction or an exponent that is not followed by a digit
    /// ends the number before it, as CPython's scanner does, and what is left is then not JSON.
    fn number(&mut self) -> Result<Json, ParseError> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err(self.error("expecting value")),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') && matches!(self.bytes.get(self.at + 1), Some(b'0'..=b'9')) {
            self.at += 1;
            self.digits();
            is_float = true;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mark = self.at;
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if self.digits() == 0 {
                self.at = mark;
            } else {
                is_float = true;
            }
        }
        let lexeme = std::str::from_utf8(&self.bytes[start..self.at]).map_err(|_| self.error("expecting value"))?;
        if is_float {
            return lexeme.parse::<f64>().map(Json::Float).map_err(|_| self.error("expecting value"));
        }
        if lexeme.trim_start_matches('-').len() > MAX_INT_DIGITS {
            return Err(self.error("an integer longer than CPython reads"));
        }
        Ok(Json::Int(lexeme.to_owned()))
    }

    fn hex4(&self, at: usize) -> Option<u32> {
        let digits = self.bytes.get(at..at + 4)?;
        let text = std::str::from_utf8(digits).ok()?;
        if !text.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return None;
        }
        u32::from_str_radix(text, 16).ok()
    }

    fn string(&mut self) -> Result<PyStr, ParseError> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            let Some(byte) = self.peek() else {
                return Err(self.error("unterminated string"));
            };
            match byte {
                b'"' => {
                    self.at += 1;
                    return Ok(PyStr(out));
                }
                b'\\' => self.escape(&mut out)?,
                0x00..=0x1f => return Err(self.error("invalid control character")),
                _ => {
                    out.push(byte);
                    self.at += 1;
                }
            }
        }
    }

    fn escape(&mut self, out: &mut Vec<u8>) -> Result<(), ParseError> {
        let Some(kind) = self.bytes.get(self.at + 1).copied() else {
            return Err(self.error("unterminated string"));
        };
        let short = match kind {
            b'"' => Some(b'"'),
            b'\\' => Some(b'\\'),
            b'/' => Some(b'/'),
            b'b' => Some(0x08),
            b'f' => Some(0x0c),
            b'n' => Some(b'\n'),
            b'r' => Some(b'\r'),
            b't' => Some(b'\t'),
            b'u' => None,
            _ => return Err(self.error("invalid \\escape")),
        };
        if let Some(byte) = short {
            out.push(byte);
            self.at += 2;
            return Ok(());
        }
        let Some(mut unit) = self.hex4(self.at + 2) else {
            return Err(self.error("invalid \\uXXXX escape"));
        };
        self.at += 6;
        // A high surrogate joins the low one escaped right after it; any other surrogate stays a lone code point.
        if (0xd800..0xdc00).contains(&unit)
            && self.bytes.get(self.at..self.at + 2) == Some(b"\\u")
            && let Some(low) = self.hex4(self.at + 2).filter(|low| (0xdc00..0xe000).contains(low))
        {
            unit = 0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00);
            self.at += 6;
        }
        push_code_point(out, unit);
        Ok(())
    }
}

/// `point` in generalized UTF-8: UTF-8, extended to give a surrogate the three bytes its value would take.
fn push_code_point(out: &mut Vec<u8>, point: u32) {
    if let Some(character) = char::from_u32(point) {
        let mut buffer = [0u8; 4];
        out.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
    } else {
        // Only a surrogate (0xD800..=0xDFFF) reaches here: a `\u` escape names at most 0xFFFF, a joined pair a valid char.
        out.extend_from_slice(&[0xe0 | (point >> 12) as u8, 0x80 | ((point >> 6) & 0x3f) as u8, 0x80 | (point & 0x3f) as u8]);
    }
}
