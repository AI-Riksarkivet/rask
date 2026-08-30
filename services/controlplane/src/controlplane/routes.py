import logging
from functools import lru_cache
from typing import Annotated

import urllib3.exceptions
from fastapi import APIRouter, Depends, HTTPException
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from pydantic import ValidationError

from controlplane import security, service
from controlplane.dependencies import ControlplaneSettingsDep
from controlplane.k8s import PROJECT_GROUP, PROJECT_PLURAL, K8sProjectReader, ProjectReader
from controlplane.schemas import ProjectsResponse


log = logging.getLogger("controlplane")

# GATED AT THE ROUTER, not per route. This response enumerates every tenant in the estate — slug,
# team, workload type, k8s namespace and each project's live ingress host — and the gateway carries
# it to the public edge. A per-route dependency would let the next route added here arrive ungated,
# which is exactly how this service came to have no auth at all. See `security.py` for the rung.
router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(security.require_read)])

# Failures of the k8s read, split by CAUSE below: an unregistered resource type → 501, everything
# else here ("control plane unreachable") → 503. Anything NOT in this tuple (e.g. a CR-mapping bug)
# propagates as a 500 so real defects aren't masked.
_K8S_ERRORS = (ApiException, ConfigException, urllib3.exceptions.HTTPError, OSError)

_UNREACHABLE_DETAIL = "cannot reach kubernetes api"
_UNREADABLE_CR_DETAIL = (
    "the cluster returned a Project custom resource this service cannot read — its shape does not "
    f"match {PROJECT_PLURAL}.{PROJECT_GROUP}/v1alpha1 as this build understands it"
)
_NO_OPERATOR_DETAIL = (
    f"project operator not installed: this cluster registers no {PROJECT_PLURAL}.{PROJECT_GROUP} resource type, so it holds no Project CRs to list"
)


def _is_unregistered_resource_type(exc: BaseException) -> bool:
    """A 404 here is the API SERVER ANSWERING, not failing to answer.

    `list_cluster_custom_object` 404s only when the cluster registers no
    `projects.platform.rask.io` type — i.e. `rask-operator`, which lives in a separate repo and is
    deliberately not shipped by this chart (`docs/DECISIONS.md`, 2026-08-16), is not installed on
    this estate. That is a permanent property of the deployment; an RBAC 403 and a refused
    connection are not, and this predicate is what keeps the three answers apart. Reporting all
    three as "cannot reach kubernetes api" sent one session after the ServiceAccount
    (`HANDOFF-lakehouse.md:101-106`) and another after shipping the CRD without its controller
    (`OPEN-WORK.md` §G1) — the one fix that ruling forbids.
    """
    return isinstance(exc, ApiException) and exc.status == 404


@lru_cache(maxsize=1)
def get_reader() -> ProjectReader:
    """Build the live k8s reader once and reuse it (it loads kube config and holds
    API clients). Overridden in tests via app.dependency_overrides, which bypasses
    this function — so the cache never leaks into tests."""
    return K8sProjectReader()


ReaderDep = Annotated[ProjectReader, Depends(get_reader)]


@router.get("/")
def list_projects(reader: ReaderDep, settings: ControlplaneSettingsDep) -> ProjectsResponse:
    try:
        dtos = service.list_project_dtos(reader, settings.project_url_scheme)
    except ValidationError as exc:
        # A CR the boundary model refuses. 502, because the failure is UPSTREAM of this service and
        # it is neither absent (501) nor unreachable (503) — the API server answered, with a Project
        # whose shape this build does not know. The alternative the `.get()` chains gave was an
        # `AttributeError` 500 naming a line in `service.py`; the alternative of skipping the row is
        # the empty-200 answer the branch below exists to refuse, one project at a time.
        log.warning("unreadable Project CR: %s", exc)
        raise HTTPException(status_code=502, detail=_UNREADABLE_CR_DETAIL) from exc
    except _K8S_ERRORS as exc:
        # NEVER an empty 200. A successful-looking empty gallery on an estate with no project
        # operator is strictly worse than a named failure: it reports "you have no projects" for a
        # capability that was never installed, and nothing anywhere says which.
        if _is_unregistered_resource_type(exc):
            log.warning("project operator not installed: the cluster registers no %s.%s resource type", PROJECT_PLURAL, PROJECT_GROUP)
            # 501, not 503: the capability is ABSENT from this deployment, not temporarily down. A
            # permanent 503 tells every retrying client that a healthy service is flapping, and it
            # spends the estate's `invokeRetry` budget on an answer that will never change — the
            # retry policy matches on status, so 503 is retried and 501 is not.
            #
            # (An earlier draft of this comment justified the choice by "the Dapr circuit breakers
            # keyed on this app-id". This estate has NONE, deliberately: `chart/templates/
            # dapr-resiliency.yaml` records that the previous one counted every non-2xx, so five
            # authorization refusals took a whole app-id offline for 30s — a denial of service any
            # unauthenticated client could aim. Do not reintroduce that reasoning.)
            raise HTTPException(status_code=501, detail=_NO_OPERATOR_DETAIL) from exc
        log.warning("kubernetes API error listing projects: %s", exc)
        raise HTTPException(status_code=503, detail=_UNREACHABLE_DETAIL) from exc
    return ProjectsResponse(projects=dtos)
