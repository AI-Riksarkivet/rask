"""The repo-side half of `fga-store-check.sh`: which store, and does it hold this checkout's model.

The pod half only fetches raw JSON. Every decision is made here, from the CHECKOUT's own rule, because
the catalog image can lag the checkout, which is exactly the state the check exists to report. A rule
imported from the image is missing there, or is a different rule from the one this checkout ships.

    plan             < {"pinned": ..., "stores": [...]}     prints "<store id> <page bound>"
    compare <store>  < one model page per line, newest first  exit 0 held, 1 absent

THE QUESTION IS THE ONE EVERY SERVICE ASKS ([[LH-201]]): does the store hold a model whose canonical
body is this checkout's `model.json`, at any depth of its history (`fga.ModelHistory`)? A service
checks against the model its own image carries, so a body held below the newest is one a pod built from
this checkout resolves, and the chart's hook writes only a body the store lacks.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

from _fga_model_rungs import rungs
from pydantic import BaseModel

from service_kit.governed.auth.write_model import STORE_NAME, model_document
from service_kit.governed.fga import MODEL_MAX_PAGES, MODEL_PAGE_SIZE, ModelHistory, canonical_model, newest_store


class StoreListing(BaseModel):
    """The pod's report on stores: the catalog's `RASK_FGA_STORE_ID`, and OpenFGA's `GET /stores`."""

    pinned: str = ""
    stores: list[dict[str, Any]] | None = None


class ModelPage(BaseModel):
    """One page of `GET /stores/{id}/authorization-models`, newest first."""

    authorization_models: list[dict[str, Any]] | None = None


def plan(listing: StoreListing) -> int:
    """Print the store the estate uses and the page bound its history is read under."""
    named = newest_store(listing.stores or [], STORE_NAME)
    if listing.pinned:
        store, how = listing.pinned, "pinned by the catalog's RASK_FGA_STORE_ID"
    elif named is not None:
        store, how = str(named["id"]), f"the newest store named {STORE_NAME!r} ({len(listing.stores or [])} listed)"
    else:
        print(f"!! no store named {STORE_NAME!r} and none pinned: the catalog has never provisioned the estate's store", file=sys.stderr)
        return 1
    print(f">> store {store}: {how}", file=sys.stderr)
    print(store, MODEL_MAX_PAGES, MODEL_PAGE_SIZE)
    return 0


def read_history(pages: Iterable[str], desired: dict[str, Any]) -> ModelHistory:
    """The streamed pages read against ``desired``, stopped at the first model that carries it."""
    wanted, history = canonical_model(desired), ModelHistory()
    for line in pages:
        if not line.strip():
            continue
        history = history.read(ModelPage.model_validate_json(line).authorization_models or [], wanted)
        if history.carrying is not None:
            break
    return history


def compare(store: str, history: ModelHistory, desired: dict[str, Any]) -> int:
    """Report whether ``store`` holds ``desired``; exit 0 when it does at any depth, 1 when it does not."""
    shipped = rungs(desired)
    if history.carrying is not None and not history.held_below_newest:
        print(f">> store {store}'s newest model {history.carrying['id']} carries this checkout's model ({len(shipped)} relations)")
        return 0
    if history.carrying is not None:
        print(f">> store {store} holds this checkout's model as {history.carrying['id']} ({len(shipped)} relations), below its newest {history.newest['id']}.")
        print("   A newer image, a revert or another writer wrote since; each service checks against the model its own")
        print("   image carries, so a pod built from this checkout resolves this one.")
        return 0
    if history.newest is None:
        print(f"!! store {store} holds no authorization model: neither the openfga-model hook nor the catalog has written one")
        return 1
    live = rungs(history.newest)
    print(f"!! STORE {store} HOLDS NO MODEL CARRYING THIS CHECKOUT'S model.json.")
    print(f"   Against its newest model {history.newest['id']}:")
    if set(shipped) == set(live):
        print("   every type#relation name matches, so a rule differs under an unchanged name.")
    else:
        print("   in the repo and NOT in the store:")
        print("".join(f"     {rung}\n" for rung in sorted(set(shipped) - set(live))), end="")
        print("   in the store and NOT in the repo:")
        print("".join(f"     {rung}\n" for rung in sorted(set(live) - set(shipped))), end="")
    print("   A pod built from this checkout has no model to check against and fails closed. The chart's")
    print("   openfga-model hook and the catalog's boot each write a model the store lacks: roll a release whose")
    print("   image carries this checkout (helm upgrade, or the hook's Job directly) and check neither write failed.")
    return 1


def main(argv: list[str]) -> int:
    match argv:
        case ["plan"]:
            return plan(StoreListing.model_validate_json(sys.stdin.read()))
        case ["compare", store]:
            desired = model_document()
            return compare(store, read_history(sys.stdin, desired), desired)
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
