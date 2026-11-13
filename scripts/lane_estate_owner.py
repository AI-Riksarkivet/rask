"""Grant one person ``owner`` on the estate's own warehouse in a lane's OpenFGA store, as the catalog's declaration does.

A lane that brings its services up as Dagger services runs no chart and mounts no declaration, so nothing grants the
estate's first owner (``auth.bootstrapAdmin``). Every name begins with a warehouse and a namespace is created by a caller holding
``can_create_namespace`` on it, so without that owner nobody in the lane can create anything in the estate's own
warehouse. This writes the one tuple the declaration grants the admin there, to the store the lane's catalog made.

Usage: ``lane_estate_owner.py <principal>`` with ``LANCE_E2E_FGA`` naming the store's API, ``RASK_FGA_TOKEN_FILE`` the
token it admits (empty where the lane's OpenFGA admits anyone), and ``RASK_FGA_DEFAULT_WAREHOUSE_OBJECT`` the estate's
warehouse where the lane renamed it.
"""

from __future__ import annotations

import os
import sys

import httpx

from lakehouse.governed.auth.write_model import STORE_NAME, fga_headers
from lakehouse.governed.fga import newest_store, user_object


def main(principal: str) -> int:
    api = os.environ["LANCE_E2E_FGA"].rstrip("/")
    headers = fga_headers(os.environ.get("RASK_FGA_TOKEN_FILE", ""))
    warehouse = os.environ.get("RASK_FGA_DEFAULT_WAREHOUSE_OBJECT", "warehouse:lance_catalog")
    stores = httpx.get(f"{api}/stores", headers=headers, timeout=10).raise_for_status().json().get("stores", [])
    store = newest_store(stores, STORE_NAME)
    if store is None:
        print(f"the OpenFGA at {api} holds no store named {STORE_NAME!r}: the catalog has not booted", file=sys.stderr)
        return 1
    owner = {"user": user_object(principal), "relation": "owner", "object": warehouse}
    # A tuple the store already holds is the post-condition, and the server's to ignore: the code it answers a
    # duplicate with is the one it answers an invalid tuple with, so reading that code as "held" would report a
    # mistyped warehouse or principal as seeded.
    writes = {"tuple_keys": [owner], "on_duplicate": "ignore"}
    httpx.post(f"{api}/stores/{store['id']}/write", headers=headers, json={"writes": writes}, timeout=10).raise_for_status()
    print(f"seeded {owner['user']} owner on {warehouse}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
