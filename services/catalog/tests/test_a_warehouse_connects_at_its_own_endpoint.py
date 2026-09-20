"""A warehouse record may name its own object-store endpoint, and the connection must use it.

[[LH-067]]. `build_namespace_for_root` swapped ONLY `root` off `settings.namespace_properties()`, so
every warehouse — whatever bucket it named — was reached at the estate's single endpoint with the
estate's single credential. Its docstring said the quiet part: "the creds are bucket-agnostic on the
S3 target", which is true of one target and is the assumption that makes a second one unreachable.

`allow_http` IS THE LEG THAT MATTERS, and it is not symmetry. The property is DERIVED from the
endpoint's scheme, so an override that changes the endpoint and leaves the derivation reading the
estate's default produces the wrong-but-plausible failure: an `http://` warehouse behind an `https://`
estate gets `allow_http=false` and every open fails with a TLS error naming the store rather than the
configuration. Deriving it from the endpoint actually being used is the fix, and the test pins the
mixed-scheme pair rather than a matching one, because a matching pair passes either way.

CREDENTIALS ARE DELIBERATELY NOT HERE. The row asked for "per-warehouse endpoint/credential fields on
the warehouse record", and the credential half of that phrasing is refused by the estate's standing
secrets rule — material never travels in a record. The catalog already resolves its own S3 secret from
the Dapr secret store (`dapr_secret_store`/`dapr_secret_key`/`dapr_secret_s3_field`), so a second
store's material belongs behind the same door under its own key, with the record naming a REFERENCE.
The endpoint is not a secret and is what a second store needs first.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import Request

from catalog.core import namespace as namespace_module
from catalog.core.config import Settings


_STORAGE = "storage."


@pytest.fixture
def settings() -> Settings:
    """An HTTPS estate default, so an http warehouse below is a genuine mixed pair."""
    return Settings(
        LANCE_S3_ACCESS_KEY_ID="k",
        LANCE_S3_SECRET_ACCESS_KEY="s",
        LANCE_S3_ENDPOINT="https://estate.example:9000",
        LANCE_REST_ROOT="s3://estate-root",
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The properties handed to `lance_namespace.connect`, which is the whole contract under test."""
    seen: list[dict[str, str]] = []

    def _connect(impl: str, properties: dict[str, str]) -> Any:
        seen.append(dict(properties))
        return object()

    monkeypatch.setattr(namespace_module, "connect", _connect)
    return seen


def test_a_warehouse_with_NO_endpoint_uses_the_estate_default(settings: Settings, captured: list[dict[str, str]]) -> None:
    """The control. Without it, a builder that ignored the override entirely would pass below."""
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket")

    assert captured[0][f"{_STORAGE}endpoint"] == "https://estate.example:9000"
    assert captured[0]["root"] == "s3://tenant-bucket"


def test_a_warehouse_endpoint_OVERRIDES_the_estate_default(settings: Settings, captured: list[dict[str, str]]) -> None:
    """THE DEFECT: a second object store is unreachable while only `root` is swapped."""
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket", endpoint="http://tenant.store:9000")

    assert captured[0][f"{_STORAGE}endpoint"] == "http://tenant.store:9000"
    assert captured[0]["root"] == "s3://tenant-bucket", "the root must still be the warehouse's"


def test_allow_http_follows_the_WAREHOUSE_endpoint(settings: Settings, captured: list[dict[str, str]]) -> None:
    """The wrong-but-plausible failure: an http warehouse behind an https estate cannot open at all.

    A MIXED pair on purpose — matching schemes pass whichever endpoint the derivation reads.
    """
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket", endpoint="http://tenant.store:9000")

    assert captured[0][f"{_STORAGE}allow_http"] == "true", "allow_http was derived from the ESTATE endpoint, so this warehouse cannot be opened"


def test_allow_http_is_FALSE_for_an_https_warehouse(settings: Settings, captured: list[dict[str, str]]) -> None:
    """The other direction, so a builder hardcoding `true` fails here rather than in production."""
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket", endpoint="https://tenant.store:9000")

    assert captured[0][f"{_STORAGE}allow_http"] == "false"


def test_the_estate_CREDENTIALS_still_travel(settings: Settings, captured: list[dict[str, str]]) -> None:
    """An endpoint override must not silently drop the credential the connection still needs.

    This is the leg that says the increment is the ENDPOINT half and nothing more: the material comes
    from the estate's own resolved settings, exactly as before, and nothing per-warehouse rides here.
    """
    namespace_module.build_namespace_for_root(settings, "s3://tenant-bucket", endpoint="http://tenant.store:9000")

    assert captured[0][f"{_STORAGE}access_key_id"] == "k"
    assert captured[0][f"{_STORAGE}secret_access_key"] == "s"


class _Req:
    """The two pieces of `request` the resolver touches, without standing up an app.

    Handed over with an explicit `cast` at each call rather than a `type: ignore`: these functions read
    `request.app.state.warehouse_binding_cache` and `.warehouse_namespaces` and nothing else, so the
    cast states what is actually required of the argument instead of silencing the checker.
    """

    class _State:
        def __init__(self) -> None:
            self.warehouse_binding_cache: dict[str, dict[str, str]] = {}
            self.warehouse_namespaces: dict[tuple[str, str | None], object] = {}

    def __init__(self) -> None:
        self.app = type("_App", (), {"state": _Req._State()})()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_endpoint", "expected"),
    [("http://tenant.store:9000", "http://tenant.store:9000"), (None, None), ("", None)],
)
async def test_the_resolver_carries_the_RECORDS_endpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, record_endpoint: str | None, expected: str | None
) -> None:
    """The wiring leg. The builder honouring an endpoint proves nothing if the record's never reaches it.

    The empty-string case is not padding: a record written with `endpoint: ""` must mean "the estate's",
    not an override to the empty endpoint, which would build a connection pointing nowhere.
    """
    from catalog.api import dependencies

    record: dict[str, str] = {"id": "wh-1", "status": "active"}
    if record_endpoint is not None:
        record["endpoint"] = record_endpoint
    monkeypatch.setattr(dependencies.warehouses, "binding_for_namespace", lambda *a, **k: {"warehouse_id": "wh-1", "root_uri": "s3://tenant-bucket"})
    monkeypatch.setattr(dependencies.warehouses, "get_warehouse", lambda *a, **k: record)

    resolved = await dependencies._resolve_warehouse_root(cast(Request, _Req()), settings, "tenant-ns")

    assert resolved == ("s3://tenant-bucket", expected)


@pytest.mark.asyncio
async def test_a_DEACTIVATED_warehouse_is_still_refused(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate this read had to keep. Reading the whole record instead of just the status is only safe
    if the fail-closed answer survives it — including for a record that is MISSING entirely."""
    from lance_namespace import PermissionDeniedError

    from catalog.api import dependencies

    monkeypatch.setattr(dependencies.warehouses, "binding_for_namespace", lambda *a, **k: {"warehouse_id": "wh-1", "root_uri": "s3://tenant-bucket"})
    for record in ({"id": "wh-1", "status": "deactivated"}, None):
        monkeypatch.setattr(dependencies.warehouses, "get_warehouse", lambda *a, _r=record, **k: _r)
        with pytest.raises(PermissionDeniedError):
            await dependencies._resolve_warehouse_root(cast(Request, _Req()), settings, "tenant-ns")


def test_the_connection_cache_keys_on_the_ENDPOINT_too(settings: Settings, captured: list[dict[str, str]]) -> None:
    """A root-only key would serve the OLD store's connection for the life of the process."""
    from catalog.api import dependencies

    request = cast(Request, _Req())
    dependencies.namespace_for_root(request, settings, "s3://tenant-bucket", endpoint="http://old.store:9000")
    dependencies.namespace_for_root(request, settings, "s3://tenant-bucket", endpoint="http://new.store:9000")

    assert [p["storage.endpoint"] for p in captured] == ["http://old.store:9000", "http://new.store:9000"]


def test_every_namespace_for_root_CALLER_passes_an_endpoint() -> None:
    """A warehouse-rooted connection built without the record's endpoint opens the ESTATE's store.

    DERIVED from the source rather than listed, the shape `test_the_maintenance_doors_refuse_a_branch_
    they_cannot_honour` uses: a new door resolving a warehouse connection inherits this without an edit
    here. Three callers were found ignoring the endpoint after the resolver already threaded it — the
    create-namespace door, the delete cascade and the undrop — and none of them failed loudly: each
    opened the estate's store, where the warehouse's namespaces simply are not, so a cascade would
    report a clean delete having dropped nothing.

    Matched on the AST's keywords, not on source text, so a docstring naming the parameter cannot
    satisfy it.
    """
    import ast
    import pathlib

    import catalog

    offenders: list[str] = []
    for path in sorted(pathlib.Path(catalog.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name != "namespace_for_root":
                continue
            if not any(kw.arg == "endpoint" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"{offenders} build a warehouse connection without the record's `endpoint`, so they open the ESTATE's store"
