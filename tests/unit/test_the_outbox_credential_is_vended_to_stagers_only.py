"""CONTRACT (CP-007): the estate vends a WRITE credential for its lineage outbox, to stagers only.

The outbox is the crash-recovery seam — a producer stages the full RunEvent JSON before it publishes,
and the relay re-ingests whatever survives a crash. INGEST COULD NOT STAGE AT ALL: it holds no S3 key
by design (STS-only, the stronger posture), so `_outbox_storage_options` returned an endpoint and no
credential and `stage_event` died `ACCESS_DENIED` on HeadBucket. Measured twice inside one real run
2026-09-10 — the primary emit was refused AND its backstop could not write, so the run lost its event
outright and still reported COMPLETE.

THE MECHANISM WAS DECIDED BY THE STANDING RULE, not by this door: "STS for STORAGE… a scoped static
key is not a fix". `vending.build_session_policy(bucket, prefix, tier, bases)` is prefix-GENERIC
rather than table-keyed, so it already expresses `(lance-catalog, _lineage_outbox, write)` and can
only RESTRICT the catalog's role, never widen it. What was missing was a door — the sole vending route
was `POST /v1/table/{id}/credentials`, and a control prefix is not a table.

THE DOOR TAKES NO PATH. It vends for the estate's OWN configured outbox and nothing else, so there is
no caller-supplied prefix to traverse and no way to ask it for a tenant's bucket. That is what keeps a
new vending surface from being a new attack surface.
"""

from __future__ import annotations

from typing import Any

import pytest

from catalog.api.v1.endpoints import outbox_credentials


def test_the_route_is_mounted_where_no_table_id_can_reach_it() -> None:
    """A control prefix is not a table, so the door must not live under `/v1/table/{id}`.

    Mounted there it would be addressable by a caller-supplied id, which is the one property this
    door must not have.
    """
    paths = {getattr(route, "path", "") for route in outbox_credentials.router.routes}
    assert paths == {"/v1/outbox/credentials"}, f"unexpected route surface: {paths}"


def test_the_gate_is_can_stage_events_on_the_ROOT_object() -> None:
    """Not a table rung and not a warehouse rung — staging is an estate privilege held by services.

    Read off the module rather than restated, so a gate that is quietly relaxed to `can_write_data`
    (which every tenant writer holds) fails here rather than in production.
    """
    import inspect

    body = inspect.getsource(outbox_credentials.vend_outbox_credentials)
    assert "can_stage_events" in body, "the door does not check the rung minted for it"
    assert "fga_root_object" in body, "the rung must be checked on the ESTATE root, never on a tenant object"


@pytest.mark.asyncio
async def test_it_vends_for_the_CONFIGURED_outbox_and_takes_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The location comes from settings, never from the caller — the property that bounds this door."""
    seen: dict[str, Any] = {}

    class _Vendor:
        def vend(self, *, table_location: str, tier: str, **_kw: Any) -> None:
            seen["location"] = table_location
            seen["tier"] = tier

    await outbox_credentials.vend_outbox_credentials(
        settings=_settings(),
        token=None,
        client=None,
        vendor=_Vendor(),
        web_identity_token=None,
    )
    assert seen["location"] == "s3://lance-catalog/_lineage_outbox"
    assert seen["tier"] == "write"


def _settings() -> Any:
    from catalog.core.config import Settings

    return Settings.model_validate(
        {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y", "LANCE_LINEAGE_OUTBOX_URI": "s3://lance-catalog/_lineage_outbox"}
    )
