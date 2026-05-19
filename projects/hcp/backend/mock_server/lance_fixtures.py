"""Mock Lance dataset fixtures for frontend development."""

from __future__ import annotations

LANCE_TABLES: dict[str, list[str]] = {
    "ml-data/": ["embeddings", "documents", "images"],
    "analytics/reports": ["metrics"],
}

LANCE_SCHEMAS: dict[str, dict] = {
    "embeddings": {
        "table_name": "embeddings",
        "fields": [
            {
                "name": "id",
                "type": "int64",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "text",
                "type": "string",
                "nullable": True,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "vector",
                "type": "fixed_size_list<item: float>[512]",
                "nullable": True,
                "is_vector": True,
                "is_binary": False,
                "vector_dim": 512,
            },
            {
                "name": "metadata",
                "type": "struct<source: string, date: string>",
                "nullable": True,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
        ],
    },
    "images": {
        "table_name": "images",
        "fields": [
            {
                "name": "id",
                "type": "int64",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "label",
                "type": "string",
                "nullable": True,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "image",
                "type": "binary",
                "nullable": True,
                "is_vector": False,
                "is_binary": True,
                "vector_dim": None,
            },
            {
                "name": "embedding",
                "type": "fixed_size_list<item: float>[256]",
                "nullable": True,
                "is_vector": True,
                "is_binary": False,
                "vector_dim": 256,
            },
        ],
    },
    "documents": {
        "table_name": "documents",
        "fields": [
            {
                "name": "doc_id",
                "type": "string",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "content",
                "type": "string",
                "nullable": True,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "created_at",
                "type": "timestamp[us]",
                "nullable": True,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
        ],
    },
    "metrics": {
        "table_name": "metrics",
        "fields": [
            {
                "name": "ts",
                "type": "timestamp[us]",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "value",
                "type": "float64",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
        ],
    },
}

LANCE_ROWS: dict[str, list[dict]] = {
    "embeddings": [
        {
            "id": 1,
            "text": "Sample document about AI",
            "vector": {
                "type": "vector",
                "dim": 512,
                "norm": 1.0,
                "min": -0.05,
                "max": 0.08,
                "mean": 0.001,
                "preview": [0.02, -0.01, 0.03, 0.05],
            },
            "metadata": {"source": "web", "date": "2024-01-15"},
        },
        {
            "id": 2,
            "text": "Another document",
            "vector": {
                "type": "vector",
                "dim": 512,
                "norm": 0.98,
                "min": -0.04,
                "max": 0.07,
                "mean": 0.002,
                "preview": [0.01, 0.02, -0.01, 0.04],
            },
            "metadata": {"source": "pdf", "date": "2024-02-10"},
        },
    ],
    "images": [
        {
            "id": 1,
            "label": "cat",
            "image": {"size": 1024},
            "embedding": {
                "type": "vector",
                "dim": 256,
                "norm": 0.95,
                "min": -0.1,
                "max": 0.1,
                "mean": 0.0,
                "preview": [0.01, -0.02],
            },
        },
    ],
    "documents": [
        {
            "doc_id": "doc-001",
            "content": "Introduction to LanceDB",
            "created_at": "2024-06-01T12:00:00",
        },
    ],
    "metrics": [
        {"ts": "2024-01-01T00:00:00", "value": 42.5},
        {"ts": "2024-01-02T00:00:00", "value": 43.1},
    ],
}

LANCE_VECTOR_PREVIEW: dict[str, dict[str, dict]] = {
    "embeddings": {
        "vector": {
            "stats": {
                "count": 2,
                "dim": 512,
                "min": -0.05,
                "max": 0.08,
                "mean": 0.0015,
            },
            "preview": [
                {"norm": 1.0, "sample": [0.02, -0.01, 0.03, 0.05]},
                {"norm": 0.98, "sample": [0.01, 0.02, -0.01, 0.04]},
            ],
        },
    },
}
