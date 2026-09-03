"""The ingest run as a Dapr Workflow.

Adopted by owner ruling 2026-08-03 (docs/DECISIONS.md "Ingest orchestration — Dapr Workflow IS
adopted"). It executes in the daprd sidecar the chart already injects, on the actor state store that
already exists — the adoption cost is a dependency, not infrastructure.

THE SHAPE, and why each piece is where it is:

    ingest_run                 (parent — one per POST /v1/ingests)
      emit_start               activity: lineage START, so a run is visible before it does anything
      resolve_limits           activity: the run's POLICY ceilings, pinned into history
      ensure_dataset           activity: the table's location AND its base version, resolved once
      enumerate_chunks         activity: source -> CHUNK descriptors, never units
      when_all(chunk_run ...)  child workflow per chunk, fanned out
      finalize                 activity: the lander commits ONE Lance version through the catalog
      emit_terminal            activity: lineage COMPLETE or FAIL

    chunk_run                  (child — one per ~1-10k units)
      publish_units            activity: units onto the JetStream work queue
      drain_chunk              activity: fetch, validate, stage a fragment, ack — the actual work
      reconcile_chunk          activity: queue truth, only when the drain reports short

**Chunks, never units.** A unit is one source object; a run is millions of them. Persisting and
replaying a million activity results would melt the state store — the plan said so, and it is the
reason a per-unit ledger looked necessary. Chunking answers it: one child workflow per ~1-10k keys
returns ONE compact result, and the workflow's own durable state becomes the ledger. That, plus the
work queue's own retention, is why this estate carries no transfer-ledger package at all
(DECISIONS.md).

**Determinism.** Workflow functions replay from history, so every non-deterministic thing — clocks,
randomness, I/O, network — lives in an ACTIVITY. `ctx.current_utc_datetime` and `ctx.create_timer`
are the replay-safe clock; `datetime.now()` here would produce a different history on every replay
and wedge the run. This is the single easiest way to break a workflow, hence the rule stated rather
than assumed.

**ENV IS I/O, and it is the one that hides.** A module-level `os.getenv` looks like a constant and
behaves like a clock: a replay lands on whatever pod is free, so a rolling deploy that changes a
value mid-run makes the SAME history replay into a DIFFERENT action stream — the timer the first
execution created has no counterpart in the replay, or the other way round, and the run wedges. So
nothing in workflow scope reads env, INCLUDING through model validation: `RunSpec`/`ChunkSpec` are
validated at the top of both generator bodies, so a `default_factory` that reads env is a workflow
env read wearing a Pydantic disguise. Every value a workflow branches on arrives either on the input
or as an ACTIVITY RESULT, which is history and therefore replays byte-identical.

**No polling** (A13). The drain blocks on JetStream's pull `fetch`, which the server fulfils when
messages exist — a blocking wait, not a loop asking "is it done yet". Nothing in this plane holds an
ack across a job's runtime, which is the specific thing A13 outlaws.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import dapr.ext.workflow as wf
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from ingest.config import settings
from ingest.metrics import record_run, record_units
from ingest.sizing import ResolvedSizing
from service_kit.activity_loop import run_activity


if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext

# One child workflow per this many keys. Small enough that a chunk's result stays compact in the
# state store, large enough that a million-unit run does not spawn a million children. The plan's
# own figure (open_ingest.md: "child workflow per ~1-10k keys").
#
# RAISED 1000 -> 5000 (2026-08-24) BECAUSE THE ESTATE'S OWN SCALE DID NOT FIT. This value sets the
# dispatch ceiling, and the arithmetic is not obvious in either direction: `enumerate_chunks` returns
# ONE activity result holding every chunk, so a SMALLER chunk size means MORE descriptors and a
# LARGER payload. Measured — a pointer-form descriptor serialises to 317 bytes, so at 1000 the
# ceiling was 9,923,000 units, and this estate holds "over 10 million images" (owner, 2026-08-24).
# A corpus already in the building serialised to 3.02 MiB against the 3 MiB budget and would have
# been REFUSED, surfacing as RESOURCE_EXHAUSTED from inside the SDK on a workflow that then retries
# four times and wedges, with nothing naming a knob.
#
# 10000 puts the ceiling near 99M units and stays at the top of the plan's own 1-10k range. The
# number is chosen by this suite's OWN rule, not by preference: `test_the_headroom_is_at_least_five
# _times_the_advertised_scale` requires 5x headroom over the advertised scale, and with that scale
# now 10M (owner, 2026-08-24) 5000 gave 49.6M — marginally UNDER its own bar. 10000 gives ~10x.
# The cost is real and bounded: a chunk is the unit of RETRY, so a failed chunk now re-drains up to
# 10000 units instead of 1000, and fan-out narrows tenfold. Both are the right trade against a run
# that cannot start at all. `test_dispatch_ceiling_at_real_scale.py` pins the headroom so a
# descriptor that grows fails there, naming this knob, rather than in production.
#
# Annotated `int` because it is a KNOB, not an identity: unannotated, its declared type is the
# literal 1000, and rebinding it — which the chunk-boundary test does, to slice at 3 without writing
# a 1000-file fixture — is then a type error rather than the intended tuning.
CHUNK_SIZE: int = 10000

# Retries are the activity's, not a hand-rolled loop: Dapr owns the backoff and the replay.
ACTIVITY_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=5),
    max_number_of_attempts=4,
    backoff_coefficient=2.0,
)

#: The most per-unit error entries any workflow payload may carry. The map is keyed by UNIT — one
#: entry per corrupt page — so on the million-unit harvest this plane advertises a bad source prefix
#: makes it a million entries, and the workflow persists it at least four times over (the drain's
#: activity result, the child's return, the parent's merge, `finalize`'s input). That is the state
#: store paying, repeatedly, for a payload nobody reads past the first screen.
#:
#: So the COUNT is kept exact (`errors_total`) and the PAYLOAD is capped. A number is what an
#: operator acts on — "412 units failed" — and the units themselves are already durable where they
#: belong: parked on the queue's DLQ with their reasons.
MAX_REPORTED_ERRORS = 100

#: The key under which a truncated error map names its own overflow. Reserved, like the reconcile
#: marker's own `chunk:{id}` entries.
ERRORS_TRUNCATED_KEY = "__truncated__"


class RunLimits(BaseModel):
    """The run's POLICY ceilings — resolved ONCE, in activity scope, and carried.

    ZERO MEANS UNBOUNDED for both, and that is the default IN CODE. This plane's own docstrings
    advertise million-unit harvests, so a live default would kill the legitimate long run the
    ceilings exist to protect; the deployment opts in with `RASK_INGEST_MAX_RUN_HOURS` /
    `RASK_INGEST_MAX_UNITS`.

    **These were module-level `os.getenv` reads, and the workflow branched on them.** That is the
    determinism break this module's header names: `if max_run_hours > 0` decides whether a durable
    timer is created, so a rolling deploy that set or cleared the variable between a run's first
    execution and its replay produced an action stream the history does not match — the classic way
    to wedge a durable workflow permanently. `max_units` had the milder version of it: the same run
    reporting FAILED-by-ceiling or COMPLETE depending on which pod replayed it.

    The fix is the one `sizing` already uses — resolve once, carry the resolved value — with the
    resolution in an ACTIVITY (`resolve_limits`) so it is pinned in workflow history. `RunSpec.limits`
    is the accept-time seam: a door that resolves the ceilings when it validates the request supplies
    them here and the activity passes them straight through.

    `max_units` refuses the RUN — the queue publish, the fan-out, the state-store churn of a fan-out
    nobody intended. It does NOT prevent the source LISTING, because all three adapters materialize
    their listing below this seam (`S3FileSystemSource` does a full recursive LIST when `prefix` is empty, which
    the registry explicitly invites). An `islice` at the ceiling would look like a bound and stop
    nothing: the walk has already happened by the time a key reaches us. So there is deliberately NO
    "stops pulling at the limit" test — it would pass against a lazy fake and be false for every
    production source.

    `max_run_hours` is the enforcement half of gate A15, which asserts `maintenance.olderThanDays * 24
    >= RASK_INGEST_MAX_RUN_HOURS` so version GC cannot delete the version a live run is committing
    against. That assertion was passing while NOTHING read the value — a gate certifying a relation
    with only one side implemented.
    """

    max_run_hours: float = 0.0
    max_units: int = 0
    #: How many rows the incremental anti-join may read from bronze before the run is REFUSED.
    #:
    #: §1c chose the anti-join so incremental ingest needs no second store, and named its cost in the
    #: same breath — O(existing rows) per tick, not O(new rows). `enumerate_chunks` materialises every
    #: id into a set, so on the million-unit harvests this plane advertises that is the whole table in
    #: memory on every tick, and the activity retries.
    #:
    #: REFUSES, never samples. Truncating an anti-join does not degrade it, it INVERTS it: a partial
    #: "already have" set makes the run treat rows bronze holds as new and re-land every one of them.
    #: Bounded memory bought with silent duplication is a worse trade than the unbounded read, which
    #: is why there is no `limit=` at the read site and why breaching this raises the same
    #: `AntiJoinUnavailable` an unreadable id column does — in both cases the run cannot tell what
    #: bronze already holds.
    incremental_max_rows: int = 0

    @classmethod
    def from_env(cls) -> RunLimits:
        """The DEPLOYMENT's ceilings. Called from activity scope only — never during validation.

        An empty value is unbounded rather than a crash: `kubectl set env FOO=` leaves an empty
        string and `float("")` raises, which would take the ceiling's own activity down for a config
        typo. `IngestSettings` drops blank values before validation for exactly that reason, so the
        guarantee now lives with the declaration instead of being re-implemented at each read.
        """
        config = settings()
        return cls(
            max_run_hours=config.max_run_hours,
            max_units=config.max_units,
            incremental_max_rows=config.incremental_max_rows,
        )


def anti_join_within_ceiling(existing_rows: int, ceiling: int) -> bool:
    """May a run whose bronze table holds ``existing_rows`` perform the anti-join?

    Zero is unbounded and is the default in code, matching the two ceilings beside it: this plane
    advertises long harvests, so a live default would kill the legitimate run the ceiling protects.

    Inclusive at the boundary — a ceiling of N allows N rows. An exclusive one refuses a run the
    operator deliberately sized to fit, which reads as the ceiling being off by one rather than as a
    policy.
    """
    return ceiling <= 0 or existing_rows <= ceiling


class DatasetHandle(BaseModel):
    """Where this run writes, and the version it writes AGAINST — both resolved once, at `ensure`.

    `read_version` is here rather than re-read at `finalize`, and that is the whole of finding F12a.
    The catalog's commit door is idempotent per run — it stamps `rask.ingest.run_id=<id>` into the
    Lance transaction and answers a repeat with the version that run already committed — but it finds
    that earlier commit by scanning versions AFTER the `read_version` the caller presents
    (`catalog/services/dataplane.py::_find_run_commit`). `finalize` re-read the version per attempt,
    so a replay presented the version its OWN first attempt had just produced, the scan window was
    empty, and the dedupe could never fire: commit lands -> the pod dies before Dapr records the
    activity result -> `discover_staged` still returns the same fragments -> a second Append of the
    same rows, which does not even 409 because Append never conflicts with Append.

    Carrying the version pins the scan window to where the run's own commit actually is. A stale base
    is safe by the format's own rule: an Append auto-rebases against concurrent appends and conflicts
    only with Overwrite/Restore, so a long run committing against the version it started from lands
    exactly the rows it wrote.
    """

    location: str
    read_version: int = 0


def bound_errors(errors: Mapping[str, str], total: int | None = None) -> tuple[dict[str, str], int]:
    """Cap a per-unit error map at `MAX_REPORTED_ERRORS`, keeping the COUNT exact.

    Returns `(listed, total)`. `total` overrides the input's own length for a map that has ALREADY
    been truncated once — the parent merges N bounded child results, and counting its keys would
    under-report every unit a child had to drop.

    DETERMINISTIC, because this runs in workflow scope: the survivors are chosen by sorted key, never
    by dict order, so a replay selects the same entries.

    The supplied `total` is a FLOOR, not the last word: a caller can only ever under-state it (a
    result carrying no `errors_total` at all reads as zero), and returning a count smaller than the
    entries in hand would be a pair no reader can act on — `errors_total: 0` beside two named failed
    units. What is listed is known to have failed, so it is the floor the count cannot go under.
    """
    listed = {key: value for key, value in errors.items() if key != ERRORS_TRUNCATED_KEY}
    count = len(listed) if total is None else max(total, len(listed))
    if len(listed) > MAX_REPORTED_ERRORS:
        listed = {key: listed[key] for key in sorted(listed)[:MAX_REPORTED_ERRORS]}
    if count > len(listed):
        listed[ERRORS_TRUNCATED_KEY] = f"{count - len(listed)} further units failed and are not listed here — {count} failed in total; see the queue's DLQ"
    return listed, count


class AntiJoinUnavailable(RuntimeError):
    """Bronze's `id` column could not be read, so nothing is known about what is already there.

    Raised rather than degraded, which is finding F12c. The anti-join decides which units are ALREADY
    present and must be skipped; an empty result means "skip nothing", i.e. re-fetch and re-land every
    object in the source. A `except Exception` around the read turned one transient S3 blip into
    silent full re-duplication of an entire run — the failure mode the anti-join exists to prevent,
    reached through the anti-join's own error path.

    Raising puts it under `ACTIVITY_RETRY` (four attempts, exponential backoff), so a blip costs a
    retry and a permanent failure costs the run — with a reason, routed through the parent's error
    boundary into a FAIL lineage record. Both are better than a green run that doubled the tier.

    That last clause is only true because the boundary MOVED to make it true: it used to open below
    `enumerate_chunks`, so this raise would have killed the workflow before its own FAIL record and
    left the START emitted at accept orphaned forever — the boundary's own defect, one activity
    earlier. Turning a swallow into a raise is half a fix until the raise has somewhere to land.
    """


class ChunkSpec(BaseModel):
    """One dispatchable slice of a run."""

    run_id: str
    chunk_id: str
    #: THE POINTER (§2.13). A chunk names its window into the run's unit manifest
    #: (`staging.unit_manifest_uri`) instead of carrying the keys themselves. Carrying them put the
    #: whole key set into ONE activity result and again into EACH child's input, which is what met
    #: grpc's 4 MiB ceiling at roughly 38k units — on a plane whose docstrings advertise millions.
    #: With pointers, workflow history is O(chunks) and the dispatch budget stops being a scale limit.
    #:
    #: `count` of 0 with an empty `keys` means an empty chunk, which `enumerate_chunks` never emits.
    offset: int = 0
    count: int = 0
    #: THE LEGACY INLINE FORM, kept for the rollout only. A chunk enqueued by the previous build is
    #: replayed by the new one — Dapr hands back the recorded input verbatim — so the publish path
    #: must still understand a descriptor that carries its keys. New descriptors leave these empty.
    keys: list[str] = Field(default_factory=list)
    #: Positional-parallel VERSION TOKENS for `keys` (S3 listing ETags) — identity material the
    #: workers fold into row ids. Defaulted empty so a chunk enqueued by an older build still
    #: validates; publish pads with None.
    tokens: list[str | None] = Field(default_factory=list)

    @property
    def expected_units(self) -> int:
        """How many units this chunk stands for, whichever form it is in.

        The ONE place the pointer/inline distinction is resolved. Without it every consumer would grow
        its own `len(keys) or count`, and the one that forgot would silently treat a pointer chunk as
        empty — which reads as a chunk that legitimately had no work rather than one whose intent was
        never loaded.
        """
        return self.count or len(self.keys)

    #: Resolved ONCE, by `enumerate_chunks`, and carried. It used to be re-derived at each end from
    #: env — `RASK_INGEST_ACTIVE_DATASET` or `{warehouse}/{run_id}.lance` for workers, and
    #: `{warehouse}/{project}/{dataset}.lance` for finalize. Those are different datasets: workers
    #: wrote their fragments into one and the lander committed against another, so every run would
    #: have committed an empty version while its pages sat orphaned under a run-id path. Two
    #: derivations of one location is the bug; carrying the resolved value is the fix.
    dataset_uri: str = ""

    #: The catalog NAMESPACE this chunk writes into, RESOLVED at dispatch and carried — the same rule
    #: as `dataset_uri` above, for a sharper reason. The worker composes a table id from it to ask the
    #: catalog for a scoped credential, and the id cannot be recovered downstream: parsing it back out
    #: of `dataset_uri` is refused because rask-lance-catalog documents FIVE dataset-URI layouts where
    #: reducing the wrong one silently yields `None` or the PARENT namespace, and a credential vended
    #: for the wrong table surfaces as a 403 at write time that reads as a permission problem.
    #:
    #: NAMESPACE, never `project`. `RunSpec.namespace` is "THE ONE PLACE a project becomes a
    #: namespace", and the estate has already paid for the confusion: a parameter named `project`
    #: composed `bind86$e2ewin`, an object nobody had granted anything on. Carrying the project here
    #: would rebuild that one layer down.
    #:
    #: DEFAULTS deliberately: a chunk enqueued by the previous build is replayed by the new one (Dapr
    #: hands back the recorded input verbatim), so a required field would fail every in-flight run at
    #: the moment of deploy.
    namespace: str = ""

    #: The run's write partitioning, RESOLVED at accept and carried — same reason as `dataset_uri`.
    #: Re-reading env inside the drain would let a rolling restart change a live run's fragment size
    #: mid-fan-out, so two chunks of one run could write to different layouts.
    #:
    #: The default is `None` and NOT `resolve()`, which is a determinism fix rather than a style
    #: choice: `resolve()` reads env, this model is validated at the top of the `chunk_run` generator,
    #: and a default_factory firing there is an env read in WORKFLOW scope — see the module header.
    #: `None` means "nothing supplied any", and the drain resolves the deployment default in ACTIVITY
    #: scope (`Worker.__init__`), which is where an env read belongs. `POST /v1/ingests` always sends
    #: resolved sizing, so production never takes that path.
    sizing: ResolvedSizing | None = None

    #: The SOURCE identity, carried so `publish_chunk_units` can ask the adapter for each unit's
    #: `partition_key`. Only the adapter knows what a unit key means (a IIIF volume, an S3 folder),
    #: and the worker deliberately does not — it resolves by URI scheme. Defaulted so a chunk
    #: enqueued by an older build still validates; an empty `kind` simply yields a null partition.
    kind: str = ""
    project: str = ""
    dataset: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class ChunkResult(BaseModel):
    """What a drained chunk reports back — the fragments to commit, and what refused to land."""

    chunk_id: str
    fragments: list[str] = Field(default_factory=list, description="FragmentMetadata JSON blobs")
    errors: dict[str, str] = Field(default_factory=dict, description="unit key -> reason, capped at MAX_REPORTED_ERRORS")
    errors_total: int = Field(default=0, description="how many units failed, whether or not each is listed above")
    #: Units this chunk actually landed. The drain reports it and `chunk_run` reads it to decide whether
    #: to reconcile — and then dropped it, so the parent could not aggregate what its children achieved
    #: even at fan-in, and the run's `units_done` had to wait for the terminal output's `rows`.
    units_done: int = Field(default=0, description="units this chunk landed, as confirmed against the queue")


class RunSpec(BaseModel):
    """The parent workflow's input — the request, plus the identity minted at accept."""

    run_id: str
    kind: str
    project: str
    dataset: str
    options: dict[str, Any] = Field(default_factory=dict)
    #: The HUMAN who asked for this run, captured at the accept door — the ONE place their identity
    #: exists. Everything after runs as a workflow activity behind a service token, and lineage's
    #: `enforce_author` then stamps THAT as the author, so without carrying it here an ingest run is
    #: announced to an inbox named `service-ingest` and the person who started it is never told.
    #: Empty for a service-token call, which has no human behind it — see `notifications` ORIGINATOR.
    originator: str = ""
    #: Resolved at ACCEPT (`api.create_ingest`) so a refusal is a 400 rather than a drain that hangs,
    #: and so the whole fan-out shares one set of numbers. `None` rather than a `resolve()` default
    #: for the reason spelled out on `ChunkSpec.sizing`: this model is validated inside the
    #: `ingest_run` generator, where an env read breaks replay.
    sizing: ResolvedSizing | None = None
    #: The POLICY ceilings, when the accepting door resolved them. `None` means "ask the deployment",
    #: which `resolve_limits` then does ONCE in activity scope so the answer rides workflow history.
    limits: RunLimits | None = None

    @property
    def namespace(self) -> str:
        """The catalog NAMESPACE this run writes into — `bind86-bronze`, or `bronze` untenanted.

        THE ONE PLACE a project becomes a namespace, and the interface this plane was missing. The
        two are different levels — a project selects the storage root, a namespace is the medallion
        tier — and every consumer that needed a namespace was handed `spec.project` instead. The
        catalog client's parameter was even NAMED `project`, so the mistake type-checked and read
        correctly at every site while composing `bind86$e2ewin`: the 403's object, which nobody had
        granted anything on because `namespace:bind86` does not exist.

        Resolving it here is what makes the plane's names agree BY CONSTRUCTION rather than by four
        call sites each remembering to qualify. The catalog keys on it, OpenFGA authorizes against
        exactly the object it composes, and the lineage output the cascade head matches derives from
        the same function — so the graph and the catalog cannot name different tables.
        """
        from ingest.naming import bronze_namespace_for

        return bronze_namespace_for(self.project)


class RunOutcome(BaseModel):
    committed_version: int | None = None
    rows: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
    #: How many entries `errors` WOULD hold unbounded. Equal to `len(errors)` until the cap bites.
    errors_total: int = 0
    status: str = "COMPLETE"
    #: DECLARED for the same reason the publication verdict below is, and found by the same class of
    #: failure. `finalize` and the workflow's refusal/empty/failed paths all set `units_total` on the
    #: outcome, and every one of them was dropped here by pydantic's default `extra="ignore"`. It
    #: went unnoticed while the workflow handed `emit_terminal` a RAW DICT, because history kept the
    #: key even though this model discarded it; typing the activity's input (DWF-ACT-009) made the
    #: loss visible, which is precisely the validation the typing exists to buy.
    units_total: int = 0
    #: THE PUBLICATION VERDICT, and it has to be DECLARED to survive.
    #:
    #: `finalize_run` has always returned these keys and the pull surface has always rendered them —
    #: but this is a plain `BaseModel`, so pydantic's default `extra="ignore"` dropped every one of
    #: them at `emit_terminal`'s `model_validate`. What reached the graph was then byte-identical to a
    #: run that published, which is worse than silence: the durable record asserted the opposite of
    #: what happened, and it is the record `RunListResponse` names as the authoritative history.
    #:
    #: `published` is TRI-STATE and the third value is load-bearing: `None` means no version existed
    #: to gate (nothing was committed), which is not a refusal. Collapsing it to `False` would report
    #: a quality gate that never ran.
    published: bool | None = None
    from_version: int | None = None
    to_version: int | None = None
    publish_reason: str | None = None
    publish_error: str | None = None


def ingest_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Parent workflow: enumerate, fan out, finalize once, emit lineage.

    Typed as a Generator because it IS one — every `yield` is a durable await point. Annotating it
    `-> dict` (the shape it conceptually returns) is a lie `ty` catches, and `error-on-warning` makes
    it a build failure; `flows/workflow.py:46` had already ruled this the same way.
    """
    spec = RunSpec.model_validate(payload)

    yield ctx.call_activity(emit_start, input=spec, retry_policy=ACTIVITY_RETRY)

    # THE ERROR BOUNDARY — every exit routes through ONE terminal step.
    #
    # Without it, a chunk that exhausted its retries raised straight out of `when_all` and the
    # workflow died BEFORE `finalize`, before the FAIL lineage record, and before the queue release
    # that rides `emit_terminal` — so a run with one permanently bad object lost its entire
    # bookkeeping: no FAIL in the graph (the START emitted at accept orphaned forever), units
    # leaking until stream retention, and the operator reading a bare Dapr failure instead of a
    # reason. `tests/test_empty_commit.py` documented the loss verbatim for the finalize leg; the
    # fan-in leg was the same hole one line earlier.
    #
    # IT OPENS IMMEDIATELY AFTER `emit_start`, not at the fan-in, and that is F12c's other half.
    # The boundary once began below `enumerate_chunks`, which was survivable while that activity
    # could only degrade — and stopped being survivable the moment it learned to REFUSE: an
    # unreadable bronze now raises `AntiJoinUnavailable` rather than silently skipping nothing, and
    # a raise above the boundary reproduces exactly the loss the boundary exists to prevent, one
    # activity earlier. Everything between the START and the terminal is inside it, so the START
    # emitted at accept can no longer be orphaned by ANY step.
    #
    # REPLAY-SAFE by construction: the runtime re-raises the RECORDED child failure identically on
    # every replay (`_durabletask/task.py` raises the persisted failure detail), so the except
    # branch is as deterministic as the success branch, and the handler does nothing but call an
    # activity — no clock, no I/O, no randomness (DWF-DET rules).
    #
    # `emit_terminal` itself failing after ITS retries still kills the workflow — deliberately.
    # There is no record to write about failing to write the record, and pretending otherwise
    # would just bury the loss one level deeper.
    #
    # WHICH IS WHY `terminal_emitted` EXISTS. Widening the boundary put the already-terminal
    # short-circuits (unit ceiling, empty source, deadline) INSIDE it, and each emits its own
    # terminal — so a permanently-failed emit on the "empty source is COMPLETE with zero rows" path
    # arrived at this handler and was answered with a SECOND emit carrying a FAILED outcome. If that
    # attempt then succeeded where the first did not — a transient outage outlasting four retries but
    # not eight is the ordinary shape of one — the graph would permanently record a run that
    # completed as failed, and the workflow would RETURN that lie. Re-raising instead keeps the rule
    # above literally true for every path rather than only for the ones that reach `finalize`.
    #
    # `units_total` is bound BEFORE the try because the FAIL record reports it and a failure above
    # `enumerate_chunks` has no count to report. Zero there is the truth: nothing was enumerated.
    units_total = 0
    terminal_emitted = False
    #: The fan-out's child instance ids, so an ABANDONMENT path can name what it is walking away from.
    #: Bound here rather than at the fan-out because the error boundary below opens at `emit_start` —
    #: a failure before the fan-out has no children, and `[]` makes that the same code path.
    child_ids: list[str] = []
    try:
        # THE CEILINGS, PINNED. An activity result is history, so every replay of this run branches on
        # the numbers its FIRST execution saw — which is what stops a rolling deploy changing whether a
        # durable timer exists half way through a run. See `RunLimits`.
        #
        # After `emit_start` on purpose: a run must be visible in the graph before anything can refuse it,
        # so a run that dies resolving its own policy is still a visibly incomplete run rather than an
        # absence someone has to notice.
        resolved_limits: dict[str, Any] = yield ctx.call_activity(resolve_limits, input=spec, retry_policy=ACTIVITY_RETRY)
        limits = RunLimits.model_validate(resolved_limits)

        # BEFORE the fan-out, not before the commit. D6's creation two-step said "created empty before
        # any fragment is COMMITTED", and that reading put it in `finalize` — which is too late, because
        # a fragment written against a dataset that does not exist yet takes lance's DEFAULTS for the
        # creation-time flags. The first in-cluster run fetched, validated and wrote every fixture and
        # then died at commit with "added files with version 2.1. However, the data storage version is
        # 2.2." The dataset must exist before the first WRITE, so the writers inherit its format.
        # The location the CATALOG vends, captured once and threaded into every chunk. Deriving it a
        # second time inside `enumerate_chunks` is what broke the first catalog-backed run: workers wrote
        # fragments to the env-composed warehouse path while the catalog's table lived somewhere else, and
        # the commit was refused with "commit references 4 data file(s) not present under the table
        # location (did the direct write target the wrong prefix?)" — after every unit had been fetched.
        # One resolution, carried; never two derivations of one location. The BASE VERSION rides the same
        # handle for the same reason, and finding F12a is what it costs when it does not — see
        # `DatasetHandle`.
        handle: dict[str, Any] = yield ctx.call_activity(ensure_dataset, input=spec, retry_policy=ACTIVITY_RETRY)
        target = DatasetHandle.model_validate(handle)

        chunks: list[dict[str, Any]] = yield ctx.call_activity(
            enumerate_chunks,
            # The ceiling travels WITH the request. It is resolved (`resolve_limits`, above) before
            # this call precisely so the activity can refuse before building a payload the transport
            # cannot carry — see `enumerate_chunks` for why the check below cannot do it alone.
            input=EnumerateChunksInput(
                spec=spec,
                dataset_uri=target.location,
                max_units=limits.max_units,
                # Travels with the request for the same reason `max_units` does: resolved once, in
                # activity scope, and pinned in history — a ceiling the body re-read would replay
                # against whatever the deployment says now.
                incremental_max_rows=limits.incremental_max_rows,
            ),
            retry_policy=ACTIVITY_RETRY,
        )
        # The enumerated total, published as CUSTOM STATUS so it is readable while the run is still
        # going. `units_total` was declared on the run record and never assigned by anything, so the API
        # could say "4 done" and never "4 of 500" — no progress bar was possible for exactly the long
        # harvest where one matters. Custom status rides the workflow's own durable state, so it survives
        # a pod death like every other run fact and needs no second writable copy.
        # `expected_units`, NOT `len(keys)`. §2.13 turned chunks into POINTERS — `offset` + `count`
        # into the run's unit manifest — and left `keys` empty by design (:230). This line kept summing
        # `keys`, so it was ZERO for every chunk a current build produces, and zero is not inert: it is
        # the early-return condition below. Every real run therefore reported COMPLETE having published
        # nothing, drained nothing and committed nothing, before the fan-out — and silently disabled
        # the `max_units` ceiling too, since 0 is never over any bound.
        #
        # The accessor exists precisely to resolve the pointer/inline split and the CHILD workflow
        # already used it (:663); only the parent did not. Every existing test drove the body with the
        # legacy inline shape, so nothing saw it.
        # THE REFUSAL FIRST, before ANYTHING treats the return value as a sequence of chunks.
        # `enumerate_chunks` may return a compact dict `{"__refused__": reason}` instead of a list, and
        # this check used to sit twelve lines BELOW the units_total sum — which iterates a dict as its
        # string KEYS, so `chunk.get(...)` raised `AttributeError: 'str' object has no attribute 'get'`
        # first. The error boundary then rendered that AttributeError as the run's FAILED reason, so an
        # operator whose run hit the unit ceiling was told about a missing `.get` instead of the
        # ceiling. The guard reported the wrong thing at exactly the moment it mattered.
        if isinstance(chunks, dict):
            reason = str(chunks.get(REFUSAL_KEY) or "enumeration was refused")
            terminal_emitted = True
            yield ctx.call_activity(
                emit_terminal,
                input=TerminalInput(spec=spec, outcome=RunOutcome(status="FAILED", errors={"run": reason}, errors_total=1)),
                retry_policy=ACTIVITY_RETRY,
            )
            return RunOutcome(status="FAILED", errors={"run": reason}, errors_total=1).model_dump()

        # Read the two fields directly rather than `ChunkSpec.model_validate(...).expected_units`:
        # validating here would REQUIRE run_id/chunk_id, and this sum must tolerate any descriptor the
        # enumerate activity hands back — including a partial one from an older build replaying across
        # a deploy. Same precedence the accessor uses: `count` when present, else `len(keys)`.
        units_total = sum(int(chunk.get("count") or 0) or len(chunk.get("keys") or ()) for chunk in chunks)
        ctx.set_custom_status(json.dumps({"units_total": units_total, "chunks": len(chunks)}))

        # THE REFUSAL enumerate_chunks may return instead of chunks — the unit ceiling and the
        # dispatch budget, both decided where the payload is built because neither can be decided
        # after it has failed to arrive. Rendered through the SAME returned-FAILED path as the ceiling
        # below, so a refusal reads identically to an operator however it was reached.
        # THE UNIT CEILING — refused HERE, before a single task is published.
        #
        # A mis-pointed source is the case: `s3-prefix` with an empty `prefix` lists a whole bucket, and
        # the registry invites exactly that ("Leave empty to take the whole bucket"). Without a ceiling
        # that becomes millions of queue messages, a fan-out of thousands of child workflows, and the
        # state-store churn of both — for a run nobody meant to start.
        #
        # It refuses by POLICY (a returned FAILED outcome), not by raising: raising inside the activity
        # would burn four retries re-LISTING the source before failing, and the operator would read a
        # crash rather than a limit. That path only reports honestly because the status merge now carries
        # a returned FAILED through to the door — see `runs.merge_workflow_state`.
        #
        # Placed after `enumerate_chunks` rather than inside it because the ceiling is a property of the
        # RUN, and this is where the run's decisions live. It cannot prevent the listing itself (see
        # `RunLimits`) — what it prevents is acting on it.
        if limits.max_units > 0 and units_total > limits.max_units:
            refused: dict[str, Any] = RunOutcome(
                status="FAILED",
                errors={
                    "run": (
                        f"source enumerated {units_total} units, over the {limits.max_units} ceiling "
                        f"(RASK_INGEST_MAX_UNITS). Narrow the source — an empty s3-prefix lists the whole "
                        f"bucket — or raise the ceiling for this deployment."
                    )
                },
                errors_total=1,
            ).model_dump()
            terminal_emitted = True
            yield ctx.call_activity(emit_terminal, input=TerminalInput(spec=spec, outcome=RunOutcome.model_validate(refused)), retry_policy=ACTIVITY_RETRY)
            return refused

        # AN EMPTY SOURCE IS A SUCCESS WITH ZERO ROWS, and it short-circuits HERE.
        #
        # Not a failure, deliberately. An empty folder or prefix is a legitimate state of the world — a
        # scheduled ETL over a quiet source hits it routinely — and reporting it as FAILED would alert
        # every time nothing happened to arrive, which is how an alert stops being read. "0 rows" is the
        # honest answer and it is queryable; "failed" is neither.
        #
        # Short-circuited rather than left to fall through, because the fall-through is `when_all([])`
        # and its behaviour on an empty list is not a documented guarantee — the run would be resting on
        # an implementation detail of the fan-in. Returning here makes the empty case a decision the
        # workflow states rather than an accident it survives.
        #
        # `finalize` is skipped too, which is what keeps the "no empty version" half true: it would find
        # no fragments and `commit_fragments` already refuses to commit an empty list (a run whose every
        # unit failed should leave no version behind to explain), but not calling it at all means the
        # dataset is not even opened for a run that had nothing to write.
        if units_total == 0:
            empty: dict[str, Any] = RunOutcome(status="COMPLETE", rows=0).model_dump()
            empty["units_total"] = 0
            terminal_emitted = True
            yield ctx.call_activity(emit_terminal, input=TerminalInput(spec=spec, outcome=RunOutcome.model_validate(empty)), retry_policy=ACTIVITY_RETRY)
            return empty

        # Fan out. when_all is fan-in: the parent suspends until every child has drained, and survives
        # its own pod dying because the history replays. This is the durable-orchestration property that
        # a hand-rolled counter had to imitate.
        # DERIVED FROM POSITION, not read off the payload. It is the same string either way — a real
        # `ChunkSpec.chunk_id` is `<run_id>-c<index // CHUNK_SIZE>`, and these are enumerated in that
        # order — but indexing the dict would put a `KeyError` inside the ORCHESTRATOR, where it is a
        # terminal workflow failure rather than a bad payload. Position is always available and always
        # replays the same. Without an id the runtime mints one, and a parent that abandons the fan-out
        # then has no NAME for the children it must stop.
        child_ids = [f"{spec.run_id}-c{i}" for i in range(len(chunks))]
        fanout = wf.when_all([ctx.call_child_workflow(chunk_run, input=c, instance_id=cid) for c, cid in zip(chunks, child_ids, strict=True)])

        # THE RUN DEADLINE — A15's other half, which nothing enforced.
        #
        # `RASK_INGEST_MAX_RUN_HOURS` was declared in `chart/values.yaml` and read by NO code, while the
        # A15 gate asserted `maintenance.olderThanDays * 24 >= max_run_hours` and passed. That gate
        # certifies "version GC keeps more history than a run can take" — a guarantee that is fiction
        # while nothing bounds how long a run takes. A green gate over an unenforced relation is worse
        # than no gate: it is a promise with nothing behind it, and the failure it exists to prevent
        # (GC deleting the version a live run is committing against) is silent data loss.
        #
        # `ctx.create_timer` is a DURABLE Dapr timer — runtime-managed, replay-safe, and explicitly NOT
        # counted against A13's in-process timer budget (the condition names Dapr workflow timers as the
        # carve-out). It is not a poll: the workflow suspends and the runtime wakes it once.
        #
        # ZERO MEANS UNBOUNDED, and that is the default in code. The plane's own docstrings advertise
        # million-unit runs, so a live default would break the legitimate long harvest this is meant to
        # protect — the deployment opts in, exactly like the other ceilings. The value is `limits`, an
        # ACTIVITY RESULT, and never env read here: whether a timer exists must not change under a replay.
        # CANCELLATION IS A TERMINAL PATH, NOT A KILL — and it has to be raced whether or not this
        # deployment sets a deadline, so the fan-in is one `when_any` in both branches.
        #
        # `terminate_workflow(run_id)` used to be the whole of `POST /v1/ingests/{id}/terminate`. It
        # sets the instance TERMINATED and never resumes the generator, so `emit_terminal` — the ONLY
        # caller of `release_run_units` — never runs. The run's JetStream subject and its per-run
        # durable consumer were left behind permanently (WORK_QUEUE retention means a message leaves
        # only when acked, and no consumer for that run id is ever created again), and no FAIL record
        # reached lineage: the run simply vanished. This is the `messages: 1, consumers: 0` the
        # release comment in `emit_terminal` records from the live estate.
        #
        # Asking the run to stop costs what the ruling accepted knowingly: terminate is asynchronous
        # now, so a parent wedged before its select will not honour it. What it buys is ONE cleanup
        # path — the deadline branch below already does exactly the right sequence, and cancellation
        # joins it rather than inventing a second one.
        cancel = ctx.wait_for_external_event(CANCEL_EVENT)
        deadline = ctx.create_timer(timedelta(hours=limits.max_run_hours)) if limits.max_run_hours > 0 else None
        winner = yield wf.when_any([fanout, cancel] if deadline is None else [fanout, deadline, cancel])

        # ONE terminal sequence for BOTH early exits. They differ only in the reason, and the whole
        # finding is that a second exit skipped the cleanup this one does — so they share the code
        # rather than agreeing by inspection. `terminal_emitted` has to be set BETWEEN the two calls
        # (a failing `terminate_chunks` should still reach the outer boundary's FAIL emit, while a
        # failing `emit_terminal` must not be answered with a second, contradicting record), which is
        # why this is inline rather than a delegated helper.
        terminal: dict[str, Any] | None = None
        if deadline is not None and winner is deadline:
            # Does NOT fall through to `finalize`: committing a partial harvest under a deadline would
            # publish a dataset nobody asked for and mark it complete. The staged fragments stay
            # staged — recoverable by a re-run, which converges because unit ids are content-derived.
            terminal = RunOutcome(
                status="FAILED",
                errors={"run": f"exceeded the {limits.max_run_hours}h ceiling (RASK_INGEST_MAX_RUN_HOURS) with {units_total} units enumerated"},
                errors_total=1,
            ).model_dump()
        elif winner is cancel:
            # TERMINATED, not FAILED. This branch is reached only because a PERSON asked, through
            # `POST /ingests/{id}/terminate` — it is the one terminal state in this workflow that is
            # an intended outcome rather than a defect, and collapsing it into FAILED made a
            # deliberate stop indistinguishable from a crash in any list. The deadline branch above
            # stays FAILED on purpose: nobody chose it.
            #
            # `errors` still carries the reason, because the reason is the whole value of the record.
            terminal = RunOutcome(
                status="TERMINATED",
                errors={"run": f"terminated by operator{_cancel_detail(cancel.get_result())} with {units_total} units enumerated"},
                errors_total=1,
            ).model_dump()

        if terminal is not None:
            # STOP THE CHILDREN BEFORE RECLAIMING THEIR QUEUE. `emit_terminal` releases the run's
            # JetStream subject and DELETES the per-run durable (`runtime.release_run_units` →
            # `queue.release_run`) — the exact consumer every live `drain_chunk` is pulling from.
            # Abandoning the fan-out and then pulling the queue out from under it is §2.4.
            yield ctx.call_activity(terminate_chunks, input=TerminateChunksInput(child_ids=child_ids), retry_policy=ACTIVITY_RETRY)
            terminal_emitted = True
            yield ctx.call_activity(emit_terminal, input=TerminalInput(spec=spec, outcome=RunOutcome.model_validate(terminal)), retry_policy=ACTIVITY_RETRY)
            return terminal

        results = fanout.get_result()

        parsed = [ChunkResult.model_validate(r) for r in results]
        # BOUNDED ON THE RETURN LEG TOO (DWF-ACT-004). `enumerate_chunks` is refused above its
        # budget; this direction had no guard at all, and it is the larger of the two: every child's
        # serialised FragmentMetadata is flattened here and handed to `finalize` as one activity
        # input, so it is persisted in history AND re-delivered on every parent replay.
        #
        # The carried list is a FALLBACK, not the commit: `finalize_run` commits what
        # `discover_staged` finds ("STORAGE TRUTH, and it is the ONLY truth") and reads this only
        # when staging returns nothing. A staging-prefix pointer cannot replace it — the pointer is
        # worthless in exactly the case the fallback exists for — so the ruling (owner, 2026-08-25)
        # is to keep the fallback while it fits and drop it LOUDLY past the budget rather than build
        # a message grpc refuses.
        fragments, fallback_dropped = _bound_carried_fragments([f for r in parsed for f in r.fragments], run_id=spec.run_id)
        # BOUNDED at the merge, not merely at each child. N chunks each carrying up to
        # MAX_REPORTED_ERRORS entries is still N * MAX_REPORTED_ERRORS in the parent's history, and
        # the parent's dict is the one that rides into `finalize`'s input and out again as the run's
        # own outcome. The exact count comes from the children's totals rather than from counting
        # keys, so a unit a child had to drop is still counted here.
        merged: dict[str, str] = {}
        for result in parsed:
            merged.update(result.errors)
        errors, errors_total = bound_errors(merged, sum(r.errors_total for r in parsed))
        # Now that children report what they landed, the fan-in can say how much of the run actually
        # arrived — before `finalize` runs, rather than only after its terminal output exists.
        ctx.set_custom_status(json.dumps({"units_total": units_total, "units_done": sum(r.units_done for r in parsed), "finalizing": len(fragments)}))

        # Exactly one commit for the whole run — D6. Nothing is visible in bronze until this returns, so
        # there is no observable partially-ingested state to reason about. INSIDE the boundary on
        # purpose: a permanently-refused commit is exactly as much a run failure as a dead chunk, and
        # it must leave the same FAIL record. Staged fragments stay staged either way — a re-run
        # converges via content-derived identity, and the orphan scan reports what nothing reclaimed.
        #
        # `read_version` rides the input, so an activity RETRY and a workflow REPLAY present the same
        # base version the first attempt did — which is the only reason the catalog can recognize its
        # own earlier commit rather than appending the run's rows a second time (F12a, `DatasetHandle`).
        outcome: dict[str, Any] = yield ctx.call_activity(
            finalize,
            input=FinalizeInput(
                spec=spec,
                fragments=fragments,
                # So `finalize_run` can tell "this run genuinely wrote nothing" from "its fallback
                # was too large to carry" — the two look identical from an empty list.
                fallback_dropped=fallback_dropped,
                errors=errors,
                errors_total=errors_total,
                units_total=units_total,
                read_version=target.read_version,
            ),
            retry_policy=ACTIVITY_RETRY,
        )
    except Exception as exc:
        if terminal_emitted:
            # The failure IS a terminal emit — this run already stated how it ended, and answering
            # that with a second, contradicting record is worse than dying. See `terminal_emitted`.
            raise
        failed: dict[str, Any] = RunOutcome(
            # The recorded failure detail replays identically, so this string is deterministic.
            status="FAILED",
            errors={"run": f"unrecoverable before finalize completed: {str(exc)[:400]}"},
            errors_total=1,
        ).model_dump()
        failed["units_total"] = units_total
        # THE SAME STOP, and this branch is why §2.4 got WORSE rather than staying put. `when_all`
        # completes on the FIRST failed child (`_durabletask/task.py` — late arrivals are ignored), so
        # this boundary is reached while every SIBLING is still draining, and `emit_terminal` below
        # deletes the durable they are pulling from. Before the boundary existed, only a >24h deadline
        # could reach that state; now one permanently-bad object does.
        #
        # Dispatched only when there ARE children: the boundary opens at `emit_start`, long before the
        # fan-out, so most failures reaching here have none — and an activity that would immediately
        # return still costs a real workflow-history event on every one of them. The branch is
        # replay-safe: `child_ids` derives from `chunks`, an ACTIVITY RESULT, so it is history.
        if child_ids:
            yield ctx.call_activity(terminate_chunks, input=TerminateChunksInput(child_ids=child_ids), retry_policy=ACTIVITY_RETRY)
        yield ctx.call_activity(emit_terminal, input=TerminalInput(spec=spec, outcome=RunOutcome.model_validate(failed)), retry_policy=ACTIVITY_RETRY)
        return failed

    yield ctx.call_activity(emit_terminal, input=TerminalInput(spec=spec, outcome=RunOutcome.model_validate(outcome)), retry_policy=ACTIVITY_RETRY)
    return outcome


def chunk_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Child workflow: publish this chunk's units, then drain them.

    A Generator, for the reason spelled out on `ingest_run` — the annotation names the protocol, not
    the payload the last `return` carries.

    THE DRAIN IS AN ACTIVITY, and the first version's was not — it suspended on an external event
    that nothing in the estate ever raised. Read together, `worker.signal_drained` published to a
    NATS subject while this waited on a Dapr workflow event, with no bridge between them; and no
    Deployment ran a `Worker` at all. So every chunk would have published its units, waited the full
    ten-minute fallback, reconciled an untouched queue, and reported zero fragments. The lane looked
    complete because a run with no units never reached this code.

    Draining inside an activity is what makes the queue real rather than decorative: Dapr persists
    the activity's result and replays the activity — not the workflow's decisions — if the pod dies,
    while JetStream still owns redelivery, poison parking and `max_ack_pending` backpressure within
    the chunk. Fan-out is across CHUNKS (Dapr distributes child workflows over the pod fleet) and,
    within a chunk, across `sizing.fetch_concurrency`.

    Nothing here polls (A13): the activity blocks on JetStream's server-fulfilled pull fetch.
    """
    # The WORKFLOW body still validates by hand. The SDK coerces an ACTIVITY's input into its
    # annotated model (`workflow_runtime._coerce_activity_input`); a workflow body's input is not
    # coerced, so this line is what makes `chunk` a model rather than a dict.
    chunk = ChunkSpec.model_validate(payload)

    yield ctx.call_activity(publish_units, input=chunk, retry_policy=ACTIVITY_RETRY)

    drained: dict[str, Any] = yield ctx.call_activity(drain_chunk, input=chunk, retry_policy=ACTIVITY_RETRY)

    errors: dict[str, str] = dict(drained.get("errors") or {})
    # The drain already capped its own payload, so `len(errors)` is not the count — it reports the
    # true one separately and this must carry it, or every unit the cap dropped stops being counted.
    errors_total = int(drained.get("errors_total") or len(errors))

    # Confirm against the QUEUE, not against the drain's own report. A drain that returned early —
    # its fetch timed out because another pod held the units — is indistinguishable from a complete
    # one in its own result, and only the stream knows the difference. `num_pending == 0` on a
    # WORK_QUEUE stream means every unit was acked, by whichever worker did it.
    # `count` for a pointer chunk, `len(keys)` for a legacy inline one — `expected_units` is the one
    # place that difference is resolved, so no other site has to know which form it holds.
    if errors_total or int(drained.get("units_done") or 0) < chunk.expected_units:
        reconciled: dict[str, Any] = yield ctx.call_activity(reconcile_chunk, input=chunk, retry_policy=ACTIVITY_RETRY)
        extra = {key: value for key, value in (reconciled.get("errors") or {}).items() if key not in errors}
        errors.update(extra)
        errors_total += len(extra)

    units_done = int(drained.get("units_done") or 0)

    # THE FAN-OUT'S ONLY PROGRESS SIGNAL, and it has to come from the child.
    #
    # The parent sets `units_total` before the fan-out and `finalizing` after the fan-in, and in
    # between it is blocked on ONE `when_all` yield — a workflow can only set its status between
    # yields, so the parent cannot report progress during the phase that takes the longest. For a
    # million-unit harvest that is hours in which the run's own status is frozen at the value it held
    # before the first unit was published.
    #
    # Custom status rather than a metric: this is per-INSTANCE state read by that instance's own GET,
    # not an aggregate. A per-chunk metric would also mint one series per chunk id, which is exactly
    # the unbounded-label rule the estate has already been burned by.
    ctx.set_custom_status(json.dumps({"chunk_id": chunk.chunk_id, "units_done": units_done, "units_expected": chunk.expected_units}))

    listed, total = bound_errors(errors, errors_total)
    return ChunkResult(
        chunk_id=chunk.chunk_id,
        fragments=[str(fragment) for fragment in (drained.get("fragments") or ())],
        errors=listed,
        errors_total=total,
        units_done=units_done,
    ).model_dump()


# ── activities — every non-deterministic thing lives behind one of these ───────────────


# --------------------------------------------------------------------------------------------------
# ACTIVITY ENVELOPES (DWF-ACT-009)
#
# dapr-ext-workflow 1.18 coerces an activity's input into whatever model its second parameter is
# annotated with — `workflow_runtime._coerce_activity_input` -> `_model_protocol.coerce_to_model`,
# duck-typed on `model_dump`/`model_validate` rather than importing pydantic. So an annotation here
# is enforced by the runtime, not decoration.
#
# The four composite envelopes below exist because those activities read raw dicts, and every read
# was of the form `payload.get("fragments") or []` / `payload.read_version`. A key
# that went missing — a caller edited, a field renamed — did not raise; it became a plausible DEFAULT,
# and the run continued with an empty fragment list or a base version of 0. The second of those is
# the one that commits a run's rows a second time.
#
# Every field is REQUIRED on purpose. Each is unconditionally supplied at every call site, so a
# default here would only ever mask the defect this closes. The six activities that already took a
# whole `RunSpec`/`ChunkSpec` simply name it now, and their manual `model_validate` first line goes.
# --------------------------------------------------------------------------------------------------


class EnumerateChunksInput(BaseModel):
    """`enumerate_chunks`' envelope: the run, its target, and the two ceilings that travel with it."""

    spec: RunSpec
    dataset_uri: str
    max_units: int
    incremental_max_rows: int


class FinalizeInput(BaseModel):
    """`finalize`'s envelope. `read_version` is the load-bearing field: defaulting it to 0 makes the
    catalog unable to recognise its own earlier commit, and the run's rows land twice."""

    spec: RunSpec
    fragments: list[str]
    fallback_dropped: bool
    errors: dict[str, str]
    errors_total: int
    units_total: int
    read_version: int


class TerminalInput(BaseModel):
    """`emit_terminal`'s envelope: the run and the outcome being recorded for it."""

    spec: RunSpec
    outcome: RunOutcome


class TerminateChunksInput(BaseModel):
    """`terminate_chunks`' envelope: the child instance ids the parent has abandoned."""

    child_ids: list[str]


def emit_start(ctx: WorkflowActivityContext, spec: RunSpec) -> None:
    """Lineage START, through lineage-kit. A run is visible in the graph before it does any work.

    Deliberately first: the medallion emitted only on COMPLETE, so a run that died mid-harvest left
    NO record at all — not a failed one, none. A START here means a crashed run is a visibly
    incomplete run rather than an absence someone has to notice.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    spec = RunSpec.model_validate(spec)
    _lineage().start(spec.run_id, spec.project, spec.dataset, spec.kind, spec.options, spec.originator)


def resolve_limits(ctx: WorkflowActivityContext, spec: RunSpec) -> dict[str, Any]:
    """Read the run's POLICY ceilings — the plane's one sanctioned env read for them.

    AN ACTIVITY, not a module constant, and that is the entire point: an activity's result is written
    to workflow history, so every replay of this run sees the numbers its first execution saw. A
    module-level `os.getenv` looked like a constant and behaved like a clock — see `RunLimits`.

    Passes the spec's own `limits` straight through when the accepting door already resolved them, so
    a run can be pinned to what it was ACCEPTED with rather than to what the pod that first executed
    it happened to hold.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    spec = RunSpec.model_validate(spec)
    return (spec.limits or RunLimits.from_env()).model_dump()


def ensure_dataset(ctx: WorkflowActivityContext, spec: RunSpec) -> dict[str, Any]:
    """Create the run's bronze dataset EMPTY, carrying the creation-time flags. D6 step 1.

    Idempotent, so a replay is a no-op rather than a second create — which matters because this runs
    before the fan-out and is therefore the activity most likely to be replayed.

    Returns the table's location AND the version this run's fragments will be committed against.
    Both are resolved HERE, once, and carried: two derivations of one location is what made workers
    write where the catalog was not looking, and two derivations of one base VERSION is what made the
    catalog's replay dedupe unreachable (F12a — `DatasetHandle` has the mechanism).
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    spec = RunSpec.model_validate(spec)
    from ingest.runtime import ensure_dataset_at

    location, read_version = ensure_dataset_at(spec)
    return DatasetHandle(location=location, read_version=read_version).model_dump()


#: grpc's own default `max_receive_message_length`, in bytes. NOT ours to choose: `WorkflowRuntime`
#: exposes no `channel_options`, and `dapr.ext.workflow.internal.shared.get_grpc_channel` merges only
#: `DEFAULT_GRPC_KEEPALIVE_OPTIONS` — four keepalive tuples, no size keys — so this is what an
#: activity result is measured against on the way back to the sidecar.
GRPC_MAX_MESSAGE_BYTES: int = 4 * 1024 * 1024

#: What one `enumerate_chunks` result may occupy. Deliberately well under the ceiling rather than at
#: it: the measured payload is the JSON this activity builds, while what grpc weighs is that plus the
#: durabletask envelope around it, and a budget set AT the limit would refuse nothing until the
#: envelope pushed it over — which is the failure this exists to make impossible.
CHUNK_DISPATCH_BUDGET_BYTES: int = 3 * 1024 * 1024

#: The SAME ceiling, on the return leg. An activity RESULT crosses the sidecar as one gRPC message
#: exactly as its input does, and the fan-in's merged fragment list rides into `finalize` as an
#: activity input on top of that — so it is measured against the identical budget rather than a
#: second number that could drift upward and re-open the wedge. See
#: `services/ingest/tests/test_fanin_return_ceiling.py`.
FANIN_RETURN_BUDGET_BYTES: int = CHUNK_DISPATCH_BUDGET_BYTES

#: The key a refusal is carried under. A dict, where the success path returns a list, so the body can
#: tell them apart structurally rather than by inspecting contents.
REFUSAL_KEY: Final[str] = "__refused__"


#: The external event `POST /v1/ingests/{id}/terminate` raises. Named here rather than at the two
#: sites that use it, because the workflow and the route must agree on the string or the run hangs
#: until its deadline with nothing saying why (DWF-MGT-006).
CANCEL_EVENT: str = "cancel"


def _cancel_detail(reason: object) -> str:
    """The operator's reason, as a suffix, or nothing. Never raises on a shape it did not expect.

    The event payload is whatever the terminate route sent, and a run must not fail to record its own
    termination because somebody posted a bare string where a dict was expected.
    """
    if isinstance(reason, dict):
        detail = str(reason.get("reason") or "")
    elif isinstance(reason, str):
        detail = reason
    else:
        detail = ""
    return f": {detail}" if detail else ""


def _bound_carried_fragments(fragments: list[str], *, run_id: str) -> tuple[list[str], bool]:
    """The merged fallback list, or nothing plus a flag saying it was dropped.

    Returns `(carried, dropped)`. Dropping is not a silent truncation: a HALF list would be worse
    than none, because `finalize_run`'s fallback commits what it is handed and a partial fallback is
    a partial commit presented as a whole one. All or nothing, and the caller carries the fact.

    Measured against the serialised form because that is what crosses the wire — a length check on
    the list would pass a few enormous manifests and refuse many small ones.
    """
    if not fragments:
        return fragments, False
    size = len(json.dumps(fragments))
    if size <= FANIN_RETURN_BUDGET_BYTES:
        return fragments, False
    import logging

    logging.getLogger(__name__).warning(
        "ingest_fanin_fallback_dropped",
        extra={"run_id": run_id, "bytes": size, "budget": FANIN_RETURN_BUDGET_BYTES, "fragments": len(fragments)},
    )
    return [], True


def _refuse_oversized_dispatch(chunks: list[dict[str, Any]], *, units: int, max_units: int) -> dict[str, Any] | None:
    """The compact refusal, or ``None`` to proceed. See :func:`enumerate_chunks` for why it is here.

    Two ceilings, one refusal, and the ORDER matters. `max_units` is the operator's declared intent
    and is reported as such; the dispatch budget is a property of the transport and is reported as
    such. Checking the declared one first means a deployment that set a sane ceiling gets the message
    it configured, not a message about gRPC.

    The size is MEASURED rather than estimated from a per-key constant: keys are source-supplied and
    an estimate calibrated on one source is wrong for the next. `json.dumps` here is the same
    serialization the SDK performs on the way out (`shared.to_json`), so this weighs the real thing.
    """
    if max_units > 0 and units > max_units:
        return {
            REFUSAL_KEY: (
                f"source enumerated {units} units, over the {max_units} ceiling (RASK_INGEST_MAX_UNITS). "
                f"Narrow the source — an empty s3-prefix lists the whole bucket — or raise the ceiling for this deployment."
            )
        }
    size = len(json.dumps(chunks).encode())
    if size > CHUNK_DISPATCH_BUDGET_BYTES:
        return {
            REFUSAL_KEY: (
                f"source enumerated {units} units, whose chunk descriptors serialize to {size} bytes — over the "
                f"{CHUNK_DISPATCH_BUDGET_BYTES}-byte budget this activity's result must fit in (grpc's "
                f"{GRPC_MAX_MESSAGE_BYTES}-byte default, which the Dapr workflow worker does not raise). "
                f"Set RASK_INGEST_MAX_UNITS to bound the run, or narrow the source."
            )
        }
    return None


def enumerate_chunks(ctx: WorkflowActivityContext, payload: EnumerateChunksInput) -> list[dict[str, Any]] | dict[str, Any]:
    """Walk the source adapter and slice it into chunk descriptors.

    An activity, not workflow code, because it does network I/O — and because enumeration itself must
    survive a pod death: as an activity its result is persisted and replayed, which is precisely the
    gap that made a half-published enumeration the one un-redeliverable failure in the queue-only
    design.

    Returns CHUNKS, never units. One child workflow per CHUNK_SIZE keys returns one compact result;
    a million activity results would melt the state store, which is the whole reason chunking exists
    and the reason this plane needs no separate ledger.

    **THE DISPATCH CEILING, and why the run ceiling could not enforce it.** This one activity result
    carries EVERY key and token in the run, and an activity result crosses the sidecar as one gRPC
    message. `dapr-ext-workflow` builds its worker channel with `DEFAULT_GRPC_KEEPALIVE_OPTIONS` and
    nothing else (`internal/shared.py`), so grpc's 4 MiB default `max_receive_message_length` stands —
    measured at ~83 bytes per serialized key+token, that is a hard wedge somewhere near 25,000 units,
    against a plane whose own docstrings advertise million-unit harvests.

    `max_units` was supposed to stop that and CANNOT: it is checked in the workflow body, which only
    runs once this result has been DELIVERED. The oversized message fails on the way back, so the
    guard sits behind the failure it guards against — and the failure it becomes is a
    `RESOURCE_EXHAUSTED` from inside the SDK, on a workflow that then retries the listing four times
    and wedges, with nothing naming a knob.

    So the ceiling is enforced HERE, before the payload exists, and the policy decision still belongs
    to the workflow: this returns a compact REFUSAL marker instead of the chunks, and the body renders
    it through the same returned-FAILED path `max_units` already uses. A refusal is one small dict, so
    it always fits.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    payload = EnumerateChunksInput.model_validate(payload)
    from ingest.identity import unit_id
    from ingest.sources import SourceSpec, build_source, iter_versioned_unit_keys

    spec = payload.spec
    uri = payload.dataset_uri

    # THE ANTI-JOIN — what makes repeated ingest CONVERGE (owner goal 2026-08-07). Before this,
    # `lander.py` committed a blind Append and a re-run duplicated every row: nine runs of one
    # fixture prefix measured nine copies per file. The identity is `unit_id(key, token)` — the
    # SAME derivation the worker writes (`identity.py`, one function, two callers) — so:
    #   unchanged object  -> same id -> skipped HERE, its bytes never fetched;
    #   replaced object   -> new etag -> new id -> lands as a NEW row, the old row stays;
    #   new object        -> no id match -> lands.
    # Projecting the `id` column ALONE (int64, 8 bytes/row) deliberately: `payload` is a blob-v2
    # sidecar and never rides a projection that does not name it. A quiet tick drops every key,
    # falls into the units_total == 0 short-circuit, and leaves NO Lance version. The skip is
    # LOGGED with counts — silent truncation would read as data loss in reverse.
    #
    # A FAILED READ IS A FAILURE (F12c). This sat under a bare `except Exception` that logged and
    # continued with an EMPTY set — and an empty set does not mean "nothing to skip", it means "skip
    # NOTHING": one transient object-store error and the run re-fetches and re-lands every object
    # bronze already holds, silently duplicating a whole tier. That is not a degradation of the
    # anti-join, it is precisely the failure the anti-join exists to prevent, reached through the
    # anti-join's own error path. `ensure_dataset` ran immediately before this activity and returned
    # this very location, so the table exists by construction and nothing here is a legitimate
    # absence — an unreadable `id` column is a read failure, and the honest answer is to stop.
    #
    # BEFORE the source walk, so a doomed attempt costs one listing instead of four: raising puts this
    # under ACTIVITY_RETRY, and every attempt re-executes the whole activity body.
    try:
        import lance

        dataset = lance.dataset(uri)
        rows = dataset.count_rows()
        # BEFORE the read, not after: the point of the ceiling is to avoid materialising the set, so
        # checking once it is already in memory would enforce nothing. Refusing — never sampling —
        # because a truncated anti-join INVERTS: a partial "already have" set makes the run treat
        # rows bronze holds as new and re-land every one of them.
        if not anti_join_within_ceiling(rows, payload.incremental_max_rows):
            raise AntiJoinUnavailable(
                f"{uri} holds {rows} rows, above the {payload.incremental_max_rows}-row "
                f"RASK_INGEST_INCREMENTAL_MAX_ROWS ceiling. The anti-join reads every id to learn what "
                f"bronze already has, and it cannot be truncated — a partial answer re-lands rows that "
                f"are already there. Raise the ceiling, or narrow the source so the run does not need it."
            )
        existing: set[int] = set(dataset.to_table(columns=["id"]).column("id").to_pylist()) if rows else set()
    except Exception as exc:
        raise AntiJoinUnavailable(
            f"could not read the `id` column of {uri}, so this run cannot tell which objects bronze already holds. "
            f"Ingesting anyway would re-land every one of them: {exc}"
        ) from exc

    source_spec = SourceSpec(kind=spec.kind, project=spec.project, dataset=spec.dataset, options=spec.options)
    # KEYS, not objects. `iter_units` reads every object's bytes to hand back its uri — so
    # enumerating a IIIF volume through it downloaded the whole volume here and the workers then
    # downloaded it again. Two full transfers of the source, the first with no backpressure at all.
    pairs = list(iter_versioned_unit_keys(build_source(source_spec)))

    if existing:
        before = len(pairs)
        pairs = [(key, token) for key, token in pairs if unit_id(key, token) not in existing]
        skipped = before - len(pairs)
        if skipped:
            import logging

            logging.getLogger(__name__).info(
                "anti-join: %d of %d objects already in bronze (by id) — fetching %d",
                skipped,
                before,
                len(pairs),
            )

    # THE UNIT LIST IS WRITTEN ONCE, to object storage, and the chunks POINT at it (§2.13). Inline
    # keys made this activity's result O(units) and each child's input O(units) again; the manifest
    # makes both O(chunks). Written BEFORE the descriptors exist so a descriptor can never name a
    # window that was not persisted.
    from ingest.staging import write_unit_manifest

    write_unit_manifest(uri, spec.run_id, pairs)

    chunks: list[dict[str, Any]] = []
    for index in range(0, len(pairs), CHUNK_SIZE):
        window = pairs[index : index + CHUNK_SIZE]
        chunks.append(
            ChunkSpec(
                run_id=spec.run_id,
                chunk_id=f"{spec.run_id}-c{index // CHUNK_SIZE}",
                offset=index,
                count=len(window),
                dataset_uri=uri,
                # Carried, not re-resolved — same reason as `dataset_uri` above.
                namespace=spec.namespace,
                sizing=spec.sizing,
                kind=spec.kind,
                project=spec.project,
                dataset=spec.dataset,
                options=spec.options,
            ).model_dump()
        )
    refusal = _refuse_oversized_dispatch(chunks, units=len(pairs), max_units=payload.max_units)
    return refusal if refusal is not None else chunks


def drain_chunk(ctx: WorkflowActivityContext, chunk: ChunkSpec) -> dict[str, Any]:
    """Consume this chunk's units: fetch, validate, stage a fragment, ack. The plane's actual work.

    Safe to retry, which is the only reason it can be an activity at all. Every unit acked by a
    previous attempt staged its fragment's identity next to its bytes BEFORE acking
    (`ingest.staging`), so a replay after a pod death re-drains only what is still on the queue and
    `finalize` recovers the rest from storage. Without that staging the retry would be silent data
    loss: acked units are gone from a WORK_QUEUE stream, and their fragments' names died with the
    pod.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    chunk = ChunkSpec.model_validate(chunk)
    from ingest.runtime import drain_chunk_units

    return _run_async(drain_chunk_units(chunk))


def publish_units(ctx: WorkflowActivityContext, chunk: ChunkSpec) -> int:
    """Publish this chunk's units onto the JetStream work queue.

    Idempotent by construction under replay: JetStream dedupes on the message id within the stream's
    duplicate window, and a unit's id is derived from (run, key) — both stable. A replayed activity
    therefore re-publishes without re-queuing work, which matters because Dapr replays an activity
    whose result was not durably recorded before the pod died.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    chunk = ChunkSpec.model_validate(chunk)
    from ingest.runtime import publish_chunk_units

    return _run_async(publish_chunk_units(chunk))


def reconcile_chunk(ctx: WorkflowActivityContext, chunk: ChunkSpec) -> dict[str, Any]:
    """Storage truth for a chunk whose drained signal was lost — the dead-man's one read.

    Asks the QUEUE what is outstanding rather than a ledger: with WORK_QUEUE retention an acked unit
    is gone, so `num_pending == 0` means the chunk really did drain and the signal was simply lost.
    Degrades to slow, never to stuck.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    chunk = ChunkSpec.model_validate(chunk)
    from ingest.runtime import reconcile_from_queue

    return _run_async(reconcile_from_queue(chunk))


def finalize(ctx: WorkflowActivityContext, payload: FinalizeInput) -> dict[str, Any]:
    """The lander: fragments -> ONE Lance commit, registered through the catalog.

    `read_version` comes from the INPUT and is never re-read here. Dapr re-executes an activity whose
    result it did not durably record, so a `finalize` that committed and then lost its pod runs again
    with the same input — and the catalog answers a repeat of the same (run_id, read_version) with the
    version that run already committed instead of appending its rows twice. Re-reading the version per
    attempt is what made that dedupe unreachable; see `DatasetHandle`.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    payload = FinalizeInput.model_validate(payload)
    from ingest.runtime import finalize_run

    spec = payload.spec
    outcome = finalize_run(
        spec,
        payload.fragments,
        payload.errors,
        read_version=payload.read_version,
        fallback_dropped=bool(payload.fallback_dropped),
    )
    # Carried into the terminal output so a FINISHED run still reports what it set out to do — the
    # custom status is the live view, this is the permanent one.
    outcome["units_total"] = payload.units_total
    # The EXACT failure count, which `errors` no longer is once the cap bites (`bound_errors`).
    outcome["errors_total"] = payload.errors_total
    return outcome


def emit_terminal(ctx: WorkflowActivityContext, payload: TerminalInput) -> None:
    """Lineage COMPLETE or FAIL — the FAIL branch is the gap the medallion head never closed.

    A run that fails must leave a FAIL record, not silence. The medallion turned a ValueError into a
    400 and emitted nothing, so a failed harvest was indistinguishable from one that never started.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    payload = TerminalInput.model_validate(payload)
    spec = payload.spec
    outcome = payload.outcome

    # RELEASE WHAT THIS RUN LEFT QUEUED, on every terminal path.
    #
    # `publish_units` runs in one activity and `drain_chunk` in a LATER one, and the drain is what
    # creates the durable consumer — so a run that dies between them strands its units behind a
    # consumer that was never BORN. WORK_QUEUE retention makes that permanent: a message leaves only
    # when acked, no consumer for that run id is ever created again, and nothing sweeps the stream.
    # The live estate sat at `messages: 1, consumers: 0` for hours with every other signal green.
    #
    # HERE rather than in a sweep, and that is the whole design. A sweep would have to re-derive
    # "this run has no live workflow" from outside — the same inference `/queue`'s `stranded` flag
    # exists because two readers got it wrong on the same day. The workflow does not infer: it knows
    # it is ending, and it releases what it published.
    #
    # Best-effort by construction (`release_run` never raises): tidying up must not turn a run that
    # landed its data into a run that failed. Same reasoning as I8 for lineage.
    from ingest.runtime import release_run_units

    _run_async(release_run_units(spec.run_id))

    # The verdict and the volume, from the one activity that knows both. The orchestrator RETURNS
    # failure rather than raising, so Dapr's own execution counter records `status="success"` here.
    record_run(outcome.status)
    record_units(written=outcome.rows, failed=outcome.errors_total)

    # THE SPANS THAT ALREADY EXIST carry only SDK and sidecar identifiers, so nothing on this hop names
    # the run or the dataset — the two things an operator actually knows about a failed harvest. Set on
    # the live span rather than opening a third one.
    #
    # Both ride the SPAN and neither is a metric label: a run id is per-run and a dataset is
    # caller-supplied, so either would be an unbounded series. `ingest.runs{status}` above carries the
    # closed half.
    span = trace.get_current_span()
    span.set_attribute("lance.ingest.run_id", spec.run_id)
    span.set_attribute("lance.ingest.dataset", spec.dataset)
    if outcome.status != "COMPLETE":
        # The error boundary RETURNS RunOutcome(status="FAILED") rather than raising — deliberately, so
        # the queue release and the FAIL lineage record still happen — which means daprd sees an activity
        # that completed normally and marks nothing.
        span.set_status(Status(StatusCode.ERROR, f"run {outcome.status}: {outcome.errors_total} unit(s) failed"))

    _lineage().terminal(
        spec.run_id,
        outcome.status,
        outcome.committed_version,
        outcome.rows,
        outcome.errors,
        project=spec.project,
        dataset=spec.dataset,
        originator=spec.originator,
        # `outcome.status` is deliberately NOT touched by the verdict. A refused gate is a COMPLETE
        # run whose DATA was declined, and the medallion's `/bronze-arrival` head fires only on
        # COMPLETE — flipping it to FAIL would cancel the whole bronze->silver->gold cascade for a
        # run whose rows are committed and durable.
        published=outcome.published,
        publish_reason=outcome.publish_reason,
        publish_error=outcome.publish_error,
    )


def _lineage() -> Any:  # noqa: ANN401 — resolved lazily so importing this module needs no emitter
    from ingest.runtime import lineage_emitter

    return lineage_emitter()


def _run_async(coro: Any) -> Any:  # noqa: ANN401
    """Run a coroutine from a SYNC activity body, on the worker's ONE loop.

    Dapr Workflow activities are sync callables, but the queue client is async — `nats-py` has no
    sync surface. This used to be `asyncio.run`, on the argument that an activity is a short isolated
    unit with nothing to keep warm between invocations. That argument was measured false next door:
    medallion's activities pool an HTTP client across invocations, and a fresh-loop-per-activity
    bridge left every one of them holding a connection bound to a closed loop.

    It is stated here rather than left implicit because the two services shared the bridge's shape and
    would have re-acquired the bug independently — see `service_kit.activity_loop`.
    """
    return run_activity(coro)


WORKFLOWS = (ingest_run, chunk_run)


def terminate_chunks(ctx: WorkflowActivityContext, payload: TerminateChunksInput) -> dict[str, Any]:
    """Stop the fan-out's children before the run reclaims their queue. §2.4.

    **THE DEFECT THIS CLOSES.** Two paths abandon a live fan-out — the run deadline, and the error
    boundary — and both then call `emit_terminal`, which releases the run's JetStream subject and
    DELETES the per-run durable (`runtime.release_run_units` → `queue.release_run`). That durable is
    exactly what every live `drain_chunk` is pulling from. So the run walked away from its children
    and then pulled the queue out from under them.

    It got WORSE, not better, when the §2.3 error boundary landed: `when_all` completes on the FIRST
    failed child, so the boundary is now reached while every SIBLING is still draining. Before, only a
    run that outlived `RASK_INGEST_MAX_RUN_HOURS` could reach that state; now one permanently-bad
    object does.

    **WHAT THIS CAN AND CANNOT DO, stated because the SDK is explicit and the difference matters.**
    `terminate_workflow` stops a child from scheduling ANY FURTHER work, and it is recursive by
    default. It does NOT stop an activity already executing — the SDK says so outright: "terminating a
    workflow has no effect on any in-flight activity function executions … there is no way to
    terminate an in-flight activity execution." So a `drain_chunk` mid-fetch keeps going until it
    returns, and the release may still land under it.

    That residual is BOUNDED rather than eliminated, and bounding it is the gain: no NEW drain can
    start, so the window is one activity's runtime instead of the rest of the run. Whether a drain
    that finishes against a deleted consumer fails loudly or silently is an OPEN QUESTION, which
    needs a live cluster to settle.

    Best-effort by construction, like `release_run_units` and the lineage emit: this runs while a run
    is already terminating, and a tidy-up that fails must not turn a run that recorded its outcome
    into one that died. Terminating an already-terminal child is the NORMAL race, not an error.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real run calls it (measured live on a
    # 600-object backfill: `'dict' object has no attribute 'run_id'`, run FAILED, nothing ingested).
    payload = TerminateChunksInput.model_validate(payload)
    child_ids = payload.child_ids
    if not child_ids:
        return {"terminated": 0, "requested": 0}

    # Local imports, matching every other activity here: workflow scope must stay free of anything
    # that reaches a sidecar at import time.
    import contextlib
    import logging

    import dapr.ext.workflow as wf_client

    log = logging.getLogger(__name__)
    terminated = 0
    # THE CONSTRUCTOR IS INSIDE THE GUARD, and it was not. It sat outside the per-child `try`, so if it
    # ever raised, this activity raised — and BOTH dispatch sites sit on the only path to the terminal
    # record: the deadline branch and the error boundary. ACTIVITY_RETRY would burn four attempts on a
    # deterministic failure and then propagate out of the `except` block with no handler above it,
    # skipping `emit_terminal`. A tidy-up that fails would turn a run that recorded its outcome into
    # one that vanished — the exact opposite of what this activity's own docstring promises.
    #
    # So it is best-effort IN FACT now, not only in prose: an unusable client reports zero terminated
    # and names why, and the run still reaches its terminal record.
    try:
        client = wf_client.DaprWorkflowClient()
    except Exception as exc:
        log.warning("could not reach the workflow engine to terminate %d chunk workflows: %s", len(child_ids), exc)
        return {"terminated": 0, "requested": len(child_ids), "error": str(exc)}

    # CLOSED deterministically. `DaprWorkflowClient` owns a TaskHubGrpcClient channel and exposes
    # `close()`; without this it was left to refcounting, and this activity runs once per abandoned
    # fan-out on a worker thread Dapr may not reuse.
    try:
        for child_id in child_ids:
            try:
                client.terminate_workflow(child_id)
                terminated += 1
            except Exception:
                log.debug("could not terminate chunk workflow %s — it has most likely already finished", child_id, exc_info=True)
    finally:
        # Suppressed, not guarded by `hasattr`: an SDK that stops offering `close()` should not make
        # this activity — which runs while a run is terminating — start failing.
        with contextlib.suppress(Exception):
            client.close()
    log.info("terminated %d of %d chunk workflows before releasing the run's queue", terminated, len(child_ids))
    return {"terminated": terminated, "requested": len(child_ids)}


ACTIVITIES = (
    emit_start,
    resolve_limits,
    ensure_dataset,
    enumerate_chunks,
    publish_units,
    drain_chunk,
    reconcile_chunk,
    finalize,
    emit_terminal,
    terminate_chunks,
)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered.

    THE `<verb>_activity` SUFFIX IS DELIBERATELY NOT USED (DWF-ACT-008, owner ruling 2026-08-25).
    `register_activity` takes no explicit name, so the runtime registers by `__name__` and these
    function names ARE the wire names. Renaming them would therefore break replay for every in-flight
    instance — the estate has no versioning seam — and the convention buys nothing here: the registry
    is single-sourced through this function, and nothing cross-language calls these activities.

    Recorded rather than left silent, because a sweep that finds no reasoning re-raises the finding.
    Pinned by `tests/unit/test_activity_naming_is_a_recorded_deviation.py`.
    """
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)
