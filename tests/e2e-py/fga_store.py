"""The catalog's OpenFGA store, read directly, for a claim about the tuples it holds.

No catalog door lists a store's raw tuples to a tenant, so a leg holding a door to the tuples it seeds, or a refused call
to the tuples it leaves, reads the store itself, as the bootstrap Job's ServiceAccount (`LANCE_E2E_FGA_TOKEN_FILE`,
[[XC-077]]). The store is `LANCE_E2E_FGA_STORE_ID` when the runner pinned one, else the newest store named
`lance-catalog` (`fga.newest_store`), the rule the hook and every service share ([[LH-201]]).
"""

from __future__ import annotations

import os
from typing import Any

import requests

from service_kit.governed.auth.write_model import STORE_NAME, fga_headers
from service_kit.governed.fga import newest_store


FGA = os.environ.get("LANCE_E2E_FGA", "").rstrip("/")
FGA_STORE = os.environ.get("LANCE_E2E_FGA_STORE_ID", "")
FGA_TOKEN_FILE = os.environ.get("LANCE_E2E_FGA_TOKEN_FILE") or None


def _store() -> tuple[dict[str, str], str]:
    """The headers and the id of the catalog's OpenFGA store."""
    assert FGA, "set LANCE_E2E_FGA: this claim is about the tuples the store holds"
    headers = fga_headers(FGA_TOKEN_FILE)
    named = newest_store(requests.get(f"{FGA}/stores", headers=headers, timeout=10).json().get("stores") or [], STORE_NAME)
    store = FGA_STORE or (str(named["id"]) if named else "")
    assert store, f"the OpenFGA at {FGA} holds no store named {STORE_NAME!r}"
    return headers, store


def read(**query: object) -> list[dict[str, Any]]:
    """Every tuple the store answers ``query`` with, across every page of the read."""
    headers, store = _store()
    tuples: list[dict[str, Any]] = []
    continuation = ""
    while True:
        body: dict[str, Any] = {**query, "page_size": 100, **({"continuation_token": continuation} if continuation else {})}
        answer = requests.post(f"{FGA}/stores/{store}/read", headers=headers, json=body, timeout=10)
        assert answer.status_code == 200, answer.text
        page = answer.json()
        tuples += page.get("tuples") or []
        continuation = page.get("continuation_token") or ""
        if not continuation:
            return tuples


def tuples_on(obj: str) -> frozenset[tuple[str, str, str]]:
    """Each tuple the store holds on ``obj``, as ``(user, relation, timestamp)``: a reading that does not depend on the
    order the store reads in, and still tells a tuple written again from the one that stood."""
    return frozenset((held["key"]["user"], held["key"]["relation"], held["timestamp"]) for held in read(tuple_key={"object": obj}))
