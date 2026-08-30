"""Read/write ONE JSON document on the control root — the primitive under every catalog registry.

The warehouse registry, the namespace→warehouse bindings and the project registry are all the same
shape: one JSON object per record, at a plain (unhashed) key under the control root, no DB. The two
IO halves lived as ``warehouses._read_json`` / ``warehouses._write_json``, and the project registry
imported both across the module boundary (CAT-CORE-14) — which made "private" a lie and welded the two
registries together over a helper that is about neither of them.

They are here because they are about neither: "one JSON document on the control root" is the shared
concept, and both registries now import it under a public name. Conditional writes (mint-iff-absent,
ETag-guarded mutate) stay in ``service_kit.lakehouse.records``; these two are the unconditional pair
those build on.

All IO here is BLOCKING; callers threadpool it.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)


def write_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, str]) -> None:
    """Unconditionally write ``record`` at ``key``, creating the prefix first.

    UNCONDITIONAL, which is why the registries only reach for it on paths with no concurrency to lose
    to (seeding, fixtures): a put assembled from an earlier read silently discards whatever landed in
    between. Every real mutation goes through ``service_kit.lakehouse.records``' conditional doors.
    """
    fs, base = fs_and_base(root_uri, storage_options)
    parent = f"{base}/{key}".rsplit("/", 1)[0]
    fs.create_dir(parent, recursive=True)  # local FS needs the parent dir; an S3 prefix marker is harmless
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps(record).encode("utf-8"))


def read_json(root_uri: str, storage_options: StorageOptions, key: str) -> dict[str, str] | None:
    """The record at ``key``, or ``None`` when nothing is stored there."""
    fs, base = fs_and_base(root_uri, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{key}")
    except FileNotFoundError:
        return None
    with stream:
        return json.loads(stream.readall().decode("utf-8"))


# --------------------------------------------------------------------------- #
# The DECLARED shape of each registry record (CAT-CORE-12).
#
# All three listings used to parse into a raw dict and hand-check it with `.get()` truthiness, which
# answers "present and non-empty" and never "the right type". Every consumer treats these fields as
# strings — the bucket-claim guard compares `record["bucket"] == bucket`, the resolver feeds
# `root_uri` straight into a namespace connection — so a record whose `bucket` is a list passed the
# guard, was returned as live, and then matched no claim at all. That is the wrong direction to fail
# in on a guard whose job is refusing a second project's claim on somebody's bucket.
#
# `extra="allow"` because these records carry lifecycle fields the identity check has no business
# knowing about (`status`, `protected`, `serving`, `created_at`, …) and a model that dropped them
# would silently disarm a quarantine. Only the fields every consumer keys on are declared.
# --------------------------------------------------------------------------- #


class _Record(BaseModel):
    """Base for the registry record shapes: identity is validated, everything else rides along."""

    model_config = ConfigDict(extra="allow")


class WarehouseRecord(_Record):
    """A warehouse: one physical S3 bucket owned by one project."""

    id: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    project: str = Field(min_length=1)


class BindingRecord(_Record):
    """A top-level namespace physically bound to one warehouse."""

    top_ns: str = Field(min_length=1)
    warehouse_id: str = Field(min_length=1)


class ProjectRecord(_Record):
    """A tenant. Existence lives in the record, so the id is the whole of its identity."""

    id: str = Field(min_length=1)


def validated(record: object, model: type[_Record], *, event: str, path: str) -> dict[str, str] | None:
    """``record`` as a plain dict if it matches ``model``, else ``None`` with a warning.

    TOLERANT BY DESIGN, and the tolerance is the point: these registries feed listings whose own
    docstrings promise that one bad object never voids the whole result (a single tenant's corruption
    would otherwise be an estate-wide control-plane outage). So a refusal is a SKIP, logged under the
    caller's own event name, never an exception — the destructive callers get their fail-closed
    behaviour from being told WHICH paths were skipped, not from this raising.

    Returns the validated dump rather than the input, so a consumer can rely on the declared fields
    being the declared types.
    """
    try:
        return model.model_validate(record).model_dump()
    except ValidationError as exc:
        log.warning(event, extra={"path": path, "error": exc.errors(include_url=False)})
        return None
