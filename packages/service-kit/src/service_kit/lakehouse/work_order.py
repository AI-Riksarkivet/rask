"""`WorkOrder` — WHAT must happen, in no engine's vocabulary.

docs/DECISIONS.md "The compute plane is decoupled" (§2.3), step 1 of the owner-ordered §7.4. It lifts the dict
`medallion/services/ray_submit.py` already builds: that dict IS the executor contract, and only its
transport and the program's name were ever Ray-shaped. Naming it here makes the platform able to state
what a conforming unit of work is without naming the engine that runs it — which is the whole of the
decoupling claim.

It lives in `service-kit` beside `transform_specs`, `gate_specs` and `maintenance_policies`, the same
one-writer/one-reader shape. **`service-kit` must not gain a `ray` dependency**; it has none today and
this module adds none.

TWO RULES THE SHAPE ENFORCES RATHER THAN DOCUMENTS:

* **`credential_ref` NAMES, never carries.** `ray_submit.py` already refuses to put `S3_SECRET` or
  `S3_KEY` in a submission body, because the Jobs API echoes `runtime_env` on an unauthenticated
  dashboard — and the estate spent three commits putting the Ray plane on a scoped credential the
  control plane cannot reach. A `WorkOrder` able to hold `storage_options` would undo that BY
  SIGNATURE, so `extra="forbid"` means there is no field for one and inventing it is refused.
* **`to_env()` is the ONE serialization.** Ray's `runtime_env.env_vars` merge-over-process-env
  semantics are the ADAPTER's knowledge and stay there. An adapter that hand-rolls this mapping is how
  two submitters come to disagree about what one order means.

FROZEN: a work order crosses a submit boundary and is read again by a poller, so a mutated copy would
make the submitter and the watcher disagree about the same run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class WorkSource(BaseModel):
    """Where the bytes come from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    #: The catalog identifier, e.g. `acme-bronze$events` — how the platform names it, not a path.
    table_id: str
    #: ``None`` = read everything; an int = ``_row_created_at_version > floor``. None is NOT 0: a floor
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

    #: The governed tier: bronze | silver | gold.
    stage: str
    #: One of `stage_stamp.CARDINALITIES`. DECLARED: an engine that inferred it could not be checked.
    cardinality: str
    #: R26 consume-layer provenance JSON. `""` means DROP any inherited document rather than carry one
    #: forward — silence and inheritance are different claims.
    lineage_document: str = ""


class WorkIdentity(BaseModel):
    """Who this run is for, and which build declared it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    project: str = ""
    #: A PERSON's subject, or "". Never a role literal and never a service name — `rask-notifications`
    #: records that both are worse than silence, because they look delivered.
    originator: str = ""
    code_version: str = ""


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
    source: WorkSource
    destination: WorkDestination
    stamp: WorkStamp
    identity: WorkIdentity
    observability: WorkObservability = WorkObservability()
    #: Opaque `str -> str`; the adapter applies its own namespacing on delivery.
    params: dict[str, str] = {}
    #: A NAME the executor resolves against its own credential source. NEVER a credential value.
    credential_ref: str = ""
    #: Deterministic in (stage, token, from->to, code_version), so a redelivery re-attaches rather than
    #: starting a second run.
    idempotency_key: str

    def to_env(self) -> dict[str, str]:
        """The ONE serialization, so no adapter hand-rolls it.

        Emits only names an executor needs and no credential value — there is none to emit, which is
        the point of `credential_ref`. Absent optionals are OMITTED rather than blanked: a consumer
        reads a missing floor as "full scan", and `""` would be a different claim.
        """
        env: dict[str, str] = {
            "RASK_TASK": self.task,
            "RASK_SOURCE_URI": self.source.uri,
            "RASK_SOURCE_TABLE": self.source.table_id,
            "RASK_DEST_URI": self.destination.uri,
            "RASK_DEST_TABLE": self.destination.table_id,
            "RASK_MERGE_KEY": self.destination.merge_key,
            "RASK_WRITE_MODE": self.destination.write_mode,
            "RASK_STAGE": self.stamp.stage,
            "RASK_CARDINALITY": self.stamp.cardinality,
            "RASK_RUN_ID": self.identity.run_id,
            "RASK_IDEMPOTENCY_KEY": self.idempotency_key,
        }
        if self.source.version_floor is not None:
            env["RASK_VERSION_FLOOR"] = str(self.source.version_floor)
        optional = (
            ("RASK_LINEAGE_DOCUMENT", self.stamp.lineage_document),
            ("RASK_PROJECT", self.identity.project),
            ("RASK_ORIGINATOR", self.identity.originator),
            ("RASK_CODE_VERSION", self.identity.code_version),
            ("RASK_CREDENTIAL_REF", self.credential_ref),
            ("TRACEPARENT", self.observability.traceparent),
            ("TRACESTATE", self.observability.tracestate),
            ("OTEL_SERVICE_NAME", self.observability.service_name),
        )
        env.update({key: value for key, value in optional if value})
        env.update({f"RASK_PARAM_{k}": v for k, v in self.params.items()})
        env.update(self.observability.otlp)
        return env
