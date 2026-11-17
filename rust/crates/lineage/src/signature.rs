//! Whether lineage takes an event on its signature, and what a verified one lets it admit.
//!
//! The signer and delegator sets are rendered by the chart from the identities it mints keys for, so what lineage
//! trusts and what the estate provisions cannot drift. An empty signer set is an estate that does not enforce signing:
//! nothing is verified, a facet the event carries is ignored, and nothing a signature would attest is admitted on it.

use lakehouse_core::pywire::json::Json;
use lakehouse_core::signing::{KeysUnavailable, NotVerified, PublicKeySource, Refusal, VerifiedSignature, verify_event};
use std::collections::BTreeSet;

/// Who may sign what lineage records: `signers` sign at all, and `delegators` among them may stamp a person as the author.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Signing {
    pub signers: BTreeSet<String>,
    pub delegators: BTreeSet<String>,
}

impl Signing {
    /// Whether lineage refuses an event no listed signer's key verifies.
    pub fn enforced(&self) -> bool {
        !self.signers.is_empty()
    }
}

/// What a door does with an event that arrived, as far as its signature decides.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Admission {
    /// The estate enforces no signing: nothing was verified, and nothing may be admitted on a signature.
    Unenforced,
    Verified(VerifiedSignature),
    /// Final: no redelivery gives bytes already published a signature.
    Refused(Refusal),
    /// The signer's keys could not be read: the delivery is asked for again, and a staged object stays staged.
    Unavailable(KeysUnavailable),
}

/// The signature on the event exactly as it arrived, never a re-serialisation of a parsed model, which grows members
/// the producer never sent.
pub fn verify_arrived(arrived: &Json, keys: &dyn PublicKeySource, signing: &Signing) -> Admission {
    if !signing.enforced() {
        return Admission::Unenforced;
    }
    match verify_event(arrived, keys, &signing.signers, &signing.delegators) {
        Ok(verified) => Admission::Verified(verified),
        Err(NotVerified::Refused(refusal)) => Admission::Refused(refusal),
        Err(NotVerified::Unavailable(unavailable)) => Admission::Unavailable(unavailable),
    }
}

/// Whether the catalog attests an event: one of the delegators signed it, as this verification established.
///
/// The catalog checked the caller's rung on every table before it committed and signed the announcement, so its
/// verified signature is a fact here rather than a grant to ask again; a facet merely present proves nothing.
pub fn attested(admission: &Admission, signing: &Signing) -> bool {
    matches!(admission, Admission::Verified(verified) if signing.delegators.contains(&verified.identity))
}
