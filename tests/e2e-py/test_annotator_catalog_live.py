"""MERGE MILESTONE 2 — the annotator SERVICE in catalog mode, against a LIVE catalog.

One level above ``test_catalog_live.py`` (which proves the transports): here the
REAL FastAPI app runs with ``MEDIA_READ/WRITE_BACKEND=catalog`` and the whole HTTP
loop goes browser-shaped requests → annotator → lance-ns catalog → RustFS:
wire GET (catalog-sourced version header) → save 2xx → STALE base_version → 409
problem+json → tag batch → re-read. Auto-skips unless ``MEDIA_CATALOG_URL`` is set
AND the demo dataset (``transcripts_v2.lance``) exists locally (the corpus stays
direct/local by decision — only the annotations plane routes through the catalog).

The catalog table lives under a fresh uuid namespace per run (settings-derived via
``MEDIA_CATALOG_NAMESPACE``), seeded through the catalog by the same helper the
E2E seed script uses — never by writing local files.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pyarrow as pa
import pytest


CATALOG_URL = os.environ.get("MEDIA_CATALOG_URL", "")
# The corpus stays on its own mount (runbook: hostPath, NO data move) — honor MEDIA_DB
# so the suite runs wherever the demo dataset is mounted; the lance-audio default keeps
# pre-merge invocations working unchanged.
DB = Path(os.environ.get("MEDIA_DB", "transcripts_v2.lance"))
DOC, SPEECH, CHUNK = "fe00cd746463ad2c", 0, 19  # the demo unit the seeder targets

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.media_catalog,
    pytest.mark.skipif(
        not CATALOG_URL or not DB.exists(),
        reason="MEDIA_CATALOG_URL not set or demo dataset absent — live catalog-mode run required",
    ),
]


@pytest.fixture(scope="module")
def client():
    """The real annotator app, booted in catalog mode against a fresh catalog table."""
    from fastapi.testclient import TestClient

    # Module-scoped fixture ⇒ the function-scoped `monkeypatch` fixture is
    # unavailable; a directly instantiated MonkeyPatch is pytest's documented
    # answer, and undo() restores the env AND sys.path in one sweep.
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from seed_annotations import seed_catalog  # ty: ignore[unresolved-import] — sys.path above

    namespace = f"m2_{uuid.uuid4().hex[:8]}"
    seed_catalog(CATALOG_URL, namespace, DOC)

    for key, value in {
        "MEDIA_DB": str(DB),
        "MEDIA_READ_BACKEND": "catalog",
        "MEDIA_WRITE_BACKEND": "catalog",
        "MEDIA_CATALOG_URI": CATALOG_URL,
        "MEDIA_CATALOG_NAMESPACE": namespace,
    }.items():
        mp.setenv(key, value)
    try:
        from annotator.core.config import get_annotator_settings
        from annotator.main import app

        get_annotator_settings.cache_clear()
        with TestClient(app) as test_client:
            yield test_client
    finally:
        mp.undo()
        get_annotator_settings.cache_clear()


def _get_annotations(client) -> tuple[pa.Table, int]:
    response = client.get(f"/api/annotations/{DOC}/{SPEECH}/{CHUNK}")
    assert response.status_code == 200, response.text
    table = pa.ipc.open_stream(response.content).read_all()
    return table, int(response.headers["X-Annotations-Version"])


def test_service_loop_in_catalog_mode(client) -> None:
    """The condition-4 story over the SERVICE API, versions sourced from the catalog."""
    # 1. Wire GET: the seeded 3 rows arrive as Arrow, version header from the catalog.
    table, loaded = _get_annotations(client)
    assert table.num_rows == 3
    assert loaded >= 1

    # 2. Save: one edit + one new shape, against the loaded version → 2xx, version advances.
    save = client.post(
        f"/api/annotations/{DOC}/{SPEECH}/{CHUNK}",
        json={
            "edits": [{"id": "a3", "label": "figure-reviewed", "status": "accepted"}],
            "inserts": [
                {
                    "id": "m2-new-1",
                    "shape_type": "rectangle",
                    "x": 10.0,
                    "y": 10.0,
                    "width": 50.0,
                    "height": 40.0,
                    "label": "stamp",
                }
            ],
            "base_version": loaded,
        },
        headers={"X-User": "m2-tester"},
    )
    assert save.status_code == 200, save.text
    committed = save.json()["version"]
    assert committed > loaded

    # 3. A STALE save (the version we loaded before the commit) → 409 problem+json.
    stale = client.post(
        f"/api/annotations/{DOC}/{SPEECH}/{CHUNK}",
        json={"edits": [{"id": "a1", "label": "too-late"}], "base_version": loaded},
        headers={"X-User": "m2-tester"},
    )
    assert stale.status_code == 409, stale.text
    assert "changed on the server" in stale.json()["detail"]

    # 4. Tag batch through the same catalog path.
    tags = client.post(
        "/api/annotations/tags",
        json={
            "adds": [{"doc_id": DOC, "keys": [SPEECH, CHUNK], "labels": ["m2-tag"]}],
            "base_version": committed,
        },
        headers={"X-User": "m2-tester"},
    )
    assert tags.status_code == 200, tags.text
    assert tags.json()["version"] > committed

    # 5. Re-read: the edit, the new shape, and the tag all round-trip; the header
    #    reflects the newest catalog version, and the server-stamped reviewer stuck.
    final, version = _get_annotations(client)
    assert version == tags.json()["version"]
    by_id = {row["id"]: row for row in final.to_pylist()}
    assert by_id["a3"]["label"] == "figure-reviewed"
    assert by_id["a3"]["reviewer"] == "m2-tester"  # server-stamped, not client-claimed
    assert by_id["m2-new-1"]["label"] == "stamp"
    assert any(row["label"] == "m2-tag" for row in by_id.values())
