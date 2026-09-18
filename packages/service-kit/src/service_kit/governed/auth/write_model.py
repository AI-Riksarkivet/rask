"""Write this package's authorization model into an OpenFGA store, if it is not already there.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the services derive, and nothing put
it in the store — `openfga-migrate` is the DATABASE migration, and `make fga-test` diffs `model.json`
against `model.fga`, two of three copies. The store was the third, checked by nothing, and fell nine
relations behind: measured 2026-09-18 it defined 26/27/26 on warehouse/namespace/table against the
repo's 30/29/29, so `warehouse#maintainer` could not be written and the tuple-granting hook CrashLooped
on it, wedging the release.

IT LIVES HERE, BESIDE THE MODEL IT WRITES, rather than inline in the chart's hook. The comparison
below is the part that matters and the part a YAML heredoc cannot test: an OpenFGA model is immutable
and versioned, so writing unconditionally mints a new id on every upgrade — the store already held 50.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def model_document() -> dict[str, Any]:
    """The model as the services import it — the same bytes, never a second copy."""
    return json.loads((files("service_kit.governed.auth") / "model.json").read_text(encoding="utf-8"))


def shape(model: dict[str, Any]) -> dict[str, list[str]]:
    """What makes two models EQUAL for the purpose of deciding whether to write.

    Types and their relation NAMES. Comparing whole documents would differ on the server-assigned
    ``id`` every stored model carries, so every upgrade would look like a change and write again —
    which is how a store reaches fifty models. Comparing only type names would miss exactly the drift
    this exists to catch, since the nine missing relations were all on types that already existed.
    """
    return {t["type"]: sorted(t.get("relations") or {}) for t in model.get("type_definitions", [])}


def needs_write(stored: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    """Whether the store's newest model differs from this package's.

    ``None`` — an empty store — is a write: there is nothing for the services to check against.
    """
    return stored is None or shape(stored) != shape(desired)


def _call(api: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    import json as _json
    import urllib.request

    data = _json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    request = urllib.request.Request(f"{api.rstrip('/')}{path}", data=data, headers=headers)  # noqa: S310 — in-cluster URL from env
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def main() -> int:
    """The chart's `openfga-model` hook. Writes this package's model unless the store already has it.

    Run as a module rather than a heredoc in the Job's `args` so the comparison above is covered by
    `packages/service-kit/tests/test_the_model_is_written_only_when_it_differs.py` — the idempotency
    is the part that decides between growing the store forever and never converging, and neither
    failure announces itself.
    """
    import os
    import sys
    import time

    api = os.environ["FGA_API_URL"]
    # The store may still be coming up behind the migrate hook, which is the ordering this hook sits
    # in the middle of. Any transport error here means "not ready yet" rather than "broken".
    stores: list[dict[str, Any]] = []
    for attempt in range(60):
        try:
            stores = _call(api, "/stores").get("stores") or []
            break
        except Exception as exc:  # any transport error here means "not ready yet"; the last attempt re-raises
            if attempt == 59:
                print(f"openfga never became reachable: {exc}", file=sys.stderr)
                raise
            time.sleep(2)

    store = os.environ.get("RASK_FGA_STORE_ID", "") or (stores[0]["id"] if stores else "")
    if not store:
        print("no OpenFGA store exists yet and none is pinned; nothing to write a model into", file=sys.stderr)
        return 1

    desired = model_document()
    desired.pop("id", None)
    existing = _call(api, f"/stores/{store}/authorization-models?page_size=1").get("authorization_models") or []
    if not needs_write(existing[0] if existing else None, desired):
        print(f"model already current in store {store} ({len(shape(desired))} types); nothing to write")
        return 0

    written = _call(api, f"/stores/{store}/authorization-models", desired, timeout=60)
    print(f"wrote authorization model {written.get('authorization_model_id')} to store {store}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the container entrypoint
    raise SystemExit(main())
