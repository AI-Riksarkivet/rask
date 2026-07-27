"""MERGE MILESTONE 1 — the annotations loop against a LIVE lance-ns catalog.

Auto-skips unless ``MEDIA_CATALOG_URL`` points at a running catalog (e.g.
``http://127.0.0.1:8180`` booted from the lance-ns checkout against RustFS).
Everything here drives OUR seams — ``RestCatalogTransport`` /
``RestCatalogWriteTransport`` behind ``MEDIA_READ/WRITE_BACKEND`` and the real
``check_base_version_value`` conflict check — against THEIR wire: table create
(Arrow-IPC stream), query (empty-vector scan → Arrow-IPC file), merge_insert
(stream body), delete, and the version primitive
(``describe?load_detailed_metadata=true``).

Each run creates a FRESH uuid-suffixed table so re-runs never collide; the
catalog owns the storage (RustFS bucket) and its own cleanup policy. The loop
is ONE scenario test — its steps are a story (save → conflict → predict →
replace), deliberately sequential, not independent cases.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import UTC, datetime

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from annotator.annotations.commit import check_base_version_value
from annotator.annotations.schema import EMPTY_SCHEMA
from common.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from common.lancekit.reader import CatalogTableReader, RestCatalogTransport
from common.lancekit.writer import CatalogTableWriter, RestCatalogWriteTransport


CATALOG_URL = os.environ.get("MEDIA_CATALOG_URL", "")
# Auth-on catalogs need a bearer (the merged estate's default posture) — same env the
# catalog-mode transports and the seeder honor; empty = auth-off, header omitted.
CATALOG_TOKEN = os.environ.get("MEDIA_CATALOG_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {CATALOG_TOKEN}"} if CATALOG_TOKEN else {}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.media_catalog,
    pytest.mark.skipif(not CATALOG_URL, reason="MEDIA_CATALOG_URL not set — live lance-ns catalog required"),
]

#: The milestone-1 annotations schema: descriptor identity + the contract columns.
#: The settle-while-empty additions are DONE — created_at/updated_at live in
#: EMPTY_SCHEMA itself now (and the save paths stamp them), so this fixture is
#: pure composition again: identity + the backend contract, nothing appended.
SCHEMA = pa.schema(
    [
        ("doc_id", pa.string()),
        ("speech_id", pa.int64()),
        ("chunk_id", pa.int64()),
        ("frame_idx", pa.int64()),
        *EMPTY_SCHEMA,
    ]
)

NAMESPACE = "media"


def _row(id_: str, *, source: str, status: str, confidence: float, label: str = "person") -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "doc_id": "m1doc",
        "speech_id": 0,
        "chunk_id": 1,
        "frame_idx": 0,
        "id": id_,
        "shape_type": "rectangle",
        "x": 1.0,
        "y": 2.0,
        "width": 3.0,
        "height": 4.0,
        "rotation": 0.0,
        "polygon": [],
        "t_start": 0.0,
        "t_end": 0.0,
        "text": "",
        "label": label,
        "status": status,
        "source": source,
        "reviewer": "m1",
        "confidence": confidence,
        "uncertainty": 1.0 - confidence,
        "model_version": source.split("@")[-1] if "@" in source else "",
        "group": "",
        "group_id": "",
        "reading_order": 0,
        "difficult": False,
        "links": "",
        "mask": "",
        "metadata": "",
        "created_at": now,
        "updated_at": now,
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return pa.table(
        {f.name: pa.array([r[f.name] for r in rows], type=f.type) for f in SCHEMA},
        schema=SCHEMA,
    )


@pytest.fixture(scope="module")
def table_id() -> list[str]:
    """Create a fresh annotations table THROUGH the catalog (namespace + Arrow-IPC
    stream create + maintenance policy) — the milestone's condition-3 path."""
    name = f"annotations_m1_{uuid.uuid4().hex[:8]}"
    buf = io.BytesIO()
    with ipc.new_stream(buf, SCHEMA) as writer:
        writer.write_table(_table([]))
    with httpx.Client(base_url=CATALOG_URL, timeout=30, headers=AUTH_HEADERS) as client:
        ns = client.post(f"/v1/namespace/{NAMESPACE}/create", json={})
        assert ns.status_code == 200 or "exist" in ns.text.lower(), ns.text
        created = client.post(
            f"/v1/table/{NAMESPACE}${name}/create",
            content=buf.getvalue(),
            headers={"Content-Type": "application/vnd.apache.arrow.stream"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["version"] == 1
        policy = client.post(
            f"/v1/table/{NAMESPACE}${name}/policy/set",
            json={"retention_days": 30, "retain_versions": 100, "compact_enabled": True},
        )
        assert policy.status_code == 200, policy.text
    return [NAMESPACE, name]


@pytest.fixture(scope="module")
def reader(table_id: list[str]) -> CatalogTableReader:
    return CatalogTableReader(RestCatalogTransport(CATALOG_URL, token=CATALOG_TOKEN or None), table_id)


@pytest.fixture(scope="module")
def writer(table_id: list[str]) -> CatalogTableWriter:
    return CatalogTableWriter(RestCatalogWriteTransport(CATALOG_URL, table_id, token=CATALOG_TOKEN or None), table_id)


def test_schema_round_trips_all_31_columns(reader: CatalogTableReader) -> None:
    table = reader.to_table()
    assert table.num_rows == 0
    assert table.schema.names == SCHEMA.names
    assert len(table.schema) == 31
    # .schema is a limit-0 scan under the hood — must round-trip over REST too.
    assert reader.schema.names == SCHEMA.names
    assert reader.to_table(limit=0).num_rows == 0


def test_milestone_loop(reader: CatalogTableReader, writer: CatalogTableWriter) -> None:
    """The whole condition-4 story in order: human save → 409 handshake → model
    predictions → replace-protects-humans → insert-only never clobbers."""
    # 1. Human save, then read it back.
    writer.merge_upsert(_table([_row("human-1", source="human", status="accepted", confidence=1.0)]), on="id")
    got = reader.to_table(filter="source = 'human'")
    assert got.num_rows == 1
    assert got["label"][0].as_py() == "person"

    # 2. The 409 handshake against THEIR version primitive: client A loads at v,
    #    client B commits, A's stale save must be rejected.
    loaded = reader.table_version()
    writer.merge_upsert(_table([_row("human-2", source="human", status="accepted", confidence=1.0)]), on="id")
    current = reader.table_version()
    assert current > loaded
    with pytest.raises(ConflictError, match=rf"loaded v{loaded}, now v{current}"):
        check_base_version_value(current, loaded)
    check_base_version_value(current, current)  # a fresh load passes

    # 3. Model predictions land…
    writer.merge_upsert(
        _table(
            [
                _row("pred-1", source="model:x@1", status="prediction", confidence=0.7, label="car"),
                _row("pred-2", source="model:x@1", status="prediction", confidence=0.4, label="dog"),
            ]
        ),
        on="id",
    )
    assert reader.count_rows("source LIKE 'model:%'") == 2

    # 4. …and a model re-run deletes ONLY its predictions: humans survive.
    writer.delete("source LIKE 'model:%' AND status = 'prediction'")
    remaining = reader.to_table(columns=["id", "source"])
    assert sorted(remaining["id"].to_pylist()) == ["human-1", "human-2"]
    assert set(remaining["source"].to_pylist()) == {"human"}

    # 5. The insert-only leg leaves an existing human row AS-IS.
    writer.merge_insert_only(
        _table(
            [
                _row(
                    "human-1",
                    source="human",
                    status="accepted",
                    confidence=1.0,
                    label="OVERWRITE-ATTEMPT",
                )
            ]
        ),
        on="id",
    )
    got = reader.to_table(filter="id = 'human-1'", columns=["label"])
    assert got["label"][0].as_py() == "person"


def test_catalog_errors_translate_to_domain_errors() -> None:
    """A missing table surfaces as OUR typed DomainError (problem+json downstream),
    never the generated client's raw ApiException → opaque 500. Auth-off: NotFoundError.
    GOVERNED (token set): the catalog fail-closes authz BEFORE existence — a table with no
    FGA object answers 403, never leaking whether it exists — so ForbiddenError is the
    correct governed translation (the estate's no-existence-leak posture)."""
    missing = CatalogTableReader(RestCatalogTransport(CATALOG_URL, token=CATALOG_TOKEN or None), [NAMESPACE, "no_such_table"])
    with pytest.raises(ForbiddenError if CATALOG_TOKEN else NotFoundError):
        missing.to_table(limit=1)
