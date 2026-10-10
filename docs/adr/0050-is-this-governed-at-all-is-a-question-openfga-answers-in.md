# 0050. "Is this governed at all" is a question OpenFGA answers in one pass (2026-09-11)

The reconcile sweep reported three datasets as `storage_loss` — "a bad restore, a wipe... only a human
can answer for it" — and all three held ZERO authorization tuples. A table nobody holds a tuple on
cannot be read, maintained, dropped or re-created by anyone including its creator, so its bytes being
absent is not an incident a person can act on. Splitting `graph_ahead` out of that line had already
fixed 29 of the 32 it once carried; these were the remaining 100%, and a second miscategorisation of
the same kind.

**The technique is the part worth keeping, because the obvious shape is unaffordable and the working
one is not discoverable.** OpenFGA REFUSES a `Read` whose `tuple_key` carries an empty object id —
"the object type field is required and both the object id and user cannot be empty" — so there is no
type-only filter, and every caller concludes the question costs one call per object. It does not:
omitting `tuple_key` ENTIRELY is a different call that pages the whole store. Measured against the
live estate, 51 pages and 5,027 tuples in 0.1 s, 1,232 governed tables. That turns a governance check
from N calls a tick into one, which is what makes it affordable anywhere it is wanted — the sweep is
simply the first caller.

**Absent evidence must not become a verdict, and here the failure is inverted rather than partial.**
The set is asked what is NOT in it, so a short read does not degrade the answer — it marks everything
the pass never reached as ungoverned. `governed_objects` therefore raises rather than returning what it
managed to read, and the sweep turns any failure into `None` ("we did not ask") rather than an empty
set, which would condemn the whole estate at exactly the moment authorization is in trouble.

`ReconcileState.UNGOVERNED` is reported on its own line rather than folded into loss, for the reason
the `graph_ahead` split established: an alarm that is mostly benign is one an operator learns to skim.
