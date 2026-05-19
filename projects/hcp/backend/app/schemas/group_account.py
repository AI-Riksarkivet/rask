"""Tenant-level and system-level group account models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List
from .common import RoleList


class GroupAccountCreate(BaseModel):
    """Properties for creating a group account (PUT)."""

    groupname: Optional[str] = None
    externalGroupID: Optional[str] = None
    roles: Optional[RoleList] = None


class GroupAccountUpdate(BaseModel):
    """Properties for modifying a group account (POST)."""

    roles: Optional[RoleList] = None
    allowNamespaceManagement: Optional[bool] = None


class GroupAccountResponse(BaseModel):
    """Full group account response."""

    model_config = {"extra": "allow"}

    groupname: Optional[str] = None
    externalGroupID: Optional[str] = None
    roles: Optional[RoleList] = None
    allowNamespaceManagement: Optional[bool] = None


class GroupAccountList(BaseModel):
    """List of group names."""

    model_config = {"extra": "allow"}

    groupname: Optional[List[str]] = None
