"""Network settings models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional
from .common import DownstreamDNSMode


class NetworkSettings(BaseModel):
    model_config = {"extra": "allow"}

    downstreamDNSMode: Optional[DownstreamDNSMode] = None
