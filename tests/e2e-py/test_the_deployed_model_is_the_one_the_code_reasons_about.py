"""The model in the STORE is the model this repo ships — the third copy, checked at last.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the services derive, and there are
three copies of it: the `.fga` source, the `model.json` the package ships, and whatever OpenFGA
actually holds. `make fga-test` diffs the first two. Nothing ever looked at the third.

MEASURED 2026-09-18, BEFORE THE FIX: the store's newest model defined 26/27/26 relations on
warehouse/namespace/table against the repo's 30/29/29 — nine relations the code reasons about and the
store could not express. `warehouse#maintainer` was one, so `rask-bootstrap-admin` — a Helm HOOK —
CrashLooped writing a tuple naming it, `make k3s-up` hung, and the release wedged in `pending-upgrade`,
which refuses every later upgrade. Nine relations behind, and not one test went red.

THE WRITE IS NOW A HOOK (`chart/templates/openfga-model.yaml`, weight 0 — after the DB migration,
before tuples). This is the other half: a hook that writes and a check that the write LANDED are
different assertions, and only the second one notices a hook that silently stopped running.

TWO GRAINS. The name checks (`service_kit.governed.auth.write_model.shape`) say WHICH type or relation
is missing or extra, which is what an operator needs from a red run. The body check asks the question
the writer decides on (`write_model.needs_write`): does the store hold this repo's rules, not only its
names — a `can_*` narrowed under an unchanged name passes every name check. Neither compares whole
documents: a stored model carries a server-assigned `id` and default fills the shipped document does
not, so that comparison would fail permanently and for the wrong reason.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from service_kit.governed.auth.write_model import model_document, needs_write, shape


#: ALREADY CARRIES ITS SCHEME. `scripts/e2e_live.sh`'s `url` helper exports `http://<host>`, so
#: prefixing again builds `http://http://…` — which fails as a URLError and reads exactly like "the
#: store is down", i.e. a SKIP. A gate that skips for a reason it misreports is worse than no gate.
FGA = os.environ.get("LANCE_E2E_FGA", "").rstrip("/")
STORE = os.environ.get("LANCE_E2E_FGA_STORE_ID", "")

pytestmark = pytest.mark.e2e


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{FGA}{path}", timeout=30) as response:  # noqa: S310 — in-cluster address from the runner
        return json.loads(response.read() or b"{}")


@pytest.fixture(scope="module")
def deployed() -> dict:
    if not FGA:
        pytest.skip("set LANCE_E2E_FGA (a deployed OpenFGA) — scripts/e2e_live.sh discovers it")
    try:
        stores = _get("/stores").get("stores") or []
    except Exception as exc:  # noqa: BLE001 — an unreachable store is a skip, not a drift failure
        pytest.skip(f"openfga not reachable: {type(exc).__name__}")
    store = STORE or (stores[0]["id"] if stores else "")
    if not store:
        pytest.skip("the deployed OpenFGA holds no store")
    models = _get(f"/stores/{store}/authorization-models?page_size=1").get("authorization_models") or []
    if not models:
        pytest.skip(f"store {store} holds no authorization model at all")
    return models[0]


def test_the_store_defines_every_type_the_repo_does(deployed: dict) -> None:
    missing = set(shape(model_document())) - set(shape(deployed))

    assert not missing, f"the deployed model is missing whole types the code reasons about: {sorted(missing)}"


def test_the_store_defines_every_RELATION_the_repo_does(deployed: dict) -> None:
    """The drift that actually happened. All nine missing relations were on types that already
    existed, so a check keyed on type names alone would have reported no change forever."""
    shipped, live = shape(model_document()), shape(deployed)
    missing = {t: sorted(set(rels) - set(live.get(t, []))) for t, rels in shipped.items() if set(rels) - set(live.get(t, []))}

    assert not missing, (
        "the deployed model cannot express relations this repo's code checks, so a tuple naming one is "
        f"refused and whatever depends on it fails at runtime: {missing}"
    )


def test_the_store_carries_nothing_the_repo_has_dropped(deployed: dict) -> None:
    """The other direction. A relation left in the store after the repo dropped it is a grant path
    nothing in this tree describes — which is worse than a missing one, because it still works."""
    shipped, live = shape(model_document()), shape(deployed)
    extra = {t: sorted(set(rels) - set(shipped.get(t, []))) for t, rels in live.items() if set(rels) - set(shipped.get(t, []))}

    assert not extra, f"the deployed model grants through relations this repo no longer defines: {extra}"


def test_the_store_holds_every_RULE_the_repo_does(deployed: dict) -> None:
    """The drift the name checks above cannot see: the same types and relations, one rule different.
    A `can_*` narrowed in `model.fga` and never written leaves the store granting through the path the
    repo removed, and every check still answers — so nothing downstream reports it."""
    assert not needs_write(deployed, model_document()), (
        "the deployed model's rules differ from this repo's `model.json`, so the store authorizes by rules "
        "the code does not define. With the name checks green, a rule changed and the store never took it: "
        "the `openfga-model` hook has not run since, or ran from a catalog image older than this checkout"
    )
