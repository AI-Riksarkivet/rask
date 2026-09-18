"""The catalog Service pins a caller to one replica, because its poll cursor is per-replica.

[[LH-107]]. `control_buffer.py` states the shape: each catalog replica subscribes to
`catalog.control.v1` WITHOUT a queueGroupName, so every replica holds its OWN ring buffer and its own
monotonic cursor. `values-prod.yaml` runs two of them. Round-robined, the console hands replica A's
cursor to replica B, which reads it against a counter that never produced it.

MEASURED ON TWO LIVE REPLICAS 2026-09-18, and the row's stated symptom was the milder one. Thirty
polls from a single pod with affinity OFF returned cursors `0, 0, 0, 0, 18, 18, 18, 0` and
**`reset=0`** — a cursor AHEAD of a replica's counter has not fallen off the end, so that replica
answers an EMPTY PAGE. The console is never told to re-read; it is told there is nothing new, and
whatever that replica holds is silently skipped. The same thirty polls with affinity ON held cursor
18 throughout.

RECOVERABLE, NOT CORRUPTING, which is why the fix is affinity rather than a shared buffer: the events
are hints and the audit trail is the durable record, so nothing is lost that cannot be re-read. What
is wrong is that the console cannot tell a quiet estate from a replica it is not talking to.

THE CALLER IS A POD, which is what makes `ClientIP` the right key here and is worth pinning because
it is the assumption the whole fix rests on. The console's `/v1/events` poll runs in a SvelteKit
remote function (`feeds.remote.ts`), server-side, and the home zone reaches
`CATALOG_API=http://<release>-catalog:<port>` — the catalog Service DIRECTLY, not through the
gateway. So the source address the Service sees is a stable zone pod, and every other caller (stage
runners, medallion, maintenance) is its own pod and keeps spreading across replicas.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", *extra]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d]


def _service(docs: list[dict], suffix: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "Service" and doc.get("metadata", {}).get("name", "").endswith(suffix):
            return doc
    raise AssertionError(f"no Service ending in {suffix!r} rendered")


def test_the_catalog_service_pins_a_caller_to_one_replica() -> None:
    spec = _service(_render(), "-catalog")["spec"]

    assert spec.get("sessionAffinity") == "ClientIP", (
        "the catalog Service load-balances a poll whose cursor is per-replica, so the console's "
        f"`/v1/events` resets on every replica change: {spec.get('sessionAffinity')!r}"
    )


def test_the_affinity_survives_a_poll_interval() -> None:
    """An affinity that expires between polls is the same defect with a delay. Stated explicitly
    rather than left to kube-proxy's default, so the window is a decision someone made."""
    spec = _service(_render(), "-catalog")["spec"]
    timeout = spec.get("sessionAffinityConfig", {}).get("clientIP", {}).get("timeoutSeconds")

    assert timeout, "no explicit affinity timeout — the poll window is whatever kube-proxy defaults to"
    assert timeout >= 600, f"an affinity of {timeout}s is shorter than a console session, so the cursor still resets"


def test_the_GATEWAY_is_not_pinned_the_same_way() -> None:
    """The guard against fixing this estate-wide. The gateway sits behind the Ingress, so every
    request reaches it from the ingress controller's pod — one address for the whole internet. Keyed
    on ClientIP, that pins ALL traffic to a single gateway replica and turns a load balancer into a
    single instance. The catalog is safe to pin precisely because its callers are distinct pods."""
    spec = _service(_render(), "-gateway")["spec"]

    assert "sessionAffinity" not in spec, (
        "the gateway Service is pinned by ClientIP, but its client is the ingress controller — "
        "one source address for every caller, so this sends the whole estate to one replica"
    )
