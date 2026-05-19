"""Static fixture data for the mock MAPI server.

All dicts are keyed by resource name for O(1) lookups in the stateful mock.
"""

from __future__ import annotations

from typing import Any

# ── Tenants ──────────────────────────────────────────────────────────

TENANTS: dict[str, dict] = {
    "default": {
        "name": "default",
        "systemVisibleDescription": "Default tenant",
        "hardQuota": "100 GB",
        "softQuota": 90,
        "namespaceQuota": "10",
        "authenticationTypes": {"authenticationType": ["LOCAL"]},
        "tags": {"tag": []},
        "administrationAllowed": True,
        "maxNamespacesPerUser": 100,
        "snmpLoggingEnabled": False,
        "syslogLoggingEnabled": False,
        "tenantVisibleDescription": "",
    },
    "research": {
        "name": "research",
        "systemVisibleDescription": "Research tenant",
        "hardQuota": "500 GB",
        "softQuota": 85,
        "namespaceQuota": "20",
        "authenticationTypes": {"authenticationType": ["LOCAL"]},
        "tags": {"tag": []},
        "administrationAllowed": True,
        "maxNamespacesPerUser": 100,
        "snmpLoggingEnabled": False,
        "syslogLoggingEnabled": False,
        "tenantVisibleDescription": "",
    },
    "mock": {
        "name": "mock",
        "systemVisibleDescription": "Mock development tenant",
        "hardQuota": "250 GB",
        "softQuota": 90,
        "namespaceQuota": "15",
        "authenticationTypes": {"authenticationType": ["LOCAL"]},
        "servicePlan": "Default",
        "tags": {"tag": []},
        "administrationAllowed": True,
        "maxNamespacesPerUser": 100,
        "snmpLoggingEnabled": False,
        "syslogLoggingEnabled": True,
        "tenantVisibleDescription": "Mock tenant for local development and testing.",
    },
}

# ── Namespaces (tenant -> ns_name -> data) ───────────────────────────

NAMESPACES: dict[str, dict[str, dict]] = {
    "default": {
        "default-ns": {
            "name": "default-ns",
            "nameIDNA": "default-ns",
            "hardQuota": "50 GB",
            "softQuota": 90,
            "description": "Default namespace",
            "versioningSettings": {"enabled": False},
            "hashScheme": "SHA-256",
            "searchEnabled": True,
            "tags": {"tag": ["s3"]},
        },
        "shared": {
            "name": "shared",
            "nameIDNA": "shared",
            "hardQuota": "30 GB",
            "softQuota": 85,
            "description": "Shared data namespace",
            "versioningSettings": {"enabled": True},
            "hashScheme": "SHA-256",
            "searchEnabled": False,
            "tags": {"tag": ["nfs", "cifs"]},
        },
    },
    "research": {
        "experiments": {
            "name": "experiments",
            "nameIDNA": "experiments",
            "hardQuota": "200 GB",
            "softQuota": 90,
            "description": "Experiment data",
            "versioningSettings": {"enabled": True},
            "hashScheme": "SHA-256",
            "searchEnabled": True,
            "tags": {"tag": ["lakefs", "hdfs"]},
        },
    },
    "mock": {
        "documents": {
            "name": "documents",
            "nameIDNA": "documents",
            "hardQuota": "100 GB",
            "softQuota": 90,
            "description": "General documents",
            "versioningSettings": {"enabled": False},
            "hashScheme": "SHA-256",
            "searchEnabled": True,
            "tags": {"tag": ["lakefs"]},
        },
        "archives": {
            "name": "archives",
            "nameIDNA": "archives",
            "hardQuota": "50 GB",
            "softQuota": 90,
            "description": "Archived data",
            "versioningSettings": {"enabled": False},
            "hashScheme": "SHA-256",
            "searchEnabled": False,
            "tags": {"tag": ["s3"]},
        },
        "logs": {
            "name": "logs",
            "nameIDNA": "logs",
            "hardQuota": "25 GB",
            "softQuota": 85,
            "description": "Application logs",
            "versioningSettings": {"enabled": False},
            "hashScheme": "SHA-256",
            "searchEnabled": True,
            "tags": {"tag": ["nfs"]},
        },
    },
}

# ── User accounts (tenant -> username -> data) ───────────────────────

USER_ACCOUNTS: dict[str, dict[str, dict]] = {
    "default": {
        "admin": {
            "username": "admin",
            "fullName": "Admin User",
            "description": "Tenant administrator",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["ADMINISTRATOR", "SECURITY", "MONITOR", "COMPLIANCE"]},
            "userGUID": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        },
        "user1": {
            "username": "user1",
            "fullName": "Regular User",
            "description": "Standard user",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["MONITOR"]},
            "userGUID": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        },
    },
    "research": {
        "researcher": {
            "username": "researcher",
            "fullName": "Research User",
            "description": "Research tenant admin",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["ADMINISTRATOR", "MONITOR"]},
            "userGUID": "c3d4e5f6-a7b8-9012-cdef-123456789012",
        },
    },
    "mock": {
        "admin": {
            "username": "admin",
            "fullName": "Mock Admin",
            "description": "Mock tenant administrator",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["ADMINISTRATOR", "SECURITY", "MONITOR", "COMPLIANCE"]},
            "userGUID": "d4e5f6a7-b8c9-0123-defa-234567890123",
        },
        "analyst": {
            "username": "analyst",
            "fullName": "Data Analyst",
            "description": "Read-only data analyst",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["MONITOR"]},
            "userGUID": "e5f6a7b8-c9d0-1234-efab-345678901234",
        },
        "developer": {
            "username": "developer",
            "fullName": "App Developer",
            "description": "Application developer",
            "localAuthentication": True,
            "enabled": True,
            "roles": {"role": ["MONITOR"]},
            "userGUID": "f6a7b8c9-d0e1-2345-fabc-456789012345",
        },
    },
}

# ── Group accounts (tenant -> groupname -> data) ─────────────────────

GROUP_ACCOUNTS: dict[str, dict[str, dict]] = {
    "default": {
        "admins": {
            "groupname": "admins",
            "description": "Administrator group",
            "roles": {"role": ["ADMINISTRATOR", "SECURITY"]},
        },
        "users": {
            "groupname": "users",
            "description": "Regular users group",
            "roles": {"role": ["MONITOR"]},
        },
    },
    "research": {
        "researchers": {
            "groupname": "researchers",
            "description": "Research group",
            "roles": {"role": ["ADMINISTRATOR"]},
        },
    },
    "mock": {
        "administrators": {
            "groupname": "administrators",
            "description": "Tenant administrators",
            "roles": {"role": ["ADMINISTRATOR", "SECURITY"]},
        },
        "developers": {
            "groupname": "developers",
            "description": "Application developers",
            "roles": {"role": ["MONITOR"]},
        },
        "analysts": {
            "groupname": "analysts",
            "description": "Data analysts with read-only access",
            "roles": {"role": ["MONITOR"]},
        },
    },
}

# ── Content classes (tenant -> cc_name -> data) ──────────────────────

CONTENT_CLASSES: dict[str, dict[str, dict]] = {
    "default": {
        "document": {
            "name": "document",
            "contentProperties": [],
        },
    },
    "research": {
        "dataset": {
            "name": "dataset",
            "contentProperties": [],
        },
    },
    "mock": {
        "document-metadata": {
            "name": "document-metadata",
            "contentProperties": [
                {
                    "name": "title",
                    "expression": "//title",
                    "type": "STRING",
                    "multivalued": False,
                },
                {
                    "name": "author",
                    "expression": "//author",
                    "type": "STRING",
                    "multivalued": True,
                },
                {
                    "name": "created",
                    "expression": "//created",
                    "type": "DATETIME",
                    "multivalued": False,
                    "format": "yyyy-MM-dd",
                },
            ],
            "namespaces": ["documents"],
        },
        "image-metadata": {
            "name": "image-metadata",
            "contentProperties": [
                {
                    "name": "width",
                    "expression": "$.width",
                    "type": "INTEGER",
                    "multivalued": False,
                },
                {
                    "name": "height",
                    "expression": "$.height",
                    "type": "INTEGER",
                    "multivalued": False,
                },
                {
                    "name": "tags",
                    "expression": "$.tags[*]",
                    "type": "STRING",
                    "multivalued": True,
                },
            ],
            "namespaces": ["archives"],
        },
        "audit-log": {
            "name": "audit-log",
            "contentProperties": [
                {
                    "name": "action",
                    "expression": "$.action",
                    "type": "STRING",
                    "multivalued": False,
                },
                {
                    "name": "timestamp",
                    "expression": "$.timestamp",
                    "type": "DATETIME",
                    "multivalued": False,
                    "format": "epoch",
                },
            ],
            "namespaces": [],
        },
    },
}

# ── Retention classes ((tenant, ns) -> rc_name -> data) ──────────────

RETENTION_CLASSES: dict[tuple[str, str], dict[str, dict]] = {
    ("default", "default-ns"): {
        "standard": {
            "name": "standard",
            "description": "Standard retention",
            "value": 365,
            "autoDelete": False,
        },
    },
}

# ── Static fixtures (read-only) ─────────────────────────────────────

TENANT_STATISTICS: dict = {
    "objectCount": 1234,
    "bytesUsed": "53687091200",
    "customMetadataObjectCount": 100,
    "shredObjectCount": 0,
    "namespacesUsed": 3,
}

NS_STATISTICS: dict = {
    "objectCount": 500,
    "bytesUsed": "21474836480",
    "customMetadataObjectCount": 50,
    "shredObjectCount": 0,
}

TENANT_CHARGEBACK: dict = {
    "chargebackData": [
        {
            "namespaceName": "documents",
            "objectCount": 750,
            "ingestedVolume": 32212254720,
            "storageCapacityUsed": 34359738368,  # ~32 GB (quota: 100 GB)
            "bytesIn": 8589934592,
            "bytesOut": 4294967296,
            "reads": 15230,
            "writes": 3420,
            "deletes": 85,
            "valid": True,
        },
        {
            "namespaceName": "archives",
            "objectCount": 320,
            "ingestedVolume": 55834574848,
            "storageCapacityUsed": 59055800320,  # ~55 GB (quota: 50 GB — over quota!)
            "bytesIn": 2147483648,
            "bytesOut": 1073741824,
            "reads": 4870,
            "writes": 980,
            "deletes": 12,
            "valid": True,
        },
        {
            "namespaceName": "logs",
            "objectCount": 164,
            "ingestedVolume": 2147483648,
            "storageCapacityUsed": 2147483648,  # ~2 GB (quota: 25 GB)
            "bytesIn": 1073741824,
            "bytesOut": 536870912,
            "reads": 2340,
            "writes": 1560,
            "deletes": 230,
            "valid": True,
        },
    ],
}

NS_CHARGEBACK: dict = {
    "chargebackData": [
        {
            "startTime": "2024-04-08T00:00:00+0000",
            "endTime": "2024-04-09T00:00:00+0000",
            "objectCount": 480,
            "bytesIn": 1073741824,
            "bytesOut": 536870912,
            "reads": 950,
            "writes": 210,
            "deletes": 5,
            "valid": True,
        },
        {
            "startTime": "2024-04-09T00:00:00+0000",
            "endTime": "2024-04-10T00:00:00+0000",
            "objectCount": 485,
            "bytesIn": 1610612736,
            "bytesOut": 805306368,
            "reads": 1200,
            "writes": 340,
            "deletes": 8,
            "valid": True,
        },
        {
            "startTime": "2024-04-10T00:00:00+0000",
            "endTime": "2024-04-11T00:00:00+0000",
            "objectCount": 490,
            "bytesIn": 2147483648,
            "bytesOut": 1073741824,
            "reads": 1450,
            "writes": 280,
            "deletes": 3,
            "valid": True,
        },
        {
            "startTime": "2024-04-11T00:00:00+0000",
            "endTime": "2024-04-12T00:00:00+0000",
            "objectCount": 492,
            "bytesIn": 1342177280,
            "bytesOut": 671088640,
            "reads": 1100,
            "writes": 190,
            "deletes": 6,
            "valid": True,
        },
        {
            "startTime": "2024-04-12T00:00:00+0000",
            "endTime": "2024-04-13T00:00:00+0000",
            "objectCount": 495,
            "bytesIn": 1879048192,
            "bytesOut": 939524096,
            "reads": 1350,
            "writes": 310,
            "deletes": 10,
            "valid": True,
        },
        {
            "startTime": "2024-04-13T00:00:00+0000",
            "endTime": "2024-04-14T00:00:00+0000",
            "objectCount": 498,
            "bytesIn": 805306368,
            "bytesOut": 402653184,
            "reads": 680,
            "writes": 150,
            "deletes": 2,
            "valid": True,
        },
        {
            "startTime": "2024-04-14T00:00:00+0000",
            "endTime": "2024-04-15T00:00:00+0000",
            "objectCount": 500,
            "bytesIn": 1879048192,
            "bytesOut": 939524096,
            "reads": 1470,
            "writes": 620,
            "deletes": 11,
            "valid": True,
        },
    ],
}

AVAILABLE_SERVICE_PLANS: dict[str, dict] = {
    "Default": {
        "name": "Default",
        "description": "Default service plan",
        "tags": {"tag": []},
    },
}

# ── Settings factories ───────────────────────────────────────────────


def default_tenant_settings() -> dict[str, dict]:
    """Return a fresh dict of default tenant-level settings sub-resources."""
    return {
        "consoleSecurity": {
            "ipSettings": {
                "allowAddresses": [],
                "denyAddresses": [],
                "allowIfInBothLists": False,
            },
        },
        "contactInfo": {
            "name": "",
            "email": "",
            "phone": "",
        },
        "emailNotification": {
            "enabled": False,
            "smtpServer": "",
            "smtpPort": 25,
            "senderAddress": "",
        },
        "namespaceDefaults": {
            "hardQuota": "1 GB",
            "softQuota": 90,
            "optimizedFor": "cloud",
            "hashScheme": "SHA-256",
            "searchEnabled": False,
            "versioningEnabled": False,
        },
        "searchSecurity": {
            "ipSettings": {
                "allowAddresses": [],
                "denyAddresses": [],
                "allowIfInBothLists": False,
            },
        },
        "permissions": {
            "namespaceDeleteAllowed": True,
            "namespaceManageAllowed": True,
            "namespaceUndeleteAllowed": True,
            "erasureCodingAllowed": True,
            "replicationAllowed": True,
            "searchAllowed": True,
            "complianceAllowed": True,
            "taggingAllowed": True,
        },
    }


# ── Metadata Query fixtures ────────────────────────────────────────────

MOCK_QUERY_OBJECTS: list[dict] = [
    {
        "urlName": "/documents/report-q1-2024.pdf",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1706745600000",
        "changeTimeString": "2024-01-31T20:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "report-q1-2024.pdf",
        "size": 2458624,
        "contentType": "application/pdf",
        "hold": False,
        "retention": "0",
        "retentionString": "Deletion Allowed",
        "hash": "SHA-256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "customMetadata": True,
        "replicated": False,
        "index": True,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/documents/contract-acme.docx",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1707350400000",
        "changeTimeString": "2024-02-08T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "contract-acme.docx",
        "size": 184320,
        "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "hold": True,
        "retention": "1735689600000",
        "retentionString": "2025-01-01T00:00:00+0000",
        "retentionClass": "standard",
        "hash": "SHA-256:b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
        "customMetadata": True,
        "replicated": True,
        "index": True,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/documents/invoice-2024-001.pdf",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1708560000000",
        "changeTimeString": "2024-02-22T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "invoice-2024-001.pdf",
        "size": 98304,
        "contentType": "application/pdf",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "analyst",
        "type": "object",
    },
    {
        "urlName": "/documents/memo-internal.txt",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1709164800000",
        "changeTimeString": "2024-02-29T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "memo-internal.txt",
        "size": 4096,
        "contentType": "text/plain",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "developer",
        "type": "object",
    },
    {
        "urlName": "/archives/backup-2024-01.tar.gz",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1706832000000",
        "changeTimeString": "2024-02-01T20:00:00+0000",
        "version": "0",
        "namespace": "archives",
        "utf8Name": "backup-2024-01.tar.gz",
        "size": 536870912,
        "contentType": "application/gzip",
        "hold": False,
        "retention": "1738368000000",
        "retentionString": "2025-02-01T00:00:00+0000",
        "hash": "SHA-256:e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
        "customMetadata": False,
        "replicated": True,
        "index": False,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/archives/compliance-records-2023.zip",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1704067200000",
        "changeTimeString": "2024-01-01T00:00:00+0000",
        "version": "0",
        "namespace": "archives",
        "utf8Name": "compliance-records-2023.zip",
        "size": 1073741824,
        "contentType": "application/zip",
        "hold": True,
        "retention": "1767225600000",
        "retentionString": "2026-01-01T00:00:00+0000",
        "retentionClass": "standard",
        "hash": "SHA-256:f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
        "customMetadata": True,
        "replicated": True,
        "index": False,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/archives/audit-trail-q4.json",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1711929600000",
        "changeTimeString": "2024-04-01T00:00:00+0000",
        "version": "0",
        "namespace": "archives",
        "utf8Name": "audit-trail-q4.json",
        "size": 262144,
        "contentType": "application/json",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:a1a2a3a4a5a6a7a8a1a2a3a4a5a6a7a8a1a2a3a4a5a6a7a8a1a2a3a4a5a6a7a8",
        "customMetadata": False,
        "replicated": False,
        "index": False,
        "owner": "analyst",
        "type": "object",
    },
    {
        "urlName": "/logs/app-2024-03-15.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1710460800000",
        "changeTimeString": "2024-03-15T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
        "utf8Name": "app-2024-03-15.log",
        "size": 10485760,
        "contentType": "text/plain",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:b1b2b3b4b5b6b7b8b1b2b3b4b5b6b7b8b1b2b3b4b5b6b7b8b1b2b3b4b5b6b7b8",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "developer",
        "type": "object",
    },
    {
        "urlName": "/logs/error-2024-03-20.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1710892800000",
        "changeTimeString": "2024-03-20T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
        "utf8Name": "error-2024-03-20.log",
        "size": 2097152,
        "contentType": "text/plain",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:c1c2c3c4c5c6c7c8c1c2c3c4c5c6c7c8c1c2c3c4c5c6c7c8c1c2c3c4c5c6c7c8",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "developer",
        "type": "object",
    },
    {
        "urlName": "/logs/access-2024-04-01.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1711929600000",
        "changeTimeString": "2024-04-01T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
        "utf8Name": "access-2024-04-01.log",
        "size": 5242880,
        "contentType": "text/plain",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:d1d2d3d4d5d6d7d8d1d2d3d4d5d6d7d8d1d2d3d4d5d6d7d8d1d2d3d4d5d6d7d8",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/documents/presentation-board.pptx",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1712534400000",
        "changeTimeString": "2024-04-08T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "presentation-board.pptx",
        "size": 8388608,
        "contentType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:e1e2e3e4e5e6e7e8e1e2e3e4e5e6e7e8e1e2e3e4e5e6e7e8e1e2e3e4e5e6e7e8",
        "customMetadata": True,
        "replicated": False,
        "index": True,
        "owner": "admin",
        "type": "object",
    },
    {
        "urlName": "/documents/data-export-2024.csv",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1713139200000",
        "changeTimeString": "2024-04-15T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
        "utf8Name": "data-export-2024.csv",
        "size": 15728640,
        "contentType": "text/csv",
        "hold": False,
        "retention": "0",
        "hash": "SHA-256:f1f2f3f4f5f6f7f8f1f2f3f4f5f6f7f8f1f2f3f4f5f6f7f8f1f2f3f4f5f6f7f8",
        "customMetadata": False,
        "replicated": False,
        "index": True,
        "owner": "analyst",
        "type": "object",
    },
]

MOCK_QUERY_OPERATIONS: list[dict] = [
    {
        "urlName": "/documents/report-q1-2024.pdf",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1706745600000",
        "changeTimeString": "2024-01-31T20:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/archives/compliance-records-2023.zip",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1704067200000",
        "changeTimeString": "2024-01-01T00:00:00+0000",
        "version": "0",
        "namespace": "archives",
    },
    {
        "urlName": "/archives/backup-2024-01.tar.gz",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1706832000000",
        "changeTimeString": "2024-02-01T20:00:00+0000",
        "version": "0",
        "namespace": "archives",
    },
    {
        "urlName": "/documents/contract-acme.docx",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1707350400000",
        "changeTimeString": "2024-02-08T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/documents/invoice-2024-001.pdf",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1708560000000",
        "changeTimeString": "2024-02-22T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/logs/app-2024-03-15.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1710460800000",
        "changeTimeString": "2024-03-15T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
    },
    {
        "urlName": "/logs/error-2024-03-20.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1710892800000",
        "changeTimeString": "2024-03-20T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
    },
    {
        "urlName": "/documents/old-draft.txt",
        "operation": "DELETED",
        "changeTimeMilliseconds": "1710979200000",
        "changeTimeString": "2024-03-21T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/logs/access-2024-04-01.log",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1711929600000",
        "changeTimeString": "2024-04-01T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
    },
    {
        "urlName": "/archives/audit-trail-q4.json",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1711929600000",
        "changeTimeString": "2024-04-01T00:00:00+0000",
        "version": "0",
        "namespace": "archives",
    },
    {
        "urlName": "/documents/presentation-board.pptx",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1712534400000",
        "changeTimeString": "2024-04-08T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/documents/data-export-2024.csv",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1713139200000",
        "changeTimeString": "2024-04-15T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
    {
        "urlName": "/archives/old-backup-2022.tar.gz",
        "operation": "PURGED",
        "changeTimeMilliseconds": "1713744000000",
        "changeTimeString": "2024-04-22T00:00:00+0000",
        "version": "0",
        "namespace": "archives",
    },
    {
        "urlName": "/logs/debug-temp.log",
        "operation": "DELETED",
        "changeTimeMilliseconds": "1714348800000",
        "changeTimeString": "2024-04-29T00:00:00+0000",
        "version": "0",
        "namespace": "logs",
    },
    {
        "urlName": "/documents/memo-internal.txt",
        "operation": "CREATED",
        "changeTimeMilliseconds": "1709164800000",
        "changeTimeString": "2024-02-29T00:00:00+0000",
        "version": "0",
        "namespace": "documents",
    },
]


# ── System-level fixtures ────────────────────────────────────────────

SYSTEM_NETWORK: dict = {
    "downstreamDNSMode": "ADVANCED",
}

SYSTEM_LICENSES: list[dict] = [
    {
        "serialNumber": "SN-HCP-2024-001",
        "localCapacity": 107374182400,
        "expirationDate": "2027-12-31T23:59:59+0000",
        "extendedCapacity": 0,
        "feature": "HCP Storage",
        "uploadDate": "2024-01-15T10:30:00+0000",
    },
    {
        "serialNumber": "SN-HCP-2024-002",
        "localCapacity": 53687091200,
        "expirationDate": "2026-06-30T23:59:59+0000",
        "extendedCapacity": 21474836480,
        "feature": "HCP Replication",
        "uploadDate": "2024-03-01T08:00:00+0000",
    },
]

SYSTEM_NODE_STATISTICS: dict = {
    "requestTime": 1710460800000,
    "nodes": [
        {
            "nodeNumber": 1,
            "frontendIpAddresses": ["10.0.1.10"],
            "backendIpAddress": "192.168.1.10",
            "managementIpAddresses": ["172.16.0.10"],
            "openHttpConnections": 45,
            "openHttpsConnections": 230,
            "maxHttpConnections": 1000,
            "maxHttpsConnections": 1000,
            "cpuUser": 12.5,
            "cpuSystem": 4.2,
            "cpuMax": 100.0,
            "ioWait": 1.3,
            "swapOut": 0.0,
            "maxFrontEndBandwidth": 10000000000,
            "frontEndBytesRead": 52428800.0,
            "frontEndBytesWritten": 104857600.0,
            "maxBackEndBandwidth": 10000000000,
            "backEndBytesRead": 209715200.0,
            "backEndBytesWritten": 314572800.0,
            "maxManagementPortBandwidth": 1000000000,
            "managementBytesRead": 1048576.0,
            "managementBytesWritten": 2097152.0,
            "collectionTimestamp": 1710460800000,
            "volumes": [
                {
                    "id": "/dev/sda1",
                    "blocksRead": 15234.0,
                    "blocksWritten": 8912.0,
                    "diskUtilization": 42.5,
                    "transferSpeed": 125.3,
                    "totalBytes": 1099511627776,
                    "freeBytes": 631244390400,
                    "totalInodes": 67108864,
                    "freeInodes": 63504384,
                },
                {
                    "id": "/dev/sdb1",
                    "blocksRead": 8456.0,
                    "blocksWritten": 12034.0,
                    "diskUtilization": 68.2,
                    "transferSpeed": 98.7,
                    "totalBytes": 2199023255552,
                    "freeBytes": 698771456000,
                    "totalInodes": 134217728,
                    "freeInodes": 120795955,
                },
            ],
        },
        {
            "nodeNumber": 2,
            "frontendIpAddresses": ["10.0.1.11"],
            "backendIpAddress": "192.168.1.11",
            "managementIpAddresses": ["172.16.0.11"],
            "openHttpConnections": 38,
            "openHttpsConnections": 195,
            "maxHttpConnections": 1000,
            "maxHttpsConnections": 1000,
            "cpuUser": 8.7,
            "cpuSystem": 3.1,
            "cpuMax": 100.0,
            "ioWait": 0.8,
            "swapOut": 0.0,
            "maxFrontEndBandwidth": 10000000000,
            "frontEndBytesRead": 41943040.0,
            "frontEndBytesWritten": 83886080.0,
            "maxBackEndBandwidth": 10000000000,
            "backEndBytesRead": 167772160.0,
            "backEndBytesWritten": 251658240.0,
            "maxManagementPortBandwidth": 1000000000,
            "managementBytesRead": 524288.0,
            "managementBytesWritten": 1048576.0,
            "collectionTimestamp": 1710460800000,
            "volumes": [
                {
                    "id": "/dev/sda1",
                    "blocksRead": 12890.0,
                    "blocksWritten": 7654.0,
                    "diskUtilization": 38.9,
                    "transferSpeed": 110.2,
                    "totalBytes": 1099511627776,
                    "freeBytes": 670014898176,
                    "totalInodes": 67108864,
                    "freeInodes": 64424509,
                },
            ],
        },
    ],
}

SYSTEM_SERVICE_STATISTICS: dict = {
    "requestTime": 1710460800000,
    "services": [
        {
            "name": "Disposition",
            "state": "RUNNING",
            "startTime": 1709424000000,
            "formattedStartTime": "2024-03-03T00:00:00+0000",
            "performanceLevel": "MEDIUM",
            "objectsExamined": 154230,
            "objectsExaminedPerSecond": 12.5,
            "objectsServiced": 1240,
            "objectsServicedPerSecond": 0.1,
            "objectsUnableToService": 3,
            "objectsUnableToServicePerSecond": 0.0,
        },
        {
            "name": "Protection",
            "state": "RUNNING",
            "startTime": 1709424000000,
            "formattedStartTime": "2024-03-03T00:00:00+0000",
            "performanceLevel": "HIGH",
            "objectsExamined": 892104,
            "objectsExaminedPerSecond": 72.3,
            "objectsServiced": 892104,
            "objectsServicedPerSecond": 72.3,
            "objectsUnableToService": 0,
            "objectsUnableToServicePerSecond": 0.0,
        },
        {
            "name": "Scavenging",
            "state": "RUNNING",
            "startTime": 1709424000000,
            "formattedStartTime": "2024-03-03T00:00:00+0000",
            "performanceLevel": "LOW",
            "objectsExamined": 45670,
            "objectsExaminedPerSecond": 3.7,
            "objectsServiced": 234,
            "objectsServicedPerSecond": 0.02,
            "objectsUnableToService": 12,
            "objectsUnableToServicePerSecond": 0.0,
        },
        {
            "name": "GarbageCollection",
            "state": "IDLE",
            "startTime": 1710288000000,
            "formattedStartTime": "2024-03-13T00:00:00+0000",
            "endTime": 1710374400000,
            "formattedEndTime": "2024-03-14T00:00:00+0000",
            "performanceLevel": "MEDIUM",
            "objectsExamined": 0,
            "objectsExaminedPerSecond": 0.0,
            "objectsServiced": 0,
            "objectsServicedPerSecond": 0.0,
            "objectsUnableToService": 0,
            "objectsUnableToServicePerSecond": 0.0,
        },
    ],
}

SYSTEM_USER_ACCOUNTS: dict[str, dict] = {
    "sysadmin": {
        "username": "sysadmin",
        "fullName": "System Administrator",
        "description": "Primary system administrator",
        "localAuthentication": True,
        "enabled": True,
        "roles": {"role": ["ADMINISTRATOR", "SECURITY"]},
        "userGUID": "00000000-0000-0000-0000-000000000001",
        "userID": 1,
    },
    "monitor": {
        "username": "monitor",
        "fullName": "Monitoring Account",
        "description": "System monitoring account",
        "localAuthentication": True,
        "enabled": True,
        "roles": {"role": ["MONITOR"]},
        "userGUID": "00000000-0000-0000-0000-000000000002",
        "userID": 2,
    },
    "service": {
        "username": "service",
        "fullName": "Service Account",
        "description": "Service automation account",
        "localAuthentication": True,
        "enabled": True,
        "roles": {"role": ["SERVICE"]},
        "userGUID": "00000000-0000-0000-0000-000000000003",
        "userID": 3,
    },
}

SYSTEM_GROUP_ACCOUNTS: dict[str, dict] = {
    "system-admins": {
        "groupname": "system-admins",
        "externalGroupID": "",
        "roles": {"role": ["ADMINISTRATOR", "SECURITY"]},
        "allowNamespaceManagement": True,
    },
    "system-monitors": {
        "groupname": "system-monitors",
        "externalGroupID": "",
        "roles": {"role": ["MONITOR"]},
        "allowNamespaceManagement": False,
    },
}

SYSTEM_SUPPORT_CREDENTIALS: dict = {
    "applyTimeStamp": 1706745600000,
    "createTimeStamp": 1704067200000,
    "type": "Default",
    "defaultKeyType": "Default",
    "serialNumberFromPackage": 12345678,
}

SYSTEM_LOG_STATUS: dict = {
    "readyForStreaming": False,
    "streamingInProgress": False,
    "started": False,
    "error": False,
    "content": "",
    "selectedNodes": "",
    "selectedContent": "",
    "packageNodes": "",
}

SYSTEM_HEALTH_STATUS: dict = {
    "readyForStreaming": False,
    "streamingInProgress": False,
    "error": False,
    "started": False,
    "content": "",
}


def default_ns_settings() -> dict[str, dict]:
    """Return a fresh dict of default namespace-level settings sub-resources."""
    return {
        "complianceSettings": {
            "enabledModes": ["enterprise"],
            "allowDisablingCompliance": True,
        },
        "protocols": {
            "http": {
                "httpEnabled": True,
                "httpsEnabled": True,
                "httpsRequired": False,
                "hsIncludedPaths": [],
                "hsExcludedPaths": [],
                "allowOrigins": [],
                "exposeHeaders": [],
                "allowHeaders": [],
            },
            "cifs": {
                "cifsEnabled": False,
                "caseSensitiveNamespaces": False,
            },
            "nfs": {
                "nfsEnabled": False,
                "uid": 0,
                "gid": 0,
            },
            "smtp": {
                "smtpEnabled": False,
                "emailDomain": "",
            },
        },
        "permissions": {
            "readAllowed": True,
            "writeAllowed": True,
            "deleteAllowed": True,
            "purgeAllowed": False,
            "searchAllowed": True,
            "readAclAllowed": True,
            "writeAclAllowed": True,
        },
        "cors": {
            "corsConfiguration": [],
        },
        "customMetadataIndexingSettings": {
            "indexingEnabled": False,
        },
        "replicationCollisionSettings": {
            "dispositionAction": "log",
        },
        "versioningSettings": {
            "enabled": False,
            "pruneDaysAfterDelete": 0,
        },
    }


# ── Replication ────────────────────────────────────────────────────

REPLICATION_SERVICE: dict[str, Any] = {
    "allowTenantsToMonitorNamespaces": True,
    "enableDNSFailover": False,
    "enableDomainAndCertificateSynchronization": True,
    "network": "ALL",
    "connectivityTimeoutSeconds": 300,
    "verification": "ON_DEMAND",
    "status": "RUNNING",
}

REPLICATION_CERTIFICATES: list[dict[str, Any]] = [
    {
        "id": "cert-001",
        "subjectDN": "CN=hcp-east.example.com,O=Example Corp,C=US",
        "validOn": "2024-01-01T00:00:00+0000",
        "expiresOn": "2027-01-01T00:00:00+0000",
    },
    {
        "id": "cert-002",
        "subjectDN": "CN=hcp-west.example.com,O=Example Corp,C=US",
        "validOn": "2025-06-01T00:00:00+0000",
        "expiresOn": "2028-06-01T00:00:00+0000",
    },
]

REPLICATION_LINKS: dict[str, dict[str, Any]] = {
    "east-west-primary": {
        "name": "east-west-primary",
        "type": "ACTIVE_ACTIVE",
        "description": "Primary replication link between east and west data centers",
        "connection": {
            "remoteHost": "hcp-west.example.com",
            "remotePort": 5748,
            "localHost": "hcp-east.example.com",
            "localPort": 5748,
        },
        "compression": True,
        "encryption": True,
        "priority": "OLDEST_FIRST",
        "id": "link-uuid-001",
        "status": "ACTIVE",
        "statusMessage": "Link is healthy and replicating",
        "suspended": False,
        "failoverSettings": {
            "local": {
                "autoFailover": True,
                "autoFailoverMinutes": 120,
                "autoCompleteRecovery": False,
            },
            "remote": {
                "autoFailover": True,
                "autoFailoverMinutes": 120,
                "autoCompleteRecovery": False,
            },
        },
        "statistics": {
            "upToDateAsOfString": "2026-03-13T10:00:00+0000",
            "upToDateAsOfMillis": 1773500400000,
            "bytesPending": 1048576,
            "bytesReplicated": 107374182400,
            "bytesPerSecond": 52428800.0,
            "objectsPending": 15,
            "objectsReplicated": 250000,
            "operationsPerSecond": 120.5,
            "errors": 0,
            "errorsPerSecond": 0.0,
        },
    },
    "east-dr-backup": {
        "name": "east-dr-backup",
        "type": "OUTBOUND_ONLY",
        "description": "Disaster recovery backup link",
        "connection": {
            "remoteHost": "hcp-dr.example.com",
            "remotePort": 5748,
            "localHost": "hcp-east.example.com",
            "localPort": 5748,
        },
        "compression": True,
        "encryption": True,
        "priority": "OLDEST_FIRST",
        "id": "link-uuid-002",
        "status": "ACTIVE",
        "statusMessage": "Outbound replication active",
        "suspended": False,
        "statistics": {
            "upToDateAsOfString": "2026-03-13T09:55:00+0000",
            "upToDateAsOfMillis": 1773500100000,
            "bytesPending": 524288,
            "bytesReplicated": 53687091200,
            "bytesPerSecond": 26214400.0,
            "objectsPending": 5,
            "objectsReplicated": 125000,
            "operationsPerSecond": 60.2,
            "errors": 2,
            "errorsPerSecond": 0.001,
        },
    },
}

REPLICATION_LINK_CONTENT: dict[str, dict[str, Any]] = {
    "east-west-primary": {
        "tenants": ["tenant1"],
        "defaultNamespaceDirectories": [],
        "chainedLinks": [],
    },
    "east-dr-backup": {
        "tenants": ["tenant1"],
        "defaultNamespaceDirectories": [],
        "chainedLinks": [],
    },
}

REPLICATION_LINK_SCHEDULES: dict[str, dict[str, Any]] = {
    "east-west-primary": {
        "local": {
            "scheduleOverride": "NONE",
            "transition": [
                {"time": "00:00", "performanceLevel": "HIGH"},
                {"time": "08:00", "performanceLevel": "MEDIUM"},
                {"time": "18:00", "performanceLevel": "HIGH"},
            ],
        },
        "remote": {
            "scheduleOverride": "NONE",
            "transition": [
                {"time": "00:00", "performanceLevel": "HIGH"},
            ],
        },
    },
    "east-dr-backup": {
        "local": {"scheduleOverride": "NONE", "transition": []},
        "remote": {"scheduleOverride": "NONE", "transition": []},
    },
}

# ── Erasure Coding ─────────────────────────────────────────────────

EC_TOPOLOGIES: dict[str, dict[str, Any]] = {
    "ec-topology-1": {
        "name": "ec-topology-1",
        "type": "2+1",
        "description": "Primary erasure coding topology for cost-efficient storage",
        "erasureCodingDelay": 0,
        "fullCopy": False,
        "minimumObjectSize": 4096,
        "restorePeriod": 0,
        "id": "ec-uuid-001",
        "state": "ACTIVE",
        "protectionStatus": "PROTECTED",
        "readStatus": "AVAILABLE",
        "erasureCodedObjects": 85000,
        "replicationLinks": [
            {
                "name": "east-west-primary",
                "uuid": "link-uuid-001",
                "hcpSystems": ["hcp-east.example.com", "hcp-west.example.com"],
                "pausedTenantsCount": 0,
                "state": "ACTIVE",
            },
        ],
        "hcpSystems": [
            "hcp-east.example.com",
            "hcp-west.example.com",
            "hcp-dr.example.com",
        ],
        "tenants": ["tenant1"],
    },
}

EC_TOPOLOGY_TENANTS: dict[str, list[dict[str, Any]]] = {
    "ec-topology-1": [
        {
            "name": "tenant1",
            "uuid": "tenant-uuid-1",
            "hcpSystems": ["hcp-east.example.com"],
        },
    ],
}
