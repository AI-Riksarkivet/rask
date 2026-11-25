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

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from importlib.resources import files
from typing import Any, Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.governed.fga_provision import MODEL_MAX_PAGES, MODEL_PAGE_SIZE, ModelHistory, ModelHistoryTooLongError, canonical_model, newest_store
from lakehouse.governed.machine_identity import identity_bearer
from lakehouse.process.log_handler import setup_logging


log = logging.getLogger(__name__)

#: The store the estate uses, found the way every service finds it: `fga_provision.newest_store`.
STORE_NAME: Final = "lance-catalog"


def model_document() -> dict[str, Any]:
    """The model as the services import it — the same bytes, never a second copy."""
    return json.loads((files("lakehouse.governed.auth") / "model.json").read_text(encoding="utf-8"))


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
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    headers |= fga_headers(token_file)
    request = urllib.request.Request(f"{api.rstrip('/')}{path}", data=data, headers=headers)  # noqa: S310 — in-cluster URL from env
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — the in-cluster URL above
        return json.loads(response.read() or b"{}")


def history(api: str, store: str, desired: dict[str, Any], *, token_file: str | None) -> ModelHistory[dict[str, Any]]:
    """The store's models read against ``desired``, newest first and stopped at the first that carries it.

    The same rule the catalog's boot and every service's resolve apply (``fga_provision.ModelHistory``), over
    OpenFGA's HTTP API. A history longer than ``MODEL_MAX_PAGES`` raises rather than reading as absent,
    because absent is what makes this hook write.
    """
    wanted = canonical_model(desired)
    read, token = ModelHistory[dict[str, Any]](), ""
    for _ in range(MODEL_MAX_PAGES):
        query = f"page_size={MODEL_PAGE_SIZE}" + (f"&continuation_token={urllib.parse.quote(token)}" if token else "")
        page = _call(api, f"/stores/{store}/authorization-models?{query}", token_file=token_file)
        read = read.read(page.get("authorization_models") or [], wanted)
        token = page.get("continuation_token") or ""
        if read.carrying is not None or not token:
            return read
    raise ModelHistoryTooLongError(f"store {store}'s model history exceeded {MODEL_MAX_PAGES} pages; refusing to read it as absent")


class ModelHookSettings(BaseSettings):
    """What the chart's `openfga-model` Job is told: where OpenFGA is, the token it presents, and the store when one is pinned."""

    model_config = SettingsConfigDict(frozen=True)

    api_url: str = Field(alias="FGA_API_URL")
    token_file: str = Field(alias="RASK_FGA_TOKEN_FILE")
    store_id: str = Field(default="", alias="RASK_FGA_STORE_ID")


#: What a store still coming up answers a request with: a refused or reset connection, a timeout, a status that is not
#: 200, a connection closed before the status line, or a body that is not JSON yet. Anything else is not "not ready".
_NOT_READY: Final = (OSError, http.client.HTTPException, json.JSONDecodeError)

#: How many times the hook asks for the stores, and the seconds between two asks.
_ATTEMPTS: Final = 60
_ATTEMPT_SECONDS: Final = 2


def main() -> int:
    """The chart's `openfga-model` hook: write this image's model unless the store already holds it.

    Pre-upgrade, so a new image's model exists before its pods resolve it ([[LH-201]]). The store is the
    newest one named `lance-catalog` unless `RASK_FGA_STORE_ID` pins one, and a held body is not written
    again wherever it sits in the history. Nothing to pre-write exits 0 rather than failing the upgrade:
    with no store yet, or OpenFGA unreachable, the catalog writes its image's model when it boots, and a
    failed pre-upgrade hook would refuse the very upgrade that repairs OpenFGA. Every request presents the
    Job's projected `rask-openfga` token from `RASK_FGA_TOKEN_FILE` ([[XC-077]]), which the chart always sets.
    A 401 or 403 exits 1, and a token file the Job cannot read raises: neither is a store still coming up, and
    no later boot repairs either.

    Raises:
        IdentityTokenUnavailableError: The Job's token file cannot be read.
    """
    setup_logging()
    settings = ModelHookSettings()
    api, token_file = settings.api_url, settings.token_file
    # The store may still be coming up behind the migrate hook, which is the ordering this hook sits in the middle of.
    stores: list[dict[str, Any]] = []
    for attempt in range(_ATTEMPTS):
        try:
            # The stores of the estate's name, never the first page of every store (`fga_provision.newest_store`).
            stores = _call(api, f"/stores?name={urllib.parse.quote(STORE_NAME)}", token_file=token_file).get("stores") or []
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                log.error("openfga_model_hook_token_refused", extra={"api": api, "token_file": token_file, "status": exc.code})
                return 1
            if attempt == _ATTEMPTS - 1:
                log.warning("openfga_model_hook_openfga_unreachable", extra={"api": api, "error": str(exc)})
                return 0
            time.sleep(_ATTEMPT_SECONDS)
        except _NOT_READY as exc:
            if attempt == _ATTEMPTS - 1:
                log.warning("openfga_model_hook_openfga_unreachable", extra={"api": api, "error": str(exc)})
                return 0
            time.sleep(_ATTEMPT_SECONDS)

    named = newest_store(stores, STORE_NAME)
    store = settings.store_id or (str(named["id"]) if named else "")
    if not store:
        log.info("openfga_model_hook_no_store", extra={"api": api})
        return 0

    desired = model_document()
    desired.pop("id", None)
    held = history(api, store, desired, token_file=token_file)
    if held.carrying is not None:
        log.info(
            "openfga_model_hook_model_already_held",
            extra={"store": store, "model": held.carrying["id"], "types": len(shape(desired)), "below_newest": held.held_below_newest},
        )
        return 0

    written = _call(api, f"/stores/{store}/authorization-models", desired, token_file=token_file, timeout=60)
    log.info("openfga_model_hook_model_written", extra={"store": store, "model": written.get("authorization_model_id")})
    return 0


if __name__ == "__main__":  # pragma: no cover - the container entrypoint
    raise SystemExit(main())


__all__ = [
    "STORE_NAME",
    "ModelHookSettings",
    "fga_headers",
    "history",
    "main",
    "model_document",
    "shape",
]
