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

from service_kit.lancekit.descriptor import DatasetDescriptor
from service_kit.lancekit.registry import DatasetRegistry, UnknownDatasetError
from service_kit.media.deps import StateDep
from service_kit.media.state import AppState, dataset_handle


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
def list_datasets(state: StateDep) -> DatasetsResponse:
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
    return DatasetsResponse(datasets=datasets)


@router.get("/datasets/{dataset_id}/descriptor")
def dataset_descriptor(dataset_id: str, state: StateDep) -> DatasetDescriptor:
    """The full merged descriptor (discovered tables + declared roles); 404 via
    the domain handler for unknown or descriptor-less ids."""
    return dataset_handle(state, dataset_id).descriptor
