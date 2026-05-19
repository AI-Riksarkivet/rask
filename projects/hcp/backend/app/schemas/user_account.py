"""Tenant-level and system-level user account models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List
from .common import RoleList


class UserAccountCreate(BaseModel):
    """Properties for creating a user account (PUT)."""

    username: str
    fullName: str
    localAuthentication: bool
    enabled: bool
    forcePasswordChange: bool
    description: Optional[str] = None
    roles: Optional[RoleList] = None
    allowNamespaceManagement: Optional[bool] = None


class UserAccountUpdate(BaseModel):
    """Properties for modifying a user account (POST)."""

    fullName: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    forcePasswordChange: Optional[bool] = None
    roles: Optional[RoleList] = None
    allowNamespaceManagement: Optional[bool] = None


class UserAccountResponse(BaseModel):
    """Full user account response."""

    model_config = {"extra": "allow"}

    username: Optional[str] = None
    fullName: Optional[str] = None
    description: Optional[str] = None
    localAuthentication: Optional[bool] = None
    enabled: Optional[bool] = None
    forcePasswordChange: Optional[bool] = None
    roles: Optional[RoleList] = None
    allowNamespaceManagement: Optional[bool] = None
    userGUID: Optional[str] = None
    userID: Optional[int] = None


class UserAccountList(BaseModel):
    """List of usernames."""

    model_config = {"extra": "allow"}

    username: Optional[List[str]] = None


class UpdatePasswordRequest(BaseModel):
    """Request body for changing a password."""

    newPassword: str
    oldPassword: Optional[str] = None
