"""Quality-gate registry — a gate DECLARED as a governed record instead of a Deployment's env block.

The gate decides whether a stage's output may publish: which column identifies a row
(``key_column``), which columns a consumer depends on (``required_columns``), and how far a row
count may move before a promotion needs a human (``review_band``). Every one of those lived ONLY as
environment on a mover pod, so changing a threshold meant editing a values file and running
``helm upgrade`` — and nothing could enumerate the gates, review one, or gate who changed it. That
is the same defect ``transform_specs`` was written to end for lanes.

Same stateless-over-object-store shape as ``transform_specs`` and the warehouse registry, chosen for
the same reason: one service WRITES (the catalog, admin-gated, audited) and a different one READS
(the medallion, on a path holding no catalog client). Both need one format, so the format lives here
rather than as two copies that drift.

Each record is one JSON document under ``<control_root>/_gates/``, keyed by ``project``.

**KEYED BY PROJECT, NOT BY LANE.** ``promotion_review_band`` and ``quality_key_column`` are
tenant-level thresholds in the chart today; making them per-lane here would invent a granularity the
estate does not have and force every lane to answer a question nobody is asking. A per-lane override
is an added key later, not a redesign.

**UNSET IS NOT ZERO**, and this is the load-bearing invariant. ``get_spec`` answers ``None`` when
nobody declared, exactly like the lane registry: the medallion must be able to tell "not configured"
(keep the chart's settings, byte-for-byte) from "configured to 0.0" (a band of zero, which every
non-empty delta breaches). Collapsing the two would put an estate that never opted in under a new
scheme without anyone asking for it.
"""

from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, Field, field_validator

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

#: Where the records live, relative to the control root.
GATES_PREFIX = "_gates"


class GateSpec(BaseModel):
    """One project's quality-gate settings."""

    model_config = ConfigDict(extra="forbid")

    project: str
    #: The column that identifies a row. The gate's not-null and uniqueness assertions run against
    #: it, so an absent column is a gate that passes everything.
    key_column: str = "id"
    #: Columns a downstream consumer declares a dependency on. Each becomes a `column_declared`
    #: assertion, so a transform that silently stops emitting one is caught at the gate rather than
    #: by the consumer.
    required_columns: list[str] = Field(default_factory=list)
    #: How far a stage's row count may move from its predecessor before the promotion is HELD for a
    #: human. A ratio, not a row count — 0.25 means "a quarter". `ge=0` because a band is a
    #: magnitude; a negative one would make every delta a breach and read as the gate being broken.
    review_band: float = Field(default=0.25, ge=0)
    #: Whether a breach parks for a human at all. With this off a breach is only logged, which is
    #: the pre-review behaviour and still a legitimate choice for a tenant with nobody to ask.
    review_enabled: bool = False

    @field_validator("project", "key_column")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        """Reject a value that could escape its record path or its SQL predicate.

        Checked here rather than at the door because both the catalog (writer) and the medallion
        (reader) depend on it, and a guard on one side only is a guard the other side does not have.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        if not all(ch.isalnum() or ch in "-_" for ch in cleaned):
            raise ValueError(f"{cleaned!r} may contain only letters, digits, '-' and '_'")
        return cleaned

    @field_validator("required_columns")
    @classmethod
    def _safe_columns(cls, value: list[str]) -> list[str]:
        for column in value:
            if not column.strip() or not all(ch.isalnum() or ch in "-_" for ch in column.strip()):
                raise ValueError(f"column {column!r} may contain only letters, digits, '-' and '_'")
        return [column.strip() for column in value]


def _key(project: str) -> str:
    """A collision-free record key. The project id is user-supplied, so hash rather than concatenate
    it into a path — the same reason `transform_specs._key` does."""
    digest = hashlib.sha256(project.encode()).hexdigest()[:24]
    return f"{GATES_PREFIX}/{project}-{digest}.json"


def put_spec(control_root: str, storage_options: StorageOptions, spec: GateSpec) -> None:
    """Persist one project's gate settings (overwrite — declaring is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _key(spec.project)
    fs.create_dir(f"{base}/{key}".rsplit("/", 1)[0], recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(spec.model_dump_json().encode("utf-8"))


def get_spec(control_root: str, storage_options: StorageOptions, project: str) -> GateSpec | None:
    """This project's gate settings, or ``None`` when nobody declared them.

    ``None`` rather than a populated default is the contract: the caller must be able to tell
    "unset" from "set to these values", because unset means the chart's settings still govern.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{_key(project)}")
    except FileNotFoundError:
        return None
    with stream:
        raw = stream.readall().decode("utf-8")
    return _parse(raw, path=_key(project))


def delete_spec(control_root: str, storage_options: StorageOptions, project: str) -> bool:
    """Remove one declaration; ``False`` when there was none (delete is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(project)}")
    except FileNotFoundError:
        return False
    return True


def _parse(raw: str, *, path: str) -> GateSpec | None:
    """A record this build cannot read is skipped, LOUDLY, never raised.

    A single unreadable document must not take down the gate for a whole estate — the medallion
    falls back to the chart's settings, which is the honest behaviour for "the declaration could not
    be read". Logged at ERROR because it is a real defect, just not one worth failing a run over.
    """
    try:
        return GateSpec.model_validate(json.loads(raw))
    except (ValueError, TypeError):
        log.exception("gate_spec_unreadable", extra={"path": path})
        return None
