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
from collections.abc import Sequence
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


class BaseJudge(BaseModel):
    """What a declared base is judged against: the catalog's record, and the configured external blob bases."""

    registry: base_registry.BaseRegistry
    #: ``LANCE_EXTERNAL_BLOB_BASES`` — sanctioned by configuration as plain (pointer) bases.
    configured: list[str]

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
        )


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
    judged = base_registry.judge_bases(location, refs, configured=resolved.configured, load_record=partial(resolved.read_record, location))
    if refused := [judgement.ref.path for judgement in judged if judgement.standing is base_registry.BaseStanding.UNRECORDED]:
        log.warning("table_refused_unrecorded_bases", extra={"location": location, "bases": refused[:_NAMED_BASES]})
        raise InvalidTableStateError(
            f"the table declares base(s) {refused[:_NAMED_BASES]} that are not inside its own location, not a configured external "
            "blob base and not in the catalog's record for it. A manifest's base list can be extended by any holder of the "
            "table's write credential, so the catalog serves no read or credential through a base it did not sanction. "
            "Restore the table to a version from before the base was added, or drop it."
        )
    return judged
