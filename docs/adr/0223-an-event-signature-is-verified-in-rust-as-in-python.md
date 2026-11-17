# 0223. An event's signature is verified in Rust as in Python (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and its second zero-trust code after ADR 0220. It decides
whether an event's `rask_signature` proves the author it stamps. Reading the published keys from the secret store is
not ported, so no door verifies with it yet. Source: the owner, 2026-11-17 ("try to port the code to rust following
these principles, keep ADR of features").

## Context

A bus door authenticates the transport an event arrived on, not the producer that wrote it, so the author an event
stamps is a claim until its Ed25519 signature proves it (`docs/COMMIT-EVENTS.md`). In Python the verifier is
`lakehouse.lineage.signing` (canon-1, the facet, the checks and their order, `RefusalReason`), lineage's use of it is
`lineage.services.signature.verify_arrived_event`, and what a verified signature admits is `_attested` in
`lineage.api.fga_deps`. A port that takes one event Python refuses forges provenance; one that refuses an event Python
takes destroys an honest event, since a refusal is final.

## Decision

1. **Verification is pure functions over the event as it arrived, the keys and the signer sets.**

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `lakehouse_core::pywire::json` | the document a text holds, read as `json.loads` reads it | CPython's `json` |
   | `lakehouse_core::signing::canon` | canon-1: the bytes a signature covers, or that a value has none | `canon_1` |
   | `lakehouse_core::signing::nkey` | the raw key a published NKEY encodes, its key id, and how a list's `keys` field splits | `_decode_nkey`, `_kid_of`, `parse_published_keys` |
   | `lakehouse_core::signing` | the facet, the checks in their order, the verdict and its `RefusalReason`, for run and dataset events and for control events | `verify_signature`, `verify_control_signature` |
   | `lineage::signature` | whether lineage enforces signing, its answer for an arrived event, and whether the catalog attests it | `Signing`, `verify_arrived_event`, `_attested` |

2. **The event is read by a reader of its own, not by `serde_json`.** canon-1 is defined on what CPython reads, and a
   general reader loses three things the verdict depends on: an integer's digits (one past 2^53-1 has no canonical
   form, and a reader that turns it into a float would sign it), the `NaN` and `Infinity` literals, and a lone
   surrogate escape, which CPython keeps and which has no UTF-8 form. Strings are held as generalized UTF-8, so key
   order is code point order and equality is CPython's. Floats are written with CPython's `repr` rules from the
   shortest digits Rust's formatter gives.
3. **The keys are an argument** (`PublicKeySource`): what an identity publishes, and a second read after a key id the
   first did not list. A source that cannot answer, or answers an empty list, is `Unavailable` and never a refusal, as
   in Python. The cache, its TTL and the pacing of reads for unknown key ids (`lakehouse.lineage.keys.PublishedKeys`)
   read a clock and are the shell's.
4. **Ed25519 is `ed25519-dalek` 2**, with its cofactorless `verify`, which refuses a non-canonical `S` as OpenSSL does.
5. **Parity is shown through the real Python path** (`scripts/_port_signing_vectors.py` →
   `rust/vectors/signing.json`). Every event is signed with the real `attach_signature` or `attach_control_signature`
   under key pairs derived from a fixture seed in the script, and judged by the real verifiers; lineage's answers come
   from the real `verify_arrived_event` and `_attested`. Recorded: 105 verdicts over 97 events (23 verified, 80
   refused, each of the twelve reasons at least once, 2 unavailable), each with the canon-1 bytes of the document as signed;
   24 lineage answers; 648 canon-1 cases (48 written by hand, 600 floats drawn from the fixture seed; 10 have no
   form); 14 published entries and 9 `keys` fields. The corpus covers changed fields and a changed facet member, a
   reordered object (which verifies: canon-1 sorts) and a reordered array, removed author and signature facets, unknown
   and foreign key ids, rejected signers, every delegation case, malformed facets member by member, non-ASCII text,
   floats, integers at and past 2^53-1, nesting at and past 64, `NaN`, lone surrogates, duplicate members, and
   unsigned events. Rust gives the same verdict and the same bytes for every case. The generator writes the same file
   on every run. Six planted defects each failed cases: skipping the key-id check (5 verdicts), accepting any signer
   (5), accepting a delegation from any signer (5), canonicalising without sorting keys (23 verdicts, 84 signed
   documents, 1 canon case), keeping a whole float as a float (2 verdicts, 4 documents, 6 canon cases), and leaving the
   signature value in the signed bytes (23 verdicts, 83 documents).

## What differs from the Python, on purpose

Each is outside the corpus:

- **Refusal messages are the port's own, and quote nothing from the event.** Python quotes the identity, algorithm and
  delegation the event claims, bounded to 300 characters by lineage. Which events are refused, and the reason, are the same.
- **A control event that is not a JSON object is refused as `error`.** Python tests membership on whatever it was given,
  so a list or a string answered `unsigned` or `error` by what it held. A control event is the flat object a model has
  already parsed, so neither reaches a verifier.
- **The reader refuses a document nested deeper than 512 levels.** CPython reads deeper; a bus delivery deeper than 300
  is refused before it is verified (`lakehouse.bus_delivery.MAX_ENVELOPE_DEPTH`), so nothing a door verifies differs.
- **The reader takes text.** CPython's `json.loads` of bytes also takes a UTF-8 byte-order mark and UTF-16 or UTF-32;
  the shell hands this reader the UTF-8 text a delivery carries.

## Not ported yet

- **Signing.** No Rust producer signs yet, so `attach_signature` is not ported; the vectors carry Python's signatures.
- **Reading the published keys** from the secret store under the service's own login, the key cache and its pacing
  (`PublishedKeys`), and the metrics a refusal and a verification count.
- **The doors that verify**: the bus lane, the outbox drain and the replay of a parked delivery in lineage, and the
  control lane and relay in the catalog.

## Consequences

- `lakehouse-core` is 2,072 of its 11,234 budgeted lines, 769 of them this feature's; `lineage` is 652 of 2,621.
- A change to canon-1, to the order of the checks or to who may sign changes a vector, and `make rust-check` fails until
  the Rust agrees.
