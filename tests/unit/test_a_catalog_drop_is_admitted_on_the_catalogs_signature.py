"""A catalog DROP is admitted on the catalog's verified signature, because the grants it would be checked against are gone.

The catalog emits `drop_table` / `deregister_table` and then revokes the table's tuples in the same request,
so a delivery after the revoke — every bus delivery and every outbox drain — asked FGA about a table nobody
may write, and the drop was refused and never recorded. Owner ruling 2026-09-26: a DROP whose signature
verifies as the catalog is the catalog's attestation that it enforced `can_drop` at commit. Everything
else keeps the full check, so the tests below pin the refusals as well as the admission.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from lance_namespace import PermissionDeniedError

from catalog.core.lineage_emit import DEREGISTER_TABLE, DROP_TABLE, DaprEmitter, emit_write_event
from lineage.api import fga_deps, reconcile_cron
from lineage.core.config import LineageSettings
from lineage.models import DatasetEvent, parse_event
from lineage_kit.signing import SIGNATURE_FACET, attach_signature, signature_of
from service_kit.governed import fga
from service_kit.lakehouse import outbox


_REPO = Path(__file__).resolve().parents[2]
_CATALOG = "service-catalog"
_CATALOG_KEY = "FGjbWnUx1oRd7TqYpE4sLvZc0MhA6iK2eB9wQn3t"
_OTHER = "service-medallion-producer"
_OTHER_KEY = "Zq8Kx2Lm5Nb7Vc9Xz1As3Df4Gh6Jk0Pl2Oi4Uy6"
_AUTHOR = "CgVhbGljZRIFbG9jYWw"
_SEGMENTS: list[str] = ["gov", "bronze"]
_TABLE = "$".join(_SEGMENTS)


class _SidecarDown:
    async def publish_event(self, **_kwargs: object) -> None:
        raise ConnectionError("sidecar unreachable")


class _Publisher:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish_event(
        self,
        pubsub_name: str,
        topic_name: str,
        data: bytes | str,
        publish_metadata: dict[str, str] | None = None,
        metadata: tuple[tuple[str, str | bytes], ...] | None = None,
        data_content_type: str | None = None,
    ) -> None:
        self.published.append(data if isinstance(data, str) else data.decode())


class _Repo:
    def __init__(self) -> None:
        self.runs: list[Any] = []
        self.datasets: list[DatasetEvent] = []
        self.refusals: list[dict[str, str | None]] = []

    async def ingest_event(self, event: Any) -> None:  # noqa: ANN401 — the doors' own shape
        self.runs.append(event)

    async def ingest_dataset_event(self, event: DatasetEvent) -> None:
        self.datasets.append(event)

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})


def _settings(tmp_path: Path) -> LineageSettings:
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate({"database_url": "postgresql://x/y", "outbox_uri": str(tmp_path / "outbox"), "fga_enabled": True, **auth})


def _nobody_may_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state a drop is delivered into: both keys resolvable, and no grant left on the table."""

    async def batch_check(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    async def read_object_tuples(_client: object, _obj: str) -> list[object]:
        return []

    keys = {_CATALOG: _CATALOG_KEY, _OTHER: _OTHER_KEY}
    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: keys.get)
    monkeypatch.setattr(fga, "batch_check", batch_check)
    monkeypatch.setattr(fga, "read_object_tuples", read_object_tuples)


def _stage(settings: LineageSettings, operation: str) -> str:
    """What the catalog leaves in the outbox when a drop's publish fails: the real emitter, signed."""
    emitter = DaprEmitter(
        cast("Any", _SidecarDown()),
        "pubsub",
        "lineage.events.v1",
        job_namespace="lance",
        timeout_seconds=5.0,
        outbox_uri=settings.outbox_uri,
        service_identity=_CATALOG,
        token_resolver=lambda identity: _CATALOG_KEY if identity == _CATALOG else None,
    )
    asyncio.run(emit_write_event(emitter, _SEGMENTS, delimiter="$", author=_AUTHOR, version=None, operation=operation, authorization=None))
    [(_, staged_json)] = list(outbox.list_events(settings.outbox_uri, {}))
    doc = json.loads(staged_json)
    assert "dataset" in doc and signature_of(doc) is not None, "the staged drop is not a signed DatasetEvent, so this file tests nothing"
    return staged_json


def _request() -> Any:  # noqa: ANN401 — the gate reads only app.state
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))


def _gate(settings: LineageSettings, doc: dict[str, Any]) -> None:
    asyncio.run(fga_deps.enforce_bus_authz(parse_event(doc), _request(), settings, doc))


def test_the_catalog_identity_lineage_trusts_is_the_one_the_chart_gives_the_catalog() -> None:
    """Two readers of one name: the catalog signs as `catalog.serviceIdentity`, and lineage admits that signer's drops."""
    chart = yaml.safe_load((_REPO / "chart" / "values.yaml").read_text())

    assert chart["catalog"]["serviceIdentity"] == LineageSettings.model_fields["catalog_service_identity"].default


@pytest.mark.parametrize("operation", [DROP_TABLE, DEREGISTER_TABLE])
def test_the_relay_recovers_a_catalog_drop_after_its_grants_are_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    settings = _settings(tmp_path)
    staged_json = _stage(settings, operation)
    _nobody_may_write(monkeypatch)
    repo, publisher = _Repo(), _Publisher()

    outcome = asyncio.run(reconcile_cron._drain_outbox(_request(), cast("Any", repo), settings, {}, publisher))

    assert (outcome.drained, outcome.refused) == (1, 0), f"the catalog's {operation} was refused: {outcome}, {repo.refusals}"
    assert [(e.dataset.name, e.operation) for e in repo.datasets] == [(_TABLE, operation)]
    assert publisher.published == [staged_json]


def test_an_unsigned_drop_keeps_the_full_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    doc = json.loads(_stage(settings, DROP_TABLE))
    doc["dataset"]["facets"].pop(SIGNATURE_FACET)
    _nobody_may_write(monkeypatch)

    with pytest.raises(PermissionDeniedError):
        _gate(settings, doc)


def test_a_drop_signed_by_another_service_keeps_the_full_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid signature is not enough: only the catalog attests to a catalog drop."""
    settings = _settings(tmp_path)
    doc = json.loads(_stage(settings, DROP_TABLE))
    doc["dataset"]["facets"].pop(SIGNATURE_FACET)
    doc = attach_signature(doc, key=_OTHER_KEY, identity=_OTHER, on_behalf_of=_AUTHOR)
    _nobody_may_write(monkeypatch)

    with pytest.raises(PermissionDeniedError):
        _gate(settings, doc)


def test_a_catalog_signed_create_still_needs_its_authors_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The attestation is for DROPs only: every other catalog event is checked against the author's grants."""
    settings = _settings(tmp_path)
    emitter_json = _stage(settings, "create_table")
    _nobody_may_write(monkeypatch)

    with pytest.raises(PermissionDeniedError):
        _gate(settings, json.loads(emitter_json))


def test_a_drop_claiming_the_catalogs_signature_that_does_not_verify_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The attestation is the VERIFIED signature, never the name it carries: a drop re-targeted after the
    catalog signed it still names `service-catalog`, and must be refused on the signature before the
    admission is even considered."""
    settings = _settings(tmp_path)
    doc = json.loads(_stage(settings, DROP_TABLE))
    doc["dataset"]["name"] = "gov$someone-elses"
    found = signature_of(doc)
    assert found is not None and found.identity == _CATALOG, "the re-targeted drop no longer names the catalog, so this tests nothing"
    _nobody_may_write(monkeypatch)

    with pytest.raises(PermissionDeniedError, match="does not verify"):
        _gate(settings, doc)
