"""Namespace resource models."""

from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, List

from .common import (
    AclsUsage,
    HashScheme,
    OptimizedFor,
    DirectoryUsage,
    RetentionType,
    CustomMetadataChanges,
    ReplicationCollisionAction,
    CaseForcing,
    EmailFormat,
    IpSettings,
    PermissionList,
    TagList,
    VersioningSettings,
)


# ── Namespace ──────────────────────────────────────────────────────────


class NamespaceCreate(BaseModel):
    """Properties for creating a namespace (PUT)."""

    name: str
    description: Optional[str] = None
    hashScheme: Optional[HashScheme] = None
    enterpriseMode: Optional[bool] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    servicePlan: Optional[str] = None
    optimizedFor: Optional[OptimizedFor] = None
    directoryUsage: Optional[DirectoryUsage] = None
    aclsUsage: Optional[AclsUsage] = None
    owner: Optional[str] = None
    ownerType: Optional[str] = None
    searchEnabled: Optional[bool] = None
    indexingEnabled: Optional[bool] = None
    indexingDefault: Optional[bool] = None
    customMetadataIndexingEnabled: Optional[bool] = None
    customMetadataValidationEnabled: Optional[bool] = None
    replicationEnabled: Optional[bool] = None
    readFromReplica: Optional[bool] = None
    allowErasureCoding: Optional[bool] = None
    serviceRemoteSystemRequests: Optional[bool] = None
    appendEnabled: Optional[bool] = None
    atimeSynchronizationEnabled: Optional[bool] = None
    allowPermissionAndOwnershipChanges: Optional[bool] = None
    authUsersAlwaysGrantedAllPermissions: Optional[bool] = None
    authMinimumPermissions: Optional[PermissionList] = None
    authAndAnonymousMinimumPermissions: Optional[PermissionList] = None
    multipartUploadAutoAbortDays: Optional[int] = None
    retentionType: Optional[RetentionType] = None
    s3UnversionedOverwrite: Optional[bool] = None
    versioningSettings: Optional[VersioningSettings] = None
    tags: Optional[TagList] = None


class NamespaceUpdate(BaseModel):
    """Properties for modifying a namespace (POST)."""

    name: Optional[str] = None
    description: Optional[str] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    servicePlan: Optional[str] = None
    optimizedFor: Optional[OptimizedFor] = None
    aclsUsage: Optional[AclsUsage] = None
    owner: Optional[str] = None
    ownerType: Optional[str] = None
    searchEnabled: Optional[bool] = None
    indexingEnabled: Optional[bool] = None
    indexingDefault: Optional[bool] = None
    customMetadataIndexingEnabled: Optional[bool] = None
    customMetadataValidationEnabled: Optional[bool] = None
    replicationEnabled: Optional[bool] = None
    readFromReplica: Optional[bool] = None
    allowErasureCoding: Optional[bool] = None
    serviceRemoteSystemRequests: Optional[bool] = None
    appendEnabled: Optional[bool] = None
    atimeSynchronizationEnabled: Optional[bool] = None
    allowPermissionAndOwnershipChanges: Optional[bool] = None
    authUsersAlwaysGrantedAllPermissions: Optional[bool] = None
    authMinimumPermissions: Optional[PermissionList] = None
    authAndAnonymousMinimumPermissions: Optional[PermissionList] = None
    multipartUploadAutoAbortDays: Optional[int] = None
    retentionType: Optional[RetentionType] = None
    s3UnversionedOverwrite: Optional[bool] = None
    tags: Optional[TagList] = None


class NamespaceResponse(BaseModel):
    """Full namespace response from MAPI."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    description: Optional[str] = None
    hashScheme: Optional[HashScheme] = None
    enterpriseMode: Optional[bool] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    servicePlan: Optional[str] = None
    optimizedFor: Optional[OptimizedFor] = None
    directoryUsage: Optional[DirectoryUsage] = None
    aclsUsage: Optional[AclsUsage] = None
    owner: Optional[str] = None
    ownerType: Optional[str] = None
    searchEnabled: Optional[bool] = None
    indexingEnabled: Optional[bool] = None
    customMetadataIndexingEnabled: Optional[bool] = None
    replicationEnabled: Optional[bool] = None
    readFromReplica: Optional[bool] = None
    allowErasureCoding: Optional[bool] = None
    serviceRemoteSystemRequests: Optional[bool] = None
    versioningSettings: Optional[VersioningSettings] = None
    tags: Optional[TagList] = None
    creationTime: Optional[str] = None
    id: Optional[str] = None


class NamespaceList(BaseModel):
    """List of namespace names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None


# ── Compliance Settings ────────────────────────────────────────────────


class ComplianceSettings(BaseModel):
    """Namespace compliance settings."""

    model_config = {"extra": "allow"}

    retentionDefault: Optional[str] = None
    minimumRetentionAfterInitialUnspecified: Optional[str] = None
    shreddingDefault: Optional[bool] = None
    customMetadataChanges: Optional[CustomMetadataChanges] = None
    dispositionEnabled: Optional[bool] = None


# ── Custom Metadata Indexing ───────────────────────────────────────────


class CustomMetadataIndexingSettings(BaseModel):
    """Namespace custom metadata indexing settings."""

    model_config = {"extra": "allow"}

    contentClasses: Optional[List[str]] = None
    fullIndexingEnabled: Optional[bool] = None
    excludedAnnotations: Optional[str] = None


# ── Replication Collision Settings ─────────────────────────────────────


class ReplicationCollisionSettings(BaseModel):
    """Namespace replication collision handling settings."""

    model_config = {"extra": "allow"}

    action: Optional[ReplicationCollisionAction] = None
    deleteDays: Optional[int] = None
    deleteEnabled: Optional[bool] = None


# ── Protocol Settings ──────────────────────────────────────────────────


class HttpProtocol(BaseModel):
    """HTTP/REST/S3/WebDAV protocol settings for a namespace."""

    model_config = {"extra": "allow"}

    httpsEnabled: Optional[bool] = None
    httpEnabled: Optional[bool] = None
    restEnabled: Optional[bool] = None
    restRequiresAuthentication: Optional[bool] = None
    hs3Enabled: Optional[bool] = None
    hs3RequiresAuthentication: Optional[bool] = None
    httpActiveDirectorySSOEnabled: Optional[bool] = None
    webdavEnabled: Optional[bool] = None
    webdavBasicAuthEnabled: Optional[bool] = None
    webdavBasicAuthUsername: Optional[str] = None
    webdavBasicAuthPassword: Optional[str] = None
    webdavCustomMetadata: Optional[bool] = None
    ipSettings: Optional[IpSettings] = None


class CifsProtocol(BaseModel):
    """CIFS protocol settings for a namespace."""

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    caseForcing: Optional[CaseForcing] = None
    caseSensitive: Optional[bool] = None
    requiresAuthentication: Optional[bool] = None
    ipSettings: Optional[IpSettings] = None


class NfsProtocol(BaseModel):
    """NFS protocol settings for a namespace."""

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    ipSettings: Optional[IpSettings] = None


class SmtpProtocol(BaseModel):
    """SMTP protocol settings for a namespace."""

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    emailFormat: Optional[EmailFormat] = None
    emailLocation: Optional[str] = None
    separateAttachments: Optional[bool] = None
    ipSettings: Optional[IpSettings] = None


class Protocols(BaseModel):
    """Default namespace protocol settings (legacy)."""

    model_config = {"extra": "allow"}

    httpEnabled: Optional[bool] = None
    httpsEnabled: Optional[bool] = None
    cifsEnabled: Optional[bool] = None
    nfsEnabled: Optional[bool] = None
    smtpEnabled: Optional[bool] = None
    ipSettings: Optional[IpSettings] = None


# ── CORS ───────────────────────────────────────────────────────────────


class CorsConfiguration(BaseModel):
    """CORS rules configuration (raw XML string in CDATA)."""

    model_config = {"extra": "allow"}

    cors: Optional[str] = None


# ── Namespace Defaults ─────────────────────────────────────────────────


class NamespaceDefaults(BaseModel):
    """Default settings for namespace creation for a tenant."""

    model_config = {"extra": "allow"}

    description: Optional[str] = None
    hashScheme: Optional[HashScheme] = None
    enterpriseMode: Optional[bool] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    servicePlan: Optional[str] = None
    optimizedFor: Optional[str] = None
    directoryUsage: Optional[str] = None
    allowErasureCoding: Optional[bool] = None
    replicationEnabled: Optional[bool] = None
    searchEnabled: Optional[bool] = None
    multipartUploadAutoAbortDays: Optional[int] = None
    retentionType: Optional[RetentionType] = None
    s3UnversionedOverwrite: Optional[bool] = None
    versioningSettings: Optional[VersioningSettings] = None
    dpl: Optional[str] = None
    effectiveDpl: Optional[str] = None
