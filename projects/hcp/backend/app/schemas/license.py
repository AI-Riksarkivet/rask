"""License resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List


class License(BaseModel):
    localCapacity: Optional[int] = None
    expirationDate: Optional[str] = None
    extendedCapacity: Optional[int] = None
    feature: Optional[str] = None
    serialNumber: Optional[str] = None
    uploadDate: Optional[str] = None


class LicenseList(BaseModel):
    model_config = {"extra": "allow"}

    license: Optional[List[License]] = None
