"""Erasure coding topology resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List
from .common import ECTopologyType


class ECReplicationLink(BaseModel):
    """Replication link within an EC topology."""

    name: str
    uuid: Optional[str] = None
    hcpSystems: Optional[List[str]] = None
    pausedTenantsCount: Optional[int] = None
    state: Optional[str] = None


class ECTopologyCreate(BaseModel):
    """Properties for creating an erasure coding topology (PUT)."""

    name: str
    type: ECTopologyType
    replicationLinks: List[ECReplicationLink]
    description: Optional[str] = None
    erasureCodingDelay: Optional[int] = 0
    fullCopy: Optional[bool] = False
    minimumObjectSize: Optional[int] = 4096
    restorePeriod: Optional[int] = 0


class ECTopologyResponse(BaseModel):
    """Full erasure coding topology response."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    erasureCodingDelay: Optional[int] = None
    fullCopy: Optional[bool] = None
    minimumObjectSize: Optional[int] = None
    restorePeriod: Optional[int] = None
    replicationLinks: Optional[List[ECReplicationLink]] = None
    hcpSystems: Optional[List[str]] = None
    tenants: Optional[List[str]] = None
    id: Optional[str] = None
    erasureCodedObjects: Optional[int] = None
    protectionStatus: Optional[str] = None
    readStatus: Optional[str] = None
    state: Optional[str] = None


class ECTopologyList(BaseModel):
    """List of EC topology names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None


class TenantCandidate(BaseModel):
    """Tenant eligible for EC topology."""

    name: Optional[str] = None
    uuid: Optional[str] = None
    hcpSystems: Optional[List[str]] = None


class TenantCandidateList(BaseModel):
    """List of tenant candidates."""

    model_config = {"extra": "allow"}

    tenantCandidate: Optional[List[TenantCandidate]] = None


class ECLinkCandidateList(BaseModel):
    """List of candidate replication links for EC."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None
