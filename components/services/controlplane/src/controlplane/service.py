"""Pure mapping from raw Project CR dicts to API DTOs. No I/O, no k8s client."""

from typing import Any

from controlplane.k8s import ProjectReader
from controlplane.schemas import ProjectDTO


def to_dto(cr: dict[str, Any]) -> ProjectDTO:
    meta = cr.get("metadata", {})
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    return ProjectDTO(
        slug=meta.get("name", ""),
        name=meta.get("name", ""),
        team=spec.get("team", ""),
        workload=spec.get("workload", {}).get("type", ""),
        phase=status.get("phase") or "Pending",
        namespace=status.get("namespace", ""),
        created_at=meta.get("creationTimestamp", ""),
    )


def list_project_dtos(reader: ProjectReader) -> list[ProjectDTO]:
    dtos = [to_dto(cr) for cr in reader.list_projects()]
    return sorted(dtos, key=lambda d: d.created_at)
