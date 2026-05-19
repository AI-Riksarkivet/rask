"""Content class resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List


class ContentProperty(BaseModel):
    """A content property within a content class."""

    name: str
    expression: str
    type: str
    multivalued: Optional[bool] = False
    format: Optional[str] = None


class ContentClassCreate(BaseModel):
    """Properties for creating a content class (PUT)."""

    name: str
    contentProperties: Optional[List[ContentProperty]] = None
    namespaces: Optional[List[str]] = None


class ContentClassUpdate(BaseModel):
    """Properties for modifying a content class (POST)."""

    contentProperties: Optional[List[ContentProperty]] = None
    namespaces: Optional[List[str]] = None


class ContentClassResponse(BaseModel):
    """Full content class response."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    contentProperties: Optional[List[ContentProperty]] = None
    namespaces: Optional[List[str]] = None


class ContentClassList(BaseModel):
    """List of content class names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None
