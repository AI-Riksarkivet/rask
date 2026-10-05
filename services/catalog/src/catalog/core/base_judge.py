"""Whether the bases a table's manifest declares are ones the catalog sanctioned — asked by every door that reads or vends it ([[LH-279]]).

A MANIFEST'S BASE LIST IS A WRITER'S CLAIM. Any holder of the table's write vend can land an
``UpdateBases`` commit, and a base pylance resolves a fragment through is a base the reader reads through:
measured on pylance 12.0.0 (lh279 m2), a table that planted another table's root answered a query with
the other table's rows, and a vend for it would have granted READ on that root. So the doors that hand
out a table's rows or a credential for them ask :func:`service_kit.lakehouse.base_registry.judge_bases`
first — its own root, a configured external blob base, or the catalog's record — and refuse the rest with
``InvalidTableStateError`` (code 19, 409): the caller holds the rung, the TABLE is in a state no read
should serve. A 403 would send an operator to FGA tuples that are not what is wrong.

THE JUDGE IS DEPLOYMENT CONFIGURATION, installed once by the lifespan: the control root the records live
on and ``LANCE_EXTERNAL_BLOB_BASES``, the same two values the create and register doors write the record
against. A door that holds ``Settings`` builds its own through :meth:`BaseJudge.from_settings` — the same
constructor — and the dataset seam in :mod:`catalog.core.namespace`, which takes no settings, reads the
installed one. A dataset declaring only bases inside its own root is answered without either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError
from lance_namespace import InvalidTableStateError, ServiceUnavailableError
from pydantic import BaseModel

from service_kit.lakehouse import base_refs, base_registry
from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.features import BasePathRef
from service_kit.lakehouse.objectfs import StorageOptions


if TYPE_CHECKING:
    from catalog.core.config import Settings


log = logging.getLogger(__name__)

#: How many refused bases a refusal names. A plant is one base; the bound keeps a manifest carrying
#: thousands from turning one error body into a listing of them.
_NAMED_BASES = 10


class GovernedStorage(BaseModel):
    """Storage the catalog governs, which no external blob base may reach ([[LH-209]]).

    Lance resolves an external pointer relative to its base (``blob_id`` > 0), so a base covering governed
    bytes lets every pointer through it name them. ``roots`` are the catalog, control, model-registry and
    model-artifact roots; ``governed_bucket`` answers whether a bucket is reserved or claimed by a warehouse.
    """

    roots: list[str]
    governed_bucket: Callable[[str], bool]

    def covers(self, base: str) -> bool:
        """Whether ``base`` overlaps a governed root (either way) or sits in a governed bucket."""
        if any(base_refs.location_in_store(root, base) or base_refs.location_in_store(base, root) for root in self.roots if root):
            return True
        return base_refs.store_of(base) == "s3" and self.governed_bucket(base.partition("://")[2].split("/", 1)[0])

    @classmethod
    def from_settings(cls, settings: Settings) -> GovernedStorage:
        return cls(
            roots=[settings.root, settings.registry_root, settings.models_root, settings.model_artifacts_root],
            governed_bucket=partial(_governed_bucket, settings),
        )


def _governed_bucket(settings: Settings, bucket: str) -> bool:
    # The claim read lives with the warehouse records in `catalog.services`, which imports this module.
    from catalog.services import warehouses

    return bucket in settings.reserved_bucket_set or warehouses.bucket_claim(settings.registry_root, settings.storage_options(), bucket=bucket) is not None


class BaseJudge(BaseModel):
    """What a declared base is judged against: the catalog's record, and the configured external blob bases."""

    registry: base_registry.BaseRegistry
    #: ``LANCE_EXTERNAL_BLOB_BASES`` — sanctioned by configuration as plain (pointer) bases.
    configured: list[str]
    #: Where a configured base earns no standing ([[LH-209]]): a plain base inside a configured entry that
    #: covers governed storage is sanctioned only by the table's record. ``None`` judges configuration alone.
    governed: GovernedStorage | None = None

    def read_record(self, location: str) -> base_registry.BaseRecord | None:
        """The base record of the table rooted at ``location``; ``None`` when it has none.

        Raises:
            ServiceUnavailableError: The record exists and cannot be read, or the store holding it did not
                answer (a throttle, a denied or failed GET) — never answered as "no record", which would demote
                a recorded clone source and let its bytes be rewritten. Retryable: nothing was judged.
        """
        try:
            return base_registry.read_base_record(self.registry, location)
        except base_registry.UnreadableBaseRecordError as exc:
            log.error("table_base_record_unreadable", extra={"location": location, "error": str(exc)[:300]})
            raise ServiceUnavailableError("a table's base record could not be read, so its declared bases were not judged") from exc
        except (ClientError, BotoCoreError, OSError) as exc:
            log.error("table_base_record_store_unavailable", extra={"location": location, "error": f"{type(exc).__name__}: {exc}"[:300]})
            raise ServiceUnavailableError("the store holding a table's base record did not answer, so its declared bases were not judged") from exc

    def sibling_base_refs(self, location: str, storage_options: StorageOptions) -> BaseRefs:
        """:func:`service_kit.lakehouse.base_refs.sibling_base_refs`, judged against this catalog's record and configured bases.

        The on-demand compaction, reclamation, version-delete and erasure doors ask this, so a planted base
        freezes no table at those doors either ([[LH-279]]).
        """
        return base_refs.sibling_base_refs(location, storage_options, configured=self.configured, record_of=self.read_record)

    @classmethod
    def from_settings(cls, settings: Settings) -> BaseJudge:
        """The judge a catalog configured by ``settings`` answers with — the lifespan's and every door's."""
        return cls(
            registry=base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options()),
            configured=settings.external_blob_base_list,
            governed=GovernedStorage.from_settings(settings),
        )

    def judge(self, location: str, refs: Sequence[BasePathRef]) -> list[base_registry.BaseJudgement]:
        """:func:`service_kit.lakehouse.base_registry.judge_bases`, with a configured base that covers governed storage judged by the record alone.

        The chart's default entry is the model-artifact tree, so CONFIGURED standing there would let a holder
        of a table's write credential ``add_bases`` another model's tree and have the blob door serve its
        weights. Only the read and register doors that authorize ask this; the maintenance and lineage
        protection passes keep configuration's standing, which only ever protects bytes from reclaim.
        """
        judged = base_registry.judge_bases(location, refs, configured=self.configured, load_record=partial(self.read_record, location))
        governed = self.governed
        if governed is None or not any(j.standing is base_registry.BaseStanding.CONFIGURED and governed.covers(j.ref.path) for j in judged):
            return judged
        record = self.read_record(location)
        return [
            base_registry.judge_base(location, j.ref, configured=(), record=record)
            if j.standing is base_registry.BaseStanding.CONFIGURED and governed.covers(j.ref.path)
            else j
            for j in judged
        ]


class _Installed(BaseModel):
    judge: BaseJudge | None = None


#: The judge this process answers with. Written by the lifespan, cleared when it ends, so a later app
#: in the same process — a test, a reload — never inherits the previous one's control root.
_INSTALLED = _Installed()


def install(judge: BaseJudge | None) -> None:
    """Make ``judge`` the one :func:`installed_judge` returns; ``None`` uninstalls it."""
    _INSTALLED.judge = judge


def installed_judge() -> BaseJudge:
    """The judge the lifespan installed.

    Raises:
        ServiceUnavailableError: Nothing installed one, so no foreign base can be judged. Raised only when
            a dataset declares a base outside its own root; a caller never meets it otherwise.
    """
    if _INSTALLED.judge is None:
        raise ServiceUnavailableError("the catalog's base judge is not installed, so a table declaring a foreign base cannot be read")
    return _INSTALLED.judge


def _foreign(location: str, refs: Sequence[BasePathRef]) -> list[BasePathRef]:
    return [ref for ref in refs if not base_registry.is_own(location, ref)]


def require_sanctioned_bases(location: str, refs: Sequence[BasePathRef], *, judge: BaseJudge | None) -> list[base_registry.BaseJudgement]:
    """Every declared base's standing; raises when one is sanctioned by nothing.

    ``location`` is the table's root — for a branch handle too, whose base on its parent is the table's
    own. ``judge`` ``None`` means "the installed one", fetched only when a base lies outside the root.

    Raises:
        InvalidTableStateError: A base is not the table's own, not configured and not in its record. The
            message names the bases and the remedy: restore the table to a version before the base
            arrived (``Restore`` rolls the base list back), or drop it.
        ServiceUnavailableError: The table's record exists and cannot be read, or no judge is installed.
    """
    if not _foreign(location, refs):
        return [base_registry.BaseJudgement(ref=ref, standing=base_registry.BaseStanding.OWN) for ref in refs]
    resolved = judge or installed_judge()
    judged = resolved.judge(location, refs)
    if refused := [judgement.ref.path for judgement in judged if judgement.standing is base_registry.BaseStanding.UNRECORDED]:
        log.warning("table_refused_unrecorded_bases", extra={"location": location, "bases": refused[:_NAMED_BASES]})
        raise InvalidTableStateError(
            f"the table declares base(s) {refused[:_NAMED_BASES]} that are not inside its own location, not a configured external "
            "blob base and not in the catalog's record for it. A manifest's base list can be extended by any holder of the "
            "table's write credential, so the catalog serves no read or credential through a base it did not sanction. "
            "Restore the table to a version from before the base was added, or drop it."
        )
    return judged
