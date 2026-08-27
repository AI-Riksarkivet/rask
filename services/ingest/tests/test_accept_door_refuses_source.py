"""A refused source must be refused AT THE DOOR, not by a run record twenty seconds later.

Two docstrings in this plane already promised accept-time refusal — `LanceFragmentSource.probe`
("the ACCEPT-time half of the plan's second guard … refused while the caller is still holding the
request") and `_lance_append`, which repeats it — and neither was wired. `build_source` was called in
exactly ONE place, the enumerate activity, which runs long after `POST /ingests` has answered 202.

The cost was not just a worse error. `ensure_dataset` provisions the target table BEFORE enumerate
reaches the guards, so a request refused for a SECURITY reason had already created one: a
`lance-append` naming `s3://lance-catalog/...` came back 202 ACCEPTED and left
`acme-bronze$should_refuse` registered as an empty dataset at version 1 (measured in-cluster
2026-08-23).

So these tests assert two different things, and both matter: that each guard's refusal is a 400 the
caller receives, and that the check runs BEFORE dispatch — a refusal that still dispatched would
still orphan a table while looking correct from the caller's side.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from ingest.api import IngestRequest, _refuse_unusable_source

from service_kit.exceptions import ValidationError


@pytest.fixture
def lance_root(tmp_path, monkeypatch):
    """A real readable dataset, with the confinement root pointed at its parent."""
    lance = pytest.importorskip("lance")
    import pyarrow as pa

    uri = str(tmp_path / "ok.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri, mode="create")
    monkeypatch.setenv("RASK_INGEST_LANCE_ROOT", str(tmp_path))
    monkeypatch.delenv("LANCE_REST_ROOT", raising=False)
    return uri


def _req(*, kind: str = "lance-append", options: dict[str, object] | None = None) -> IngestRequest:
    return IngestRequest(kind=kind, project="acme", dataset="probe", options=options or {})


@pytest.mark.asyncio
async def test_unknown_kind_is_400_not_202(lance_root: str) -> None:
    """`build_source` refuses loudly; the door must turn that into the caller's answer."""
    with pytest.raises(ValidationError, match="unknown source kind"):
        await _refuse_unusable_source(_req(kind="not-a-real-kind"))


@pytest.mark.asyncio
async def test_outside_the_confinement_root_is_400(lance_root: str, tmp_path) -> None:
    with pytest.raises(ValidationError, match="outside RASK_INGEST_LANCE_ROOT"):
        await _refuse_unusable_source(_req(options={"uri": "/etc/passwd.lance"}))


@pytest.mark.asyncio
async def test_a_governed_dataset_is_400_and_names_the_mover(lance_root: str, tmp_path, monkeypatch) -> None:
    """The security guard. Its message must name where the caller SHOULD go, or they build a copy path by hand."""
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    with pytest.raises(ValidationError, match="catalog-governed"):
        await _refuse_unusable_source(_req(options={"uri": str(tmp_path / "ok.lance")}))


@pytest.mark.asyncio
async def test_an_unreadable_dataset_is_400(lance_root: str, tmp_path) -> None:
    """lance raises ValueError for a missing dataset — guard 2, the one `probe()` exists for."""
    with pytest.raises(ValidationError, match=r"[Nn]ot found"):
        await _refuse_unusable_source(_req(options={"uri": str(tmp_path / "absent.lance")}))


@pytest.mark.asyncio
async def test_a_usable_source_passes(lance_root: str) -> None:
    """The door must not become a second place runs die."""
    await _refuse_unusable_source(_req(options={"uri": lance_root}))


@pytest.mark.asyncio
async def test_missing_required_option_is_400(lance_root: str) -> None:
    with pytest.raises(ValidationError, match=r"requires options\.uri"):
        await _refuse_unusable_source(_req(options={}))


@pytest.mark.asyncio
async def test_the_check_runs_BEFORE_dispatch(lance_root: str, tmp_path, monkeypatch) -> None:
    """Ordering is the whole point: dispatching a refused run is what orphaned the table.

    Asserting the 400 alone would pass even if the guard ran after `dispatch_run` — the caller would
    see the same status while a table was quietly created. So this drives the real endpoint and
    asserts dispatch was never reached.
    """
    from ingest import api

    dispatched: list[object] = []

    async def _spy(*args: object, **kwargs: object) -> object:
        dispatched.append(args)
        return None

    monkeypatch.setattr(api, "dispatch_run", _spy)

    async def _no_auth(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(api, "authorize_ingest", _no_auth)

    with pytest.raises(ValidationError):
        # `cast`, not a suppression: these five are genuinely unused on the refusal path — the
        # guard raises before `dispatch_run` touches any of them, which is exactly what is asserted.
        await api.create_ingest(
            body=_req(options={"uri": "/etc/passwd.lance"}),
            response=cast(Any, None),
            request=cast(Any, None),
            store=cast(Any, None),
            starter=cast(Any, None),
            settings=cast(Any, None),
            idempotency_key="idem-test",
        )
    assert dispatched == [], "the run was dispatched despite a refused source — the table gets orphaned"
