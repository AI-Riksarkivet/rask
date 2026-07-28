"""The compute service's health endpoint — process liveness (Ray reachability is
`/ray/health`'s job, not this one's)."""

from fastapi import APIRouter
from pydantic import BaseModel


class Health(BaseModel):
    status: str


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> Health:
    return Health(status="ok")
