"""Dataset enumeration + descriptor endpoints — the multi-dataset front door.

``GET /api/datasets`` lists every ``<id>.lance`` directory the registry can
serve (tables + available capabilities per dataset); ``GET /api/datasets/{id}/
descriptor`` hands the frontend the full merged descriptor it renders from
(LANCE_MEDIA_MERGE §4.2/§4.4). Datasets whose descriptor fails to load are
skipped from the listing (logged), never half-served.
"""

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import FAILURE, audit
from service_kit.lancekit.descriptor import DatasetDescriptor
from service_kit.lancekit.registry import DatasetRegistry, UnknownDatasetError
from service_kit.media.deps import StateDep
from service_kit.media.state import AppState, dataset_handle
from viewer.api.security import READ_METADATA, CheckerDep, CurrentSubject, SettingsDep, corpus_object


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
            storage_options=state.settings.storage_options,
        )
    return state.registry


@router.get("/datasets")
async def list_datasets(state: StateDep, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep) -> DatasetsResponse:
    """The corpora this CALLER may see.

    It used to be every corpus on disk, to anyone. A corpus list is itself sensitive — it names data
    someone may not know exists — so each entry is now gated on `can_get_metadata` for that corpus's
    ROW table, which is the table search would actually read.

    Filtered, not refused: a caller with access to two of five corpora gets two, because the honest
    answer to "what can I search" is a shorter list, not a 403.
    """
    registry = _registry(state)
    datasets: list[DatasetSummary] = []
    for dataset_id in registry.list_ids():
        try:
            descriptor = registry.get(dataset_id).descriptor
        except (UnknownDatasetError, ValueError) as exc:
            logger.warning("skipping dataset %s: %s", dataset_id, exc)
            continue
        datasets.append(
            DatasetSummary(
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
        )

    # Checked CONCURRENTLY: a registry of a dozen corpora would otherwise pay a dozen serial
    # round-trips to OpenFGA on the first page every user loads. With FGA off the checker is a local
    # `return True`, so this costs nothing on a dev stack.
    async def _may_see(dataset_id: str) -> bool:
        table = _row_table(registry, dataset_id)
        if table is None:
            # Nothing to name as an FGA object. Deny under authz rather than guess an identifier —
            # but only when authz is ON, so a dev stack with FGA off still lists it as it always did.
            return not settings.fga_enabled
        return await checker(user=subject, relation=READ_METADATA, obj=corpus_object(settings, dataset_id, table))

    permitted = await asyncio.gather(*(_may_see(d.id) for d in datasets))
    return DatasetsResponse(datasets=[d for d, ok in zip(datasets, permitted, strict=True) if ok])


def _row_table(registry: DatasetRegistry, dataset_id: str) -> str | None:
    """The table a corpus's search reads — the one whose grant decides whether the corpus is visible.

    A corpus has many tables and gating on ALL of them would make visibility mean "may read
    everything", which is stricter than the question being asked ("may I search this corpus").

    `None` when the corpus declares no search at all (`Search | None` on the descriptor). That is a
    real shape, not a defect — a corpus can be viewable without being searchable — and it is handled
    at the call site by DENYING rather than by inventing an object to check. Guessing an identifier
    would authorize against something the catalog never governs, which reads as a grant.
    """
    search = registry.get(dataset_id).descriptor.declared.search
    return search.row_table if search is not None else None


@router.get("/datasets/{dataset_id}/descriptor")
async def dataset_descriptor(dataset_id: str, state: StateDep, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep) -> DatasetDescriptor:
    """The full merged descriptor (discovered tables + declared roles); 404 via
    the domain handler for unknown or descriptor-less ids.

    Gated on the same relation as the listing, because it is strictly MORE than the listing reveals:
    every table, every column name, every declared capability. Guarding the list and leaving this
    open would mean the list only had to be guessed.
    """
    handle = dataset_handle(state, dataset_id)
    obj = corpus_object(settings, dataset_id, handle.descriptor.declared.search.row_table)
    if not await checker(user=subject, relation=READ_METADATA, obj=obj):
        audit("viewer.descriptor.read", FAILURE, subject=subject, resource=dataset_id, relation=READ_METADATA)
        raise ForbiddenError(f"{subject} lacks {READ_METADATA} on {obj}")
    return handle.descriptor
