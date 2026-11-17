"""The control lane's relay, end to end through the RENDER — a values key that renders nothing is the bug.

`catalog/api/control_relay.py` drains `LANCE_CONTROL_OUTBOX_URI` and re-publishes what a bus outage
left staged, on the catalog's own timer, which runs when the catalog is given a period
(`LANCE_CONTROL_RELAY_PERIOD_SECONDS`). Staging and the relay are one switch, and the control prefix must not be the
LINEAGE prefix: lineage's relay parses every object in its own prefix as an OpenLineage `RunEvent` and DELETES what it
cannot parse, so a shared prefix hands the control lane's only durable copy to something that destroys it, strictly
worse than staging nowhere.

Why this matters more than the usual wiring gate: a pipeline whose next step starts on a control event (a tag's move)
and polls nothing loses that step with the tag moved, the route 200 and every pod green.
"""

from __future__ import annotations

import pathlib

from tests.unit.chart_render import render


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _render(*set_values: str) -> list[dict]:
    sets = tuple(arg for value in set_values for arg in ("--set", value))
    return list(render("--set", "image.localImages=true", "--set-string", "auth.oidc.issuer=http://localhost:8080/dex", *sets))


def _catalog_env(docs: list[dict]) -> dict[str, str]:
    """The catalog container's plain-valued env, keyed by name (selected by LABEL, not by release name)."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        labels = ((doc["spec"]["template"].get("metadata") or {}).get("labels")) or {}
        if labels.get("app.kubernetes.io/component") != "catalog":
            continue
        env: dict[str, str] = {}
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            for item in container.get("env") or []:
                if "value" in item:
                    env[item["name"]] = str(item["value"])
        return env
    return {}


def test_the_catalog_stages_control_events_into_a_prefix_its_OWN_relay_drains() -> None:
    """Staged, and staged somewhere the control relay reads — never the lineage prefix."""
    env = _catalog_env(_render())
    assert env, "no catalog Deployment rendered"

    staged = env.get("LANCE_CONTROL_OUTBOX_URI", "")
    assert staged, (
        "the catalog Deployment renders no LANCE_CONTROL_OUTBOX_URI, so `BusControlEmitter.emit` degrades "
        "to a plain publish: a NATS blip loses a control event outright, and with it the step of any pipeline "
        "that event is the only trigger for"
    )
    lineage_prefix = env.get("LANCE_LINEAGE_OUTBOX_URI", "")
    assert staged.rstrip("/") != lineage_prefix.rstrip("/"), (
        f"the control lane stages to the LINEAGE prefix ({staged!r}) — lineage's relay parses every "
        "object there as an OpenLineage RunEvent and DELETES what it cannot parse, so each staged control "
        "event is destroyed as poison by the very drain that was supposed to save it"
    )


def test_staging_and_the_relay_are_ONE_switch() -> None:
    """Half of this mechanism is worse than neither half: staging with no relay accumulates a durable
    copy nothing reads, and a relay with no staging drains an empty prefix forever."""
    on, off = _catalog_env(_render()), _catalog_env(_render("catalog.controlOutbox.enabled=false"))

    assert float(on.get("LANCE_CONTROL_RELAY_PERIOD_SECONDS", 0)) > 0, (
        "the catalog stages control events and is given no relay period, so nothing republishes them"
    )
    assert "LANCE_CONTROL_OUTBOX_URI" not in off, "catalog.controlOutbox.enabled=false must also stop the staging (its relay is off with it)"
    assert "LANCE_CONTROL_RELAY_PERIOD_SECONDS" not in off, "the relay's timer runs with no prefix to drain"
