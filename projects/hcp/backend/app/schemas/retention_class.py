"""Retention class resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List


class RetentionClassCreate(BaseModel):
    """Properties for creating a retention class (PUT)."""

    name: str
    value: str
    description: Optional[str] = None
    allowDisposition: Optional[bool] = None


class RetentionClassUpdate(BaseModel):
    """Properties for modifying a retention class (POST)."""

    value: Optional[str] = None
    description: Optional[str] = None
    allowDisposition: Optional[bool] = None


class RetentionClassResponse(BaseModel):
    """Full retention class response."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None
    allowDisposition: Optional[bool] = None


class RetentionClassList(BaseModel):
    """List of retention class names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None
