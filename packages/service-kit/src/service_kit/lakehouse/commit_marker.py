"""The commit marker: the run a Lance commit belongs to, stamped in that commit's transaction properties (CP-029 D-5).

The destination's Lance history is the durable record of what a run wrote. A job that commits stamps
`rask.action_id` (the run's content-addressed key, `work_order.derive_idempotency_key`) and `rask.run_id` on its
FINAL commit, and whoever later asks "did this run land?" reads `read_transaction` for every version above the one the
destination held when the run was planned. One convention with LH-225, which adds `rask.author`, `rask.operation` and
`rask.on_behalf_of` beside these two; the names live here so no writer spells them.

WHAT A FOUND MARKER MEANS. "A marker is found above the planned version" must imply "every write of the run landed",
so a writer stamps only its LAST commit and orders every commit it cannot stamp before that one. Measured on the
installed pylance 12.0.0 (2026-10-05, a real dataset on a local root): `write_dataset` in create and overwrite mode takes
`transaction_properties`; a merge takes them only through `MergeInsertBuilder.execute_uncommitted` and
`LanceDataset.commit` with a `lance.Transaction` carrying them (:func:`stamped`); `LanceDataset.delete` and
`update_schema_metadata` commit with empty properties and cannot carry one. `read_transaction(version)` returns the
`Transaction` (read_version, operation, uuid, transaction_properties) the spec describes at
`lance_docs/file_format.md:4802-4803`, with the properties exactly as written.

WHAT IT CANNOT COVER, stated so a reader does not have to derive it:

* a version maintenance's cleanup has removed takes its marker with it (`read_transaction` cannot read a version whose
  manifest is gone), so a marker read long after a run reads "absent";
* a marker is a CONVENTION, not proof: any writer of the table can put any key on a commit (LH-280's finding), so a
  reader trusts it only for a run it already holds an open record of, never to attribute a commit to someone;
* `lance_ray.write_lance` (0.5.0) exposes no transaction properties, so a distributed write is stamped by the
  commit that LANDS it (a create or a merge), never by the distributed append into a staging dataset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from service_kit.lancekit.absence import reads_as_absent


if TYPE_CHECKING:
    import lance

    from service_kit.lakehouse.objectfs import StorageOptions


#: The run's content-addressed key: the plan's action id, which is also the engine's submission id.
ACTION_ID_PROPERTY: Final = "rask.action_id"

#: The run the stage minted for this hop, the id the run's lineage events carry.
RUN_ID_PROPERTY: Final = "rask.run_id"


class CommitMarker(BaseModel):
    """Which run a commit belongs to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1)
    run_id: str = ""

    def properties(self) -> dict[str, str]:
        """The transaction properties that carry the marker; an absent run id is omitted rather than written blank."""
        props = {ACTION_ID_PROPERTY: self.action_id}
        if self.run_id:
            props[RUN_ID_PROPERTY] = self.run_id
        return props


def stamped(transaction: lance.Transaction, marker: CommitMarker) -> lance.Transaction:
    """``transaction`` with the marker added to its properties: the way a merge carries one (``execute_uncommitted``)."""
    import lance

    return lance.Transaction(
        read_version=transaction.read_version,
        operation=transaction.operation,
        uuid=transaction.uuid,
        transaction_properties={**(transaction.transaction_properties or {}), **marker.properties()},
    )


def marked_version(uri: str, storage_options: StorageOptions, *, action_id: str, above: int | None) -> int | None:
    """The newest version of the dataset at ``uri`` above ``above`` whose commit carries ``action_id``, else ``None``.

    ``above`` is the version the destination held when the run was planned, ``None`` when it did not exist then (every
    version is a candidate). A run leaves two or three commits, so the scan reads one transaction per version above the
    plan's. A dataset that does not exist holds no marker; any other failure to read it propagates, so an outage is
    retried by the caller rather than read as "the run wrote nothing".
    """
    import lance

    try:
        dataset = lance.dataset(uri, storage_options=storage_options)
    except ValueError as exc:
        # pylance 12 raises ValueError for an absent dataset AND for a store that cannot answer (an unreachable S3
        # endpoint arrives as `LanceError(IO): Generic S3 error ...`, measured 2026-10-05), so the estate's absence
        # vocabulary decides which: an outage read as "absent" would fail a run whose commit landed.
        if reads_as_absent(exc):
            return None
        raise
    versions = sorted(int(entry["version"]) for entry in dataset.versions() if above is None or int(entry["version"]) > above)
    for version in reversed(versions):
        transaction = dataset.read_transaction(version)
        if transaction is not None and (transaction.transaction_properties or {}).get(ACTION_ID_PROPERTY) == action_id:
            return version
    return None
