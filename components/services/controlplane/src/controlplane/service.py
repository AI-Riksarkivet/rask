"""Pure mapping from raw Project CR dicts (+ live Ingress host) to API DTOs."""

from typing import Any

from controlplane.k8s import ProjectReader
from controlplane.schemas import ProjectDTO


# The project entry surface. Reused MFE images serve at /default/<domain> today;
# Spec II (drop /default) flips this to "/overview".
PROJECT_ENTRY_PATH = "/default/overview"


def _namespace(cr: dict[str, Any]) -> str:
    status = cr.get("status", {})
    ns = status.get("namespace", "")
    if ns:
        return ns
    name = cr.get("metadata", {}).get("name", "")
    return f"project-{name}" if name else ""


def to_dto(cr: dict[str, Any], url: str) -> ProjectDTO:
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
        url=url,
        created_at=meta.get("creationTimestamp", ""),
    )


def list_project_dtos(reader: ProjectReader, scheme: str) -> list[ProjectDTO]:
    dtos: list[ProjectDTO] = []
    for cr in reader.list_projects():
        ns = _namespace(cr)
        host = reader.ingress_host(ns) if ns else None
        url = f"{scheme}://{host}{PROJECT_ENTRY_PATH}" if host else ""
        dtos.append(to_dto(cr, url))
    return sorted(dtos, key=lambda d: d.created_at)
