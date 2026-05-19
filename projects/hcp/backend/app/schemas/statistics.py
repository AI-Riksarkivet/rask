"""Node and service statistics models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List


class Volume(BaseModel):
    id: Optional[str] = None
    blocksRead: Optional[float] = None
    blocksWritten: Optional[float] = None
    diskUtilization: Optional[float] = None
    transferSpeed: Optional[float] = None
    totalBytes: Optional[int] = None
    freeBytes: Optional[int] = None
    totalInodes: Optional[int] = None
    freeInodes: Optional[int] = None


class NodeStats(BaseModel):
    nodeNumber: Optional[int] = None
    frontendIpAddresses: Optional[List[str]] = None
    backendIpAddress: Optional[str] = None
    managementIpAddresses: Optional[List[str]] = None
    openHttpConnections: Optional[int] = None
    openHttpsConnections: Optional[int] = None
    maxHttpConnections: Optional[int] = None
    maxHttpsConnections: Optional[int] = None
    cpuUser: Optional[float] = None
    cpuSystem: Optional[float] = None
    cpuMax: Optional[float] = None
    ioWait: Optional[float] = None
    swapOut: Optional[float] = None
    maxFrontEndBandwidth: Optional[int] = None
    frontEndBytesRead: Optional[float] = None
    frontEndBytesWritten: Optional[float] = None
    maxBackEndBandwidth: Optional[int] = None
    backEndBytesRead: Optional[float] = None
    backEndBytesWritten: Optional[float] = None
    maxManagementPortBandwidth: Optional[int] = None
    managementBytesRead: Optional[float] = None
    managementBytesWritten: Optional[float] = None
    collectionTimestamp: Optional[int] = None
    volumes: Optional[List[Volume]] = None


class NodeStatistics(BaseModel):
    model_config = {"extra": "allow"}

    requestTime: Optional[int] = None
    nodes: Optional[List[NodeStats]] = None


class ServiceInfo(BaseModel):
    name: Optional[str] = None
    state: Optional[str] = None
    startTime: Optional[int] = None
    formattedStartTime: Optional[str] = None
    endTime: Optional[int] = None
    formattedEndTime: Optional[str] = None
    performanceLevel: Optional[str] = None
    objectsExamined: Optional[int] = None
    objectsExaminedPerSecond: Optional[float] = None
    objectsServiced: Optional[int] = None
    objectsServicedPerSecond: Optional[float] = None
    objectsUnableToService: Optional[int] = None
    objectsUnableToServicePerSecond: Optional[float] = None


class ServiceStatistics(BaseModel):
    model_config = {"extra": "allow"}

    requestTime: Optional[int] = None
    services: Optional[List[ServiceInfo]] = None
    nodes: Optional[List[dict]] = None


class NamespaceStatistics(BaseModel):
    """Statistics for a namespace or tenant."""

    model_config = {"extra": "allow"}

    customMetadataCount: Optional[int] = None
    customMetadataSize: Optional[int] = None
    objectCount: Optional[int] = None
    ingestedVolume: Optional[int] = None
    shredCount: Optional[int] = None
    shredSize: Optional[int] = None
    storageCapacityUsed: Optional[int] = None
