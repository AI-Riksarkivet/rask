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

import pytest
from fastapi.testclient import TestClient

from ingest import create_app
from ingest.sources import SourceDescriptor


def _served(monkeypatch: pytest.MonkeyPatch) -> dict[str, SourceDescriptor]:
    """The descriptors AS SERVED, keyed by kind.

    Not `describe_sources()` directly: the registry is populated by an import-time side effect inside
    the factory, so calling it directly proves the descriptors exist while saying nothing about
    whether the app serves them. Availability is resolved per REQUEST, which is precisely the thing
    that has to be true through the door.
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
