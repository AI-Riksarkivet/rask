//! A published NKEY public key (`U...`): the raw Ed25519 key it encodes and the key id derived from it.

use sha2::{Digest, Sha256};

/// The NKEY user type byte, which spells `U` as the first base32 character.
const PUBLIC_PREFIX: u8 = 0xa0;
const PUBLIC_TEXT_LENGTH: usize = 56;
const ALPHABET: &[u8; 32] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/// The raw 32-byte key a public NKEY encodes, or nothing when the text is not one.
///
/// Strict, because a published key is a trust anchor: only an uppercase RFC 4648 base32 string of the exact length,
/// with a matching CRC-16/XMODEM and the user type byte, is read. 56 characters are exactly 35 bytes, so no spare bits
/// are left for a second spelling.
pub fn raw_public_key(text: &str) -> Option<[u8; 32]> {
    let text = text.trim_matches(is_python_whitespace);
    if text.len() != PUBLIC_TEXT_LENGTH {
        return None;
    }
    let mut decoded = Vec::with_capacity(35);
    let (mut bits, mut held) = (0u32, 0u32);
    for byte in text.bytes() {
        let value = ALPHABET.iter().position(|letter| *letter == byte)? as u32;
        bits = (bits << 5) | value;
        held += 5;
        if held >= 8 {
            held -= 8;
            decoded.push((bits >> held) as u8);
            bits &= (1 << held) - 1;
        }
    }
    let (body, checksum) = decoded.split_at(decoded.len() - 2);
    if crc16_xmodem(body).to_le_bytes() != checksum {
        return None;
    }
    let (prefix, raw) = body.split_first()?;
    if *prefix != PUBLIC_PREFIX {
        return None;
    }
    raw.try_into().ok()
}

/// The key id of a raw public key: the first 16 hex characters of its SHA-256, so a re-minted key has a new id.
pub fn kid_of(raw: &[u8; 32]) -> String {
    Sha256::digest(raw).iter().take(8).map(|byte| format!("{byte:02x}")).collect()
}

/// The public NKEYs a published list's `keys` field names, current first: comma-separated, blanks and padding dropped.
pub fn parse_published_keys(field: &str) -> Vec<String> {
    field
        .split(',')
        .map(|part| part.trim_matches(is_python_whitespace))
        .filter(|part| !part.is_empty())
        .map(str::to_owned)
        .collect()
}

/// `string.whitespace`: the six ASCII characters CPython's `strip` is asked to remove here.
fn is_python_whitespace(character: char) -> bool {
    matches!(character, ' ' | '\t' | '\n' | '\r' | '\u{b}' | '\u{c}')
}

fn crc16_xmodem(bytes: &[u8]) -> u16 {
    let mut crc: u16 = 0;
    for byte in bytes {
        crc ^= u16::from(*byte) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 { (crc << 1) ^ 0x1021 } else { crc << 1 };
        }
    }
    crc
}
