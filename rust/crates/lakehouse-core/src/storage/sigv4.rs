//! AWS Signature Version 4 for S3, as botocore's `S3SigV4Auth` signs a request: the canonical request over the path as
//! the URL encodes it (S3 paths are not normalised), the query sorted by its encoded names and values, every header the
//! request carries, and the SHA-256 of the payload, which is always signed.

use super::KeyPair;
use chrono::{DateTime, Utc};
use hmac::{Hmac, Mac};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use sha2::{Digest, Sha256};

const ALGORITHM: &str = "AWS4-HMAC-SHA256";
/// What a query name or value keeps unencoded: RFC 3986's unreserved characters, as botocore quotes with `safe='-_.~'`.
const QUERY: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'_').remove(b'.').remove(b'~');
/// What a key keeps unencoded in a path: the same and `/`, as botocore's serializer quotes a key with `safe='/~'`.
const PATH: &AsciiSet = &QUERY.remove(b'/');

/// A query name or value, encoded.
pub fn encode(text: &str) -> String {
    utf8_percent_encode(text, QUERY).to_string()
}

/// An object key as it stands in a request's path.
pub fn encode_key(key: &str) -> String {
    utf8_percent_encode(key, PATH).to_string()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hmac(key: &[u8], text: &str) -> Vec<u8> {
    // An HMAC takes a key of any length, so this cannot fail.
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(key) else {
        return Vec::new();
    };
    mac.update(text.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

/// What a request is signed over. `path` and `host` are as the URL has them; `query` and `headers` are unencoded,
/// header names in lower case.
pub struct Request<'a> {
    pub method: &'a str,
    pub host: &'a str,
    pub path: &'a str,
    pub query: &'a [(&'a str, &'a str)],
    pub headers: &'a [(&'a str, &'a str)],
    pub body: &'a [u8],
}

/// The headers that sign `request` as `key_pair` in `region` at `at`: the date, the payload's hash, the session token
/// where the pair has one, and the `Authorization`, in that order.
pub fn sign(request: &Request<'_>, key_pair: &KeyPair, region: &str, at: DateTime<Utc>) -> Vec<(&'static str, String)> {
    let stamp = at.format("%Y%m%dT%H%M%SZ").to_string();
    let day = &stamp[..8];
    let payload = hex(&Sha256::digest(request.body));
    let mut added = vec![("x-amz-date", stamp.clone()), ("x-amz-content-sha256", payload.clone())];
    if let Some(token) = &key_pair.session_token {
        added.push(("x-amz-security-token", token.clone()));
    }
    let mut headers: Vec<(String, String)> = std::iter::once(("host", request.host))
        .chain(request.headers.iter().copied())
        .chain(added.iter().map(|(name, value)| (*name, value.as_str())))
        .map(|(name, value)| (name.to_owned(), value.split_whitespace().collect::<Vec<_>>().join(" ")))
        .collect();
    headers.sort();
    let mut query: Vec<String> = request
        .query
        .iter()
        .map(|(name, value)| format!("{}={}", encode(name), encode(value)))
        .collect();
    query.sort();
    let signed_headers = headers.iter().map(|(name, _)| name.as_str()).collect::<Vec<_>>().join(";");
    let canonical = [
        request.method.to_owned(),
        request.path.to_owned(),
        query.join("&"),
        headers.iter().map(|(name, value)| format!("{name}:{value}\n")).collect(),
        signed_headers.clone(),
        payload,
    ]
    .join("\n");
    let scope = format!("{day}/{region}/s3/aws4_request");
    let string_to_sign = format!("{ALGORITHM}\n{stamp}\n{scope}\n{}", hex(&Sha256::digest(canonical.as_bytes())));
    let signing_key = ["s3", "aws4_request"].iter().fold(
        hmac(&hmac(format!("AWS4{}", key_pair.secret_access_key).as_bytes(), day), region),
        |key, part| hmac(&key, part),
    );
    let signature = hex(&hmac(&signing_key, &string_to_sign));
    added.push((
        "authorization",
        format!(
            "{ALGORITHM} Credential={}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
            key_pair.access_key_id
        ),
    ));
    added
}
