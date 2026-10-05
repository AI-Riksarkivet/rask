"""`WorkOrder` — WHAT must happen, in no engine's vocabulary.

docs/DECISIONS.md "The compute plane is decoupled", step 1 of the owner-ordered §7.4. It lifts the dict the
medallion's Ray submissions were built from: that dict IS the executor contract, and only its transport and the
program's name were ever Ray-shaped. Naming it here makes the platform able to state what a conforming unit of work
is without naming the engine that runs it — which is the whole of the decoupling claim. Both of the medallion's
planned lanes submit one: a stage transform, and a training run (CP-044), which has no source table.

It lives in `service-kit` beside `transform_specs`, `gate_specs` and `maintenance_policies`, the same
one-writer/one-reader shape. **`service-kit` must not gain a `ray` dependency**; it has none today and
this module adds none.

TWO RULES THE SHAPE ENFORCES RATHER THAN DOCUMENTS:

* **`credential_ref` NAMES, never carries.** No submission body may carry `S3_SECRET` or `S3_KEY`,
  because the Jobs API echoes `runtime_env` on an unauthenticated dashboard — and the estate spent
  three commits putting the Ray plane on a scoped credential the control plane cannot reach. A
  `WorkOrder` able to hold `storage_options` would undo that BY SIGNATURE, so `extra="forbid"` means
  there is no field for one and inventing it is refused.
* **`to_env()` is the ONE serialization.** Ray's `runtime_env.env_vars` merge-over-process-env
  semantics are the ADAPTER's knowledge and stay there. An adapter that hand-rolls this mapping is how
  two submitters come to disagree about what one order means.

FROZEN: a work order crosses a submit boundary and is read again by a poller, so a mutated copy would
make the submitter and the watcher disagree about the same run.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class WorkSource(BaseModel):
    """Where the bytes come from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    #: The catalog identifier, e.g. `acme-bronze$events` — how the platform names it, not a path.
    table_id: str
    #: ``None`` = read everything; an int = the window ``(floor, …]``, resolved by the change-feed
    #: consumer rule (`catalog.services.changes`). None is NOT 0: a floor
    #: of 0 asserts a prior version that may not exist, the same distinction `build_stage_trigger`
    #: enforces on the wire.
    version_floor: int | None = None


class WorkDestination(BaseModel):
    """Where the bytes land, and how they are reconciled with what is already there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    table_id: str
    merge_key: str = "id"
    #: `merge_insert` by default because delivery is at-least-once: an append would double a redelivered
    #: delta, which is the failure `ray_stage_job` already records.
    write_mode: Literal["merge_insert", "overwrite"] = "merge_insert"


class WorkStamp(BaseModel):
    """The provenance the output must carry — declared by the caller, never inferred by the engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The governed tier: bronze | silver | gold; for a training run, the models namespace it registers into.
    stage: str
    #: One of `stage_stamp.CARDINALITIES`. DECLARED: an engine that inferred it could not be checked. `""` only on an
    #: order with no source (a training run maps no rows), which `WorkOrder` holds.
    cardinality: str = ""
    #: R26 consume-layer provenance JSON. `""` means DROP any inherited document rather than carry one
    #: forward — silence and inheritance are different claims.
    lineage_document: str = ""
    #: The trigger's work token, or `""`. PLATFORM fact, not an engine's: it is one of the four axes
    #: `derive_idempotency_key` hashes, and it was previously an argument that reached the submitter and
    #: never the order — so an adapter could not stamp it even though the Ray lane does ([[LH-159]]).
    token: str = ""
    #: The declaration's NAME, distinct from `WorkOrder.task` which is the registered task key. Two
    #: facts, not one: a declaration names what an operator wrote, the task names what the registry
    #: resolves it to, and the Ray lane stamps both. `""` when the lane is chart-configured rather than
    #: declared.
    transform: str = ""


class WorkIdentity(BaseModel):
    """Who this run is for and which build declared it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    project: str = ""
    #: A PERSON's subject, or "". Never a role literal and never a service name — `rask-notifications`
    #: records that both are worse than silence, because they look delivered.
    originator: str = ""
    code_version: str = ""
    # NO LINEAGE IDENTITY AND NO LINEAGE ENDPOINT ride the order. A job reports to lineage as its pod's
    # projected ServiceAccount token names it ([[LH-220]]), so a subject here would be a claim nothing
    # verifies. The endpoint belongs to the pod too: measured live 2026-09-21, the Ray head carries
    # `RASK_LINEAGE_ENDPOINT` already, and Ray merges `runtime_env.env_vars` OVER process env, so
    # sending it would give one address two owners and let a submission silently outvote a repointed
    # pod. Same ruling as `S3_ENDPOINT`.


class WorkObservability(BaseModel):
    """Trace context and OTLP config — standard names, not any engine's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    traceparent: str = ""
    tracestate: str = ""
    otlp: dict[str, str] = {}
    service_name: str = ""


class WorkOrder(BaseModel):
    """One unit of work, complete and engine-free.

    `extra="forbid"` is load-bearing rather than tidy: it is what makes "a credential cannot ride the
    submission" a property of the TYPE instead of a rule someone must remember at each adapter.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: A task key registered in `<control_root>/_tasks/`, opaque to the platform and the catalog.
    task: str
    #: ``None`` for work that reads no governed table row by row: a training run reads its pinned features from
    #: ``params`` and writes the registry named by the destination.
    source: WorkSource | None = None
    destination: WorkDestination
    stamp: WorkStamp
    identity: WorkIdentity
    observability: WorkObservability = WorkObservability()
    #: Opaque `str -> str`; the adapter applies its own namespacing on delivery.
    params: dict[str, str] = {}
    #: A NAME the executor resolves against its own credential source. NEVER a credential value.
    credential_ref: str = ""
    #: Deterministic in (stage, token, from->to, code_version), so a redelivery re-attaches rather than
    #: starting a second run. **Derive it with :func:`derive_idempotency_key`** — see that function for
    #: why a caller spelling it itself is the defect rather than the convenience.
    idempotency_key: str
    #: Where the job reports its own terminal state: the outcome door of the service that PLANNED this run, already
    #: naming the run's key (CP-029 D-3). A URL and never a secret: the job authenticates with its pod's projected
    #: token. It rides the order rather than the pod, unlike the lineage endpoint, because it is this run's address and
    #: no pod value could name it. `""` means nobody planned the run, and the job reports nothing.
    outcome_url: str = ""
    #: What a submit does when a prior run under this key already ended FAILED. True (a stage) replaces it, so a
    #: redelivery retries the work on a healthy worker; False (a training run, D2) leaves it and answers
    #: `SubmitOutcome.ALREADY_FAILED`, because training compute is expensive and a failed run stays failed until a
    #: person asks again under a new token. A submit policy for the adapter, so it is not serialized into the env.
    replace_failed_run: bool = True

    @model_validator(mode="after")
    def _a_source_declares_its_cardinality(self) -> WorkOrder:
        if self.source is not None and not self.stamp.cardinality:
            raise ValueError(f"order {self.idempotency_key!r} reads {self.source.uri!r} and declares no row cardinality")
        return self

    def to_env(self) -> dict[str, str]:
        """The ONE serialization, so no adapter hand-rolls it.

        Emits only names an executor needs and no credential value — there is none to emit, which is
        the point of `credential_ref`. Absent optionals are OMITTED rather than blanked: a consumer
        reads a missing floor as "full scan", and `""` would be a different claim.
        """
        env: dict[str, str] = {
            "RASK_TASK": self.task,
            "RASK_DEST_URI": self.destination.uri,
            "RASK_MERGE_KEY": self.destination.merge_key,
            "RASK_WRITE_MODE": self.destination.write_mode,
            "RASK_STAGE": self.stamp.stage,
            "RASK_IDEMPOTENCY_KEY": self.idempotency_key,
        }
        if self.source is not None:
            env["RASK_SOURCE_URI"] = self.source.uri
            if self.source.version_floor is not None:
                env["RASK_VERSION_FLOOR"] = str(self.source.version_floor)
        optional = (
            # THE PROVENANCE IDENTITIES ARE OMITTED WHEN UNWIRED, not blanked, and the difference is
            # not stylistic. A consumer's documented fallback is `e.get(NAME, "") or from_the_uri`, so
            # absent and empty take the same branch TODAY — but the moment a consumer asks the natural
            # question "was I wired?" by testing for the key's PRESENCE, a blank answers yes and the
            # platform has asserted an identifier it does not have. An identifier the graph and the FGA
            # objects are keyed by is the wrong field to guess at.
            ("RASK_SOURCE_TABLE", self.source.table_id if self.source is not None else ""),
            ("RASK_CARDINALITY", self.stamp.cardinality),
            # The trigger's work token, under a prefixed name and never a bare TOKEN: lance's object-store env fallback
            # reads TOKEN as the AWS session token and stamps it on every S3 request (RustFS 500s, live 2026-07-13).
            ("RASK_TOKEN", self.stamp.token),
            ("RASK_DEST_TABLE", self.destination.table_id),
            ("RASK_RUN_ID", self.identity.run_id),
            ("RASK_LINEAGE_DOCUMENT", self.stamp.lineage_document),
            ("RASK_PROJECT", self.identity.project),
            ("RASK_ORIGINATOR", self.identity.originator),
            ("RASK_CODE_VERSION", self.identity.code_version),
            ("RASK_CREDENTIAL_REF", self.credential_ref),
            ("RASK_OUTCOME_URL", self.outcome_url),
            ("TRACEPARENT", self.observability.traceparent),
            ("TRACESTATE", self.observability.tracestate),
            ("OTEL_SERVICE_NAME", self.observability.service_name),
        )
        env.update({key: value for key, value in optional if value})
        env.update({f"RASK_PARAM_{k}": v for k, v in self.params.items()})
        env.update(self.observability.otlp)
        return env


def derive_idempotency_key(*, stage: str, token: str | None, from_uri: str, to_uri: str, code_version: str) -> str:
    """The order's identity to an executor, derived in ONE place.

    [[LH-157]] TWO CALL SITES SPELLED THIS DIFFERENTLY AND ONE DROPPED AN AXIS (measured 2026-09-07). The Ray
    lane keyed on all four axes (stage, token, from->to, code version), while the in-process lane keyed on
    stage, token and from->to with no ``code_version``. `inprocess_executor` uses this key AS the re-attach
    handle, so a rebuilt stage kept its old key and re-attached to the PREVIOUS build's outcome: COMPLETE
    reported against an artifact the current code never produced, with no counter moving and nothing in
    the log. The Ray lane, keying on all four, re-ran correctly — so the two lanes disagreed about what
    "the same work" means.

    A second inline copy of the expression is how a poller ends up watching an id the submitter never
    used, and how ``code`` reaches one call and not the other. So the derivation lives on the ORDER: a
    caller that cannot spell the key cannot spell it differently. A training run derives its key here
    too, with no source and no code version (`train_plans.train_action_id`).

    HASHED RATHER THAN CONCATENATED, and the reason is the absent token rather than length. The old inline
    form rendered a missing token as the literal ``notoken`` — a spelling invented at one call site that
    the other never knew — so two lanes could disagree about an order carrying no token at all. Hashing a
    NUL-joined tuple makes absence its own value rather than a word a caller might also pass, and no
    field's content can straddle the separator.

    IT IS THE RUN'S ONE NAME (CP-029 clause a): the plan document's action id, the engine's submission id
    (`RayJobsApiExecutor` posts it verbatim) and the outcome door's key are this value, so a redelivery
    after a deploy that changed ``code_version`` names a different run and submits the new build's job.
    """
    parts = (stage, "\x00" if token is None else token, from_uri, to_uri, code_version)
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:40]
