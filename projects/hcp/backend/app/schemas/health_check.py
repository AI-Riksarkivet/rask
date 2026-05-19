"""Health check report models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class HealthCheckPrepare(BaseModel):
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    exactTime: Optional[str] = None
    collectCurrent: Optional[bool] = True


class HealthCheckDownload(BaseModel):
    nodes: Optional[str] = None
    content: Optional[str] = None


class HealthCheckDownloadStatus(BaseModel):
    model_config = {"extra": "allow"}

    readyForStreaming: Optional[bool] = None
    streamingInProgress: Optional[bool] = None
    error: Optional[bool] = None
    started: Optional[bool] = None
    content: Optional[str] = None
