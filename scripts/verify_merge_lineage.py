"""#Phase-2 live proof: a version-pinned + run-faceted merge_insert → the lineage graph shows it.

As alice (a real Dex id_token), create a source + dest table she owns, write the source TWICE (so the pin
names a NON-latest version), then merge_insert into dest with ``source=<src>&source_version=1`` and an
``X-Lance-Run-Facets`` header. Then read the live lineage graph and prove the three training-shaped artifacts,
plus a failure-injection round proving the audit's forgery/500 vectors are rejected on the deployed image:

  1. input edge      — GET /datasets/<dst>/upstream shows <src>              (DERIVED_FROM)
  2. version pin      — GET /runs/<run_id>/inputs shows <src> @ version 1    (#115 reproducibility READ pin)
  3. params run facet — GET /events shows the merge event's run.facets.params carried verbatim
  4. hardening        — reserved-name / producer-key / giant-int facets → 400; unreadable source → 403

Driven by ``scripts/verify_merge_lineage.sh`` (port-forwards + Dex token). Env: CATALOG, LINEAGE, ALICE, NS.
"""

from __future__ import annotations

import os
import sys
import time

import httpx
import pyarrow as pa


CATALOG = os.environ["CATALOG"].rstrip("/")
LINEAGE = os.environ["LINEAGE"].rstrip("/")
ALICE = os.environ["ALICE"]
NS = os.environ.get("NS", "mrg")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
AUTH = {"Authorization": f"Bearer {ALICE}"}


def ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def step(msg: str) -> None:
    print(f"\n── {msg}")


def must(cond: bool, msg: str) -> None:
    print(f"   {'✓' if cond else '✗ FAIL:'} {msg}")
    if not cond:
        sys.exit(1)


src, dst = f"{NS}$src", f"{NS}$dst"

with httpx.Client(timeout=30.0) as c:
    step(f"1) alice creates namespace {NS} + source/dest tables she owns (read src, write dst)")
    r = c.post(f"{CATALOG}/v1/namespace/{NS}/create", json={"properties": {}}, headers=AUTH)
    must(r.status_code == 200, f"create namespace {NS} -> {r.status_code} {r.text[:120]}")
    r = c.post(
        f"{CATALOG}/v1/table/{src}/create",
        content=ipc(pa.table({"id": [1, 2], "v": [10, 20]})),
        headers={**ARROW, **AUTH},
    )
    must(r.status_code == 200, f"create {src}@v1 -> {r.status_code} {r.text[:120]}")
    r = c.post(
        f"{CATALOG}/v1/table/{src}/insert?mode=append",
        content=ipc(pa.table({"id": [3], "v": [30]})),
        headers={**ARROW, **AUTH},
    )
    must(r.status_code == 200, f"append {src} -> v2 -> {r.status_code} {r.text[:120]}")
    r = c.post(
        f"{CATALOG}/v1/table/{dst}/create",
        content=ipc(pa.table({"id": [1], "v": [100]})),
        headers={**ARROW, **AUTH},
    )
    must(r.status_code == 200, f"create {dst} -> {r.status_code} {r.text[:120]}")

    step(f"2) merge_insert into {dst} with source={src}&source_version=1 (a NON-latest pin) + run facets")
    merge_url = f"{CATALOG}/v1/table/{dst}/merge_insert?on=id&when_matched_update_all=true&when_not_matched_insert_all=true&source={src}&source_version=1"
    r = c.post(
        merge_url,
        content=ipc(pa.table({"id": [1, 2], "v": [111, 222]})),
        headers={
            **ARROW,
            **AUTH,
            "X-Lance-Run-Facets": '{"params": {"lr": 0.01, "epochs": 5, "model": "demo"}}',
        },
    )
    must(r.status_code == 200, f"merge_insert -> {r.status_code} {r.text[:200]}")
    print(f"   merge response: {r.json()}")

    step("3) wait for the Dapr lineage event to ingest into AGE, then read the graph")
    time.sleep(6)

    r = c.get(f"{LINEAGE}/datasets/{dst}/upstream", headers=AUTH)
    must(r.status_code == 200, f"GET /datasets/{dst}/upstream -> {r.status_code} {r.text[:200]}")
    names = {u.get("name") for u in r.json().get("related", [])}
    must(src in names, f"(1) input edge: {dst} DERIVED_FROM {src} (got {names})")

    r = c.get(f"{LINEAGE}/events?limit=50", headers=AUTH)
    must(r.status_code == 200, f"GET /events -> {r.status_code} {r.text[:200]}")
    merge_ev = None
    for rec in r.json().get("events", []):
        full = rec.get("event", {}) or {}
        facets = (full.get("run", {}) or {}).get("facets", {}) or {}
        if dst in set(rec.get("outputs", [])) and "params" in facets:
            merge_ev = full
            break
    must(merge_ev is not None, f"merge event for {dst} present in /events with a params facet")
    assert merge_ev is not None
    params = merge_ev["run"]["facets"]["params"]
    print(f"   params facet = {params}")
    must(
        params.get("lr") == 0.01 and params.get("epochs") == 5,
        "(3) params carried verbatim (lr=0.01, epochs=5)",
    )
    must("_producer" in params and "_schemaURL" in params, "params facet stamped spec-legal by the catalog")
    pinned = [i for i in merge_ev.get("inputs", []) if i.get("name") == src and (i.get("facets", {}) or {}).get("version")]
    must(bool(pinned), f"emitted event input {src} carries a DatasetVersionDatasetFacet")
    must(pinned[0]["facets"]["version"].get("datasetVersion") == "1", "input version facet pinned to 1")

    run_id = merge_ev["run"]["runId"]
    r = c.get(f"{LINEAGE}/runs/{run_id}/inputs", headers=AUTH)
    must(r.status_code == 200, f"GET /runs/{run_id}/inputs -> {r.status_code} {r.text[:200]}")
    ri = [i for i in r.json().get("inputs", []) if i.get("name") == src]
    print(f"   run_inputs = {r.json().get('inputs')}")
    must(
        bool(ri) and str(ri[0].get("version")) == "1",
        "(2) READ-edge version pinned to 1 (the #115 reproducibility pin)",
    )

    step("4) live failure-injection — the audit's forgery/500 vectors must be rejected on the DEPLOYED image")
    base = f"{CATALOG}/v1/table/{dst}/merge_insert?on=id&when_matched_update_all=true"
    body = ipc(pa.table({"id": [1], "v": [1]}))

    def negative(desc: str, expect: int, *, header: str | None = None, qs: str = "") -> None:
        extra = {"X-Lance-Run-Facets": header} if header is not None else {}
        rr = c.post(base + qs, content=body, headers={**ARROW, **AUTH, **extra})
        must(rr.status_code == expect, f"{desc}: got {rr.status_code} (want {expect}) {rr.text[:100]}")

    negative("forge author facet → 400", 400, header='{"author": {"sub": "admin"}}')
    negative("forge lance op facet → 400", 400, header='{"lance": {"operation": "create_table"}}')
    negative("producer-key facet → 400", 400, header='{"p": {"producer": "x"}}')
    negative("giant-int facet → 400 (not 500)", 400, header='{"p": {"n": ' + "9" * 4400 + "}}")
    negative("unreadable source → 403", 403, qs="&source=nobody$secret&source_version=1")
    negative("blank source + version → 400", 400, qs="&source=&source_version=1")

print("\n✅ PHASE 2 LIVE PROOF: input edge + version pin + params facet present in the live lineage graph,")
print("   AND every audit forgery/500 vector rejected on the deployed image.")
