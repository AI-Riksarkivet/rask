from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from controlplane import service
from controlplane.k8s import K8sProjectReader, ProjectReader
from controlplane.schemas import ProjectsResponse


router = APIRouter(prefix="/projects", tags=["projects"])


def get_reader() -> ProjectReader:
    """Build the live k8s reader. Overridden in tests via app.dependency_overrides."""
    return K8sProjectReader()


ReaderDep = Annotated[ProjectReader, Depends(get_reader)]


@router.get("/")
def list_projects(reader: ReaderDep) -> ProjectsResponse:
    try:
        dtos = service.list_project_dtos(reader)
    except Exception as exc:  # broad catch: any k8s failure surfaces as a clean 503
        raise HTTPException(status_code=503, detail="cannot reach kubernetes api") from exc
    return ProjectsResponse(projects=dtos)
