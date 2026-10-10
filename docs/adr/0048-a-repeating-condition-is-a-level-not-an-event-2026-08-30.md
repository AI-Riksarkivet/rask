# 0048. A repeating condition is a LEVEL, not an event (2026-08-30)

Measured, and the measurement is the argument. Between 2026-08-29 22:00 and 08:08 the notifications
reconciler logged **1210 `lineage_feed_pruned_below_cursor` + 1210 `lineage_event_invalid`** — two per
cron tick for ten hours, from ONE pruned feed and ONE malformed row. The pod never restarted; it healed
only when fresh traffic dragged the cursor out of range. Both diagnoses were correct. The REPETITION
was the defect.

Two costs, and the second is the one that matters. It buried every other service's errors for ten
hours. And `record_feed_gap()` counted one gap 1210 times, so any alert on that series measured tick
frequency rather than gaps — the detector was worse than absent, because it looked present.

THE RULE. A condition that is true CONTINUOUSLY and read by asking is a level: publish it as a GAUGE
and alert with a `for:` clause. A condition that HAPPENS is an event: count it, or log it once at
first sight. Emitting a level per tick converts a steady state into a stream that no threshold can
interpret. `cascade_lag` is built on this — a lag is a level, so it is a gauge, and an edge whose
value is unknown publishes NOTHING rather than a sentinel, because a gauge has no "unknown" and every
sentinel becomes a number somebody reads.

The corollary the fix turned on: an unknown must never borrow the healthy value. `0` is what a level
edge reports, so a failed read rendering as 0 turns an outage into a clean bill of health — the same
refusal `maintenance/services/reconcile.py` makes by keeping unavailable categories OUT of its counts
rather than zeroing them.
