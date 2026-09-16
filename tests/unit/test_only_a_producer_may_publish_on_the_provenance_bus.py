"""`lineage.events.v1` is deny-by-default: each app gets only the directions it actually uses.

[[LH-064]]. The row asked for a Dapr `accessControl` policy. That block governs SERVICE INVOCATION
only — `docs.dapr.io/operations/configuration/invoke-allowlist`, verbatim: it restricts "what the
operations *calling* applications can perform, **via service invocation**, on the *called* application"
— so it would never see a pub/sub delivery, and building it would have shipped a control that cannot
fire. The mechanism that does reach pub/sub is `protectedTopics` + `publishingScopes` /
`subscriptionScopes`.

BOTH HALVES ARE REQUIRED AND EITHER ALONE IS INERT. The pub/sub-scopes page says an unspecified
`publishingScopes` means "all apps can publish to all topics", and an app merely omitted from it keeps
full access; `protectedTopics` is what turns the topic deny-by-default — "an application must be
explicitly granted publish or subscribe permissions". A component with scopes and no protection grants
nothing it did not already have.

THE DIRECTIONS WERE MEASURED, NOT READ OFF THE TEMPLATES, and an env-var scan gets them wrong twice:

  * `lineage` PUBLISHES and no environment variable says so — `api/reconcile_cron.py:470-474` is the
    outbox relay's drain re-publishing a recovered event, which `main.py:114` explains: without it "the
    relay repairs the GRAPH while the cascade it was meant to restart stays halted". A scopes file
    written from the env pairs would have denied exactly that republish.
  * `maintenance` registers NO subscriptions at all (its write-event lane is gated on `workTopic`),
    while the templates describe one — so it is granted publish and not subscribe.

Read off the running sidecars' `/v1.0/metadata` 2026-09-16, the subscriptions were: lineage and
notifications and medallion-producer on `lineage.events.v1`; the three stage runners on
`medallion.bronze|silver|media` and their own DLQ; maintenance none.

ONLY `lineage.events.v1` IS PROTECTED, deliberately. `medallion.*`, `training.jobs` and every `dlq.*`
keep exactly today's behaviour, so this change cannot stop a cascade trigger or a dead-letter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]
TOPIC = "lineage.events.v1"

#: app-id -> (may publish the provenance topic, may subscribe to it). The one consumer-only identity is
#: `notifications`, and denying it is what this buys today; the durable value is that a NINTH app-id
#: added later inherits the refusal instead of silently gaining publish rights on the provenance bus.
_ROLES = {
    "lineage-pubsub": ("catalog", True, False),
    "lineage-pubsub-lineage": ("lineage", True, True),
    "lineage-pubsub-medallion-producer": ("medallion-producer", True, True),
    "lineage-pubsub-bronze-to-silver": ("bronze-to-silver", True, False),
    "lineage-pubsub-silver-to-gold": ("silver-to-gold", True, False),
    "lineage-pubsub-media-to-silver": ("media-to-silver", True, False),
    "lineage-pubsub-notifications": ("notifications", False, True),
}


def _components() -> dict[str, dict[str, str]]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm, "template", "rask", str(REPO / "chart"),
        "--set-string", "frontend.oidc.sessionSecret=ci-dummy-session-secret-at-least-32-chars",
        "--set-string", "frontend.oidc.publicIssuer=http://auth/dex",
        "--set-string", "frontend.oidc.publicOrigin=http://auth",
        "--set", "image.localImages=true",
    ]  # fmt: skip
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    found: dict[str, dict[str, str]] = {}
    for doc in yaml.load_all(out, Loader=FAST_LOADER):
        if isinstance(doc, dict) and doc.get("kind") == "Component" and doc["spec"]["type"] == "pubsub.jetstream":
            found[doc["metadata"]["name"]] = {m["name"]: str(m.get("value", "")) for m in doc["spec"].get("metadata", [])}
    return found


_RENDERED = _components()


def test_the_lineage_components_render_at_all() -> None:
    """Without this the parametrized assertions below could pass by finding nothing."""
    missing = [name for name in _ROLES if name not in _RENDERED]

    assert not missing, f"these lineage pub/sub components did not render, so their scopes are unasserted: {missing}"


@pytest.mark.parametrize("component", sorted(_ROLES))
def test_the_provenance_topic_is_protected_on_every_lineage_component(component: str) -> None:
    """Without `protectedTopics` the scopes below grant nothing — an omitted app keeps full access."""
    protected = _RENDERED[component].get("protectedTopics", "")

    assert TOPIC in protected.split(","), (
        f"{component} carries scopes but does not protect {TOPIC!r} ({protected!r}), so every scope on it is inert "
        "and any app reaching this component may still publish the provenance bus"
    )


@pytest.mark.parametrize("component", sorted(_ROLES))
def test_each_app_is_granted_exactly_the_directions_it_uses(component: str) -> None:
    app, may_publish, may_subscribe = _ROLES[component]
    metadata = _RENDERED[component]

    published = metadata.get("publishingScopes", "")
    subscribed = metadata.get("subscriptionScopes", "")

    assert (f"{app}={TOPIC}" in published) is may_publish, (
        f"{component}: publish grant for {app!r} is {published!r}, expected may_publish={may_publish} — "
        "a wrong grant here either lets a consumer forge provenance or stops a real producer silently"
    )
    assert (f"{app}={TOPIC}" in subscribed) is may_subscribe, (
        f"{component}: subscribe grant for {app!r} is {subscribed!r}, expected may_subscribe={may_subscribe} — "
        "`protectedTopics` gates subscribe as well as publish, so a missing grant stops delivery"
    )


def test_notifications_is_the_denial_this_buys() -> None:
    """Named rather than left implicit: it is the only app the change refuses today, and if it ever
    becomes a producer this test is where that decision has to be made rather than inherited."""
    metadata = _RENDERED["lineage-pubsub-notifications"]

    assert "publishingScopes" not in metadata, f"notifications was granted publish on the provenance bus: {metadata.get('publishingScopes')!r}"
    assert f"notifications={TOPIC}" in metadata.get("subscriptionScopes", ""), "notifications lost its bus ingress — the inbox would go silent"


def test_no_other_topic_was_swept_into_the_protection() -> None:
    """The blast radius, asserted. Protecting `medallion.bronze` or a `dlq.*` here would stop a cascade
    trigger or a dead-letter, on components whose other topics this row never examined."""
    for name, metadata in _RENDERED.items():
        protected = [topic for topic in metadata.get("protectedTopics", "").split(",") if topic]
        assert protected in ([], [TOPIC]), f"{name} protects {protected}, which reaches beyond the provenance topic this change measured"
