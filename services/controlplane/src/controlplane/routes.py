import logging
import os
from functools import lru_cache
from typing import Annotated

import urllib3.exceptions
from fastapi import APIRouter, Depends, HTTPException
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from controlplane import security, service
from controlplane.k8s import K8sProjectReader, ProjectReader
from controlplane.schemas import ProjectsResponse


log = logging.getLogger("controlplane")

# GATED AT THE ROUTER, not per route. This response enumerates every tenant in the estate — slug,
# team, workload type, k8s namespace and each project's live ingress host — and the gateway carries
# it to the public edge. A per-route dependency would let the next route added here arrive ungated,
# which is exactly how this service came to have no auth at all. See `security.py` for the rung.
router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(security.require_read)])

# k8s/transport failures that mean "control plane unreachable" → 503. Anything
# else (e.g. a CR-mapping bug) propagates as a 500 so real defects aren't masked.
_K8S_ERRORS = (ApiException, ConfigException, urllib3.exceptions.HTTPError, OSError)


@lru_cache(maxsize=1)
def get_reader() -> ProjectReader:
    """Build the live k8s reader once and reuse it (it loads kube config and holds
    API clients). Overridden in tests via app.dependency_overrides, which bypasses
    this function — so the cache never leaks into tests."""
    return K8sProjectReader()


ReaderDep = Annotated[ProjectReader, Depends(get_reader)]


@router.get("/")
def list_projects(reader: ReaderDep) -> ProjectsResponse:
    scheme = os.environ.get("RASK_PROJECT_URL_SCHEME", "http")
    try:
        dtos = service.list_project_dtos(reader, scheme)
    except _K8S_ERRORS as exc:
        log.warning("kubernetes API error listing projects: %s", exc)
        raise HTTPException(status_code=503, detail="cannot reach kubernetes api") from exc
    return ProjectsResponse(projects=dtos)
