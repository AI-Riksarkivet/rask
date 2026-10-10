# 0068. The `R#` rulings are the lance-ns merge's, they were ACCEPTED in July, and three rows gated on them anyway (2026-09-20)

**Finding.** Three register rows stood blocked on "owner acknowledgement of R5 / R8 / R9". Those are
not open questions and never were: `docs/architecture/lance-ns-merge.md:435` heads a table titled
"Owner rulings (2026-07-24) — ACCEPTED, supersede anything above that conflicts", and a later commit
(`f1dc8d96`, 2026-09-16) records "R1–R11 STAND — owner". So each gate asked for an acknowledgement
that had been given twice, two months earlier.

**And the citations do not bear on the rows.** That is the part worth recording, because it explains
why nobody could resolve them by reading the ruling:

| row | subject | cites | what that ruling actually says |
| --- | --- | --- | --- |
| LH-035 | a blob-bytes opt-in on the query door | R8 | the surviving frontend ZONE SET (`home + lakehouse + media + annotator + compute`) |
| LH-056 | branch-scoped governance and control events | R5 | the whole-plane media namespace, `/api/media/*`, SPA fetch bases rewritten |
| LH-097 | silver re-materialising blob bytes vs a shallow clone | R9 | `studio` survives as its own top-navbar zone |

Each is a stale cross-reference to a 2026-07 merge/frontend ruling, carried through the register
rewrite because a marker naming a plausible token reads like a real gate.

**Ruling.** No row gates on "acknowledgement of R#". R1–R20 are accepted and their text is in
`lance-ns-merge.md`; a row citing one may USE it as evidence but may not wait on it. Where such a
marker existed, the row's real gate has been named instead — for LH-035 that is where a rask-only
query parameter may live (`blob_handling` is in neither `ns_catalog/spec.yaml` nor the stock
`QueryTableRequest`, so the spec door is not available to it), for LH-056 who is TARGETED by a
tag/branch control event, and for LH-097 the storage-vs-coupling trade.

**Why it is recorded here rather than left in the commit.** The ruling existed only in a commit
message and an architecture doc while three live rows pointed at it as an open question, which is
exactly the condition under which the next reader re-derives it — this entry is what stops the fourth
row from being written the same way.
