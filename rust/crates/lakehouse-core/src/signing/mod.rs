//! Verifying the Ed25519 signature an event carries, so the author a producer stamps cannot be forged.
//!
//! A bus door authenticates the transport, never the producer, so the author inside an event is a claim until its
//! signature proves it. The `rask_signature` facet rides on the event's own facet bag (`run.facets` of a run event,
//! `dataset.facets` of a dataset event) or, on a control event, as a top-level member. The signed bytes are canon-1
//! of the whole event as it arrived with exactly one member left out: the facet's `signature`. Every other member of
//! the facet is covered, since `kid`, `canon` and `onBehalfOf` choose the key, the canonicalization and the delegation.
//!
//! The signer must be the author it stamped, or declare `onBehalfOf` equal to that author and be a delegator. The
//! checks run in a fixed order, and a refusal names the first thing wrong; reading the keys is the one step that can
//! fail without saying anything about the signature, and it answers `Unavailable`, never a refusal.

pub mod canon;
pub mod nkey;

use crate::events::{AUTHOR_FACET, SIGNATURE_FACET};
use crate::pywire::json::{Json, PyStr};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use ed25519_dalek::{Signature as Ed25519Signature, Verifier, VerifyingKey};
use std::collections::{BTreeMap, BTreeSet};

pub const SIGNATURE_ALG: &str = "Ed25519";

/// How a control event names a person as its actor; any other actor, or none, is a service acting for itself.
const PERSON_ACTOR_PREFIX: &str = "user:";

/// Why a signature was not accepted, as the bounded label a refusal counter is keyed on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusalReason {
    Unsigned,
    Malformed,
    Alg,
    Canon,
    Signer,
    Delegation,
    Author,
    Encoding,
    Kid,
    Uncanonical,
    Signature,
    Error,
}

impl RefusalReason {
    /// Every reason, in the order the Python `RefusalReason` lists them: each is a series created at 0.
    pub const ALL: [RefusalReason; 12] = [
        RefusalReason::Unsigned,
        RefusalReason::Malformed,
        RefusalReason::Alg,
        RefusalReason::Canon,
        RefusalReason::Signer,
        RefusalReason::Delegation,
        RefusalReason::Author,
        RefusalReason::Encoding,
        RefusalReason::Kid,
        RefusalReason::Uncanonical,
        RefusalReason::Signature,
        RefusalReason::Error,
    ];

    pub fn label(self) -> &'static str {
        match self {
            RefusalReason::Unsigned => "unsigned",
            RefusalReason::Malformed => "malformed",
            RefusalReason::Alg => "alg",
            RefusalReason::Canon => "canon",
            RefusalReason::Signer => "signer",
            RefusalReason::Delegation => "delegation",
            RefusalReason::Author => "author",
            RefusalReason::Encoding => "encoding",
            RefusalReason::Kid => "kid",
            RefusalReason::Uncanonical => "uncanonical",
            RefusalReason::Signature => "signature",
            RefusalReason::Error => "error",
        }
    }
}

/// A refusal, which is final. The message never quotes the event, whose text is whoever sent it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub reason: RefusalReason,
    pub message: &'static str,
}

/// Why an event was not verified: refused for good, or its signer's keys could not be read, which is retried.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotVerified {
    Refused(Refusal),
    Unavailable(KeysUnavailable),
}

/// The keys an identity publishes could not be read, or it publishes none: nothing is known about the signature.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeysUnavailable {
    pub identity: String,
}

/// What a verified signature established. A caller acts on this, never on a facet the event merely carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedSignature {
    pub identity: String,
    pub kid: String,
    pub on_behalf_of: Option<String>,
}

/// Where a verifier reads the public NKEYs an identity publishes, current key first.
///
/// `refresh` reads again now; it is asked once, after a key id the first read did not list, because that key may have
/// been published after the read. An empty list is read as `Unavailable`, like an error.
pub trait PublicKeySource {
    fn published(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable>;
    fn refresh(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable>;
}

/// Lists already read, by identity: a refresh reads the same list, and an identity not held publishes nothing.
impl PublicKeySource for BTreeMap<String, Vec<String>> {
    fn published(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable> {
        Ok(self.get(identity).cloned().unwrap_or_default())
    }

    fn refresh(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable> {
        self.published(identity)
    }
}

/// The facet as it is read: strictly, so a member of the wrong JSON type is malformed.
#[derive(Debug, Clone)]
struct Claim {
    identity: PyStr,
    kid: String,
    value: PyStr,
    alg: PyStr,
    canon: String,
    on_behalf_of: Option<PyStr>,
}

fn refused(reason: RefusalReason, message: &'static str) -> NotVerified {
    NotVerified::Refused(Refusal { reason, message })
}

fn is_kid(text: &str) -> bool {
    text.len() == 16 && text.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// Members the claim does not name are ignored here and still covered by the signed bytes.
fn read_claim(facet: &Json) -> Result<Claim, NotVerified> {
    let malformed = || refused(RefusalReason::Malformed, "the signature facet is malformed");
    let text = |member: &str| match facet.get(member) {
        Some(Json::Str(text)) => Ok(text.clone()),
        _ => Err(malformed()),
    };
    let (identity, kid, value, alg) = (text("identity")?, text("kid")?, text("signature")?, text("alg")?);
    let kid = kid.as_str().filter(|kid| is_kid(kid)).ok_or_else(malformed)?.to_owned();
    let Some(Json::Int(canon)) = facet.get("canon") else {
        return Err(malformed());
    };
    let on_behalf_of = match facet.get("onBehalfOf") {
        None | Some(Json::Null) => None,
        Some(Json::Str(text)) => Some(text.clone()),
        Some(_) => return Err(malformed()),
    };
    Ok(Claim {
        identity,
        kid,
        value,
        alg,
        canon: canon.clone(),
        on_behalf_of,
    })
}

/// Which member holds the event's facets: `run` decides it, else `dataset`, as lineage parses an event.
fn holder_of(event: &Json) -> Option<&'static str> {
    ["run", "dataset"].into_iter().find(|holder| matches!(event.get(holder), Some(Json::Object(_))))
}

fn facet_bag<'a>(event: &'a Json, holder: &str) -> Option<&'a BTreeMap<PyStr, Json>> {
    event.get(holder)?.get("facets")?.as_object()
}

/// The subject a producer stamped on its own event: the author facet's non-empty `sub`.
fn author_of(event: &Json) -> Option<&PyStr> {
    let bag = facet_bag(event, holder_of(event)?)?;
    match bag.get(&PyStr::from(AUTHOR_FACET))?.get("sub")? {
        Json::Str(sub) if !sub.is("") => Some(sub),
        _ => None,
    }
}

fn person_actor(envelope: &Json) -> Option<&PyStr> {
    match envelope.get("actor")? {
        Json::Str(actor) if actor.starts_with(PERSON_ACTOR_PREFIX) => Some(actor),
        _ => None,
    }
}

/// The checks that need neither a key nor the author: the algorithm, the canonicalization, and whether the signer may sign.
fn check_signer(claim: &Claim, signers: &BTreeSet<String>) -> Result<(), NotVerified> {
    if !claim.alg.is(SIGNATURE_ALG) {
        return Err(refused(RefusalReason::Alg, "the signature names an algorithm other than Ed25519"));
    }
    if claim.canon != "1" {
        return Err(refused(RefusalReason::Canon, "the signature names a canonicalization other than 1"));
    }
    if !claim.identity.as_str().is_some_and(|identity| signers.contains(identity)) {
        return Err(refused(RefusalReason::Signer, "the signer is not an identity that may sign"));
    }
    Ok(())
}

/// Whether the signer may vouch for `author`: unaided only when `signs_unaided`, else under a delegation it declared as a delegator.
fn check_authorship(claim: &Claim, author: Option<&PyStr>, signs_unaided: bool, delegators: &BTreeSet<String>) -> Result<(), NotVerified> {
    match &claim.on_behalf_of {
        None if signs_unaided => Ok(()),
        None => Err(refused(
            RefusalReason::Author,
            "the signer signed an event stamped for another subject and declared no delegation",
        )),
        Some(_) if !claim.identity.as_str().is_some_and(|identity| delegators.contains(identity)) => Err(refused(
            RefusalReason::Delegation,
            "the signer is not an identity that may sign for another subject",
        )),
        Some(subject) if Some(subject) != author => Err(refused(
            RefusalReason::Author,
            "the declared delegation names another subject than the one stamped",
        )),
        Some(_) => Ok(()),
    }
}

/// The 64 signature bytes, from the one spelling an encoder writes, so no second copy of an event verifies.
fn raw_signature(text: &PyStr) -> Result<Ed25519Signature, NotVerified> {
    let encoding = || refused(RefusalReason::Encoding, "the signature is not 86 canonical base64url characters");
    let text = text.as_str().ok_or_else(encoding)?;
    if text.len() != 86 || !text.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_') {
        return Err(encoding());
    }
    let raw = URL_SAFE_NO_PAD.decode(text).map_err(|_| encoding())?;
    let raw: [u8; 64] = raw.try_into().map_err(|_| encoding())?;
    Ok(Ed25519Signature::from_bytes(&raw))
}

/// The raw key the claim's `kid` names among what its identity publishes: one refresh, then a verdict.
fn published_key(claim: &Claim, identity: &str, source: &dyn PublicKeySource) -> Result<[u8; 32], NotVerified> {
    for read in [PublicKeySource::published, PublicKeySource::refresh] {
        let listed = read(source, identity).map_err(NotVerified::Unavailable)?;
        if listed.is_empty() {
            return Err(NotVerified::Unavailable(KeysUnavailable { identity: identity.to_owned() }));
        }
        let named = listed
            .iter()
            .filter_map(|entry| nkey::raw_public_key(entry))
            .find(|raw| nkey::kid_of(raw) == claim.kid);
        if let Some(raw) = named {
            return Ok(raw);
        }
    }
    Err(refused(RefusalReason::Kid, "no key the signer publishes has the signature's key id"))
}

fn check_bytes(signed: &Json, raw_public: &[u8; 32], signature: &Ed25519Signature) -> Result<(), NotVerified> {
    let message = canon::canon_1(signed).map_err(|_| refused(RefusalReason::Uncanonical, "the event has no canonical form"))?;
    let does_not_verify = || refused(RefusalReason::Signature, "the signature does not verify against the published key");
    let key = VerifyingKey::from_bytes(raw_public).map_err(|_| does_not_verify())?;
    key.verify(&message, signature).map_err(|_| does_not_verify())
}

/// The facet with its `signature` member left out: what was in place when the value was computed.
fn without_value(facet: &Json) -> Option<Json> {
    let members = facet.as_object()?;
    Some(Json::Object(
        members
            .iter()
            .filter(|(member, _)| !member.is("signature"))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
    ))
}

/// `members` with `key` replaced by `value`.
fn replaced(members: &BTreeMap<PyStr, Json>, key: &str, value: Json) -> Json {
    let mut copy = members.clone();
    copy.insert(PyStr::from(key), value);
    Json::Object(copy)
}

/// A run or dataset event as it was signed: everything except the signature's own value. Nothing when it carries no facet.
pub fn signed_document(event: &Json) -> Option<Json> {
    let holder = holder_of(event)?;
    let section = event.get(holder)?.as_object()?;
    let bag = facet_bag(event, holder)?;
    let facet = bag.get(&PyStr::from(SIGNATURE_FACET))?;
    let bag = replaced(bag, SIGNATURE_FACET, without_value(facet)?);
    let section = replaced(section, "facets", bag);
    Some(replaced(event.as_object()?, holder, section))
}

/// A control event as it was signed: everything except its top-level facet's own value. Nothing when it carries none.
pub fn signed_control_document(envelope: &Json) -> Option<Json> {
    let members = envelope.as_object()?;
    let facet = members.get(&PyStr::from(SIGNATURE_FACET))?;
    Some(replaced(members, SIGNATURE_FACET, without_value(facet)?))
}

fn verified(claim: Claim, identity: &str) -> Result<VerifiedSignature, NotVerified> {
    let on_behalf_of = match claim.on_behalf_of {
        // A document with a canonical form holds no lone surrogate, so the delegation is text.
        Some(subject) => Some(
            subject
                .as_str()
                .ok_or_else(|| refused(RefusalReason::Error, "the delegation is not text"))?
                .to_owned(),
        ),
        None => None,
    };
    Ok(VerifiedSignature {
        identity: identity.to_owned(),
        kid: claim.kid,
        on_behalf_of,
    })
}

fn checked(
    claim: Claim,
    signed: Option<Json>,
    source: &dyn PublicKeySource,
    authorship: impl FnOnce(&Claim) -> Result<(), NotVerified>,
    signers: &BTreeSet<String>,
) -> Result<VerifiedSignature, NotVerified> {
    check_signer(&claim, signers)?;
    authorship(&claim)?;
    let signature = raw_signature(&claim.value)?;
    let identity = claim.identity.as_str().unwrap_or_default().to_owned();
    let raw_public = published_key(&claim, &identity, source)?;
    let signed = signed.ok_or_else(|| refused(RefusalReason::Error, "the event lost its facet while it was verified"))?;
    check_bytes(&signed, &raw_public, &signature)?;
    verified(claim, &identity)
}

/// Verify a run or dataset event's `rask_signature` against the keys its signer publishes.
///
/// `signers` may sign at all and `delegators`, among them, may sign for another subject. An event that is no object
/// is refused as `error`; one with no facet as `unsigned`, since absence is a refusal and never a pass.
pub fn verify_event(
    event: &Json,
    source: &dyn PublicKeySource,
    signers: &BTreeSet<String>,
    delegators: &BTreeSet<String>,
) -> Result<VerifiedSignature, NotVerified> {
    if event.as_object().is_none() {
        return Err(refused(RefusalReason::Error, "the event is not an object"));
    }
    let unsigned = || refused(RefusalReason::Unsigned, "the event carries no signature");
    let bag = holder_of(event).and_then(|holder| facet_bag(event, holder)).ok_or_else(unsigned)?;
    let claim = read_claim(bag.get(&PyStr::from(SIGNATURE_FACET)).ok_or_else(unsigned)?)?;
    let author = author_of(event);
    let authorship = |claim: &Claim| {
        let unaided = author.is_some_and(|author| *author == claim.identity);
        check_authorship(claim, author, unaided, delegators)
    };
    checked(claim, signed_document(event), source, authorship, signers)
}

/// Verify a control event's top-level `rask_signature`. Its `actor` plays the author: a `user:` principal is signed for
/// only by a delegator declaring `onBehalfOf` equal to the whole actor; any other actor, or none, by a signer unaided.
pub fn verify_control_event(
    envelope: &Json,
    source: &dyn PublicKeySource,
    signers: &BTreeSet<String>,
    delegators: &BTreeSet<String>,
) -> Result<VerifiedSignature, NotVerified> {
    let Some(members) = envelope.as_object() else {
        return Err(refused(RefusalReason::Error, "the event is not an object"));
    };
    let facet = members
        .get(&PyStr::from(SIGNATURE_FACET))
        .ok_or_else(|| refused(RefusalReason::Unsigned, "the event carries no signature"))?;
    let claim = read_claim(facet)?;
    let person = person_actor(envelope);
    let authorship = |claim: &Claim| check_authorship(claim, person, person.is_none(), delegators);
    checked(claim, signed_control_document(envelope), source, authorship, signers)
}
