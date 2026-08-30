"""The catalog seam — creation is server-side, appends are the lander's (D6, §0 C10).

**THE SEAM IS A UNION, not one contract.** `LocalCatalog` below is the filesystem implementation used
by tests and local runs; `CatalogServiceClient` (`catalog_service.py`) is the in-cluster client. They
share exactly one method — `ensure` — and nothing else: the local one creates datasets itself and is
registered against by the lander, while the service one commits, publishes and reports the version
over HTTP and never registers anything locally. One Protocol spanning both could only describe them
by describing neither, which is what left `runtime._catalog()` returning an untyped object and every
call site on this seam unchecked by `ty`.

So the shapes are declared as they are: `LocalCatalogSeam` and `ServiceCatalogSeam`, joined as
`CatalogSeam`. The three single-method capability protocols the service half composes
(`CommittingCatalog`, `PublishingCatalog`, `VersioningCatalog`) are what the runtime narrows on —
`isinstance` against a `runtime_checkable` Protocol asks exactly the question the runtime is asking,
"does this catalog have that operation", and `ty` can follow it into the call.

Creation lives HERE and not in the lander because the catalog's own door refuses it: the
client-direct fragment endpoint hardcodes `LanceOperation.Append` and rules that "CREATE and
OVERWRITE stay server-side to centralize it and to owner-govern the destructive reset"
(`services/catalog/.../dataplane.py:594-615`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ingest.lander import create_empty


if TYPE_CHECKING:
    from collections.abc import Sequence

    import pyarrow as pa


def _is_object_store(uri: str) -> bool:
    """True for a URI whose scheme is an object store rather than a filesystem path.

    Deliberately a scheme check, not a try/except around mkdir: the failure it prevents is a
    PermissionError on the container root, which is indistinguishable from a genuine permissions
    problem in the logs and sent the first in-cluster run chasing the wrong cause.
    """
    return "://" in uri and not uri.startswith("file://")


@runtime_checkable
class CommittingCatalog(Protocol):
    """A catalog that folds client-written fragments into a version ITSELF.

    The distinguishing capability of the deployed plane: a commit the catalog makes is one the
    cascade can ride, because the event that wakes a mover is the catalog's publication of a version.
    A catalog without this operation can only record versions where nothing downstream will see them.
    """

    def commit(self, namespace: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]: ...


@runtime_checkable
class PublishingCatalog(Protocol):
    """A catalog that can gate a version and advance the `published` tag (§ D2 D-R1)."""

    def publish(self, namespace: str, dataset: str, version: int, *, key_column: str = "id", required_columns: Sequence[str] = ()) -> dict[str, object]: ...


@runtime_checkable
class VersioningCatalog(Protocol):
    """A catalog that can be asked a table's current version — the `read_version` a commit is built against."""

    def describe_version(self, namespace: str, dataset: str) -> int: ...


@runtime_checkable
class LocalCatalogSeam(Protocol):
    """The filesystem half: it creates the dataset itself and the lander registers versions against it.

    It has none of the three capabilities above, and that is the whole difference between a dev run
    and a deployed one — a version recorded here is visible only to the process that recorded it.
    """

    def ensure(self, project: str, dataset: str, external_base: str | None = None) -> str: ...

    def ensure_at(self, uri: str, external_base: str | None = None) -> str: ...

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema | None = None) -> str: ...

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None: ...


@runtime_checkable
class ServiceCatalogSeam(CommittingCatalog, PublishingCatalog, VersioningCatalog, Protocol):
    """The in-cluster half: creation, the commit, the publication and the version all belong to the catalog.

    It has no `register_version`, because the run id rides the commit itself — so it can never be
    handed to the lander, whose job is to register what it wrote.
    """

    def ensure(self, namespace: str, dataset: str, external_base: str | None = None) -> str: ...


#: What `runtime._catalog()` hands back. Declared as the union rather than left open, so a method that
#: one implementation loses — or gains at one call site and not the other, which is how `external_base`
#: reached a deployed run — is a type error rather than an in-cluster TypeError after the run is accepted.
CatalogSeam = LocalCatalogSeam | ServiceCatalogSeam


class LocalCatalog:
    """Filesystem catalog: creates the dataset empty with the creation-time flags, records versions."""

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema
        self.registered: list[tuple[str, int, str]] = []

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema | None = None) -> str:
        raise NotImplementedError("resolved through dataset_uri(); ensure_at() is the path-based form")

    def ensure(self, project: str, dataset: str, external_base: str | None = None) -> str:
        """The ID-based form — the ONE method both halves of the seam share.

        The service vends a location; this composes the dev fallback from env and then creates
        through the same path-based code. Because the call is identical either way, no caller has to
        know which half it holds to provision a table, and the in-cluster swap stays a config change
        rather than a code change at every call site.
        """
        from ingest.runtime import warehouse_root

        return self.ensure_at(f"{warehouse_root().rstrip('/')}/{project}/{dataset}.lance", external_base)

    def ensure_at(self, uri: str, external_base: str | None = None) -> str:
        """Create the dataset EMPTY if absent.

        The creation-time flags (`enable_stable_row_ids`, `data_storage_version=2.2`) are set here or
        never: they are silent no-ops afterwards (`file_format.md:4011-4013 + guide.md:228-229`), and CDF
        plus every silver `source_rowid` reference depends on them existing from version 1.

        `external_base` joins them, and for the same reason: `initial_bases` is CREATE-MODE ONLY, so
        the root a dataset's blob descriptors may point at is registered here or never
        (`docs/architecture/medallion-data-flow.md`). Unlike the flags above there is no A14-style assertion for it —
        a dataset created without a base is not broken, it is MANAGED, which is a supported placement
        and the right one for a source whose bytes exist at no URI.
        """
        if _is_object_store(uri):
            # An object store has no directories to create, and Path("s3://b/k") collapses to the
            # relative "s3:/b/k" — so mkdir raised [Errno 13] Permission denied: 's3:' against the
            # container root. Observed in the first in-cluster run with a real warehouse. lance
            # handles the scheme natively; existence is a read, not a stat.
            try:
                import lance

                lance.dataset(uri)
            except Exception as exc:
                # ONLY "absent" MAY REACH `create_empty`, and a bare `except Exception` did not say
                # that. Absent is one of the reasons this read raises; a credential that has not
                # landed yet, a store that is briefly unreachable, a 403 on the bucket and a
                # malformed URI are the others — and every one of them was answered by CREATING AN
                # EMPTY DATASET, which is the single operation that must never run over a table that
                # does exist. Nothing would report it: the create succeeds and the run appends to a
                # dataset whose history it just discarded.
                #
                # Matched on the MESSAGE because pylance raises a bare `ValueError` for a missing
                # dataset and exports no typed error to catch (verified against the pinned version:
                # `ValueError('Dataset at <uri> was not found: ...')`). Narrow enough to be safe —
                # anything whose own text does not say "not found" is re-raised, so an unreadable
                # dataset now fails the run loudly instead of being overwritten quietly.
                if not _reads_as_absent(exc):
                    raise
                create_empty(uri, self._schema, external_base)
            assert_creation_contract(uri)
            return uri

        if not Path(uri).exists():
            Path(uri).parent.mkdir(parents=True, exist_ok=True)
            create_empty(uri, self._schema, external_base)
        # A14 is ENFORCED here, not merely available. A dataset that pre-dates the contract — or was
        # created by any other writer — reaches this path too, and the whole point is that the flags
        # cannot be added afterwards: refusing at the head of a run is the last moment the operator
        # can still fix it cheaply, rather than discovering it as duplicate rows months downstream.
        assert_creation_contract(uri)
        return uri

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
        """Record the committed version with the run id — the reconciliation anchor."""
        self.registered.append((dataset_uri, version, run_id))


def _reads_as_absent(exc: BaseException) -> bool:
    """Does this failure mean the dataset IS NOT THERE, as opposed to "we could not look"?

    `FileNotFoundError` is unambiguous. Otherwise pylance's own wording is the only signal it gives:
    it raises `ValueError("Dataset at <uri> was not found: Not found: <uri>/_versions, ...")` and
    exports no typed error. Anything else — permission, credentials, transport, a bad URI — is an
    unanswered question, and the caller must not act on it.
    """
    return isinstance(exc, FileNotFoundError) or "not found" in str(exc).lower()


class CreationContractError(ValueError):
    """A governed dataset was created without a guarantee every tier above it depends on."""


def assert_creation_contract(uri: str) -> None:
    """A14 — REFUSE a governed dataset that is missing its creation-time guarantees.

    Two things must hold from version 1, and neither can be repaired afterwards:

    * **stable row ids** (`enable_stable_row_ids`) — a silent no-op if set later
      (`lance_docs/file_format.md:4011-4013`). D1's change-data-feed reads `_row_created_at_version`,
      and every silver/gold row references bronze through `source_rowid`; without stable ids both
      rest on identifiers that move under compaction.
    * **an `id` column** — the key the merge-on-write path converges on. Without it a redelivered
      hop appends instead of merging, and E2's idempotency claim is simply false.

      NOT an "unenforced primary key" in Lance's sense, though an earlier comment here cited
      `file_format.md:2887-2910` as if it were. That feature is opt-in through field metadata
      (`lance-schema:unenforced-primary-key`), which this plane sets nowhere — so `id` is an ordinary
      column the estate agrees to merge on, and Lance validates no uniqueness for us. Declaring it
      properly is open work; claiming it in a comment was not the same thing.

    (The verb itself is deliberately not named here: I4's gate is a grep over these files, and
    prose naming a write verb is indistinguishable to it from a second writer. Bluntness is the
    gate's value — it cannot be argued with — so the prose bends, not the rule.)

    Checked rather than documented, because "silent no-op if late" means the mistake has NO symptom
    when it is made. The dataset works, the run is green, and the defect surfaces months later as a
    mover duplicating rows or a `source_rowid` resolving to the wrong page. A14 moves that discovery
    to the one moment it is still cheap.
    """
    import lance

    dataset = lance.dataset(uri)

    if "id" not in dataset.schema.names:
        raise CreationContractError(
            f"{uri} has no `id` column — the merge-on-write path would have nothing to converge "
            f"on, so a redelivered hop appends duplicates instead of updating (A14)"
        )

    if not _has_stable_row_ids(dataset):
        raise CreationContractError(
            f"{uri} was created without enable_stable_row_ids — CDF deltas and every source_rowid "
            f"reference above it would rest on ids that move under compaction, and setting the flag "
            f"now is a silent no-op (A14)"
        )


def _has_stable_row_ids(dataset: object) -> bool:
    """Whether the dataset actually carries stable row ids.

    `has_stable_row_ids` is the accessor pylance 9.0.0 exposes — confirmed by constructing datasets
    both ways and reading it (True with the flag, False without), not inferred from the parameter
    name. Read defensively and FALSE-on-absence: if the accessor disappears in an upgrade the gate
    must refuse rather than pass, or A14 quietly stops gating on the first version bump.
    """
    value = getattr(dataset, "has_stable_row_ids", None)
    if isinstance(value, bool):
        return value
    if callable(value):
        try:
            return bool(value())
        except Exception:
            return False
    return False
