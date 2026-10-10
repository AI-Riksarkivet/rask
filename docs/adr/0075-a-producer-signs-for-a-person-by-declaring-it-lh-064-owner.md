# 0075. A producer signs for a person by DECLARING it (LH-064, owner 2026-09-24)

*Superseded by the 2026-10-02 entry "Lineage events are signed with Ed25519, the keys in the store" below: the HMAC
scheme and its symmetric-key acceptance are gone. The delegation rule carries over as `onBehalfOf`.*

The lineage bus door reads `author.sub` off the payload and authorizes as it, so until a producer
signs, that stamp is a self-assertion any pod holding the shared Dapr app token can make. The first
binding closed that by requiring the signer to BE the author — right for a service-authored run, and
impossible for the catalog, whose eight emit sites all pass `author=token.sub`, the signed-in person's
OIDC subject, while the catalog holds a service credential rather than that person's.

**The rejected repair is the one that looks obvious.** Relaxing to "signer need not equal author"
makes a producer that stamps the wrong author byte-identical to one deliberately acting for somebody:
the mistake and the intent produce the same event, so nothing downstream can separate a delegation
from the substitution the whole mechanism exists to stop.

**So a delegation is a statement, not an inference.** A producer signing for somebody declares whom,
and `verify_signed_event` accepts a signature whose signer IS the author, or one whose declared
`on_behalf_of` equals the author. Nothing else. The declaration rides inside what the HMAC covers —
`_unsigned` strips only the signature value — because a claim outside the digest can be rewritten by
any hop that handles the event.

**What this is, and is not.** It is verified attestation: the catalog authenticated the bearer at its
own door and says so under its own key, and a reader of the graph can answer "transmitted by the
catalog, on behalf of this person". It is NOT the person's non-repudiation; only their own credential
could sign for them, and no service holds one. A row that closes on this must not claim otherwise.

**HMAC is symmetric, and that is accepted rather than overlooked.** The verifier necessarily holds
every signer's key, so lineage can forge any producer's signature. What closes is non-repudiation
BETWEEN producers — the threat the row named — never against the verifier. An asymmetric scheme is
what would close the second, and it is a road not taken rather than an oversight.

**A signing identity is a new KIND of credential.** It is PRESENTED by a workload and demanded by no
door, which falsified the estate's `seeded == privileged` invariant the moment `service-catalog` was
minted. The rule is now "seeded implies presented or demanded", both read off the rendered chart.
