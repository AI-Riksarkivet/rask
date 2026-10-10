# 0085. The bus doors verify LH-064's signatures before they act (XC-078 slice 1, owner 2026-10-04)

A Dapr scope limits which components an app may use, not which subjects it may publish, and the subscriber's own
sidecar delivers a raw publish with the real app token, so a door's token check cannot tell a forgery from a producer.
The doors therefore verify the producer's signature: the cascade heads (`/bronze-arrival`, `/publication-arrival`),
notifications (`/lineage-events`, `/control-events`) and maintenance's `/maintenance-arrival` verify the arrived event
against the published Ed25519 keys, and only an event the door would act on is verified, so an ignored event costs no
key read. Enforcing, a refusal is acknowledged and counted and drives nothing (never a DROP, which parks and re-parks on
replay); an unreadable key list is a RETRY; observe acts as before and counts what it would refuse. A control event
carries a top-level `rask_signature` signed by its emitter before it is staged: the catalog for a person it
authenticated (onBehalfOf = the actor), maintenance for its purges, the medallion producer for promotion_review_requested.

Rulings: platform services are external in production and rask references secrets by name (R1); the annotator's task
actions and its grants on an annotation project travel unsigned because it holds no key, so until slice 2 any pod can
forge those (R3, R5); maintenance's door is in scope (R4); the bronze head ignores maintenance passes and admits only the
producer and the catalog, whose writes are the only ones it acts on (R7); downstream of the OTel Collector is out of
scope (R8). Decided in the review round: the catalog's control relay never signs, because it had signed whatever any
S3 writer staged under its outbox (medallion, maintenance and Ray could write there); a signer without its key withholds
its control events and counts them rather than staging unsigned bytes, the window LH-064 accepted for lineage events;
an event naming a key id the door has not seen within the 30 s refresh limit is retried rather than refused, because
refusing would lose a genuine event signed with a key published moments before (named residual: a flood of such
forgeries holds a consumer's ack window until slice 2 limits who can publish).

Deployed in three helm revisions and read back live on 2026-10-04. Rev A (266) rendered each door's mode and signer sets
on exactly its four hosts, doors off, images unchanged. Rev B (267) rolled the images with the doors observing: a
/produce cascade reached gold, a grant reached the person's inbox, a publication woke the next tier, every control event
verified offline from the raw stream bytes, and no door would have refused genuine traffic; an unsigned bronze write and
an unsigned table_published from a keyless sidecar still acted and were counted, the hole captured live. Rev C (268)
enforces by default: the genuine flows ran unchanged, and four forgeries (an unsigned bronze write, an unsigned
table_published, a key nobody publishes, a genuine grant altered after signing) were refused at bronze-arrival,
publication-arrival, maintenance-arrival, control-events and lineage, exactly six refusals counted and nothing acted.

Kept from the readback. Every door's refusal series exists at 0 from its registration, because a cumulative counter born
at 1 makes a single refusal invisible to rate(); measured in the backend at Rev B (24 + 24 + 36 + 12 zero series). A
forged publication's trigger, acting under observe, was parked in dlq.silver-to-gold by the stage runner (DLQ seq 12495).
vmalert evaluated none of its 59 rules (GreptimeDB answers its content-type-less POST with 415); out of scope by R8.
