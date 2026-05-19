"""Tenant resource models."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List

from .common import (
    IpSettings,
    RecipientImportance,
    RecipientSeverity,
)


# ── Tenant ─────────────────────────────────────────────────────────────


class TenantCreate(BaseModel):
    """Properties for creating an HCP tenant (PUT)."""

    name: str
    systemVisibleDescription: Optional[str] = None
    tenantVisibleDescription: Optional[str] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    namespaceQuota: Optional[str] = None
    authenticationTypes: Optional[dict] = None
    complianceConfigurationEnabled: Optional[bool] = None
    versioningConfigurationEnabled: Optional[bool] = None
    searchConfigurationEnabled: Optional[bool] = None
    replicationConfigurationEnabled: Optional[bool] = None
    erasureCodingSelectionEnabled: Optional[bool] = None
    servicePlanSelectionEnabled: Optional[bool] = None
    servicePlan: Optional[str] = None
    dataNetwork: Optional[str] = None
    managementNetwork: Optional[str] = None
    tags: Optional[dict] = None


class TenantUpdate(BaseModel):
    """Properties for modifying a tenant (POST)."""

    administrationAllowed: Optional[bool] = None
    maxNamespacesPerUser: Optional[int] = None
    snmpLoggingEnabled: Optional[bool] = None
    syslogLoggingEnabled: Optional[bool] = None
    tenantVisibleDescription: Optional[str] = None
    tags: Optional[dict] = None


# ── Console Security ──────────────────────────────────────────────────


class ConsoleSecurity(BaseModel):
    """Tenant Management Console configuration."""

    model_config = {"extra": "allow"}

    automaticUserAccountUnlockSetting: Optional[bool] = None
    automaticUserAccoutUnlockDuration: Optional[int] = None
    blockCommonPassword: Optional[bool] = None
    blockPasswordReUse: Optional[bool] = None
    coolDownPeriodDuration: Optional[int] = None
    coolDownPeriodSettings: Optional[bool] = None
    disableAfterAttempts: Optional[int] = None
    disableAfterInactiveDays: Optional[int] = None
    forcePasswordChangeDays: Optional[int] = None
    ipSettings: Optional[IpSettings] = None
    loginMessage: Optional[str] = None
    logoutOnInactive: Optional[int] = None
    minimumPasswordLength: Optional[int] = None
    lowerCaseLetterCount: Optional[int] = None
    upperCaseLetterCount: Optional[int] = None
    numericCharacterCount: Optional[int] = None
    specialCharacterCount: Optional[int] = None
    passwordCombination: Optional[bool] = None
    passwordContainsUsername: Optional[bool] = None
    passwordReuseDepth: Optional[int] = None


# ── Contact Info ──────────────────────────────────────────────────────


class ContactInfo(BaseModel):
    """Tenant contact information."""

    model_config = {"extra": "allow"}

    firstName: Optional[str] = None
    lastName: Optional[str] = None
    emailAddress: Optional[str] = None
    primaryPhone: Optional[str] = None
    extension: Optional[str] = None
    address1: Optional[str] = None
    address2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipOrPostalCode: Optional[str] = None
    countryOrRegion: Optional[str] = None


# ── Email Notification ────────────────────────────────────────────────


class EmailTemplate(BaseModel):
    """Email notification template."""

    model_config = {"populate_by_name": True}

    from_: Optional[str] = Field(None, alias="from")
    subject: Optional[str] = None
    body: Optional[str] = None


class Recipient(BaseModel):
    """Email notification recipient."""

    address: str
    importance: Optional[RecipientImportance] = RecipientImportance.MAJOR
    severity: Optional[RecipientSeverity] = RecipientSeverity.ERROR
    type: Optional[str] = "GENERAL"


class RecipientList(BaseModel):
    """HCP-style wrapper: ``{"recipient": [...]}``."""

    recipient: Optional[List[Recipient]] = None


class EmailNotification(BaseModel):
    """Email notification configuration for a tenant."""

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    emailTemplate: Optional[EmailTemplate] = None
    recipients: Optional[RecipientList] = None


# ── Search Security ──────────────────────────────────────────────────


class SearchSecurity(BaseModel):
    """Search Console configuration for a tenant."""

    model_config = {"extra": "allow"}

    ipSettings: Optional[IpSettings] = None


# ── Available Service Plans ──────────────────────────────────────────


class AvailableServicePlan(BaseModel):
    """Service plan available for tenant namespace assignment."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    description: Optional[str] = None


# ── Chargeback ───────────────────────────────────────────────────────


class TenantResponse(BaseModel):
    """Full tenant response from MAPI."""

    model_config = {"extra": "allow"}

    name: Optional[str] = None
    systemVisibleDescription: Optional[str] = None
    tenantVisibleDescription: Optional[str] = None
    hardQuota: Optional[str] = None
    softQuota: Optional[int] = None
    namespaceQuota: Optional[str] = None
    authenticationTypes: Optional[dict] = None
    complianceConfigurationEnabled: Optional[bool] = None
    versioningConfigurationEnabled: Optional[bool] = None
    searchConfigurationEnabled: Optional[bool] = None
    replicationConfigurationEnabled: Optional[bool] = None
    erasureCodingSelectionEnabled: Optional[bool] = None
    servicePlanSelectionEnabled: Optional[bool] = None
    servicePlan: Optional[str] = None
    administrationAllowed: Optional[bool] = None
    maxNamespacesPerUser: Optional[int] = None
    snmpLoggingEnabled: Optional[bool] = None
    syslogLoggingEnabled: Optional[bool] = None
    tags: Optional[dict] = None


class TenantList(BaseModel):
    """List of tenant names."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None


class AvailableServicePlanList(BaseModel):
    """List of available service plans for a tenant."""

    model_config = {"extra": "allow"}

    name: Optional[List[str]] = None


# ── Chargeback ───────────────────────────────────────────────────────


class ChargebackData(BaseModel):
    """Chargeback statistics for a namespace or tenant."""

    systemName: Optional[str] = None
    tenantName: Optional[str] = None
    namespaceName: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    objectCount: Optional[int] = None
    ingestedVolume: Optional[int] = None
    storageCapacityUsed: Optional[int] = None
    bytesIn: Optional[int] = None
    bytesOut: Optional[int] = None
    reads: Optional[int] = None
    writes: Optional[int] = None
    deletes: Optional[int] = None
    multipartObjects: Optional[int] = None
    multipartObjectParts: Optional[int] = None
    multipartObjectBytes: Optional[int] = None
    multipartUploads: Optional[int] = None
    multipartUploadParts: Optional[int] = None
    multipartUploadBytes: Optional[int] = None
    deleted: Optional[str] = None
    valid: Optional[bool] = None


class ChargebackReport(BaseModel):
    """Chargeback report containing one or more data sets."""

    model_config = {"extra": "allow"}

    chargebackData: Optional[List[ChargebackData]] = None
