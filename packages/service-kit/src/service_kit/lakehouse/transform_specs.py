"""Transform-spec registry — a TRANSFORM DECLARED as a governed record instead of a Deployment's env block.

A medallion transform (one bronze->silver edge: read this, run that, write there) used to exist only as
environment on a mover pod. That makes the transform invisible to governance — nothing can list them,
review them, or gate who may add one — and it makes an undeclared transform fail deep, at the Ray submit
seam, where the error names an image rather than the key nobody declared.

This is the same stateless-over-object-store shape as ``maintenance_policies`` and the warehouse
registry, chosen for the same reason: one service WRITES (the catalog, admin-gated) and a different
one READS (the medallion mover, which holds no catalog client on its submit path). Both need one
format, so the format lives here rather than as two copies that drift.

Each spec is one JSON record under ``<control_root>/_transforms/``, keyed by ``(project, name)`` —
transforms are per-tenant, so two projects may both declare ``dummy`` without collision.

**The platform validates the SHAPE and never the meaning.** ``params`` are opaque strings forwarded
to the workload; what they mean belongs to the runner. What the platform does enforce is the handful
of invariants that are its own business: a safe transform name, string params that cannot collide with the
``RASK_PARAM_`` namespace, and — the load-bearing one — an entrypoint that is a path BAKED into the
image. Ray documents ``runtime_env`` as a development convenience, and checking "is this the
production shape?" at declaration time means an undeclarable transform can never be submitted at all,
rather than every submit path having to remember.

**NEVER A SECRET.** Same rule as the env channel this replaces: these records are readable by anyone
who can read the control bucket. A workload needing a credential resolves it from the Dapr secret
store at boot, never from a param here.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import pyarrow.fs as pafs
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.stage_stamp import CARDINALITIES, ONE_TO_ONE


log = logging.getLogger(__name__)

#: Public so tests and operators can name the prefix without re-typing the literal.
SPECS_PREFIX = "_transforms"

#: The directory a transform's entrypoint MUST live under — the path `.docker/ray-cluster.dockerfile`
#: bakes its job scripts into. Anything else is either a runtime_env upload (development-only, per
#: Ray's own docs) or a path the deployed image does not contain, which fails as `exit 2` with
#: nothing naming the image.
BAKED_JOBS_DIR = "/home/ray/jobs/"

#: The job scripts `.docker/ray-cluster.dockerfile` ACTUALLY bakes — the image the chart's KubeRay
#: cluster runs.
#:
#: THE DIRECTORY IS NOT ENOUGH, which is what this closes. The validator below used to accept any
#: path under `BAKED_JOBS_DIR`, so a declaration naming `ray_lance_job.py` — a real script, baked
#: into `.docker/ray-lance.dockerfile` for the standalone demo but NOT into the cluster image —
#: passed the door and then died `exit 2 / can't open file` on the cluster, with nothing in the
#: failure naming the image. A refusal at declaration time is the last moment that is cheap.
#:
#: Held here rather than derived, because this library runs inside a container that has no
#: dockerfile to read. `tests/unit/test_ray_job_images.py` asserts the two agree, so the drift this
#: constant could introduce fails a test instead of a cluster.
BAKED_CLUSTER_JOBS = frozenset({"ray_stage_job.py", "ray_train_job.py", "ray_dummy_job.py"})

#: DNS-safe, lowercase, bounded. The name becomes an object-store key fragment and rides into
#: identifiers elsewhere; a traversing or shell-shaped name must never reach either.
TRANSFORM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: The prefix the submit path adds when forwarding params as env vars. A declared key carrying it
#: already would either double-prefix or — if a future submit path forgot to re-prefix — escape the
#: namespace that keeps a transform away from `S3_SECRET` and friends.
_RESERVED_PARAM_PREFIX = "RASK_PARAM_"


class TransformSpec(BaseModel):
    """One declared TRANSFORM. Written by the catalog's admin-gated door, read by the mover."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: The declaration's own name, unique within the project.
    #:
    #: ACCEPTS THE OLD SPELLING ON READ (§8 change 7). The model is `extra="forbid"`, so a record
    #: written before the rename — `{"lane": "dummy", ...}` — would be REFUSED at parse rather than
    #: migrated, and a refused declaration means a mover runs the chart's program while an operator
    #: believes the record governs it. That is the exact failure `UndeclaredTransformError` exists to
    #: prevent, so the alias is not politeness, it is the rename not creating the bug it was cleaning
    #: up after. The on-disk KEY is unaffected: `_key` hashes the VALUE, never the field name.
    name: str = Field(validation_alias=AliasChoices("name", "lane"), description="the transform's name, unique within the project")
    project: str = Field(min_length=1, max_length=64)
    from_id: str = Field(min_length=1, description="upstream catalog table identifier, e.g. bronze$events")
    to_id: str = Field(min_length=1, description="downstream catalog table identifier, e.g. silver$dummy")
    entrypoint: str = Field(description=f"the Ray entrypoint; must reference a script baked under {BAKED_JOBS_DIR}")
    params: dict[str, str] = Field(default_factory=dict, description="opaque workload parameters; never secrets")
    #: How many output rows this lane may emit per input row. DECLARED, never inferred: a stage
    #: driver cannot tell a deliberate fan-out from a transform that lost rows, and guessing in
    #: either direction is wrong — guessing 1:1 forbids a shape the lakehouse supports (a video into
    #: frames, a recording into speaker turns), and guessing 1:N stops catching the transform bug the
    #: count check exists for. Defaults to the shape every un-migrated lane already has.
    cardinality: str = Field(default=ONE_TO_ONE, description=f"row cardinality; one of {sorted(CARDINALITIES)}")
    code_version: str = Field(default="", max_length=128, description="the image tag this transform is declared against")

    @field_validator("cardinality")
    @classmethod
    def _known_cardinality(cls, value: str) -> str:
        """Refused at the DOOR, not at 3am on the cluster.

        The job refuses an unknown cardinality too, but by then a Ray job has been submitted and the
        operator sees a stage FAIL rather than a 422 naming the field.
        """
        if value not in CARDINALITIES:
            raise ValueError(f"invalid cardinality {value!r}: must be one of {sorted(CARDINALITIES)}")
        return value

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if not TRANSFORM_NAME_RE.match(value):
            raise ValueError(f"invalid transform name {value!r}: must match {TRANSFORM_NAME_RE.pattern}")
        return value

    @field_validator("entrypoint")
    @classmethod
    def _baked_entrypoint(cls, value: str) -> str:
        # Substring rather than prefix: the entrypoint is a command line ("python /home/ray/jobs/x.py"),
        # so the path sits after the interpreter.
        if BAKED_JOBS_DIR not in value:
            raise ValueError(
                f"entrypoint {value!r} does not reference a baked job under {BAKED_JOBS_DIR!r}; "
                "Ray documents runtime_env as development-only, so a transform must name a script the deployed image contains"
            )
        # AND IT MUST BE ONE THE CLUSTER IMAGE ACTUALLY CARRIES. The directory check alone accepted
        # `ray_lance_job.py` — a real script, baked into the standalone demo image but not into the
        # one the chart runs — and the run then died `exit 2 / can't open file` on the cluster with
        # nothing naming the image. Refusing here names the script AND what is available.
        named = next((token for token in value.split() if BAKED_JOBS_DIR in token), "")
        script = named.rsplit("/", 1)[-1]
        if script not in BAKED_CLUSTER_JOBS:
            raise ValueError(
                f"entrypoint {value!r} names {script!r}, which the cluster image does not bake; "
                f"available: {sorted(BAKED_CLUSTER_JOBS)}. Add it to .docker/ray-cluster.dockerfile "
                "or name one of these — an unbaked script fails as `exit 2` on the cluster, naming nothing."
            )
        return value

    @field_validator("params")
    @classmethod
    def _namespaced_params(cls, value: dict[str, str]) -> dict[str, str]:
        offending = sorted(key for key in value if key.startswith(_RESERVED_PARAM_PREFIX))
        if offending:
            raise ValueError(f"param keys {offending} carry the reserved {_RESERVED_PARAM_PREFIX!r} prefix, which the submit path adds")
        return value


def _key(project: str, name: str) -> str:
    """A collision-free record key. Both halves are already shape-checked, but the ids are
    user-supplied, so hash rather than concatenate into a path."""
    digest = hashlib.sha256(f"{project}:{name}".encode()).hexdigest()[:24]
    return f"{SPECS_PREFIX}/{project}-{digest}.json"


def put_spec(control_root: str, storage_options: StorageOptions, spec: TransformSpec) -> None:
    """Persist one transform declaration (overwrite — declaring is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _key(spec.project, spec.name)
    fs.create_dir(f"{base}/{key}".rsplit("/", 1)[0], recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(spec.model_dump_json().encode("utf-8"))


def get_spec(control_root: str, storage_options: StorageOptions, project: str, name: str) -> TransformSpec | None:
    """The transform's declaration, or ``None`` when it was never declared.

    ``None`` rather than a default is the whole contract: the caller must be able to tell "nobody
    declared this" from "this is configured", so an unknown transform can be refused at the door instead
    of running something for a name that was probably a typo.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{_key(project, name)}")
    except FileNotFoundError:
        return None
    with stream:
        raw = stream.readall().decode("utf-8")
    return _parse(raw, path=_key(project, name))


def delete_spec(control_root: str, storage_options: StorageOptions, project: str, name: str) -> bool:
    """Remove one declaration; ``False`` when there was none (delete is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(project, name)}")
    except FileNotFoundError:
        return False
    return True


def list_specs(control_root: str, storage_options: StorageOptions, project: str | None = None) -> list[TransformSpec]:
    """Every readable declaration, optionally scoped to one project (unordered).

    One corrupt or unreadable record is SKIPPED with a warning rather than voiding the rest: a
    listing that silently emptied would read as "this project declares no transforms" while they
    keep running.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[TransformSpec] = []
    selector = pafs.FileSelector(f"{base}/{SPECS_PREFIX}", allow_not_found=True, recursive=False)
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                raw = stream.readall().decode("utf-8")
        except Exception as exc:
            log.warning("transform_spec_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        spec = _parse(raw, path=info.path)
        if spec is not None and (project is None or spec.project == project):
            out.append(spec)
    return out


def _parse(raw: str, *, path: str) -> TransformSpec | None:
    """Validate a stored record, warning (never raising) on one that no longer fits the model.

    A record written by an older build whose shape has since tightened must not take down the
    listing — it is reported and skipped, exactly like an unreadable one.
    """
    try:
        payload: Any = json.loads(raw)
        return TransformSpec.model_validate(payload)
    except Exception as exc:
        log.warning("transform_spec_malformed", extra={"path": path, "error": str(exc)})
        return None
