"""Replication resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List
from .common import LinkType, PerformanceLevel


# ── Connection ─────────────────────────────────────────────────────────


class Connection(BaseModel):
    """Replication link connection settings."""

    remoteHost: str
    remotePort: Optional[int] = 5748
    localHost: Optional[str] = None
    localPort: Optional[int] = 5748


# ── Failover Settings ─────────────────────────────────────────────────


class FailoverNode(BaseModel):
    """Auto failover settings for one side of a link."""

    autoFailover: Optional[bool] = False
    autoFailoverMinutes: Optional[int] = 120
    autoCompleteRecovery: Optional[bool] = None
    autoCompleteRecoveryMinutes: Optional[int] = None


class FailoverSettings(BaseModel):
    """Failover and failback settings for a replication link."""

    local: Optional[FailoverNode] = None
    remote: Optional[FailoverNode] = None


# ── Link Statistics ────────────────────────────────────────────────────


class LinkStatistics(BaseModel):
    """Replication link statistics."""

    upToDateAsOfString: Optional[str] = None
    upToDateAsOfMillis: Optional[int] = None
    bytesPending: Optional[int] = None
    bytesPendingRemote: Optional[int] = None
    bytesReplicated: Optional[int] = None
    bytesPerSecond: Optional[float] = None
    objectsPending: Optional[int] = None
    objectsPendingRemote: Optional[int] = None
    objectsReplicated: Optional[int] = None
    operationsPerSecond: Optional[float] = None
    errors: Optional[int] = None
    errorsPerSecond: Optional[float] = None
    objectsVerified: Optional[int] = None
    objectsReplicatedAfterVerification: Optional[int] = None


# ── Link ───────────────────────────────────────────────────────────────


class LinkCreate(BaseModel):
    """Properties for creating a replication link (PUT)."""

    name: str
    type: LinkType
    connection: Connection
    description: Optional[str] = None
    compression: Optional[bool] = False
    encryption: Optional[bool] = False
    priority: Optional[str] = "OLDEST_FIRST"
    failoverSettings: Optional[FailoverSettings] = None


class LinkUpdate(BaseModel):
    """Properties for modifying a replication link (POST)."""

    description: Optional[str] = None
    connection: Optional[Connection] = None
    compression: Optional[bool] = None
    encryption: Optional[bool] = None
    priority: Optional[str] = None
    failoverSettings: Optional[FailoverSettings] = None
    type: Optional[LinkType] = None


class LinkResponse(BaseModel):
    """Full replication link response."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    type: Optional[str] = None
    connection: Optional[Connection] = None
    description: Optional[str] = None
    compression: Optional[bool] = None
    encryption: Optional[bool] = None
    priority: Optional[str] = None
    failoverSettings: Optional[FailoverSettings] = None
    id: Optional[str] = None
    statistics: Optional[LinkStatistics] = None
    status: Optional[str] = None
    statusMessage: Optional[str] = None
    suspended: Optional[bool] = None


class LinkList(BaseModel):
    """List of link names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None


# ── Link Content ───────────────────────────────────────────────────────


class LinkContent(BaseModel):
    """Content included in a replication link."""

    model_config = {"extra": "allow"}

    tenants: Optional[List[str]] = None
    defaultNamespaceDirectories: Optional[List[str]] = None
    chainedLinks: Optional[List[str]] = None


# ── Schedule ───────────────────────────────────────────────────────────


class Transition(BaseModel):
    """A scheduled performance level change."""

    time: str
    performanceLevel: PerformanceLevel


class ScheduleSide(BaseModel):
    """Schedule for one side (local/remote) of a link."""

    scheduleOverride: Optional[str] = "NONE"
    transition: Optional[List[Transition]] = None


class Schedule(BaseModel):
    """Replication link schedule."""

    model_config = {"extra": "allow"}

    local: Optional[ScheduleSide] = None
    remote: Optional[ScheduleSide] = None


# ── Certificates ───────────────────────────────────────────────────────


class Certificate(BaseModel):
    """Replication certificate."""

    id: Optional[str] = None
    subjectDN: Optional[str] = None
    validOn: Optional[str] = None
    expiresOn: Optional[str] = None


class CertificateList(BaseModel):
    """List of replication certificates."""

    model_config = {"extra": "allow"}

    certificate: Optional[List[Certificate]] = None


# ── Replication Service ────────────────────────────────────────────────


class ReplicationService(BaseModel):
    """Replication service settings."""

    model_config = {"extra": "allow"}

    allowTenantsToMonitorNamespaces: Optional[bool] = None
    enableDNSFailover: Optional[bool] = None
    enableDomainAndCertificateSynchronization: Optional[bool] = None
    network: Optional[str] = None
    connectivityTimeoutSeconds: Optional[int] = None
    verification: Optional[str] = None
    status: Optional[str] = None
