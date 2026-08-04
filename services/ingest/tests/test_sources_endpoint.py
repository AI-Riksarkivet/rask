"""I1 at the UI boundary: a caller must be able to READ the registry, not restate it.

The backend half of I1 landed — a source is one `register()` call, and `build_source` refuses an
unknown kind by naming the ones that exist. The frontend half did not. `ingestIIIFVolume()` hardcoded
`kind: 'iiif'`, and the compute zone's form hardcoded it again, directly beneath a comment explaining
that the door takes `{kind, project, dataset, options}` and resolves the adapter from a registry.

That is the same defect the registry exists to prevent, one layer out. `S3PrefixSource` sat written,
unit-tested and unreachable for months because reaching it meant editing several files; leaving the
kinds unreadable means reaching a new one means editing a Svelte page. A registry nothing can read
drifts by construction, and the drift is silent — the form keeps working for the one kind it knows.

So the endpoint serves the kinds AND the options each takes, and these tests assert that what it
serves matches what the adapters actually read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingest import create_app


ADAPTERS = Path(__file__).resolve().parents[1] / "src" / "ingest" / "adapters.py"

#: The kinds `adapters.py` ships. Asserted as a SUBSET of what the endpoint serves, never as the whole
#: of it: the registry is process-global and `test_ingest_api.py` registers a `test-src` kind for its
#: own A9 fixture, so an equality assertion here passes alone and fails in the suite — which reads as
#: a flaky test rather than as the over-strict assertion it is.
BUILTIN_KINDS = ("iiif", "local-dir", "s3-prefix")


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """The endpoint's payload, read through the real app factory.

    Through `create_app()` rather than by calling `describe_sources()` directly, because the registry
    is populated by an import-time side effect inside the factory — a test that imports the registry
    itself proves the descriptors exist while saying nothing about whether the app serves them.

    `monkeypatch.setenv`, not `os.environ.setdefault`. Routers mount under `settings.api_prefix`,
    whose CODE default is `/api/v1` while every deployment sets `/api` — so the prefix has to be
    pinned or the request 404s. `setdefault` pinned nothing when another module had already set a
    different value, which is why these passed alone and errored in the full suite. Function-scoped
    for the same reason: a module-scoped fixture outlives the monkeypatch that makes it correct.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    response = TestClient(create_app()).get("/api/sources")
    assert response.status_code == 200, response.text
    return response.json()


def test_every_builtin_kind_is_offered(sources: list[dict[str, object]]) -> None:
    """The three built-ins, including the two the UI could not previously reach."""
    served = {str(entry["kind"]) for entry in sources}
    assert set(BUILTIN_KINDS) <= served, f"registered kinds missing from the endpoint: {sorted(set(BUILTIN_KINDS) - served)}"


def test_each_kind_declares_the_options_its_adapter_READS(sources: list[dict[str, object]]) -> None:
    """The descriptors are only useful if they match the code, so this checks them against it.

    Every `spec.options.get("x")` in `adapters.py` is a field that kind consumes; a form built from a
    descriptor that omits one cannot ingest with that kind at all, and the omission is invisible
    until someone tries. Read from the AST rather than by eye, so adding an option to an adapter
    without describing it fails here rather than in a cluster.
    """
    described = {str(entry["kind"]): {str(opt["name"]) for opt in entry["options"]} for entry in sources}  # type: ignore[index,union-attr]

    read: dict[str, set[str]] = {}
    tree = ast.parse(ADAPTERS.read_text(encoding="utf-8"))
    for func in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
        # `_iiif` / `_iiif_lineage` both belong to the `iiif` kind; the leading underscore and any
        # `_lineage` suffix are naming, not identity.
        kind = func.name.lstrip("_").removesuffix("_lineage").replace("_", "-")
        for node in ast.walk(func):
            is_options_get = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "options"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            )
            if is_options_get:
                read.setdefault(kind, set()).add(str(node.args[0].value))  # type: ignore[union-attr]

    assert read, "found no `spec.options.get(...)` calls — the AST walk is checking nothing"

    for kind, fields in sorted(read.items()):
        undescribed = sorted(fields - described.get(kind, set()))
        assert undescribed == [], f"{kind!r} reads options {undescribed} that no caller is told about"


def test_a_required_option_is_the_one_the_adapter_REFUSES_without(sources: list[dict[str, object]]) -> None:
    """`required` has to mean what the adapter enforces, or a form validates the wrong thing.

    Both refusals are real: "local-dir source requires options.root", "s3-prefix source requires
    options.bucket". A descriptor marking something else required would block a legal ingest; one
    marking nothing required would let the UI submit a request the backend always rejects.
    """
    required = {str(entry["kind"]): {str(opt["name"]) for opt in entry["options"] if opt["required"]} for entry in sources}  # type: ignore[index,union-attr]

    assert required["local-dir"] == {"root"}
    assert required["s3-prefix"] == {"bucket"}
    assert required["iiif"] == {"volume_id"}


def test_a_builtin_kind_carries_a_human_label(sources: list[dict[str, object]]) -> None:
    """A select showing `s3-prefix` is a registry key leaking into the product.

    Cheap to get wrong in the direction that matters: `label` defaults to `kind`, so an unlabelled
    registration renders its key and nothing fails.
    """
    unlabelled = sorted(str(entry["kind"]) for entry in sources if entry["kind"] in BUILTIN_KINDS and entry["label"] == entry["kind"])
    assert unlabelled == [], f"kinds rendering their registry key as a label: {unlabelled}"
