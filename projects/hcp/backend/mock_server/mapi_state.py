"""Stateful MAPI mock — generic helpers, state class, and URL dispatcher."""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

import httpx
import respx
from httpx import Response as HttpxResponse

from .fixtures import (
    AVAILABLE_SERVICE_PLANS,
    CONTENT_CLASSES,
    EC_TOPOLOGIES,
    EC_TOPOLOGY_TENANTS,
    GROUP_ACCOUNTS,
    MOCK_QUERY_OBJECTS,
    MOCK_QUERY_OPERATIONS,
    NAMESPACES,
    NS_CHARGEBACK,
    NS_STATISTICS,
    REPLICATION_CERTIFICATES,
    REPLICATION_LINK_CONTENT,
    REPLICATION_LINK_SCHEDULES,
    REPLICATION_LINKS,
    REPLICATION_SERVICE,
    RETENTION_CLASSES,
    SYSTEM_GROUP_ACCOUNTS,
    SYSTEM_HEALTH_STATUS,
    SYSTEM_LICENSES,
    SYSTEM_LOG_STATUS,
    SYSTEM_NETWORK,
    SYSTEM_NODE_STATISTICS,
    SYSTEM_SERVICE_STATISTICS,
    SYSTEM_SUPPORT_CREDENTIALS,
    SYSTEM_USER_ACCOUNTS,
    TENANT_CHARGEBACK,
    TENANT_STATISTICS,
    TENANTS,
    USER_ACCOUNTS,
    default_ns_settings,
    default_tenant_settings,
)

logger = logging.getLogger("mock_server.mapi")

# ── Response helpers ─────────────────────────────────────────────────


MOCK_HCP_VERSION = "9.7.0"

_HCP_HEADERS = {"X-HCP-SoftwareVersion": MOCK_HCP_VERSION}


def _json(data: Any, status: int = 200) -> HttpxResponse:
    return HttpxResponse(status_code=status, json=data, headers=_HCP_HEADERS)


def _empty(status: int = 200) -> HttpxResponse:
    return HttpxResponse(status_code=status, headers=_HCP_HEADERS)


def _not_found() -> HttpxResponse:
    return _json({"message": "Not found"}, 404)


def _not_allowed() -> HttpxResponse:
    return _json({}, 405)


# ── Generic dispatch helpers ─────────────────────────────────────────


def _crud(
    store: dict | None,
    method: str,
    name: str | None,
    body: dict,
    name_field: str = "name",
    verbose: bool = False,
) -> HttpxResponse:
    """Handle standard CRUD on a ``{name: data}`` dict.

    *name=None* → collection-level (GET list, PUT create, POST bulk).
    *name=str*  → item-level (GET, HEAD, POST modify, DELETE).
    """
    if store is None:
        return _not_found()

    # ── Collection level ──
    if name is None:
        if method == "GET":
            if verbose:
                return _json(list(store.values()))
            return _json({name_field: list(store.keys())})
        if method == "PUT":
            item_name = body.get(name_field, "")
            if item_name in store:
                return _json({"message": f"{item_name} already exists"}, 409)
            body[name_field] = item_name
            store[item_name] = body
            return _empty()
        if method == "POST":
            return _empty()  # bulk operations (e.g. resetPasswords)
        return _not_allowed()

    # ── Item level ──
    if method == "GET":
        return _json(store[name]) if name in store else _not_found()
    if method == "HEAD":
        return _empty(200 if name in store else 404)
    if method == "POST":
        if name not in store:
            return _not_found()
        store[name].update(body)
        return _empty()
    if method == "DELETE":
        if name not in store:
            return _not_found()
        del store[name]
        return _empty()
    return _not_allowed()


def _setting(settings: dict, key: str, method: str, body: dict) -> HttpxResponse:
    """Handle GET/POST on a settings sub-resource."""
    if method == "GET":
        return _json(settings.get(key, {}))
    if method == "POST":
        settings[key] = body
        return _empty()
    return _not_allowed()


def _cors(settings: dict, method: str, body: dict) -> HttpxResponse:
    """Handle GET/PUT/DELETE on a CORS sub-resource."""
    if method == "GET":
        return _json(settings.get("cors", {}))
    if method == "PUT":
        settings["cors"] = body
        return _empty()
    if method == "DELETE":
        settings["cors"] = {"corsConfiguration": []}
        return _empty()
    return _not_allowed()


def _protocol(
    settings: dict, method: str, protocol: str | None, body: dict
) -> HttpxResponse:
    """Handle GET/POST on protocol settings (default or named).

    Depth-5 GET (/protocols) returns a merged view of all protocol flags.
    Depth-6 GET/POST (/protocols/{proto}) reads/writes a specific protocol.
    """
    protocols = settings.setdefault("protocols", {})
    if protocol is None:
        # Depth-5: summary of all protocol flags
        if method == "GET":
            merged: dict[str, object] = {}
            for v in protocols.values():
                if isinstance(v, dict):
                    merged.update(v)
            return _json(merged)
        return _not_allowed()
    # Depth-6: specific protocol
    if method == "GET":
        return _json(protocols.get(protocol, {}))
    if method == "POST":
        existing = protocols.get(protocol, {})
        existing.update(body)
        protocols[protocol] = existing
        return _empty()
    return _not_allowed()


def _static_get(data: dict, method: str) -> HttpxResponse:
    """Handle a read-only GET endpoint."""
    return _json(data) if method == "GET" else _not_allowed()


# ── Resource & settings registries ───────────────────────────────────

# CRUD resources under /tenants/{T}/{resource}  →  (state_attr, name_field)
CRUD_RESOURCES: dict[str, tuple[str, str]] = {
    "userAccounts": ("user_accounts", "username"),
    "groupAccounts": ("group_accounts", "groupname"),
    "contentClasses": ("content_classes", "name"),
}

# Which CRUD resources support account sub-resources (depth 5)
ACCOUNT_TYPES: dict[str, str] = {
    "userAccounts": "user",
    "groupAccounts": "group",
}

TENANT_SETTING_KEYS = frozenset(
    {
        "consoleSecurity",
        "contactInfo",
        "emailNotification",
        "namespaceDefaults",
        "searchSecurity",
        "permissions",
    }
)

NS_SETTING_KEYS = frozenset(
    {
        "complianceSettings",
        "permissions",
        "customMetadataIndexingSettings",
        "replicationCollisionSettings",
    }
)


# ── MockMapiState ────────────────────────────────────────────────────


class MockMapiState:
    """In-memory state for all MAPI resources.

    Only contains lifecycle methods (create/delete) that initialise or
    clean up sub-collections.  Generic CRUD is handled by standalone
    helpers above.
    """

    def __init__(self) -> None:
        self.tenants: dict[str, dict] = {}
        self.namespaces: dict[str, dict[str, dict]] = {}
        self.user_accounts: dict[str, dict[str, dict]] = {}
        self.group_accounts: dict[str, dict[str, dict]] = {}
        self.content_classes: dict[str, dict[str, dict]] = {}
        self.retention_classes: dict[tuple, dict[str, dict]] = {}
        self.tenant_settings: dict[str, dict[str, dict]] = {}
        self.ns_settings: dict[tuple, dict[str, dict]] = {}
        self.data_access_perms: dict[tuple, dict] = {}
        # System-level state (not tenant-scoped)
        self.system_network: dict = {}
        self.system_licenses: list[dict] = []
        self.system_user_accounts: dict[str, dict] = {}
        self.system_group_accounts: dict[str, dict] = {}
        self.system_support_credentials: dict = {}
        self.system_log_status: dict = {}
        self.system_health_status: dict = {}
        self.replication_service: dict = {}
        self.replication_certificates: list[dict] = []
        self.replication_links: dict[str, dict] = {}
        self.replication_link_content: dict[str, dict] = {}
        self.replication_link_schedules: dict[str, dict] = {}
        self.ec_topologies: dict[str, dict] = {}
        self.ec_topology_tenants: dict[str, list[dict]] = {}
        # Cross-reference to S3 service for namespace ↔ bucket sync
        self._s3_service: Any = None

    # ── Lazy initialisation ────────────────────────────────────

    def ensure_tenant(self, name: str) -> None:
        """Ensure all sub-stores exist for *name* (no-op if already present)."""
        self.tenants.setdefault(name, {"name": name})
        self.namespaces.setdefault(name, {})
        self.user_accounts.setdefault(name, {})
        self.group_accounts.setdefault(name, {})
        self.content_classes.setdefault(name, {})
        self.tenant_settings.setdefault(name, default_tenant_settings())

    # ── Tenant lifecycle ─────────────────────────────────────────

    def create_tenant(self, name: str, body: dict) -> HttpxResponse:
        if name in self.tenants:
            return _json({"message": f"{name} already exists"}, 409)
        body["name"] = name
        self.tenants[name] = body
        self.namespaces.setdefault(name, {})
        self.user_accounts.setdefault(name, {})
        self.group_accounts.setdefault(name, {})
        self.content_classes.setdefault(name, {})
        self.tenant_settings.setdefault(name, default_tenant_settings())
        return _empty()

    def delete_tenant(self, name: str) -> HttpxResponse:
        if name not in self.tenants:
            return _not_found()
        for ns in list(self.namespaces.get(name, {})):
            self.retention_classes.pop((name, ns), None)
            self.ns_settings.pop((name, ns), None)
        self.tenants.pop(name)
        self.namespaces.pop(name, None)
        self.user_accounts.pop(name, None)
        self.group_accounts.pop(name, None)
        self.content_classes.pop(name, None)
        self.tenant_settings.pop(name, None)
        for key in [k for k in self.data_access_perms if k[0] == name]:
            del self.data_access_perms[key]
        return _empty()

    # ── Namespace lifecycle ──────────────────────────────────────

    def create_namespace(self, tenant: str, name: str, body: dict) -> HttpxResponse:
        ns_map = self.namespaces.get(tenant)
        if ns_map is None:
            return _not_found()
        if name in ns_map:
            return _json({"message": f"{name} already exists"}, 409)
        body["name"] = name
        ns_map[name] = body
        self.retention_classes.setdefault((tenant, name), {})
        self.ns_settings.setdefault((tenant, name), default_ns_settings())
        # Grant the creating user data-access (mirrors S3 create_bucket behaviour)
        self._grant_user_ns_access(tenant, "admin", name)
        # Sync: also create as S3 bucket
        self._sync_create_bucket(name)
        return _empty()

    def delete_namespace(self, tenant: str, name: str) -> HttpxResponse:
        ns_map = self.namespaces.get(tenant)
        if ns_map is None or name not in ns_map:
            return _not_found()
        del ns_map[name]
        self.retention_classes.pop((tenant, name), None)
        self.ns_settings.pop((tenant, name), None)
        # Sync: also remove S3 bucket
        self._sync_delete_bucket(name)
        return _empty()

    # ── Auto-grant helpers ─────────────────────────────────────────────

    _ALL_PERMS = ["BROWSE", "READ", "READ_ACL", "WRITE", "WRITE_ACL", "DELETE"]

    def _grant_user_ns_access(self, tenant: str, username: str, ns_name: str) -> None:
        """Grant a single user full data access to a namespace.

        Per HCP S3 docs, creating a bucket grants the *creator* browse,
        read, readACL, write, writeACL, and delete permissions.
        """
        key = (tenant, "user", username)
        perms = self.data_access_perms.get(key, {})
        ns_perms = perms.get("namespacePermission", [])
        if any(p.get("namespaceName") == ns_name for p in ns_perms):
            return
        ns_perms.append(
            {
                "namespaceName": ns_name,
                "permissions": {"permission": list(self._ALL_PERMS)},
            }
        )
        perms["namespacePermission"] = ns_perms
        self.data_access_perms[key] = perms

    # ── Namespace ↔ bucket sync helpers ─────────────────────────────────

    def _sync_create_bucket(self, name: str) -> None:
        """Create an S3 bucket when a namespace is created via MAPI."""
        s3 = self._s3_service
        if s3 is None:
            return
        if name not in s3._buckets:
            from datetime import datetime, timezone

            s3._buckets[name] = {"CreationDate": datetime.now(timezone.utc).isoformat()}
            s3._objects[name] = {}

    def _sync_delete_bucket(self, name: str) -> None:
        """Remove the S3 bucket when a namespace is deleted via MAPI."""
        s3 = self._s3_service
        if s3 is None:
            return
        s3._buckets.pop(name, None)
        s3._objects.pop(name, None)
        s3._versioning.pop(name, None)
        s3._bucket_acls.pop(name, None)
        s3._bucket_cors.pop(name, None)

    def _sync_rename_bucket(self, old_name: str, new_name: str, tenant: str) -> None:
        """Rename an S3 bucket and update data access permissions."""
        s3 = self._s3_service
        if s3 is not None:
            if old_name in s3._buckets:
                s3._buckets[new_name] = s3._buckets.pop(old_name)
            if old_name in s3._objects:
                s3._objects[new_name] = s3._objects.pop(old_name)
            if old_name in s3._versioning:
                s3._versioning[new_name] = s3._versioning.pop(old_name)
            if old_name in s3._bucket_acls:
                s3._bucket_acls[new_name] = s3._bucket_acls.pop(old_name)
            if old_name in s3._bucket_cors:
                s3._bucket_cors[new_name] = s3._bucket_cors.pop(old_name)
            # Re-key object ACLs
            for key in [k for k in s3._object_acls if k[0] == old_name]:
                s3._object_acls[(new_name, key[1])] = s3._object_acls.pop(key)
            # Re-key multipart uploads
            for uid, upload in s3._multipart_uploads.items():
                if upload.get("bucket") == old_name:
                    upload["bucket"] = new_name

        # Update data access permissions — rename namespaceName references
        for key, perms in self.data_access_perms.items():
            if key[0] != tenant:
                continue
            ns_perms = perms.get("namespacePermission", [])
            for entry in ns_perms:
                if entry.get("namespaceName") == old_name:
                    entry["namespaceName"] = new_name


# ── Seed ─────────────────────────────────────────────────────────────


def seed_mapi_state(state: MockMapiState) -> None:
    """Copy fixture data into the state and initialise default settings."""
    state.tenants = copy.deepcopy(TENANTS)
    state.namespaces = copy.deepcopy(NAMESPACES)
    state.user_accounts = copy.deepcopy(USER_ACCOUNTS)
    state.group_accounts = copy.deepcopy(GROUP_ACCOUNTS)
    state.content_classes = copy.deepcopy(CONTENT_CLASSES)
    state.retention_classes = copy.deepcopy(RETENTION_CLASSES)
    state.system_network = copy.deepcopy(SYSTEM_NETWORK)
    state.system_licenses = copy.deepcopy(SYSTEM_LICENSES)
    state.system_user_accounts = copy.deepcopy(SYSTEM_USER_ACCOUNTS)
    state.system_group_accounts = copy.deepcopy(SYSTEM_GROUP_ACCOUNTS)
    state.system_support_credentials = copy.deepcopy(SYSTEM_SUPPORT_CREDENTIALS)
    state.system_log_status = copy.deepcopy(SYSTEM_LOG_STATUS)
    state.system_health_status = copy.deepcopy(SYSTEM_HEALTH_STATUS)
    state.replication_service = copy.deepcopy(REPLICATION_SERVICE)
    state.replication_certificates = copy.deepcopy(REPLICATION_CERTIFICATES)
    state.replication_links = copy.deepcopy(REPLICATION_LINKS)
    state.replication_link_content = copy.deepcopy(REPLICATION_LINK_CONTENT)
    state.replication_link_schedules = copy.deepcopy(REPLICATION_LINK_SCHEDULES)
    state.ec_topologies = copy.deepcopy(EC_TOPOLOGIES)
    state.ec_topology_tenants = copy.deepcopy(EC_TOPOLOGY_TENANTS)
    for tenant in TENANTS:
        state.tenant_settings[tenant] = default_tenant_settings()
    for tenant, ns_map in NAMESPACES.items():
        for ns in ns_map:
            state.ns_settings[(tenant, ns)] = default_ns_settings()
    # Seed: grant all users access to all namespaces for dev convenience
    for tenant_name in state.namespaces:
        users = state.user_accounts.get(tenant_name, {})
        for ns_name in state.namespaces[tenant_name]:
            for username in users:
                state._grant_user_ns_access(tenant_name, username, ns_name)

    # Sync: ensure every MAPI namespace also exists as an S3 bucket
    if state._s3_service is not None:
        for ns_map in state.namespaces.values():
            for ns_name in ns_map:
                state._sync_create_bucket(ns_name)


# ── Sub-resource handlers ────────────────────────────────────────────


def _handle_account_sub(
    state: MockMapiState,
    tenant: str,
    acct_type: str,
    name: str,
    sub: str,
    method: str,
    body: dict,
) -> HttpxResponse:
    """Handle /tenants/{T}/(user|group)Accounts/{name}/(changePassword|dataAccessPermissions)."""
    if sub == "changePassword" and method == "POST":
        return _empty()
    if sub == "dataAccessPermissions":
        key = (tenant, acct_type, name)
        if method == "GET":
            return _json(state.data_access_perms.get(key, {}))
        if method == "POST":
            state.data_access_perms[key] = body
            return _empty()
    return _not_found()


def _export_ns(state: MockMapiState, tenant: str, ns_name: str) -> dict[str, Any]:
    """Build an export template for a single namespace."""
    ns_map = state.namespaces.get(tenant, {})
    ns_data = ns_map.get(ns_name, {})
    ns_settings = state.ns_settings.get((tenant, ns_name), {})
    rc_store = state.retention_classes.get((tenant, ns_name), {})

    config: dict[str, Any] = dict(ns_data)

    if "versioningSettings" in ns_settings:
        config["versioning"] = ns_settings["versioningSettings"]
    if "complianceSettings" in ns_settings:
        config["compliance"] = ns_settings["complianceSettings"]
    if "permissions" in ns_settings:
        config["permissions"] = ns_settings["permissions"]

    protocols: dict[str, Any] = {}
    proto_store = ns_settings.get("protocols", {})
    for proto in ("http", "cifs", "nfs", "smtp"):
        if proto in proto_store:
            protocols[proto] = proto_store[proto]
    if protocols:
        config["protocols"] = protocols

    if rc_store:
        config["retentionClasses"] = list(rc_store.values())
    if "customMetadataIndexingSettings" in ns_settings:
        config["indexing"] = ns_settings["customMetadataIndexingSettings"]
    if "cors" in ns_settings:
        config["cors"] = ns_settings["cors"]
    if "replicationCollisionSettings" in ns_settings:
        config["replicationCollision"] = ns_settings["replicationCollisionSettings"]

    return config


def _handle_namespaces(
    state: MockMapiState,
    tenant: str,
    method: str,
    segments: list[str],
    body: dict,
    query_params: dict[str, str] | None = None,
) -> HttpxResponse:
    """Handle all /tenants/{T}/namespaces/... routes."""
    n = len(segments)
    ns_map = state.namespaces.get(tenant)
    if ns_map is None:
        return _not_found()

    # /tenants/{T}/namespaces
    if n == 3:
        if method == "PUT":
            return state.create_namespace(tenant, body.get("name", ""), body)
        if method == "GET":
            return _json({"name": list(ns_map.keys())})
        return _not_allowed()

    ns_name = segments[3]

    # /tenants/{T}/namespaces/export  (bulk export)
    if ns_name == "export" and n == 4 and method == "GET":
        from datetime import datetime, timezone

        names_str = (query_params or {}).get("names", "")
        names = [x.strip() for x in names_str.split(",") if x.strip()]
        configs = [_export_ns(state, tenant, ns) for ns in names if ns in ns_map]
        return _json(
            {
                "version": "1.0",
                "exportedAt": datetime.now(timezone.utc).isoformat(),
                "sourceTenant": tenant,
                "namespaces": configs,
            }
        )

    # /tenants/{T}/namespaces/{N}
    if n == 4:
        if method == "DELETE":
            return state.delete_namespace(tenant, ns_name)
        # Handle rename: if POST body contains a new "name", re-key the dict
        if method == "POST" and "name" in body and body["name"] != ns_name:
            if ns_name not in ns_map:
                return _not_found()
            new_name = body["name"]
            data = ns_map.pop(ns_name)
            data.update(body)
            ns_map[new_name] = data
            # Move ns_settings to the new key
            old_key = (tenant, ns_name)
            new_key = (tenant, new_name)
            if old_key in state.ns_settings:
                state.ns_settings[new_key] = state.ns_settings.pop(old_key)
            # Move retention classes
            if old_key in state.retention_classes:
                state.retention_classes[new_key] = state.retention_classes.pop(old_key)
            # Rename S3 bucket and update data access permissions
            state._sync_rename_bucket(ns_name, new_name, tenant)
            return _empty()
        return _crud(ns_map, method, ns_name, body)

    ns_sub = segments[4]

    # /tenants/{T}/namespaces/{N}/export  (single export)
    if ns_sub == "export" and n == 5 and method == "GET":
        from datetime import datetime, timezone

        if ns_name not in ns_map:
            return _not_found()
        config = _export_ns(state, tenant, ns_name)
        return _json(
            {
                "version": "1.0",
                "exportedAt": datetime.now(timezone.utc).isoformat(),
                "sourceTenant": tenant,
                "namespaces": [config],
            }
        )

    ns_settings = state.ns_settings.setdefault((tenant, ns_name), {})

    # retentionClasses — standard CRUD (depth 5–6)
    if ns_sub == "retentionClasses":
        rc_store = state.retention_classes.get((tenant, ns_name))
        rc_name = segments[5] if n >= 6 else None
        return _crud(rc_store, method, rc_name, body) if n <= 6 else _not_found()

    # protocols (depth 5 = default, depth 6 = named protocol)
    if ns_sub == "protocols":
        proto = segments[5] if n == 6 else None
        return _protocol(ns_settings, method, proto, body) if n <= 6 else _not_found()

    # Everything below is depth-5 only
    if n != 5:
        return _not_found()

    if ns_sub == "cors":
        return _cors(ns_settings, method, body)
    if ns_sub == "versioningSettings":
        if method == "DELETE":
            ns_settings.pop("versioningSettings", None)
            return _empty()
        return _setting(ns_settings, "versioningSettings", method, body)
    if ns_sub == "statistics":
        return _static_get(NS_STATISTICS, method)
    if ns_sub == "chargebackReport":
        return _static_get(NS_CHARGEBACK, method)
    if ns_sub in NS_SETTING_KEYS:
        return _setting(ns_settings, ns_sub, method, body)

    return _not_found()


# ── Replication & Erasure Coding handlers ─────────────────────────────


def _handle_replication(
    state: MockMapiState,
    method: str,
    segments: list[str],
    body: dict,
    request: httpx.Request,
) -> HttpxResponse:
    """Handle /services/replication/... routes."""
    n = len(segments)

    # /services/replication (service settings)
    if n == 0:
        if method == "GET":
            return _json(state.replication_service)
        if method == "POST":
            state.replication_service.update(body)
            return _empty()
        return _not_allowed()

    # /services/replication/certificates
    if segments[0] == "certificates":
        if n == 1:
            if method == "GET":
                verbose = request.url.params.get("verbose", "false") == "true"
                if verbose:
                    return _json({"certificate": state.replication_certificates})
                return _json(
                    {
                        "certificate": [
                            c.get("id") for c in state.replication_certificates
                        ]
                    }
                )
            if method == "PUT":
                new_id = f"cert-{len(state.replication_certificates) + 1:03d}"
                state.replication_certificates.append(
                    {
                        "id": new_id,
                        "subjectDN": "CN=uploaded-cert",
                        "validOn": "2026-01-01T00:00:00+0000",
                        "expiresOn": "2029-01-01T00:00:00+0000",
                    }
                )
                return _empty()
            return _not_allowed()
        if n == 2:
            cert_id = segments[1]
            if cert_id == "server":
                if method == "GET":
                    return HttpxResponse(
                        status_code=200,
                        content=b"-----BEGIN CERTIFICATE-----\nMOCK_SERVER_CERT\n-----END CERTIFICATE-----",
                        headers={**_HCP_HEADERS, "Content-Type": "text/plain"},
                    )
                return _not_allowed()
            cert = next(
                (c for c in state.replication_certificates if c.get("id") == cert_id),
                None,
            )
            if method == "GET":
                return _json(cert) if cert else _not_found()
            if method == "DELETE":
                if cert:
                    state.replication_certificates.remove(cert)
                    return _empty()
                return _not_found()
        return _not_found()

    # /services/replication/links
    if segments[0] == "links":
        if n == 1:
            verbose = request.url.params.get("verbose", "false") == "true"
            if method == "GET":
                if verbose:
                    return _json(list(state.replication_links.values()))
                return _json({"name": list(state.replication_links.keys())})
            if method == "PUT":
                link_name = body.get("name", "")
                if link_name in state.replication_links:
                    return _json({"message": f"{link_name} already exists"}, 409)
                body.setdefault(
                    "id", f"link-uuid-{len(state.replication_links) + 1:03d}"
                )
                body.setdefault("status", "ACTIVE")
                body.setdefault("suspended", False)
                body.setdefault("statistics", {})
                state.replication_links[link_name] = body
                state.replication_link_content[link_name] = {
                    "tenants": [],
                    "defaultNamespaceDirectories": [],
                    "chainedLinks": [],
                }
                state.replication_link_schedules[link_name] = {
                    "local": {"scheduleOverride": "NONE", "transition": []},
                    "remote": {"scheduleOverride": "NONE", "transition": []},
                }
                return _empty()
            return _not_allowed()

        link_name = segments[1]
        link = state.replication_links.get(link_name)

        if n == 2:
            if method == "GET":
                return _json(link) if link else _not_found()
            if method == "HEAD":
                return _empty(200 if link else 404)
            if method == "POST":
                if not link:
                    return _not_found()
                # Check for action query params
                params = dict(request.url.params)
                if "suspend" in params:
                    link["suspended"] = True
                    link["status"] = "SUSPENDED"
                    return _empty()
                if "resume" in params:
                    link["suspended"] = False
                    link["status"] = "ACTIVE"
                    return _empty()
                if (
                    "failOver" in params
                    or "failBack" in params
                    or "beginRecover" in params
                    or "completeRecovery" in params
                ):
                    return _empty()
                # Regular update
                link.update(body)
                return _empty()
            if method == "DELETE":
                if not link:
                    return _not_found()
                del state.replication_links[link_name]
                state.replication_link_content.pop(link_name, None)
                state.replication_link_schedules.pop(link_name, None)
                return _empty()
            return _not_allowed()

        if not link:
            return _not_found()

        # /services/replication/links/{name}/content
        if segments[2] == "content":
            content = state.replication_link_content.setdefault(
                link_name,
                {"tenants": [], "defaultNamespaceDirectories": [], "chainedLinks": []},
            )
            if n == 3:
                return _json(content) if method == "GET" else _not_allowed()

            sub = segments[3]
            if sub == "tenants":
                if n == 4:
                    if method == "GET":
                        return _json({"tenants": content.get("tenants", [])})
                    return _not_allowed()
                if n == 5:
                    tenant_name = segments[4]
                    if method == "PUT":
                        tenants = content.setdefault("tenants", [])
                        if tenant_name not in tenants:
                            tenants.append(tenant_name)
                        return _empty()
                    if method == "GET":
                        return _json({"tenant": tenant_name})
                    if method == "POST":
                        return _empty()  # pause/resume
                    if method == "DELETE":
                        tenants = content.get("tenants", [])
                        if tenant_name in tenants:
                            tenants.remove(tenant_name)
                        return _empty()

            if sub == "defaultNamespaceDirectories":
                if n == 4:
                    return (
                        _json(
                            {
                                "defaultNamespaceDirectories": content.get(
                                    "defaultNamespaceDirectories", []
                                )
                            }
                        )
                        if method == "GET"
                        else _not_allowed()
                    )
                if n == 5:
                    dir_name = segments[4]
                    if method == "PUT":
                        dirs = content.setdefault("defaultNamespaceDirectories", [])
                        if dir_name not in dirs:
                            dirs.append(dir_name)
                        return _empty()
                    if method == "DELETE":
                        dirs = content.get("defaultNamespaceDirectories", [])
                        if dir_name in dirs:
                            dirs.remove(dir_name)
                        return _empty()

            if sub == "chainedLinks":
                if n == 4:
                    return (
                        _json({"chainedLinks": content.get("chainedLinks", [])})
                        if method == "GET"
                        else _not_allowed()
                    )
                if n == 5:
                    chained = segments[4]
                    if method == "PUT":
                        chains = content.setdefault("chainedLinks", [])
                        if chained not in chains:
                            chains.append(chained)
                        return _empty()
                    if method == "DELETE":
                        chains = content.get("chainedLinks", [])
                        if chained in chains:
                            chains.remove(chained)
                        return _empty()

            return _not_found()

        # /services/replication/links/{name}/schedule
        if segments[2] == "schedule" and n == 3:
            schedule = state.replication_link_schedules.get(link_name, {})
            if method == "GET":
                return _json(schedule)
            if method == "POST":
                state.replication_link_schedules[link_name] = body
                return _empty()
            return _not_allowed()

        # /services/replication/links/{name}/localCandidates
        if segments[2] == "localCandidates":
            candidates = {
                "tenants": list(state.tenants.keys()),
                "defaultNamespaceDirectories": [],
                "chainedLinks": list(state.replication_links.keys()),
            }
            if n == 3:
                return _json(candidates) if method == "GET" else _not_allowed()
            if n == 4:
                sub = segments[3]
                if sub == "tenants":
                    return (
                        _json({"tenants": candidates["tenants"]})
                        if method == "GET"
                        else _not_allowed()
                    )
                if sub == "defaultNamespaceDirectories":
                    return (
                        _json(
                            {
                                "defaultNamespaceDirectories": candidates[
                                    "defaultNamespaceDirectories"
                                ]
                            }
                        )
                        if method == "GET"
                        else _not_allowed()
                    )
                if sub == "chainedLinks":
                    return (
                        _json({"chainedLinks": candidates["chainedLinks"]})
                        if method == "GET"
                        else _not_allowed()
                    )

        # /services/replication/links/{name}/remoteCandidates
        if segments[2] == "remoteCandidates":
            remote_candidates = {
                "tenants": ["remote-tenant1"],
                "defaultNamespaceDirectories": [],
                "chainedLinks": [],
            }
            if n == 3:
                return _json(remote_candidates) if method == "GET" else _not_allowed()
            if n == 4:
                sub = segments[3]
                if sub == "tenants":
                    return (
                        _json({"tenants": remote_candidates["tenants"]})
                        if method == "GET"
                        else _not_allowed()
                    )
                if sub == "defaultNamespaceDirectories":
                    return (
                        _json(
                            {
                                "defaultNamespaceDirectories": remote_candidates[
                                    "defaultNamespaceDirectories"
                                ]
                            }
                        )
                        if method == "GET"
                        else _not_allowed()
                    )
                if sub == "chainedLinks":
                    return (
                        _json({"chainedLinks": remote_candidates["chainedLinks"]})
                        if method == "GET"
                        else _not_allowed()
                    )

        return _not_found()

    return _not_found()


def _handle_erasure_coding(
    state: MockMapiState,
    method: str,
    segments: list[str],
    body: dict,
    request: httpx.Request,
) -> HttpxResponse:
    """Handle /services/erasureCoding/... routes."""
    n = len(segments)

    # /services/erasureCoding/linkCandidates
    if n == 1 and segments[0] == "linkCandidates":
        if method == "GET":
            return _json({"name": list(state.replication_links.keys())})
        return _not_allowed()

    # /services/erasureCoding/ecTopologies
    if n >= 1 and segments[0] == "ecTopologies":
        if n == 1:
            verbose = request.url.params.get("verbose", "false") == "true"
            if method == "GET":
                if verbose:
                    return _json(list(state.ec_topologies.values()))
                return _json({"name": list(state.ec_topologies.keys())})
            if method == "PUT":
                topo_name = body.get("name", "")
                if topo_name in state.ec_topologies:
                    return _json({"message": f"{topo_name} already exists"}, 409)
                body.setdefault("id", f"ec-uuid-{len(state.ec_topologies) + 1:03d}")
                body.setdefault("state", "ACTIVE")
                body.setdefault("protectionStatus", "PROTECTED")
                body.setdefault("readStatus", "AVAILABLE")
                body.setdefault("erasureCodedObjects", 0)
                state.ec_topologies[topo_name] = body
                state.ec_topology_tenants[topo_name] = []
                return _empty()
            return _not_allowed()

        topo_name = segments[1]
        topo = state.ec_topologies.get(topo_name)

        if n == 2:
            if method == "GET":
                return _json(topo) if topo else _not_found()
            if method == "HEAD":
                return _empty(200 if topo else 404)
            if method == "POST":
                if not topo:
                    return _not_found()
                params = dict(request.url.params)
                if "retire" in params:
                    topo["state"] = "RETIRED"
                return _empty()
            if method == "DELETE":
                if not topo:
                    return _not_found()
                del state.ec_topologies[topo_name]
                state.ec_topology_tenants.pop(topo_name, None)
                return _empty()
            return _not_allowed()

        if not topo:
            return _not_found()

        sub = segments[2]

        if sub == "tenants":
            tenants = state.ec_topology_tenants.get(topo_name, [])
            if n == 3:
                if method == "GET":
                    return _json({"tenantCandidate": tenants})
                return _not_allowed()
            if n == 4:
                tenant_name = segments[3]
                if method == "PUT":
                    if not any(t.get("name") == tenant_name for t in tenants):
                        tenants.append(
                            {"name": tenant_name, "uuid": f"tenant-uuid-{tenant_name}"}
                        )
                    return _empty()
                if method == "DELETE":
                    state.ec_topology_tenants[topo_name] = [
                        t for t in tenants if t.get("name") != tenant_name
                    ]
                    return _empty()

        if sub == "tenantCandidates" and n == 3:
            # Return all tenants as candidates
            candidates = [
                {"name": t, "uuid": f"tenant-uuid-{t}"} for t in state.tenants.keys()
            ]
            return (
                _json({"tenantCandidate": candidates})
                if method == "GET"
                else _not_allowed()
            )

        if sub == "tenantConflictingCandidates" and n == 3:
            return _json({"tenantCandidate": []}) if method == "GET" else _not_allowed()

        return _not_found()

    return _not_found()


# ── Dispatcher ───────────────────────────────────────────────────────


def _parse_body(request: httpx.Request) -> dict:
    if request.content:
        try:
            return json.loads(request.content)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _make_mapi_dispatcher(state: MockMapiState):
    """Return a ``side_effect`` callable that routes all MAPI requests."""

    def dispatcher(request: httpx.Request) -> HttpxResponse:
        method = request.method
        path = request.url.path
        rest = path.removeprefix("/mapi/").strip("/")

        if not rest:
            return _not_found()

        segments = rest.split("/")
        n = len(segments)
        logger.info("%s %s", method, path)

        body = _parse_body(request) if method in ("PUT", "POST") else {}

        # ── System-level routes (not under /tenants) ──
        if segments[0] == "network":
            if n == 1:
                if method == "GET":
                    return _json(state.system_network)
                if method == "POST":
                    state.system_network.update(body)
                    return _empty()
            return _not_found()

        if segments[0] == "storage" and n >= 2 and segments[1] == "licenses":
            if n == 2:
                if method == "GET":
                    return _json({"license": state.system_licenses})
                if method == "PUT":
                    # Simulate license upload (just add a placeholder)
                    state.system_licenses.append(
                        {
                            "serialNumber": f"SN-UPLOAD-{len(state.system_licenses) + 1:03d}",
                            "localCapacity": 0,
                            "expirationDate": "2028-01-01T00:00:00+0000",
                            "feature": "Uploaded",
                            "uploadDate": "2026-03-13T00:00:00+0000",
                        }
                    )
                    return _empty()
            if n == 3:
                serial = segments[2]
                lic = next(
                    (
                        entry
                        for entry in state.system_licenses
                        if entry.get("serialNumber") == serial
                    ),
                    None,
                )
                if lic and method == "GET":
                    return _json(lic)
                return _not_found()
            return _not_found()

        if segments[0] == "nodes" and n == 2 and segments[1] == "statistics":
            return _static_get(SYSTEM_NODE_STATISTICS, method)

        if segments[0] == "services":
            if n >= 2 and segments[1] == "replication":
                return _handle_replication(state, method, segments[2:], body, request)
            if n >= 2 and segments[1] == "erasureCoding":
                return _handle_erasure_coding(
                    state, method, segments[2:], body, request
                )
            # existing: services/statistics
            if n == 2 and segments[1] == "statistics":
                return _static_get(SYSTEM_SERVICE_STATISTICS, method)
            return _not_found()

        if segments[0] == "userAccounts":
            if n == 1:
                verbose = request.url.params.get("verbose", "false") == "true"
                if verbose:
                    return (
                        _json(list(state.system_user_accounts.values()))
                        if method == "GET"
                        else _not_allowed()
                    )
                return _crud(state.system_user_accounts, method, None, body, "username")
            if n == 2:
                username = segments[1]
                if method == "POST":
                    # Password change via query param
                    if username in state.system_user_accounts:
                        return _empty()
                    return _not_found()
                return _crud(
                    state.system_user_accounts, method, username, body, "username"
                )
            if n == 3 and segments[2] == "changePassword":
                if method == "POST":
                    return (
                        _empty()
                        if segments[1] in state.system_user_accounts
                        else _not_found()
                    )
            return _not_found()

        if segments[0] == "groupAccounts":
            if n == 1:
                verbose = request.url.params.get("verbose", "false") == "true"
                if verbose:
                    return (
                        _json(list(state.system_group_accounts.values()))
                        if method == "GET"
                        else _not_allowed()
                    )
                return _crud(
                    state.system_group_accounts, method, None, body, "groupname"
                )
            if n == 2:
                return _crud(
                    state.system_group_accounts, method, segments[1], body, "groupname"
                )
            return _not_found()

        if segments[0] == "supportaccesscredentials":
            if n == 1:
                if method == "GET":
                    return _json(state.system_support_credentials)
                if method == "PUT":
                    return _empty()
            return _not_found()

        if segments[0] == "logs":
            if n == 1:
                if method == "GET":
                    return _json(state.system_log_status)
                if method == "POST":
                    return _empty()
            if n == 2:
                if segments[1] == "prepare" and method == "POST":
                    state.system_log_status["started"] = True
                    state.system_log_status["readyForStreaming"] = True
                    return _empty()
                if segments[1] == "download" and method == "POST":
                    return HttpxResponse(
                        status_code=200,
                        content=b"mock-log-data",
                        headers={
                            **_HCP_HEADERS,
                            "Content-Type": "application/octet-stream",
                        },
                    )
            return _not_found()

        if segments[0] == "healthCheckReport":
            if n == 1:
                if method == "GET":
                    return _json(state.system_health_status)
                if method == "POST":
                    # Cancel
                    state.system_health_status["started"] = False
                    state.system_health_status["readyForStreaming"] = False
                    return _empty()
            if n == 2:
                if segments[1] == "prepare" and method == "POST":
                    state.system_health_status["started"] = True
                    state.system_health_status["readyForStreaming"] = True
                    return _empty()
                if segments[1] == "download" and method == "POST":
                    return HttpxResponse(
                        status_code=200,
                        content=b"mock-health-data",
                        headers={
                            **_HCP_HEADERS,
                            "Content-Type": "application/octet-stream",
                        },
                    )
            return _not_found()

        if segments[0] != "tenants":
            return _not_found()

        # /tenants
        if n == 1:
            if method == "PUT":
                return state.create_tenant(body.get("name", ""), body)
            if method == "GET":
                verbose = request.url.params.get("verbose", "false") == "true"
                if verbose:
                    return _json(list(state.tenants.values()))
                return _json({"name": list(state.tenants.keys())})
            return _not_allowed()

        tenant = segments[1]
        state.ensure_tenant(tenant)

        # /tenants/{T}
        if n == 2:
            if method == "DELETE":
                return state.delete_tenant(tenant)
            return _crud(state.tenants, method, tenant, body)

        resource = segments[2]
        item = segments[3] if n >= 4 else None

        # ── CRUD resources (userAccounts, groupAccounts, contentClasses) ──
        if resource in CRUD_RESOURCES:
            attr, name_field = CRUD_RESOURCES[resource]
            store = getattr(state, attr).get(tenant)
            if n == 5 and item and resource in ACCOUNT_TYPES:
                return _handle_account_sub(
                    state,
                    tenant,
                    ACCOUNT_TYPES[resource],
                    item,
                    segments[4],
                    method,
                    body,
                )
            verbose = request.url.params.get("verbose", "false") == "true"
            return (
                _crud(store, method, item, body, name_field, verbose=verbose)
                if n <= 4
                else _not_found()
            )

        # ── Namespaces (CRUD + deep sub-resources) ──
        if resource == "namespaces":
            qp = dict(request.url.params)
            return _handle_namespaces(state, tenant, method, segments, body, qp)

        # ── Tenant-level leaf endpoints (depth 3 only) ──
        if n == 3:
            if resource == "statistics":
                return _static_get(TENANT_STATISTICS, method)
            if resource == "chargebackReport":
                return _static_get(TENANT_CHARGEBACK, method)
            if resource == "cors":
                return _cors(state.tenant_settings.setdefault(tenant, {}), method, body)
            if resource in TENANT_SETTING_KEYS:
                return _setting(
                    state.tenant_settings.setdefault(tenant, default_tenant_settings()),
                    resource,
                    method,
                    body,
                )

        # ── availableServicePlans (depth 3–4) ──
        if resource == "availableServicePlans":
            if n == 3 and method == "GET":
                return _json({"name": list(AVAILABLE_SERVICE_PLANS.keys())})
            if n == 4 and method == "GET":
                plan = AVAILABLE_SERVICE_PLANS.get(segments[3])
                return _json(plan) if plan else _not_found()
            return _not_allowed()

        logger.warning("Unmatched MAPI route: %s %s", method, path)
        return _not_found()

    return dispatcher


# ── Metadata Query handlers ───────────────────────────────────────────

_NS_PATTERN = re.compile(r'namespace:"([^"]+)"')


def _handle_object_query(body: dict) -> HttpxResponse:
    """Handle an object metadata query request."""
    obj = body.get("object", {})
    query_expr = obj.get("query", "*:*")
    count = obj.get("count", 100)
    offset = obj.get("offset", 0)
    verbose = obj.get("verbose", False)

    results = list(MOCK_QUERY_OBJECTS)

    # Filter by namespace if query contains namespace:"xxx"
    ns_match = _NS_PATTERN.search(query_expr)
    if ns_match:
        ns = ns_match.group(1)
        results = [r for r in results if r.get("namespace") == ns]

    total = len(results)
    page = results[offset : offset + count]

    if not verbose:
        page = [
            {
                "urlName": r["urlName"],
                "operation": r.get("operation", ""),
                "changeTimeMilliseconds": r.get("changeTimeMilliseconds"),
                "changeTimeString": r.get("changeTimeString"),
                "version": r.get("version"),
            }
            for r in page
        ]

    return _json(
        {
            "status": {
                "totalResults": total,
                "results": len(page),
                "code": "COMPLETE",
            },
            "resultSet": page,
        }
    )


def _handle_operation_query(body: dict) -> HttpxResponse:
    """Handle an operation metadata query request."""
    op = body.get("operation", {})
    count = op.get("count", 100)
    verbose = op.get("verbose", False)
    sys_meta = op.get("systemMetadata", {})

    results = list(MOCK_QUERY_OPERATIONS)

    # Filter by time range
    time_from = sys_meta.get("changeTimeFrom")
    time_to = sys_meta.get("changeTimeTo")
    if time_from:
        results = [r for r in results if r.get("changeTimeString", "") >= time_from]
    if time_to:
        results = [r for r in results if r.get("changeTimeString", "") <= time_to]

    # Filter by namespace list
    namespaces = sys_meta.get("namespaces")
    if namespaces:
        results = [r for r in results if r.get("namespace") in namespaces]

    # Filter by transaction types
    txns = sys_meta.get("transactions", {})
    tx_list = txns.get("transaction") if txns else None
    if tx_list:
        tx_upper = {t.upper() for t in tx_list}
        results = [r for r in results if r.get("operation", "").upper() in tx_upper]

    total = len(results)
    page = results[:count]

    if not verbose:
        page = [
            {
                "urlName": r["urlName"],
                "operation": r.get("operation", ""),
                "changeTimeMilliseconds": r.get("changeTimeMilliseconds"),
                "changeTimeString": r.get("changeTimeString"),
                "version": r.get("version"),
            }
            for r in page
        ]

    return _json(
        {
            "status": {
                "totalResults": total,
                "results": len(page),
                "code": "COMPLETE",
            },
            "resultSet": page,
        }
    )


def _make_query_dispatcher():
    """Return a ``side_effect`` callable that routes query requests."""

    def dispatcher(request: httpx.Request) -> HttpxResponse:
        logger.info("QUERY %s %s", request.method, request.url)
        if request.method != "POST":
            return _not_allowed()

        body = _parse_body(request)
        if "object" in body:
            return _handle_object_query(body)
        if "operation" in body:
            return _handle_operation_query(body)
        return _json({"message": "Invalid query request"}, 400)

    return dispatcher


# ── Lance mock dispatcher ────────────────────────────────────────────


def _make_lance_dispatcher():
    """Return a ``side_effect`` callable that routes /api/v1/lance/* mock requests."""
    from mock_server.lance_fixtures import (
        LANCE_ROWS,
        LANCE_SCHEMAS,
        LANCE_TABLES,
        LANCE_VECTOR_PREVIEW,
    )

    def dispatcher(request: httpx.Request) -> HttpxResponse:
        path = request.url.path.removeprefix("/api/v1/lance/").strip("/")
        params = dict(request.url.params)
        bucket = params.get("bucket", "")
        path_prefix = params.get("path", "")
        ds_key = f"{bucket}/{path_prefix}" if path_prefix else f"{bucket}/"

        if path == "tables":
            tables = LANCE_TABLES.get(ds_key, [])
            return _json({"tables": tables})

        table = params.get("table", "")
        if not table:
            return _json({"detail": "table parameter required"}, 400)

        if path == "schema":
            schema = LANCE_SCHEMAS.get(table)
            if not schema:
                return _json({"detail": f"Table {table!r} not found"}, 422)
            return _json(schema)

        if path == "rows":
            rows = LANCE_ROWS.get(table, [])
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "50"))
            columns_str = params.get("columns")
            if columns_str:
                cols = {c.strip() for c in columns_str.split(",")}
                rows = [{k: v for k, v in r.items() if k in cols} for r in rows]
            page = rows[offset : offset + limit]
            return _json(
                {
                    "rows": page,
                    "total": len(LANCE_ROWS.get(table, [])),
                    "limit": limit,
                    "offset": offset,
                }
            )

        if path == "vector-preview":
            column = params.get("column", "")
            table_previews = LANCE_VECTOR_PREVIEW.get(table, {})
            preview = table_previews.get(column, {"stats": None, "preview": []})
            return _json(preview)

        return _not_found()

    return dispatcher


# ── Route setup ──────────────────────────────────────────────────────


def setup_mapi_routes(
    mock: respx.MockRouter, state: MockMapiState, hcp_base: str
) -> None:
    """Register catch-all respx routes for MAPI, Query, and Lance APIs."""
    mock.route(url__startswith=hcp_base).mock(side_effect=_make_mapi_dispatcher(state))

    # Query API: https://*.*/query
    mock.route(method="POST", url__regex=r"https://[^/]+/query$").mock(
        side_effect=_make_query_dispatcher()
    )

    # Lance explorer mock routes
    mock.route(url__startswith="/api/v1/lance/").mock(
        side_effect=_make_lance_dispatcher()
    )
