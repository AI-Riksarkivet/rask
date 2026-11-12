"""The properties no catalog door honours, refused at one choke point: a non-Lance format, Lance's commit-path auto-cleanup,
and Lance's compaction config.

STANDING RULING (owner, 2026-08-15): rask stores Lance tables and no other format, ever. That makes
this a PRODUCT invariant rather than an implementation detail of any one door, which is why it lives
in `core/` and not beside the door it was first written for.

It began as a module-private helper inside `api/v1/endpoints/data.py` with exactly one caller, the
create door. `declare_table`, `register_table`, `create_namespace` and `update_table` all take the
same `properties` map through their spec request models and none of them checked it — so a client
could select a non-Lance format through four of the five doors that accept one. Moving it here is
what lets every door call the same rule instead of re-deriving it or forgetting to.

The same doors refuse ``lance.auto_cleanup.*`` ([[LH-245]]). Those keys arm Lance to delete versions inside
whichever commit next lands, past every legal hold and protected base (:mod:`lakehouse.auto_cleanup`).
No property a door accepts reaches the manifest config those keys live in (create stamps properties into the
schema metadata, which arms nothing: measured on pylance 12.0.0), so honouring one is impossible and echoing
it back would let a client believe it had armed the table.

They refuse ``lance.compaction.*`` for the same reason ([[LH-538]]). Lance plans a compaction from those manifest config
keys whenever the plan names no option (`lance_docs/lance/format/table/table_config.md`; measured on pylance 13.0.0: a
plan of 25 fragments is empty under ``lance.compaction.max_source_fragments=2``), so a writer holding them would steer
or silence the plan maintenance makes from the table's maintenance policy. ``version/create`` refuses a commit carrying
one (``catalog.services.managed_versions``), the register door a dataset carrying one (``table_bases``), the restore door
a version carrying one (``dataplane``), and this guard refuses one a door would only have echoed.
"""

from __future__ import annotations

from collections.abc import Iterable

from lance_namespace import InvalidInputError, UnsupportedOperationError

from catalog.core.provenance_guard import metadata_text
from lakehouse import auto_cleanup


#: Every manifest config key Lance reads to plan a compaction (`lance_docs/lance/format/table/table_config.md`). A PREFIX:
#: the doc says Lance ignores an unrecognized ``lance.compaction.*`` key with a warning, so a key a later Lance honours is
#: refused before it is documented.
COMPACTION_CONFIG_PREFIX = "lance.compaction."

#: Keys that turn on a Lance feature this catalog does not serve, which no later commit turns off: schema metadata
#: ``lance.blob.managed = "true"`` activates Managed Blobs (flag 2^14, ``lance_docs/lance/format/table/versioning.md`` §
#: Current Feature Flags), and config ``lance.row_lineage.spill`` lets a writer spill row lineage, stamping flag 2^11
#: (``lance_docs/lance/format/table/row_id_lineage.md``). pylance 13.0.0 acts on neither, so a table holding one is locked
#: by the first newer writer's commit, whose flag every flag gate refuses (``lakehouse.features``).
FLAG_LOCKING_KEYS = frozenset({"lance.blob.managed", "lance.row_lineage.spill"})


def flag_locking_keys(keys: Iterable[str | bytes]) -> list[str]:
    """The keys among ``keys`` (config, or schema metadata as Arrow keeps it, in bytes) that would lock the table to a flag
    (:data:`FLAG_LOCKING_KEYS`)."""
    return sorted(text for text in map(metadata_text, keys) if text in FLAG_LOCKING_KEYS)


def refuse_flag_locking_keys(keys: Iterable[str | bytes], *, door: str) -> None:
    """Refuse ``keys`` that would lock the table to a feature flag no later commit clears.

    Raises:
        InvalidInputError: One of ``keys`` is in :data:`FLAG_LOCKING_KEYS`.
    """
    if locking := flag_locking_keys(keys):
        raise InvalidInputError(
            f"{door}: {locking} turn on a Lance feature this catalog does not serve, which no later commit turns off: a newer "
            "Lance stamps its feature flag, and every reader and maintainer here refuses the table from then on"
        )


def refuse_properties_kept_nowhere(properties: object, *, door: str) -> None:
    """Refuse table ``properties`` a door would keep nowhere.

    The spec keeps a table's properties only where the implementation supports them (``lance_docs/ns_catalog/spec.yaml``
    CreateTableRequest and RegisterTableRequest), and this catalog keeps a create's on the table's schema metadata, in the
    data's commit ([[LH-503]]): a declaration and a registration commit nothing to carry them, and Lance's ``Update``
    carries no schema metadata.

    Raises:
        UnsupportedOperationError: ``properties`` names a key.
    """
    if isinstance(properties, dict) and properties:
        raise UnsupportedOperationError(
            f"{door}: table properties {sorted(properties)} cannot be kept: this catalog keeps a create's on the table's schema "
            "metadata and this door commits nothing to carry them; set them with schema_metadata/update"
        )


#: Why no door serves a MemWAL (``lance_docs/lance/format/table/mem_wal.md``): ``version/create`` refuses a commit initialising
#: one and the register door a dataset carrying one, each after its own subject.
MEM_WAL_NOT_SERVED = (
    "a MemWAL (the __lance_mem_wal system index) is not served here: its shards write their logs and flushed tables under "
    "_mem_wal/, which no credential this catalog vends may write, and nothing here would reclaim them"
)


def compaction_config_keys(keys: Iterable[str]) -> list[str]:
    """The ``lance.compaction.*`` keys among ``keys``, sorted."""
    return sorted(key for key in keys if key.startswith(COMPACTION_CONFIG_PREFIX))


#: Format-selecting properties an Iceberg / Unity-Catalog client might send, expecting to choose a file
#: format. This catalog stores Lance ONLY (columnar, self-describing, versioned), so honouring them is
#: impossible — echoing them back would let the client believe it got a format it did not.
FORMAT_KEYS = ("write.format.default", "data_source_format")


def reject_unsupported_properties(properties: object) -> None:
    """Raise 400 if ``properties`` request a non-Lance file format, commit-path auto-cleanup or Lance compaction config —
    never a silent no-op.

    Tolerant of a non-mapping (``None``, an unparsed string) on purpose: the doors differ in whether
    they hand over a parsed dict or a raw body field, and a guard that raised on shape would turn a
    format check into a schema check.
    """
    if not isinstance(properties, dict):
        return
    for key in FORMAT_KEYS:
        requested = properties.get(key)
        if requested is not None and str(requested).lower() != "lance":
            raise InvalidInputError(
                f"file format {requested!r} ({key}) is not supported — this catalog stores Lance only; format-selecting properties are not silently ignored"
            )
    if armed := auto_cleanup.armed_keys(properties):
        raise InvalidInputError(
            f"properties {armed} are not accepted: Lance's commit-path auto-cleanup deletes versions past every legal hold, "
            "so version reclamation belongs to the maintenance sweep and its retention policy alone"
        )
    if steering := compaction_config_keys(properties):
        raise InvalidInputError(
            f"properties {steering} are not accepted: a table's compaction is planned from its maintenance policy alone "
            "(POST /management/v1/table/{id}/policy/set), never from Lance's lance.compaction.* config"
        )
    refuse_flag_locking_keys(properties, door="properties")
