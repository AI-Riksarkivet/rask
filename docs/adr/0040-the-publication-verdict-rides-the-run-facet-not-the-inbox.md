# 0040. The publication verdict rides the run FACET, not the inbox pointer (2026-08-16)

**Decision.** A refused publication is stamped on the terminal run event's `lance` facet
(`published` / `publish_reason` / `publish_error`). The inbox ROW that a person sees keeps saying
"Complete", and making it say otherwise is DEFERRED — deliberately, not by omission.

**What was actually broken.** `finalize_run` has always computed the verdict correctly, and the run's
own page has always rendered it ("Committed, but not published — &lt;reason&gt;"). It died crossing one
boundary: `RunOutcome` is a plain `BaseModel`, so pydantic's default `extra="ignore"` silently
discarded every publication key at `emit_terminal`'s `model_validate`. What reached the graph was
byte-identical to a run that published. That is worse than silence — `RunListResponse` names the graph
as the authoritative run history, so the durable record asserted the opposite of what happened. Fixed
by DECLARING the fields and forwarding three of them onto the facet the originator already rides.

**Why the event TYPE does not change.** A refused gate is a COMPLETE run whose DATA was declined: it
fetched, wrote and committed exactly what it was asked to. The medallion's `/bronze-arrival` head
fires only on `eventType == "COMPLETE"`, so reporting the refusal as a FAIL would cancel the entire
bronze→silver→gold cascade for a run whose rows are committed and durable — and would lie in the other
direction about the run itself. `published` is TRI-STATE for the same reason: `None` means no version
existed to gate, which is not a refusal, and collapsing it to `False` would report a gate that never
ran.

**Why the BELL is deferred.** The row's label is derived from the `@STATE` suffix of
`notification_id`, and the pointer carries no other phase information. Both ways to change that are
worse than the nuance they buy:

- **A field on `InboxPointer`** is durable actor state under `extra="forbid"`. That is the exact
  surface that took the whole inbox down on 2026-08-16 — a rollback turned unreadable rows into
  `InboxUnreadable` and a 503 for every notification the subject had, because list validation is
  all-or-nothing. A label nuance does not justify re-entering that class of risk.
- **A new state suffix** would fork the `(run_id, event_type)` dedupe identity the pointer shares with
  lineage's feed, giving one run two notification ids.

The pointer is a claim-check by design: it names the object and the REASON to look, never the outcome
detail. The person is already told about this run (ingest stamps `lance.originator`), and the run's
own page is where the verdict is read. What this change creates is the FACT on the wire — which did
not exist at all before, and which any future inbox work must read.
