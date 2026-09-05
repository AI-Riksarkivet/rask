"""Who is asking, and may they see this corpus.

The viewer shipped with no authorization at all: `GET /api/datasets` enumerated every corpus in the
registry — ids, table stats, declared capabilities — to any caller, and `/api/datasets/{id}/descriptor`
handed out a corpus's full schema. That was the documented "localhost / trusted network" posture,
and it stops being defensible the moment more than one person can reach the zone. A corpus LIST is
itself sensitive: it names data someone may not know exists.

**A corpus is not a new kind of object.** `MediaSettings.catalog_table_id(dataset_id, table)` already
maps a media dataset's table onto the catalog's identifier, and the annotator reads and writes Lance
through exactly that mapping. So the FGA object is the `table` the model already defines, with the
rungs it already has — no parallel `dataset` type to keep in step with the real one.

Everything mechanical (bearer → verified subject, the three-outcome checker) comes from
`service_kit.governed.deps`, shared with the annotator rather than copied out of it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from openfga_sdk.client import OpenFgaClient

from service_kit.governed.deps import FgaChecker, make_auth_deps
from viewer.core.config import ViewerSettings, get_viewer_settings


SettingsDep = Annotated[ViewerSettings, Depends(get_viewer_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: The RAW client, for the FILTERING path. `CheckerDep` is one relation on one object by design and
#: cannot express a batch; see `AuthDeps.get_fga_client`.
FgaClientDep = Annotated[OpenFgaClient | None, Depends(_deps.get_fga_client)]

#: The relation a READ of a corpus's metadata requires. `can_get_metadata` and not `can_read_data`:
#: listing a corpus and reading its descriptor is metadata, and the model already separates the two —
#: gating the list on data access would hide corpora from someone allowed to know they exist.
READ_METADATA = "can_get_metadata"

#: The relation a read of actual BYTES requires (#90). Separate from `READ_METADATA` because the
#: model separates them and the difference is the whole point for an archive: knowing a volume of
#: sealed records exists is not the same permission as reading the pages. `/api/page` returns image
#: bytes, so it takes this rung; `/api/pages` lists metadata and takes the metadata rung, matching
#: how `datasets.py` reasons about a corpus listing.
READ_DATA = "can_read_data"

#: Raw OBJECT-STORE browsing (#90) — the S3 list/HEAD/download routes. An ESTATE-wide privilege,
#: checked ONLY against `fga_root_object`, exactly like the catalog's `can_observe_events`.
#:
#: Not a per-store grant, and that is a decision with a reason. A `store` FGA type would need a
#: parent tuple per store to be reachable, and the four SHIPPED default stores are never registered
#: through the API — they come from `DEFAULT_STORES` in code, so nothing would ever write their
#: tuples. The model would be correct and the gate would deny everyone including the estate owner.
#: A gate that denies everyone is an outage, not a gate. Per-store granularity lands the day store
#: registration owns its own tuples.
#:
#: Owner tier, because the registry's buckets include the external RAW tier and the observability
#: bucket — outside the medallion entirely (R23) — so a per-tenant admin must not reach them.
BROWSE_STORAGE = "can_browse_storage"


# The two object-naming rules (`corpus_object`, `table_object`) moved to `service_kit.media.authz`
# when the annotator's assist plane needed the same object. Import them from there — this module's
# own docstring is why they must not be written twice.


# ── the corpus gate, as a DECORATOR dependency ──────────────────────────────────────────────────
#
# docs/DECISIONS.md "The Python estate audit" (P0): 24 of the viewer's 32 routes served corpus-derived content with no subject
# and no checker — the listing was gated while the content behind it was not, so knowing a `doc_id`
# was authorization. The fix is a dependency FACTORY rather than 24 inline checks, for one hard
# reason and one design reason. Hard: most of these routes are sync `def` with blocking Lance bodies
# (correctly threadpooled), and a sync body cannot `await` the checker — a dependency runs ON the
# loop, before the handler is threadpooled, so the bodies change zero lines. Design: the gate rides
# `dependencies=[...]` in the route decorator, and `test_every_corpus_route_is_gated` walks the
# app's dependant graph requiring a verified subject on every non-exempt route — deny-by-default,
# so route 33 arrives gated or argued, never silently open.

from starlette.concurrency import run_in_threadpool  # noqa: E402

from service_kit.exceptions import ForbiddenError  # noqa: E402
from service_kit.governed.audit import FAILURE, audit  # noqa: E402
from service_kit.media import state as _media_state  # noqa: E402
from service_kit.media.authz import corpus_object  # noqa: E402
from service_kit.media.deps import DatasetParam, StateDep  # noqa: E402


#: The verified principal, or ``anon`` — soft only on ABSENCE (a presented-but-invalid token still
#: raises; enabled-but-unwired still 503s). For the health badge, whose recorded contract is
#: "ALWAYS 200".
OptionalSubject = Annotated[str, Depends(_deps.optional_subject)]


def _corpus_gate(relation: str):
    """Gate a route on ``relation`` over the corpus's VISIBILITY object.

    The object follows `datasets.py`'s recorded rule: the search ROW table is the corpus's
    visibility object, and a corpus that declares no search block is DENIED under authz rather than
    checked against an invented identifier — "guessing an identifier would authorize against
    something the catalog never governs".

    FGA OFF RETURNS BEFORE ANYTHING RESOLVES — not merely "the checker is permissive". The early
    return is what keeps dev byte-identical (no extra registry open on every request) and keeps
    every FGA-off test harness, whose fake states cannot survive a real `dataset_handle`, out of
    this dependency entirely.
    """

    async def _gate(
        state: StateDep,
        subject: CurrentSubject,
        checker: CheckerDep,
        settings: SettingsDep,
        dataset: DatasetParam = None,
    ) -> None:
        if not settings.fga_enabled:
            return
        # Threadpooled: a COLD registry open is Lance/S3 under a lock (VS-02), and this runs on the
        # loop. The handler's own `dataset_handle` call then hits the registry's cache.
        handle = await run_in_threadpool(_media_state.dataset_handle, state, dataset)
        search = handle.descriptor.declared.search
        if search is None or not search.row_table:
            audit("viewer.corpus.read", FAILURE, subject=subject, resource=handle.id, relation=relation)
            raise ForbiddenError(f"corpus {handle.id!r} declares no searchable table, so no visibility object exists to authorize against")
        obj = corpus_object(settings, handle.id, search.row_table)
        if not await checker(user=subject, relation=relation, obj=obj):
            audit("viewer.corpus.read", FAILURE, subject=subject, resource=handle.id, relation=relation)
            raise ForbiddenError(f"{subject} lacks {relation} on {obj}")

    return Depends(_gate)


def _media_bytes_gate():
    """The media-byte family's gate: `can_read_data` over the DOCUMENT binding's table.

    A different object than `_corpus_gate` on purpose — it is the one `pages.py` and `media_clip`
    already check for byte reads, so the family stays on one grant. A corpus with no document
    binding falls through: every route in the family 404s that case in-body, and a 404 for an
    absent binding is uniform across callers (no existence oracle is created).
    """

    async def _gate(
        state: StateDep,
        subject: CurrentSubject,
        checker: CheckerDep,
        settings: SettingsDep,
        dataset: DatasetParam = None,
    ) -> None:
        if not settings.fga_enabled:
            return
        handle = await run_in_threadpool(_media_state.dataset_handle, state, dataset)
        binding = handle.descriptor.declared.document
        if binding is None:
            return
        obj = corpus_object(settings, handle.id, binding.table)
        if not await checker(user=subject, relation=READ_DATA, obj=obj):
            audit("viewer.media.read", FAILURE, subject=subject, resource=handle.id, relation=READ_DATA)
            raise ForbiddenError(f"{subject} lacks {READ_DATA} on {obj}")

    return Depends(_gate)


async def corpus_facts_visible(
    state: StateDep,
    subject: OptionalSubject,
    checker: CheckerDep,
    settings: SettingsDep,
    dataset: DatasetParam = None,
) -> bool:
    """May this caller see the health badge's corpus FACTS (table names, row counts)?

    The badge's recorded contract is ALWAYS 200 (a probe that cannot distinguish "service down"
    from "no corpus mounted" is worse than no probe — measured live 2026-07-28), so the corpus gate
    cannot ride it: this SOFT gate never raises for an anonymous caller, and the handler REDACTS
    the db facts instead of refusing the probe. Encoder reachability is never gated — it names no
    corpus. An unresolvable dataset returns True: there are no facts to protect, and the handler's
    own error path reports `db_error` as before.
    """
    if not settings.fga_enabled:
        return True
    try:
        handle = await run_in_threadpool(_media_state.dataset_handle, state, dataset)
    except Exception:
        return True
    search = handle.descriptor.declared.search
    if search is None or not search.row_table:
        return False
    return await checker(user=subject, relation=READ_METADATA, obj=corpus_object(settings, handle.id, search.row_table))


CorpusFactsVisible = Annotated[bool, Depends(corpus_facts_visible)]

#: Decorator dependencies for the gate shapes. Instantiated ONCE so every route shares the same
#: dependency object and FastAPI's per-request cache resolves each at most once per request.
REQUIRE_CORPUS_DATA = _corpus_gate(READ_DATA)
REQUIRE_CORPUS_METADATA = _corpus_gate(READ_METADATA)
REQUIRE_MEDIA_BYTES = _media_bytes_gate()
