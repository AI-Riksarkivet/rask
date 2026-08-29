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
from ingest.sources import SourceDescriptor


ADAPTERS = Path(__file__).resolve().parents[1] / "src" / "ingest" / "adapters.py"

#: The kinds `adapters.py` ships. Asserted as a SUBSET of what the endpoint serves, never as the whole
#: of it: the registry is process-global and `test_ingest_api.py` registers a `test-src` kind for its
#: own A9 fixture, so an equality assertion here passes alone and fails in the suite — which reads as
#: a flaky test rather than as the over-strict assertion it is.
BUILTIN_KINDS = ("local-dir", "s3-prefix")


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> list[SourceDescriptor]:
    """The endpoint's payload, read through the real app factory.

    Parsed back through `SourceDescriptor` rather than kept as raw JSON: the tests below read
    `entry.options[i].name` and `.required`, which on a `dict[str, object]` are untyped subscripts
    that no gate can check — the reason two of them carried a `# type: ignore`. Validating here
    also asserts the served shape IS the model the endpoint declares, which was previously assumed.

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
    return [SourceDescriptor.model_validate(entry) for entry in response.json()]


def test_every_builtin_kind_is_offered(sources: list[SourceDescriptor]) -> None:
    """The three built-ins, including the two the UI could not previously reach."""
    served = {entry.kind for entry in sources}
    assert set(BUILTIN_KINDS) <= served, f"registered kinds missing from the endpoint: {sorted(set(BUILTIN_KINDS) - served)}"


def test_each_kind_declares_the_options_its_adapter_READS(sources: list[SourceDescriptor]) -> None:
    """The descriptors are only useful if they match the code, so this checks them against it.

    Every `spec.options.get("x")` in `adapters.py` is a field that kind consumes; a form built from a
    descriptor that omits one cannot ingest with that kind at all, and the omission is invisible
    until someone tries. Read from the AST rather than by eye, so adding an option to an adapter
    without describing it fails here rather than in a cluster.
    """
    described = {entry.kind: {opt.name for opt in entry.options} for entry in sources}

    read: dict[str, set[str]] = {}
    tree = ast.parse(ADAPTERS.read_text(encoding="utf-8"))
    for func in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
        # `_iiif`, `_iiif_lineage` and `_iiif_partition` all belong to the `iiif` kind; the leading
        # underscore and the twin suffixes are naming, not identity. Missing a suffix invents a
        # PHANTOM kind — `_iiif_partition` first registered here as kind `iiif-partition`, whose
        # options are described nowhere, so this gate failed on a function that was entirely correct.
        # `_external_base` is the third twin (the §4.1 blob placement) and arrived the same way: it
        # read `options["root"]`, which local-dir DOES describe, and this gate reported it as an
        # undescribed option of a kind that does not exist.
        # `_endpoint` is the fourth (which object store the run's keys are on, carried to the worker)
        # and arrived the same way again: it reads `options["endpoint"]`, which s3-prefix DOES
        # describe, and the gate reported a phantom kind `s3-prefix-endpoint`.
        kind = (
            func.name.lstrip("_").removesuffix("_lineage").removesuffix("_partition").removesuffix("_external_base").removesuffix("_endpoint").replace("_", "-")
        )
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
                read.setdefault(kind, set()).add(str(node.args[0].value))

    assert read, "found no `spec.options.get(...)` calls — the AST walk is checking nothing"

    for kind, fields in sorted(read.items()):
        undescribed = sorted(fields - described.get(kind, set()))
        assert undescribed == [], f"{kind!r} reads options {undescribed} that no caller is told about"


def test_a_required_option_is_the_one_the_adapter_REFUSES_without(sources: list[SourceDescriptor]) -> None:
    """`required` has to mean what the adapter enforces, or a form validates the wrong thing.

    Both refusals are real: "local-dir source requires options.root", "s3-prefix source requires
    options.bucket". A descriptor marking something else required would block a legal ingest; one
    marking nothing required would let the UI submit a request the backend always rejects.
    """
    required = {entry.kind: {opt.name for opt in entry.options if opt.required} for entry in sources}

    assert required["local-dir"] == {"root"}
    assert required["s3-prefix"] == {"bucket"}


def test_a_builtin_kind_carries_a_human_label(sources: list[SourceDescriptor]) -> None:
    """A select showing `s3-prefix` is a registry key leaking into the product.

    Cheap to get wrong in the direction that matters: `label` defaults to `kind`, so an unlabelled
    registration renders its key and nothing fails.
    """
    unlabelled = sorted(entry.kind for entry in sources if entry.kind in BUILTIN_KINDS and entry.label == entry.kind)
    assert unlabelled == [], f"kinds rendering their registry key as a label: {unlabelled}"


def _served(monkeypatch: pytest.MonkeyPatch) -> dict[str, SourceDescriptor]:
    """The descriptors AS SERVED, keyed by kind — the same door the fixture above uses.

    Not `describe_sources()` directly, for the reason that fixture states: the registry is populated
    by an import-time side effect inside the factory, so calling it directly proves the descriptors
    exist while saying nothing about whether the app serves them. Availability is resolved per
    REQUEST, which is precisely the thing that has to be true through the door.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    response = TestClient(create_app()).get("/api/sources")
    assert response.status_code == 200, response.text
    return {entry["kind"]: SourceDescriptor.model_validate(entry) for entry in response.json()}


def test_a_kind_this_deployment_cannot_run_is_ADVERTISED_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lance-append` reads from `RASK_INGEST_LANCE_ROOT`, which defaults to EMPTY.

    So on a stock deployment the registry advertised a kind that refused every run, and the refusal
    named an environment variable to whoever was filling in the form. The reason now travels with the
    descriptor.

    ADVERTISED, not hidden — the estate's "show disabled, never hide" ruling. An option that vanishes
    is indistinguishable from a feature that was never built, so nobody reading the form can learn
    that a knob would enable it.
    """
    monkeypatch.delenv("RASK_INGEST_LANCE_ROOT", raising=False)
    by_kind = _served(monkeypatch)

    assert "lance-append" in by_kind, "an unusable kind must still be listed — hiding it loses the reason"
    assert by_kind["lance-append"].available is False
    assert "RASK_INGEST_LANCE_ROOT" in (by_kind["lance-append"].unavailable_reason or ""), "the reason must name the knob"


def test_the_same_kind_is_available_once_the_root_IS_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved per REQUEST, not at registration — the registry is built at import, so a knob set
    after the process started must not leave the form advertising the old answer."""
    monkeypatch.setenv("RASK_INGEST_LANCE_ROOT", "/data/exports")
    by_kind = _served(monkeypatch)

    assert by_kind["lance-append"].available is True
    assert by_kind["lance-append"].unavailable_reason is None


def test_the_kinds_that_need_nothing_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A precondition belongs to the kind that has one; the rest must not acquire a state."""
    monkeypatch.delenv("RASK_INGEST_LANCE_ROOT", raising=False)
    by_kind = _served(monkeypatch)

    assert by_kind["local-dir"].available is True
    assert by_kind["s3-prefix"].available is True
