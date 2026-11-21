"""The catalog app as deployed with authorization on, over a real `dir` namespace: a loopback Dex verifies each caller and
a real OpenFGA, on a store of the test's own, decides ([[LH-235]]).

Nothing stands in for the governance plane: the lifespan builds the deployed verifier against the root conftest's
``dex_issuer`` and the deployed OpenFGA client against ``openfga_store``. A tuple a test relies on is written by the door
that writes it in production, or seeded straight to the store where no door writes it (the estate's own declaration).
With ``held_namespace``, the namespace dependency is the same `dir` backend the catalog opens, held by the test, so a
test can wrap it to fail one backend call; without it the catalog opens each warehouse's catalog itself, as deployed.
The catalog suite's and the integration suite's ``governed_catalog`` fixtures both build from here.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.storage.options import StorageOptions


if TYPE_CHECKING:
    from httpx import Response


#: A table's rows as the create door takes them.
ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))


class GovernedCatalog:
    """The governed catalog's client, the store deciding for it, and a signed-in caller's headers."""

    #: The Dex connector the lane's people sign in through, and the idp id the deployment maps it to.
    CONNECTOR, IDP = "local", "dex"
    #: The estate's own warehouse, which every name served from the catalog's own root begins with ([[LH-593]]).
    ESTATE = "warehouse:lance_catalog"

    def __init__(self, client: TestClient, store: Any, dex_issuer: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore and DexIssuer
        self.client = client
        self.store = store
        self._dex = dex_issuer

    def person(self, user_id: str) -> str:
        """The OpenFGA user the person ``user_id`` is, the one a tuple naming them carries."""
        return f"user:{self.IDP}~{user_id}"

    def bearer(self, user_id: str) -> dict[str, str]:
        """The headers of a request the person ``user_id`` signs in for."""
        return {"Authorization": f"Bearer {self._dex.mint(user_id, connector=self.CONNECTOR, audience='lance-catalog')}"}

    def act_as(self, user_id: str) -> None:
        """Sign every later request on :attr:`client` in as the person ``user_id``."""
        self.client.headers.update(self.bearer(user_id))

    def ok(self, path: str, user_id: str, **kwargs: Any) -> Any:  # noqa: ANN401 — the request's own keywords, and the door's JSON
        """POST ``path`` as ``user_id`` and answer the door's JSON, failing the test on anything but a 200.

        A ``#`` or a ``?`` in a name is sent encoded, as a client sends it, or the URL would end at it.
        """
        answer = self.client.post(path.replace("#", "%23").replace("?", "%3F"), headers={**self.bearer(user_id), **kwargs.pop("headers", {})}, **kwargs)
        assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
        return answer.json()

    def create_table(self, user_id: str, table: str) -> None:
        """Create ``table`` holding two rows, as ``user_id``."""
        self.ok(f"/v1/table/{table}/create", user_id, content=ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})

    def uuid(self, kind: str, name: str, user_id: str) -> str:
        """The uuid the catalog minted for the table or namespace ``name``, read by ``user_id`` (ADR 0173 §8)."""
        return str(self.ok(f"/v1/{kind}/{name}/describe", user_id, json={})["properties"]["rask.uuid"])

    def create_in_the_estate(self, user_id: str, name: str) -> Response:
        """Create namespace ``lance_catalog$<name>`` as ``user_id``, who holds ``writer`` on the estate's warehouse for that create alone.

        A namespace directly in a warehouse is created on the warehouse's ``can_create_namespace`` ([[LH-593]]). The grant
        is withdrawn once the door answers, because a warehouse rung reaches every namespace beneath it.
        """
        with self.holding((self.person(user_id), "writer", self.ESTATE)):
            return self.client.post(f"/v1/namespace/lance_catalog${name}/create", json={}, headers=self.bearer(user_id))

    def naming(self, objects: list[str]) -> dict[str, set[tuple[str, str, str]]]:
        """The store's tuples naming each of ``objects`` as their object or their user, for those any tuple names."""
        return StoreTuples(self.store).naming(objects)

    @contextmanager
    def holding(self, *tuples: tuple[str, str, str]) -> Generator[None]:
        """Hold ``tuples`` on the store for the block alone."""
        self.store.write(*tuples)
        try:
            yield
        finally:
            self.store.delete(*tuples)


@contextmanager
def governed_catalog_app(  # noqa: PLR0913 - the root conftest's fixtures and the two choices a suite makes
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
    dex_issuer: Any,  # noqa: ANN401 — the root conftest's DexIssuer
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    held_namespace: bool = False,
    **env: str,
) -> Generator[GovernedCatalog]:
    """The catalog on a real ``dir`` namespace at ``root``, signed-in callers only, each decision OpenFGA's on ``openfga_store``."""
    from lance_namespace import connect

    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(root),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": GovernedCatalog.IDP,
        "RASK_OIDC_CONNECTORS": json.dumps({GovernedCatalog.CONNECTOR: GovernedCatalog.IDP}),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    if held_namespace:
        ns = connect("dir", {"root": str(root)})
        app.dependency_overrides[get_namespace] = lambda: ns
        app.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    try:
        with TestClient(app) as client:
            yield GovernedCatalog(client, openfga_store, dex_issuer)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


class StoreTuples:
    """A real OpenFGA store read back as a test reads what the doors left: every tuple it holds, and those naming an object."""

    def __init__(self, store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
        self._store = store

    @property
    def tuples(self) -> set[tuple[str, str, str]]:
        return self._store.tuples()

    def naming(self, objects: list[str]) -> dict[str, set[tuple[str, str, str]]]:
        """The store's tuples naming each of ``objects`` as their object or their user, for those any tuple names."""
        held = self.tuples
        named = {obj: {t for t in held if obj in (t[0], t[2])} for obj in objects}
        return {obj: tuples for obj, tuples in named.items() if tuples}

    def write(self, *tuples: tuple[str, str, str]) -> None:
        """Write ``tuples`` straight to the store, as a write past every door would."""
        self._store.write(*tuples)
