# 0043. The bell cannot carry the publication verdict, and the reason is the claim check (2026-08-16)

**Decision.** The inbox row for a refused publication keeps reading "Complete". Not deferred on cost
this time — **REFUSED on the plane's own invariant**, which the earlier deferral note got wrong.

**What the earlier note said, and why it was insufficient.** It framed this as a compatibility hazard:
`InboxPointer` is `extra="forbid"` over a list validation, so a field an older build cannot parse
raises for the whole page — `InboxUnreadable`, a 503 on the entire inbox. All true, and it is why a
staged two-deploy rollout was proposed. But a staged rollout only answers *when* you may add the
field. It does not answer *whether* you may.

**The answer is no, and two of the estate's own adversarial tests say so.** Attempting phase A —
declaring `published: bool | None` and writing it nowhere — failed
`test_a_stored_record_carries_no_field_a_reader_could_not_already_ask_for` and
`test_a_stored_pointer_names_no_field_the_event_did_not_have_to_earn`, which pin the stored field set
exactly and state the rule: *"a row is an id, a reason, ONE object name, a run link, a lane sequence
and an instant — adding anything read off the payload would make this store a second, ungoverned copy
of lineage."* `published` is read off the payload (`lance.published`). The module docstring is the same
rule stated positively: **pointers, never payload copies** — the body is fetched at render time
through the governed path, *"which is what keeps a revoked grant from being readable out of somebody's
inbox"*.

That last clause is the concrete harm, not an abstraction. A stored `published` flag is a fact about
DATA sitting in per-subject state that no governed read gates. After a revoke, the subject would still
be able to read "that table's publication was refused" out of their own inbox — precisely the leak the
claim-check design exists to prevent.

**So the honest options are two, and neither is the field.** Either the bell ENRICHES at render time,
fetching the verdict from lineage under the `can_get_metadata` check the render path already runs —
architecturally correct, and an N+1 on the inbox render that has to be designed rather than slipped in
— or the row keeps saying "Complete" and the run's own page carries the verdict, which it already does
correctly ("Committed, but not published — <reason>"). The durable graph is honest as of 2026-08-16;
what remains is only how loudly the bell says it.

**The generalisable rule:** when a guard refuses a change, check whether it is refusing the TIMING or
the IDEA. A staged rollout answers the first. Nothing answers the second except a different design.
