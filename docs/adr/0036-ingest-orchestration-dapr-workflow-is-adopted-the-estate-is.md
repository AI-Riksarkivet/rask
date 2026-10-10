# 0036. Ingest orchestration — Dapr Workflow IS adopted; the estate is event-driven now (2026-08-03, owner ruling)

**Superseded the same day by the owner, and the reason is not a flaw in the analysis below — it is
that the analysis answered the wrong question.** The entry that follows argued the `OPERATORS.md` §4
reopen criterion (*"a step that cannot be made idempotent by any caller-chosen key"*) and concluded
correctly that the ingest run has no such step. The owner's ruling: that criterion belongs to the
orchestrator-and-polling era it was written in. The estate has since gone event-driven with Dapr
Workflow, so "can we avoid an engine?" is no longer the question being asked — the engine is the
estate's chosen shape for durable multi-step work, and ingest is multi-step durable work.

**Ruling: Dapr Workflow orchestrates the ingest run.** `dapr-ext-workflow` is added; the workflow
owns run lifecycle (enumerate → dispatch chunks → await drain → finalize → emit), executing in the
daprd sidecars the estate already runs, on the actor state store that already exists
(`dapr-statestore.yaml:62`, `actorStateStore: "true"`) with `ingest` already scoped to it.

**And it dissolves the per-unit ledger.** The design below leaned on `packages/tracker` — a per-file
transfer ledger harvested from ra-hcp — as the unit ledger. With Dapr Workflow adopted, three
existing mechanisms cover what it was for, and none of them is a side ledger:

| question | answered by |
|---|---|
| delta *between tiers* (bronze→silver→gold) | **Lance CDF** — `_row_created_at_version` / `dataset.delta`, verified working in `open_ingest.md` §7.11 row 2. This is D4's own ruling: *"Delta bookkeeping is data, not state… never a side ledger"* |
| which units are still outstanding *during* a run | **JetStream `WorkQueuePolicy`** — a consumed-and-acked message is gone; un-acked redelivers. The stream IS the outstanding-work set |
| the fragment list to commit at finalize | **Dapr Workflow fan-out/fan-in** — chunk activities return their `FragmentMetadata` durably, replayed after a crash |

The plan's objection to activities ("millions of persisted+replayed activity results would melt the
state store") is answered by CHUNKING, which it already prescribed: a child workflow per ~1–10k keys
returns one compact result, not a million. Once chunked, the workflow's own durable state is the
ledger. `packages/tracker` therefore gained no NATS-KV backend and never acquired a consumer.
**The cleanup call was made 2026-08-30 (owner): the package is DELETED** — src, tests, its root
workspace rows, its `make tracker-postgres` / `dagger call tracker-postgres` pair, and `sqlmodel`,
`sqlalchemy` and `pytest-postgresql` out of the root lock with it. Nothing here needs resurrecting:
JetStream `WORK_QUEUE` retention plus the workflow's durable state ARE the ledger.

Residual to place when the run is built: a POISON unit is acked (so gone from the stream) yet must
still surface as `error` in run status. It rides on the chunk activity's returned result plus the
DLQ entry — not on a reinstated ledger.

---

### Superseded reasoning, kept for the trail — "Dapr Workflow stays un-adopted" (2026-08-03)

`open_ingest.md` proposed Dapr Workflow as the ingest run's orchestrator ("estate-native, zero new
infrastructure") and recorded it as a resolved open decision. It was not resolved: it contradicted
`docs/OPERATORS.md` §4, which rules **Dapr Workflow stays un-adopted**, was re-examined against the
annotation publish saga and **upheld 2026-07-28**, and is repeated at `chart/values.yaml:1142`. That
section carries an explicit reopen criterion, so the question is not whose document wins but whether
the criterion is met.

**The criterion, verbatim:** *"If a multi-step path appears whose steps cannot be made idempotent by
any caller-chosen key — the honest signal is finding yourself wanting to generate an id mid-saga — the
argument above stops applying and a workflow engine earns its dependency."*

**Ruling: the ingest run does not meet it. The pin stands.** Every step is idempotent by a key the
caller chose or the source supplied, and nothing is minted mid-run:

| step | key | why it converges |
|---|---|---|
| run identity | the caller's `Idempotency-Key` → `run_id_for("<project>-ingest-<key>")` | a retry resolves to the same run resource |
| unit task | the unit key from source enumeration | tracker `done_keys()` skips completed units; redelivery is a no-op |
| fragment write | the unit key | the tracker keys `FragmentMetadata` by unit key — it must anyway, because pre-commit fragment ids all collide at 0 |
| finalize / commit | `id = stable hash(source_uri)` | `merge_insert` re-run measured at 0 inserted / 5 updated / 10 rows (open_ingest.md §Empirical) |
| publication | the `published` tag | a tag advance is idempotent; a repeat fires one event that E2 absorbs |

The "honest signal" never fires. Contrast the publish saga, which genuinely had a
not-naturally-idempotent step (creating a table) and was saved only by minting
`pending_publish_id` at the transition and deriving the table id from it. Ingest has no equivalent:
its row identity is a hash of the source URI, which exists before the run does.

**The strongest argument for adopting anyway, and its answer.** A workflow buys a durable
fan-out/fan-in with an external wait — dispatch N units, suspend on `drained`, finalize exactly once —
and the honest gap it addresses is **enumeration**: if the API pod dies at unit 5,000 of 10,000, the
remaining units were never published, and unlike every other step that failure is not a message
anything will redeliver. But the fix is chunking, not an engine: publish enumeration itself as
work-queue messages (a chunk per ~1–10k keys, which `open_ingest.md` already describes as the
workflow's own shape), and a dead pod becomes redelivery like everything else. Nor is
"exactly-one finalize" load-bearing — finalize is `merge_insert` on a stable id, so at-least-once
finalize converges; the drained event is an optimisation over the fallback timer, not a correctness
requirement. A workflow engine would buy convenience here, and convenience is not the criterion.

**Consequences.**
- `open_ingest.md` §7.6 is overturned; its §0 C13 already flagged the contradiction. `dapr-ext-workflow`
  is not added to any `pyproject.toml`.
- The ingest run is orchestrated by the estate's existing parts: a JetStream durable work queue for
  units *and* for enumeration chunks, `packages/tracker` as the unit ledger, last-worker CAS on the
  remaining-counter publishing `ingest.run.<id>.drained`, and one dead-man timer — the single
  in-process timer A13 permits.
- **`stateStore.scopes` therefore does not gain `ingest` for workflow state.** The plan's Phase-0
  condition assumed adoption. If the ingest service ends up owning no Dapr state at all, scoping it
  would be dead config of exactly the kind `chart/values.yaml:776-777` exists to prevent.
- Reopen if enumeration chunking proves insufficient in practice, or if a later hop — silver→gold
  quality promotion is the candidate OPERATORS.md itself names — needs per-attempt run identity.
