"""Dataset enumeration + descriptor endpoints — the multi-dataset front door.

``GET /api/datasets`` lists every ``<id>.lance`` directory the registry can
serve (tables + available capabilities per dataset); ``GET /api/datasets/{id}/
descriptor`` hands the frontend the full merged descriptor it renders from
(LANCE_MEDIA_MERGE §4.2/§4.4). Datasets whose descriptor fails to load are
skipped from the listing (logged), never half-served.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from service_kit.exceptions import ForbiddenError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, audit
from service_kit.lancekit.descriptor import DatasetDescriptor
from service_kit.lancekit.registry import DatasetRegistry, UnknownDatasetError
from service_kit.media.authz import corpus_object
from service_kit.media.deps import StateDep
from service_kit.media.state import AppState, dataset_handle
from viewer.api.security import READ_METADATA, CheckerDep, CurrentSubject, FgaClientDep, SettingsDep


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["datasets"])


class TableFacts(BaseModel):
    """The discovered shape of one table, as listed per dataset."""

    row_count: int
    version: int
    n_columns: int


class DatasetSummary(BaseModel):
    """One servable dataset: its tables and the capabilities that probe available."""

    id: str
    tables: dict[str, TableFacts]
    capabilities: list[str] = Field(default_factory=list)


class DatasetsResponse(BaseModel):
    datasets: list[DatasetSummary]


def _registry(state: AppState) -> DatasetRegistry:
    """The registry behind ``dataset_handle`` — same lazy-init, needed here
    because enumeration has no dataset id to resolve through it."""
    if state.registry is None:
        state.registry = DatasetRegistry(
            state.settings.registry_root,
            state.settings.descriptor_dir,
            state.settings.default_dataset_id,
            storage_options=state.settings.storage_options(),
        )
    return state.registry


@router.get("/datasets")
async def list_datasets(state: StateDep, client: FgaClientDep, subject: CurrentSubject, settings: SettingsDep) -> DatasetsResponse:
    """The corpora this CALLER may see.

    It used to be every corpus on disk, to anyone. A corpus list is itself sensitive — it names data
    someone may not know exists — so each entry is now gated on `can_get_metadata` for that corpus's
    ROW table, which is the table search would actually read.

    Filtered, not refused: a caller with access to two of five corpora gets two, because the honest
    answer to "what can I search" is a shorter list, not a 403.
    """
    # Off the loop: `registry.get` opens Lance/S3 under a threading.Lock, and this is the first call
    # every zone makes on page load — inline it serialized the whole process behind one cold dataset
    # (open_python-audit VS-02). The row table is read HERE, in the same pass, so `_may_see` below
    # never re-opens the registry per dataset (the descriptor was being read twice).
    #
    # `_registry(state)` RUNS INSIDE the threadpool too (open_python-audit E2): building the registry
    # reads `settings.storage_options()`, a BLOCKING Dapr secret fetch on the cold path, and it used
    # to sit on the event loop above this function. The secret is `_store_secret`-cached, so the boot
    # warm usually makes this free — but a request that arrives before the warm, or after a warm that
    # failed, must not block the loop on the fetch.
    def _collect() -> list[tuple[DatasetSummary, str | None]]:
        registry = _registry(state)
        collected: list[tuple[DatasetSummary, str | None]] = []
        for dataset_id in registry.list_ids():
            try:
                descriptor = registry.get(dataset_id).descriptor
            except (UnknownDatasetError, ValueError) as exc:
                logger.warning("skipping dataset %s: %s", dataset_id, exc)
                continue
            summary = DatasetSummary(
                id=dataset_id,
                tables={
                    name: TableFacts(
                        row_count=info.row_count,
                        version=info.version,
                        n_columns=len(info.columns),
                    )
                    for name, info in descriptor.tables.items()
                },
                capabilities=[name for name in descriptor.declared.capabilities if descriptor.capability_available(name)],
            )
            # The ROW table is the visibility gate: the one search actually reads. Gating on ALL
            # tables would make visibility mean "may read everything" — stricter than the question
            # ("may I search this corpus"). None = the corpus declares no search: a real shape, and
            # the listing DENIES it under authz rather than inventing an object to check (guessing an
            # identifier would authorize against something the catalog never governs).
            search = descriptor.declared.search
            collected.append((summary, search.row_table if search is not None else None))
        return collected

    pairs = await run_in_threadpool(_collect)

    # ONE round trip, not one per corpus. This used to `asyncio.gather` a `checker(...)` per corpus,
    # under a comment reasoning carefully about serial-vs-concurrent — which is the right analysis of
    # LATENCY and the wrong question. `gather` makes N calls cheap; it does not make them fewer, and
    # this is the first call every zone makes on page load, so it was N OpenFGA requests per user per
    # page. `authz.md`: "prefer batch_check over many checks when filtering — same network round-trip
    # cost as one call", with a filtered list as its named example. The estate already agrees with
    # itself twice: `lineage/api/fga_deps.py` and `notifications/api/visibility.py`.
    #
    # THE TABLE-LESS RULE IS UNCHANGED and still keyed on `settings.fga_enabled`, not on whether a
    # client happens to be present. A corpus that declares no search names no FGA object, so it is
    # decided BEFORE the batch rather than given a guessed identifier: denied when authz is on, listed
    # when it is off, exactly as before.
    if not settings.fga_enabled:
        return DatasetsResponse(datasets=[s for s, _t in pairs])

    tabled = [(summary, table) for summary, table in pairs if table is not None]
    if not tabled or client is None:
        return DatasetsResponse(datasets=[])

    objects = [corpus_object(settings, summary.id, table) for summary, table in tabled]
    verdicts = await fga.batch_check(client, user=subject, relation=READ_METADATA, objects=objects)
    return DatasetsResponse(datasets=[summary for (summary, _t), obj in zip(tabled, objects, strict=True) if verdicts.get(obj, False)])


@router.get("/datasets/{dataset_id}/descriptor")
async def dataset_descriptor(dataset_id: str, state: StateDep, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep) -> DatasetDescriptor:
    """The full merged descriptor (discovered tables + declared roles); 404 via
    the domain handler for unknown or descriptor-less ids.

    Gated on the same relation as the listing, because it is strictly MORE than the listing reveals:
    every table, every column name, every declared capability. Guarding the list and leaving this
    open would mean the list only had to be guessed.
    """
    handle = await run_in_threadpool(dataset_handle, state, dataset_id)
    # `Search | None` — the same shape the listing handles, and it must be handled identically here:
    # with no row table there is nothing to name as an FGA object, so DENY rather than guess one.
    # Reading it unguarded was a 500 (`'NoneType' object has no attribute 'row_table'`).
    search = handle.descriptor.declared.search
    if search is None:
        # Deny under authz, serve with it off — the SAME rule the listing applies, because a corpus
        # that appears in the list must also be openable from it. Denying unconditionally here (the
        # first version of this guard) would have broken every FGA-off dev stack.
        if settings.fga_enabled:
            audit("viewer.descriptor.read", FAILURE, subject=subject, resource=dataset_id, relation=READ_METADATA)
            raise ForbiddenError(f"corpus {dataset_id} declares no searchable table — nothing to authorize against")
        return handle.descriptor
    obj = corpus_object(settings, dataset_id, search.row_table)
    if not await checker(user=subject, relation=READ_METADATA, obj=obj):
        audit("viewer.descriptor.read", FAILURE, subject=subject, resource=dataset_id, relation=READ_METADATA)
        raise ForbiddenError(f"{subject} lacks {READ_METADATA} on {obj}")
    return handle.descriptor
