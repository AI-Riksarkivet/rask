"""Support access credentials models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class SupportAccessCredentials(BaseModel):
    model_config = {"extra": "allow"}

    applyTimeStamp: Optional[int] = None
    createTimeStamp: Optional[int] = None
    type: Optional[str] = None
    defaultKeyType: Optional[str] = None
    serialNumberFromPackage: Optional[int] = None
