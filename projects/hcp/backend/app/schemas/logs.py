"""Log resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class LogPrepare(BaseModel):
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    snodes: Optional[str] = None


class LogDownload(BaseModel):
    nodes: Optional[str] = None
    snodes: Optional[str] = None
    content: Optional[str] = None


class LogDownloadStatus(BaseModel):
    model_config = {"extra": "allow"}

    readyForStreaming: Optional[bool] = None
    streamingInProgress: Optional[bool] = None
    started: Optional[bool] = None
    error: Optional[bool] = None
    content: Optional[str] = None
    selectedNodes: Optional[str] = None
    selectedContent: Optional[str] = None
    packageNodes: Optional[str] = None
