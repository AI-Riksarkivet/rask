"""A source the door will refuse must not advertise itself as available.

MEASURED on the live estate 2026-08-26. `GET /api/ingest/sources` reported::

    lance-append   available=False  "RASK_INGEST_LANCE_ROOT is not set — ..."
    local-dir      available=True
    s3-prefix      available=True

and the very next call, `POST /api/ingest/ingests` with `kind: local-dir`, answered::

    400  "local-dir is not enabled here: set RASK_INGEST_LOCAL_ROOT to the directory it may read"

Both kinds read a root env var that defaults EMPTY, and both were empty on that deployment. One
declared its precondition and one did not, so one rendered correctly disabled and the other invited a
caller into a form that could not submit.

This is the estate's own "show disabled, never hide" ruling failing in its other direction: the
control was shown ENABLED when it should have been shown disabled with a reason. `lance_append_unusable`
already records why that matters — "the kind was advertised by the registry and refused every run —
naming an environment variable to whoever was filling in the form".

The gate is written over the REGISTRY rather than against the two kinds by name, so a source added
later cannot reintroduce the asymmetry by forgetting the same field.
"""

from __future__ import annotations

import pytest
from ingest import sources
from ingest.adapters import LANCE_ROOT_ENV, LOCAL_ROOT_ENV


#: Kinds whose usability depends on deployment configuration, and the env var that supplies it.
_CONFIG_DEPENDENT = {"local-dir": LOCAL_ROOT_ENV, "lance-append": LANCE_ROOT_ENV}


@pytest.mark.parametrize(("kind", "env"), sorted(_CONFIG_DEPENDENT.items()))
def test_a_source_with_no_root_configured_reports_itself_unavailable(kind: str, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset root ⇒ the form must render it disabled, with the reason, rather than accept a doomed submit."""
    monkeypatch.delenv(env, raising=False)

    described = {d.kind: d for d in sources.describe_sources()}
    assert kind in described, f"{kind} vanished from the registry"

    assert described[kind].available is False, f"{kind} advertises itself as available while {env} is unset — the door will refuse it with 400"
    reason = described[kind].unavailable_reason
    assert reason, f"{kind} is unavailable but names no reason, so the form can only render an inert control"
    assert env in reason, f"the reason should name the knob that fixes it, got: {reason!r}"


@pytest.mark.parametrize(("kind", "env"), sorted(_CONFIG_DEPENDENT.items()))
def test_the_same_source_reports_available_once_its_root_is_set(kind: str, env: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The other half: a configured deployment must not have its sources hidden."""
    monkeypatch.setenv(env, str(tmp_path))

    described = {d.kind: d for d in sources.describe_sources()}

    assert described[kind].available is True, f"{kind} is configured ({env}={tmp_path}) but still reports unavailable"
    assert described[kind].unavailable_reason is None
