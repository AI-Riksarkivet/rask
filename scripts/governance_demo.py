#!/usr/bin/env python3
"""Governance demo — narrate the lance-ns dataops/governance loop end to end.

A runnable story (not just a smoke test): it drives the full governance stack and prints,
step by step, how the three axes meet on one identity (``table:<id>``):

  who-may   (OpenFGA)      — alice owns what she creates; bob, with no grant, is denied
  what/when (Lance)        — the bronze/silver tables are real Lance datasets
  how/who   (OpenLineage)  — the catalog records alice as the *verified* creator; a promote
                             run links silver ← bronze (the medallion lineage)

Run it through the stack that already exists: `dagger call governance-chain --demo`.
Or, against a stack you stood up yourself:

    uv run scripts/governance_demo.py

Config via env (defaults target the local governance stack):
    CATALOG_URL   (default http://localhost:2333)
    LINEAGE_URL   (default http://localhost:8000)
    DEX_URL       (default http://localhost:5556/dex)

Exits non-zero if any governance invariant does not hold.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

import pyarrow as pa
import requests

from lineage.models import author_sub_from_payload


CATALOG = os.environ.get("CATALOG_URL", "http://localhost:2333").rstrip("/")
LINEAGE = os.environ.get("LINEAGE_URL", "http://localhost:8000").rstrip("/")
DEX = os.environ.get("DEX_URL", "http://localhost:5556/dex").rstrip("/")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
GREEN, RED, BOLD, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[0m"

_failures = 0


def step(msg: str) -> None:
    print(f"\n{BOLD}== {msg}{RESET}")


def check(ok: bool, msg: str) -> None:
    global _failures
    print(f"  {GREEN + 'ok  ' if ok else RED + 'FAIL'}{RESET} {msg}")
    if not ok:
        _failures += 1


def token(username: str) -> str:
    resp = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "rask-web",
            "username": username,
            "password": "password",
            "scope": "openid federated:id audience:server:client_id:rask-lakehouse",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id_token"]


def sub_of(tok: str) -> str:
    """The principal the catalog keys this bearer on (`<idp-id>~<user id>`, [[LH-063]])."""
    resp = requests.get(f"{CATALOG}/management/v1/me", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    resp.raise_for_status()
    return str(resp.json()["sub"])


def ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    """GET + parse JSON, degrading to ``{}`` on any transport/non-JSON error (clean poll failure).

    Lineage's reads are governed per table where its auth is on, so a caller forwards the reader's bearer.
    """
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception:
        return {}


def poll(fn: Callable[[], object], want: object, *, tries: int = 20, delay: float = 0.5) -> object:
    """Poll ``fn`` until it equals ``want`` (lineage emission is async) or give up."""
    last: object = None
    for _ in range(tries):
        last = fn()
        if last == want:
            return last
        time.sleep(delay)
    return last


def main() -> int:
    ns = f"demo{os.getpid()}"
    bronze, silver = f"{ns}$bronze$events", f"{ns}$silver$events"

    step("Tokens from Dex (the IdP)")
    alice, bob = token("alice@example.com"), token("bob@example.com")
    alice_sub = sub_of(alice)
    ah, bh = {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}
    print(f"  alice sub = {alice_sub}")

    step("who-may: anonymous create is rejected (OIDC enforced)")
    anon = requests.post(f"{CATALOG}/v1/namespace/{ns}/create", json={}, timeout=10)
    check(anon.status_code == 401, f"anon create namespace -> {anon.status_code} (want 401)")

    step("alice creates a namespace + a bronze Lance table (she becomes its owner)")
    r = requests.post(f"{CATALOG}/v1/namespace/{ns}/create", headers=ah, json={}, timeout=10)
    check(r.status_code == 200, f"alice create namespace -> {r.status_code} (want 200)")
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "kind": ["a", "b", "c"]})
    r = requests.post(f"{CATALOG}/v1/table/{bronze}/create", headers={**ah, **ARROW}, data=ipc(rows), timeout=30)
    check(r.status_code == 200, f"alice create {bronze} -> {r.status_code} (want 200)")

    step("who-may: bob holds no grant, so he cannot even read the table's metadata")
    r = requests.post(f"{CATALOG}/v1/table/{bronze}/describe", headers=bh, timeout=10)
    check(r.status_code == 403, f"bob describe {bronze} -> {r.status_code} (want 403)")
    r = requests.post(f"{CATALOG}/v1/table/{bronze}/describe", headers=ah, timeout=10)
    check(r.status_code == 200, f"alice describe {bronze} -> {r.status_code} (want 200, cascade from owner)")

    step("how/who: the catalog (which verified alice's token) recorded her as the author of the create")

    def bronze_authors() -> set[str | None]:
        rows = get_json(f"{LINEAGE}/events?table={bronze}&limit=500", ah).get("events", [])
        return {author_sub_from_payload(row.get("event")) for row in rows}

    recorded = poll(lambda: alice_sub in bronze_authors(), True)
    check(recorded is True, f"lineage feed authors of {bronze} = {bronze_authors()} (want {alice_sub} among them)")

    step("a pipeline job promotes bronze -> silver and emits OpenLineage")
    promote = {
        "eventType": "COMPLETE",
        "eventTime": "2026-06-24T00:00:00+00:00",
        "run": {
            "runId": f"promote-{os.getpid()}",
            "facets": {"rask_author": {"name": alice_sub, "sub": alice_sub}},
        },
        "job": {"namespace": "pipeline", "name": "promote_bronze_to_silver"},
        "inputs": [{"namespace": ns, "name": bronze}],
        "outputs": [{"namespace": ns, "name": silver}],
    }
    r = requests.post(f"{LINEAGE}/api/v1/lineage", json=promote, headers=ah, timeout=10)
    check(r.status_code == 201, f"emit promote run -> {r.status_code} (want 201)")

    step("how: silver is derived from bronze (the medallion lineage, off lineage's edge list)")
    down = poll(
        lambda: [d["table"] for d in get_json(f"{LINEAGE}/tables/{bronze}/derived", ah).get("derived", [])],
        [silver],
    )
    check(isinstance(down, list) and silver in down, f"derived({bronze}) = {down} (want it to include {silver})")

    summary = f"{RED}{_failures} check(s) FAILED{RESET}" if _failures else f"{GREEN}ALL GOVERNANCE INVARIANTS HELD{RESET}"
    print(f"\n{summary}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
