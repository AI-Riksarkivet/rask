"""Write this package's authorization model into an OpenFGA store, if it is not already there.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the services derive, and nothing put
it in the store — `openfga-migrate` is the DATABASE migration, and `make fga-test` diffs `model.json`
against `model.fga`, two of three copies. The store was the third, checked by nothing, and fell nine
relations behind: measured 2026-09-18 it defined 26/27/26 on warehouse/namespace/table against the
repo's 30/29/29, so `warehouse#maintainer` could not be written and the tuple-granting hook CrashLooped
on it, wedging the release.

IT LIVES HERE, BESIDE THE MODEL IT WRITES, rather than inline in the chart's hook. The history read
(:func:`history`) is the part that matters and the part a YAML heredoc cannot test: an OpenFGA model is immutable
and versioned, so writing unconditionally mints a new id on every upgrade — the store already held 50.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from importlib.resources import files
from typing import Any, Final

from service_kit.governed.fga import MODEL_MAX_PAGES, MODEL_PAGE_SIZE, ModelHistory, ModelHistoryTooLongError, canonical_model, newest_store
from service_kit.governed.machine_identity import identity_bearer


#: The store the estate uses, found the way every service finds it: `fga.newest_store`.
STORE_NAME: Final = "lance-catalog"


def model_document() -> dict[str, Any]:
    """The model as the services import it — the same bytes, never a second copy."""
    return json.loads((files("service_kit.governed.auth") / "model.json").read_text(encoding="utf-8"))


def shape(model: dict[str, Any]) -> dict[str, list[str]]:
    """Each type's relation NAMES — a readable index of a model, never the test of whether to write.

    A drift report reads as "warehouse lacks maintainer" from this, where two canonical strings would
    only say "different". It cannot decide a write: a rule narrowed under an unchanged name —
    ``can_read_data: reader or pass_grants`` to ``reader`` — leaves every name in place, so a name
    comparison reports the store current while the store keeps granting through the removed path.
    """
    return {t["type"]: sorted(t.get("relations") or {}) for t in model.get("type_definitions", [])}


def fga_headers(token_file: str | None) -> dict[str, str]:
    """The ``Authorization`` header carrying this pod's `rask-openfga` token, read now; none without a file ([[XC-077]])."""
    return identity_bearer(token_file) if token_file else {}


def _call(api: str, path: str, body: dict[str, Any] | None = None, *, token_file: str | None, timeout: float = 30.0) -> dict[str, Any]:
    import json as _json
    import urllib.request

    data = _json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    headers |= fga_headers(token_file)
    request = urllib.request.Request(f"{api.rstrip('/')}{path}", data=data, headers=headers)  # noqa: S310 — in-cluster URL from env
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def history(api: str, store: str, desired: dict[str, Any], *, token_file: str | None) -> ModelHistory:
    """The store's models read against ``desired``, newest first and stopped at the first that carries it.

    The same rule the catalog's boot and every service's resolve apply (``fga.ModelHistory``), over
    OpenFGA's HTTP API. A history longer than ``MODEL_MAX_PAGES`` raises rather than reading as absent,
    because absent is what makes this hook write.
    """
    wanted = canonical_model(desired)
    read, token = ModelHistory(), ""
    for _ in range(MODEL_MAX_PAGES):
        query = f"page_size={MODEL_PAGE_SIZE}" + (f"&continuation_token={urllib.parse.quote(token)}" if token else "")
        page = _call(api, f"/stores/{store}/authorization-models?{query}", token_file=token_file)
        read = read.read(page.get("authorization_models") or [], wanted)
        token = page.get("continuation_token") or ""
        if read.carrying is not None or not token:
            return read
    raise ModelHistoryTooLongError(f"store {store}'s model history exceeded {MODEL_MAX_PAGES} pages; refusing to read it as absent")


def carried_model(api: str, *, token_file: str | None, pinned: str = "") -> tuple[str, str]:
    """``(store, model)`` for a tool that checks or writes tuples: the estate's store, and this code's model in it.

    A tuple request naming no ``authorization_model_id``, or naming the store's newest, is validated against
    whichever image wrote last ([[LH-201]]). In the LH-201 review (OpenFGA v1.18.3) a legacy image's narrower
    newest model refused every ``estate`` tuple on ``type 'estate' not found``. So the rule is the one the hook,
    ``bootstrap-admin`` and every service share: the ``pinned`` store (``RASK_FGA_STORE_ID``) when given, else
    the newest named ``STORE_NAME``, and in it the model whose canonical body is this package's ``model.json``.

    Raises:
        LookupError: there is no such store, or it holds no model carrying this body. No id is right to send
            then, and the store's newest is the one wrong answer.
    """
    store = pinned
    if not store:
        named = newest_store(_call(api, "/stores", token_file=token_file).get("stores") or [], STORE_NAME)
        if named is None:
            raise LookupError(f"OpenFGA at {api} holds no store named {STORE_NAME!r} and none is pinned")
        store = str(named["id"])
    carrying = history(api, store, model_document(), token_file=token_file).carrying
    if carrying is None:
        raise LookupError(f"store {store} at {api} holds no model carrying this code's model.json: neither the openfga-model hook nor the catalog wrote it")
    return store, str(carrying["id"])


def main() -> int:
    """The chart's `openfga-model` hook: write this image's model unless the store already holds it.

    Pre-upgrade, so a new image's model exists before its pods resolve it ([[LH-201]]). The store is the
    newest one named `lance-catalog` unless `RASK_FGA_STORE_ID` pins one, and a held body is not written
    again wherever it sits in the history. Nothing to pre-write exits 0 rather than failing the upgrade:
    with no store yet, or OpenFGA unreachable, the catalog writes its image's model when it boots, and a
    failed pre-upgrade hook would refuse the very upgrade that repairs OpenFGA. Run as a module rather
    than a heredoc in the Job's `args` so :func:`history` is covered by tests. Every request presents the
    Job's projected `rask-openfga` token from `RASK_FGA_TOKEN_FILE` ([[XC-077]]), which the chart always sets,
    and a 401 or 403 exits 1: a refused credential is a misconfiguration no later boot repairs.
    """
    import os
    import sys
    import time

    api = os.environ["FGA_API_URL"]
    token_file = os.environ["RASK_FGA_TOKEN_FILE"]
    # The store may still be coming up behind the migrate hook, which is the ordering this hook sits
    # in the middle of. Any transport error here means "not ready yet" rather than "broken".
    stores: list[dict[str, Any]] = []
    for attempt in range(60):
        try:
            stores = _call(api, "/stores", token_file=token_file).get("stores") or []
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # A refused credential is not "not ready yet": no wait mends it, and exiting 0 would record the
                # hook as succeeded with the model unwritten ([[XC-077]]).
                print(f"OpenFGA at {api} refused this Job's token from {token_file} (HTTP {exc.code})", file=sys.stderr)
                return 1
            if attempt == 59:
                print(f"OpenFGA unreachable ({exc}); nothing pre-written, the catalog writes this image's model when it boots", file=sys.stderr)
                return 0
            time.sleep(2)
        except Exception as exc:  # any transport error here means "not ready yet"
            if attempt == 59:
                print(f"OpenFGA unreachable ({exc}); nothing pre-written, the catalog writes this image's model when it boots", file=sys.stderr)
                return 0
            time.sleep(2)

    named = newest_store(stores, STORE_NAME)
    store = os.environ.get("RASK_FGA_STORE_ID", "") or (str(named["id"]) if named else "")
    if not store:
        print("no OpenFGA store yet and none pinned; nothing pre-written, the catalog creates it and writes this image's model when it boots")
        return 0

    desired = model_document()
    desired.pop("id", None)
    held = history(api, store, desired, token_file=token_file)
    if held.carrying is not None:
        where = f"below the store's newest {held.newest['id']}" if held.held_below_newest else "as the store's newest"
        print(f"store {store} already holds this image's model {held.carrying['id']} ({len(shape(desired))} types) {where}; nothing written")
        return 0

    written = _call(api, f"/stores/{store}/authorization-models", desired, token_file=token_file, timeout=60)
    print(f"wrote authorization model {written.get('authorization_model_id')} to store {store}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the container entrypoint
    raise SystemExit(main())
