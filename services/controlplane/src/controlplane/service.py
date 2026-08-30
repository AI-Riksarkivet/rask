"""Pure mapping from validated Project CRs (+ live Ingress host) to API DTOs.

The CRs arrive as raw `dict[str, Any]` from the kubernetes client and are turned into
:class:`~controlplane.schemas.ProjectCR` at ONE place — `list_project_dtos`, the function that
receives them from the reader. Everything below that line works on the model, which is why there are
no `.get()` chains here any more (CP-CR-UNVALIDATED).
"""

from typing import Any

from controlplane.k8s import ProjectReader
from controlplane.schemas import ProjectCR, ProjectDTO


# The project entry surface. Host carries the project; the path carries the domain
# (project-first URLs), so each project's MFEs serve at /<domain> — overview is the
# landing. (Bare host also redirects to /overview via the per-project ingress.)
PROJECT_ENTRY_PATH = "/overview"


def to_dto(cr: ProjectCR, url: str) -> ProjectDTO:
    """One validated CR, plus its resolved entry URL, as the API's response shape."""
    return ProjectDTO(
        slug=cr.metadata.name,
        name=cr.metadata.name,
        team=cr.spec.team,
        workload=cr.workload_type,
        phase=cr.phase,
        namespace=cr.reported_namespace,
        url=url,
        created_at=cr.metadata.creation_timestamp,
    )


def list_project_dtos(reader: ProjectReader, scheme: str) -> list[ProjectDTO]:
    """Read every Project CR, validate it, and resolve its entry URL.

    A CR that does not validate raises `pydantic.ValidationError` out of here, which `routes` turns
    into a named 502. It is NOT skipped: a project silently missing from the gallery is the same
    class of answer as the empty 200 the route already refuses to give.
    """
    raw: list[dict[str, Any]] = reader.list_projects()
    if not raw:
        return []
    crs = [ProjectCR.model_validate(cr) for cr in raw]
    # One cluster-wide ingress lookup for the whole page — not one blocking call per project.
    hosts = reader.ingress_hosts()
    dtos: list[ProjectDTO] = []
    for cr in crs:
        namespace = cr.lookup_namespace
        host = hosts.get(namespace) if namespace else None
        url = f"{scheme}://{host}{PROJECT_ENTRY_PATH}" if host else ""
        dtos.append(to_dto(cr, url))
    return sorted(dtos, key=lambda d: d.created_at)
