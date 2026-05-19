"""Common models shared across HCP MAPI resources."""

from __future__ import annotations
from pydantic import BaseModel, field_validator
from typing import Any, Optional, List
from enum import Enum


# ── Enums ──────────────────────────────────────────────────────────────


class Permission(str, Enum):
    BROWSE = "BROWSE"
    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"
    PURGE = "PURGE"
    SEARCH = "SEARCH"
    PRIVILEGED = "PRIVILEGED"
    READ_ACL = "READ_ACL"
    WRITE_ACL = "WRITE_ACL"
    CHOWN = "CHOWN"


class Role(str, Enum):
    ADMINISTRATOR = "ADMINISTRATOR"
    COMPLIANCE = "COMPLIANCE"
    MONITOR = "MONITOR"
    SECURITY = "SECURITY"
    SERVICE = "SERVICE"
    SEARCH = "SEARCH"


class AuthenticationType(str, Enum):
    LOCAL = "LOCAL"
    RADIUS = "RADIUS"
    EXTERNAL = "EXTERNAL"


class HashScheme(str, Enum):
    MD5 = "MD5"
    SHA_1 = "SHA-1"
    SHA_256 = "SHA-256"
    SHA_384 = "SHA-384"
    SHA_512 = "SHA-512"
    RIPEMD_160 = "RIPEMD-160"


class OptimizedFor(str, Enum):
    CLOUD = "CLOUD"
    ALL = "ALL"


class AclsUsage(str, Enum):
    NOT_ENABLED = "NOT_ENABLED"
    ENABLED = "ENABLED"
    ENFORCED = "ENFORCED"
    NOT_ENFORCED = "NOT_ENFORCED"


class DirectoryUsage(str, Enum):
    """HCP returns mixed casing (``Balanced`` or ``BALANCED``)."""

    BALANCED = "Balanced"
    UNBALANCED = "Unbalanced"
    DEFAULT = "Default"

    @classmethod
    def _missing_(cls, value: object) -> DirectoryUsage | None:
        if isinstance(value, str):
            for member in cls:
                if member.value.upper() == value.upper():
                    return member
        return None


class RetentionType(str, Enum):
    HCP = "HCP"
    S3 = "S3"


class CustomMetadataChanges(str, Enum):
    ADD = "ADD"
    ALL = "ALL"
    NONE = "NONE"


class ReplicationCollisionAction(str, Enum):
    MOVE = "MOVE"
    RENAME = "RENAME"


class CaseForcing(str, Enum):
    UPPER = "UPPER"
    LOWER = "LOWER"
    DISABLED = "DISABLED"


class EmailFormat(str, Enum):
    EML = ".eml"
    MBOX = ".mbox"


class Granularity(str, Enum):
    HOUR = "hour"
    DAY = "day"
    TOTAL = "total"


class PerformanceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CUSTOM = "CUSTOM"
    OFF = "OFF"
    NONE = "NONE"


class LinkType(str, Enum):
    ACTIVE_ACTIVE = "ACTIVE_ACTIVE"
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"


class ECTopologyType(str, Enum):
    FULLY_CONNECTED = "FULLY_CONNECTED"
    RING = "RING"


class DownstreamDNSMode(str, Enum):
    BASIC = "BASIC"
    ADVANCED = "ADVANCED"


class LogContent(str, Enum):
    ACCESS = "ACCESS"
    SYSTEM = "SYSTEM"
    SERVICE = "SERVICE"
    APPLICATION = "APPLICATION"


class SupportCredentialType(str, Enum):
    DEFAULT = "Default"
    EXCLUSIVE = "Exclusive"


class RecipientImportance(str, Enum):
    ALL = "ALL"
    MAJOR = "MAJOR"


class RecipientSeverity(str, Enum):
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ── Shared Sub-Models ──────────────────────────────────────────────────


class IpAddressList(BaseModel):
    """HCP wraps IP lists in {"ipAddress": [...]}."""

    ipAddress: Optional[List[str]] = None


class IpSettings(BaseModel):
    """IP address allow/deny configuration.

    HCP MAPI returns addresses wrapped as ``{"ipAddress": [...]}``.
    We normalize to flat ``list[str]`` so the frontend receives plain arrays.
    """

    model_config = {"populate_by_name": True}

    allowAddresses: list[str] | None = None
    denyAddresses: list[str] | None = None
    allowIfInBothLists: bool | None = None

    @field_validator("allowAddresses", "denyAddresses", mode="before")
    @classmethod
    def _normalize_addresses(cls, v: object) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            # HCP wraps as {"ipAddress": ["10.0.0.1", ...]}
            addrs: list[str] = v.get("ipAddress") or []  # type: ignore[assignment]
            return addrs
        if isinstance(v, IpAddressList):
            return v.ipAddress or []
        if isinstance(v, list):
            return [str(x) for x in v]
        return None

    def to_hcp_dict(self) -> dict[str, Any]:
        """Serialize with HCP's ``{"ipAddress": [...]}`` wrapper for POST."""
        d: dict[str, Any] = {}
        if self.allowAddresses is not None:
            d["allowAddresses"] = {"ipAddress": self.allowAddresses}
        if self.denyAddresses is not None:
            d["denyAddresses"] = {"ipAddress": self.denyAddresses}
        if self.allowIfInBothLists is not None:
            d["allowIfInBothLists"] = self.allowIfInBothLists
        return d


def dump_for_hcp(model: BaseModel) -> dict[str, Any]:
    """Serialize a Pydantic model for POST to HCP MAPI.

    Re-wraps any ``ipSettings`` field into HCP's
    ``{"ipAddress": [...]}`` format.
    """
    ip: IpSettings | None = getattr(model, "ipSettings", None)
    if ip is not None:
        d = model.model_dump(exclude_none=True, exclude={"ipSettings"})
        d["ipSettings"] = ip.to_hcp_dict()
    else:
        d = model.model_dump(exclude_none=True)
    return d


class PermissionList(BaseModel):
    """A list of permissions."""

    permission: Optional[List[Permission]] = None


class RoleList(BaseModel):
    """A list of roles."""

    role: Optional[List[Role]] = None


class TagList(BaseModel):
    """A list of tags."""

    tag: Optional[List[str]] = None


class NamespacePermission(BaseModel):
    """Data access permissions for a single namespace."""

    namespaceName: str
    permissions: Optional[PermissionList] = None


class DataAccessPermissions(BaseModel):
    """Data access permissions across namespaces."""

    model_config = {"extra": "allow"}

    namespacePermission: Optional[List[NamespacePermission]] = None


class VersioningSettings(BaseModel):
    """Versioning configuration for a namespace."""

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    prune: Optional[bool] = None
    pruneDays: Optional[int] = None
    pruneOnPrimary: Optional[bool] = None
    pruneOnReplica: Optional[bool] = None
    daysOnPrimary: Optional[int] = None
    daysOnReplica: Optional[int] = None
    useDeleteMarkers: Optional[bool] = None
    keepDeletionRecords: Optional[bool] = None


# ── Query Parameter Models ─────────────────────────────────────────────


class ListQueryParams(BaseModel):
    """Combined query parameters for list operations."""

    offset: Optional[int] = None
    count: Optional[int] = None
    sortType: Optional[str] = None
    sortOrder: Optional[str] = None
    filterType: Optional[str] = None
    filterString: Optional[str] = None
    verbose: Optional[bool] = False
    prettyprint: Optional[bool] = False

    def to_query(self, **extra: Any) -> dict | None:
        """Build query dict from non-None fields, plus any extra params."""
        q: dict = {}
        if self.offset is not None:
            q["offset"] = self.offset
        if self.count is not None:
            q["count"] = self.count
        if self.sortType:
            q["sortType"] = self.sortType
        if self.sortOrder:
            q["sortOrder"] = self.sortOrder
        if self.filterType:
            q["filterType"] = self.filterType
        if self.filterString:
            q["filterString"] = self.filterString
        q.update(extra)
        return q or None


class ChargebackParams(BaseModel):
    """Query parameters for chargeback reports."""

    start: Optional[str] = None
    end: Optional[str] = None
    granularity: Optional[Granularity] = Granularity.TOTAL


# ── Generic Response Models ───────────────────────────────────────────


class StatusResponse(BaseModel):
    """Standard response for mutation operations."""

    model_config = {"extra": "allow"}

    status: str


class TokenResponse(BaseModel):
    """OAuth2 token response."""

    access_token: str
    token_type: str


class PermissionsResponse(BaseModel):
    """Namespace or tenant permissions from MAPI."""

    model_config = {"extra": "allow"}
