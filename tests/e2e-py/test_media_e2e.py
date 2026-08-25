"""End-to-end test for the MEDIA lane of the event-driven cascade (§9 — the multimodal goal, live).

ONE call to medallion-producer's ``/ingest-media`` must land external media as bronze-media blobs, trigger the
media mover over Dapr pub/sub, and end with the lineage graph showing ``silver-media$features`` derived
from ``bronze-media$objects`` WITH the derived artifact columns (thumbnail + embedding) in its per-version
schema. This is the regression guard for "the deployed combination actually derives media" — the gap the
strategic audit found (derivation used to exist only in manual scripts).

Run (port-forward medallion-producer + lineage first), or ``make e2e-media``:

    kubectl port-forward svc/lance-ns-medallion-producer 8002:8000 &
    kubectl port-forward svc/lance-ns-lineage   8000:8000 &
    LANCE_E2E_LANCERAY_URL=http://localhost:8002 LANCE_E2E_LINEAGE_URL=http://localhost:8000 \
    uv run pytest tests/e2e-py/test_media_e2e.py -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests


LANCERAY = os.environ.get("LANCE_E2E_LANCERAY_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")

# Governed lineage READS use the app-token SERVICE door as `service-web` (a warehouse reader — the same
# read-only identity the web BFF uses). On an auth-OFF stack OIDC is off → authenticate() pass-through,
# so these headers are harmless; on the auth-ON stack they're what lets the reads through (else 401).
_LINEAGE_HEADERS = {"dapr-api-token": DAPR_TOKEN, "x-lance-service-identity": "service-web"} if DAPR_TOKEN else {}

#: NOT project-qualified, and that is the media lane's actual contract rather than an omission.
#: `seed_medallion_fga.sh` says it outright — "media lanes stay estate-only (the media pipeline is not
#: project-qualified — #84 scope)" — and the deployed mover agrees: it targets `silver-media$features`
#: verbatim. Qualifying these names made the suite look for `<project>-silver-media$features`, a table
#: nothing writes.
SILVER = "silver-media$features"
BRONZE = "bronze-media$objects"

pytestmark = [pytest.mark.e2e, pytest.mark.media]


@pytest.fixture(scope="module")
def urls() -> tuple[str, str]:
    if not (LANCERAY and LINEAGE):
        pytest.skip("set LANCE_E2E_LANCERAY_URL and LANCE_E2E_LINEAGE_URL (see module docstring)")
    for name, url in (("medallion-producer", LANCERAY), ("lineage", LINEAGE)):
        try:
            requests.get(f"{url.rstrip('/')}/livez", timeout=5).raise_for_status()
        except Exception:
            pytest.skip(f"{name} not reachable at {url}")
    return LANCERAY.rstrip("/"), LINEAGE.rstrip("/")


def test_ingest_media_derives_artifacts_through_the_deployed_cascade(urls: tuple[str, str]) -> None:
    lance_ray, lineage = urls

    # Snapshot the run count FIRST (like the medallion e2e): silver-media may already exist from an
    # earlier run, so schema assertions alone could false-pass — a strictly rising run count proves THIS
    # trigger flowed (ingest run + derive run = at least +2).
    before = requests.get(f"{lineage}/runs?limit=1000", headers=_LINEAGE_HEADERS, timeout=8)
    before.raise_for_status()
    runs_before = len(before.json().get("runs", []))

    headers = {"dapr-api-token": DAPR_TOKEN} if DAPR_TOKEN else {}
    resp = requests.post(f"{lance_ray}/ingest-media", headers=headers, timeout=60)
    if resp.status_code == 409:
        # The baseline demo stack runs the cascade compute-off (dummy lineage-only movers) — the media
        # head is then deliberately unconfigured. The lane is exercised on the compute-on combo:
        #   helm upgrade ... --set medallion.compute=true --set openbao.enabled=false
        pytest.skip("media head not configured (medallion.compute off on this stack)")
    assert resp.status_code == 202, resp.text
    token = resp.json()["token"]

    # Poll until THIS run's cascade lands: the graph gains at least 2 runs (ingest + derive) AND the
    # derived silver's schema carries the artifacts. Schema alone would false-pass from an earlier run
    # (silver-media persists); count alone wouldn't prove derivation — both together correlate the run.
    fields: dict[str, str] = {}
    runs_after = runs_before
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        schema = requests.get(f"{lineage}/datasets/{SILVER}/schema", headers=_LINEAGE_HEADERS, timeout=8)
        if schema.status_code == 200:
            fields = {f["name"]: f["type"] for f in schema.json().get("fields", [])}
        runs = requests.get(f"{lineage}/runs?limit=1000", headers=_LINEAGE_HEADERS, timeout=8)
        if runs.status_code == 200:
            runs_after = len(runs.json().get("runs", []))
        if "thumbnail" in fields and "embedding" in fields and runs_after >= runs_before + 2:
            break
        time.sleep(3)
    assert runs_after >= runs_before + 2, f"cascade did not flow for token {token}"
    assert "thumbnail" in fields and "embedding" in fields, f"media lane did not derive artifacts for token {token}: schema fields = {fields}"
    assert fields["payload"] == "blob"  # the heavy original is still a blob column in silver
    assert fields["embedding"].startswith("array")  # the vector column, typed in the graph

    # Dataset-level provenance: silver-media derives from the bronze-media blob head.
    upstream = requests.get(f"{lineage}/datasets/{SILVER}/upstream", headers=_LINEAGE_HEADERS, timeout=8)
    upstream.raise_for_status()
    assert BRONZE in {d["name"] for d in upstream.json().get("related", [])}

    # And the bronze head itself recorded the external source objects as its lineage inputs. NOTE: under a
    # GOVERNED read (service-web = a warehouse reader) the external ``s3://…`` source nodes are FGA-filtered
    # — they are not tables in the warehouse hierarchy, so ``can_get_metadata`` drops them (fail-closed).
    # The sources ARE recorded (verifiable ungoverned in AGE); they are only surfaced on an auth-off read,
    # so require them only then. The media PROVENANCE that governance does show (silver⟵bronze) is asserted
    # above; a warehouse reader seeing external source pointers is a separate lineage-governance decision.
    bronze_graph = requests.get(f"{lineage}/datasets/{BRONZE}/upstream", headers=_LINEAGE_HEADERS, timeout=8)
    bronze_graph.raise_for_status()
    sources = {d["name"] for d in bronze_graph.json().get("related", [])}
    if not _LINEAGE_HEADERS:  # auth-off read → external sources are visible; governed read filters them
        assert any(name.startswith("s3://") for name in sources), sources
