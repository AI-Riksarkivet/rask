"""The store the estate uses HOLDS the model this repo ships — the third copy, checked at last.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the services derive, and there are
three copies of it: the `.fga` source, the `model.json` the package ships, and whatever OpenFGA
actually holds. `make fga-test` diffs the first two. Nothing ever looked at the third.

MEASURED 2026-09-18, BEFORE THE FIX: the store's newest model defined 26/27/26 relations on
warehouse/namespace/table against the repo's 30/29/29 — nine relations the code reasons about and the
store could not express. `warehouse#maintainer` was one, so `rask-bootstrap-admin` — a Helm HOOK —
CrashLooped writing a tuple naming it, `make k3s-up` hung, and the release wedged in `pending-upgrade`,
which refuses every later upgrade. Nine relations behind, and not one test went red.

THE WRITE IS A HOOK (`chart/templates/openfga-model.yaml`, pre-upgrade — before the new pods start,
before tuples). This is the other half: a hook that writes and a check that the write LANDED are
different assertions, and only the second one notices a hook that silently stopped running.

THE QUESTION IS THE ONE EVERY SERVICE ASKS ([[LH-201]]): does the store hold a model whose canonical body
is this checkout's `model.json`, at any depth of its history (`write_model.history`, the hook's own
read)? A service checks against the model its own image carries, so a body held below the newest (an
older image, a revert, a writer that wrote since) is one every pod built from this checkout resolves,
and the hook writes only a body the store lacks. The store is `LANCE_E2E_FGA_STORE_ID` when the runner
exports it (`scripts/e2e_live.sh`, which honours the catalog's pin), else the newest named
`lance-catalog` (`fga.newest_store`). No such store FAILS the run: a skip would read as green over an
estate whose services all fail closed.

A RED RUN NAMES THE DIFF against the store's newest model per type and relation
(`service_kit.governed.auth.write_model.shape`), which is what an operator needs from it. Bodies are
compared canonically, never as whole documents: a stored model carries a server-assigned `id` and
default fills the shipped document does not, so that comparison would fail permanently and for the
wrong reason. A rule narrowed under unchanged names leaves the name diff empty, and the message says
so rather than reading as a match.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import pytest

from service_kit.governed.auth.write_model import STORE_NAME, fga_headers, history, model_document, shape
from service_kit.governed.fga import ModelHistory, newest_store


#: ALREADY CARRIES ITS SCHEME. `scripts/e2e_live.sh`'s `url` helper exports `http://<host>`, so
#: prefixing again builds `http://http://…` — which fails as a URLError and reads exactly like "the
#: store is down", i.e. a SKIP. A gate that skips for a reason it misreports is worse than no gate.
FGA = os.environ.get("LANCE_E2E_FGA", "").rstrip("/")
STORE = os.environ.get("LANCE_E2E_FGA_STORE_ID", "")
#: The `rask-openfga` token file the runner minted (`scripts/e2e_live.sh`); OpenFGA refuses a call without it ([[XC-077]]).
FGA_TOKEN_FILE = os.environ.get("LANCE_E2E_FGA_TOKEN_FILE") or None

pytestmark = pytest.mark.e2e


def _get(path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{FGA}{path}", headers=fga_headers(FGA_TOKEN_FILE))  # noqa: S310 — in-cluster address from the runner
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


@pytest.fixture(scope="module")
def held() -> tuple[str, ModelHistory]:
    """The store the estate uses, and its history read against this checkout's model."""
    if not FGA:
        pytest.skip("set LANCE_E2E_FGA (a deployed OpenFGA) — scripts/e2e_live.sh discovers it")
    try:
        stores = _get("/stores").get("stores") or []
    except Exception as exc:  # noqa: BLE001 — an unreachable store is a skip, not a drift failure
        pytest.skip(f"openfga not reachable: {type(exc).__name__}")
    named = newest_store(stores, STORE_NAME)
    store = STORE or (str(named["id"]) if named else "")
    if not store:
        pytest.fail(f"the deployed OpenFGA holds no store named {STORE_NAME!r} and none is pinned: the catalog never provisioned the estate's store")
    return store, history(FGA, store, model_document(), token_file=FGA_TOKEN_FILE)


def _absent(store: str, read: ModelHistory) -> str:
    """What a red run tells an operator: the store's newest model against this checkout's, by name."""
    if read.newest is None:
        return f"store {store} holds no authorization model at all: neither the `openfga-model` hook nor the catalog has written one"
    shipped, live = shape(model_document()), shape(read.newest)
    missing = {t: sorted(set(rels) - set(live.get(t, []))) for t, rels in shipped.items() if set(rels) - set(live.get(t, []))}
    extra = {t: sorted(set(rels) - set(shipped.get(t, []))) for t, rels in live.items() if set(rels) - set(shipped.get(t, []))}
    names = (
        f"relations this repo's code checks that it cannot express: {missing}; relations it grants through that this repo no longer defines: {extra}"
        if missing or extra
        else "every type and relation name matches, so a rule differs under an unchanged name"
    )
    return (
        f"store {store} holds no model carrying this checkout's `model.json`, so every service built from it fails closed. "
        f"Against the newest model {read.newest['id']}, {names}. The `openfga-model` hook and the catalog's boot each "
        "write a model the store lacks, so no release carrying this checkout has rolled, or its write failed"
    )


def test_the_store_holds_this_checkouts_model(held: tuple[str, ModelHistory]) -> None:
    store, read = held

    assert read.carrying is not None, _absent(store, read)
