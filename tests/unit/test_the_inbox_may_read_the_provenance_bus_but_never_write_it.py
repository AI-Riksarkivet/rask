"""`notifications` subscribes to the provenance bus and must not be able to publish to it.

[[LH-064]]. It is the one consumer-only identity on `lineage.events.v1`: measured 2026-09-16 it has no
publish call sites of its own, and its sidecar registers exactly two topics on this component —
`lineage.events.v1` and `dlq.notifications`.

SCOPED ON ONE COMPONENT, and that is the whole lesson of the first attempt. The same three keys rolled
to all eight lineage components broke delivery, because **`subscriptionScopes` is not additive**:
naming an app in it makes that list the app's COMPLETE allowlist for the component, so a topic that is
merely unlisted — and never protected — is refused too. The live refusal was on `dlq.notifications`, a
topic `protectedTopics` never mentioned, and it cost 47 denials on medallion-producer, 16 here and 4 on
lineage before the revert.

SO THE INVARIANT THIS PINS IS THE ONE THAT BROKE, not the one the first test checked. That test asserted
"no topic outside `protectedTopics`", which was true and irrelevant — the harm came through the scopes
list. What has to hold is: **every topic the app subscribes to on this component appears in its
subscriptionScopes**, and the provenance topic appears in its publishing scope NOWHERE.

Verified live on a fresh pod after applying it: all three subscriptions registered
(`catalog.control.v1`, `dlq.notifications`, `lineage.events.v1`) and zero scope denials.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]
COMPONENT = "lineage-pubsub-notifications"
TOPIC = "lineage.events.v1"

#: What the sidecar actually registers on this component, read off `/v1.0/metadata` rather than assumed.
#: `catalog.control.v1` is NOT here: it arrives on a different component and is unaffected by these keys.
_SUBSCRIBES = ("dlq.notifications", TOPIC)


def _component() -> dict[str, str]:
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
    for doc in yaml.load_all(out, Loader=FAST_LOADER):
        if isinstance(doc, dict) and doc.get("kind") == "Component" and doc["metadata"]["name"] == COMPONENT:
            return {m["name"]: str(m.get("value", "")) for m in doc["spec"].get("metadata", [])}
    pytest.fail(f"{COMPONENT} did not render — this file would pass vacuously")


_RENDERED = _component()


def test_the_provenance_topic_is_protected_here() -> None:
    """Without the protection the scopes below grant nothing: an app merely omitted from
    `publishingScopes` keeps full access, which is the documented default."""
    assert TOPIC in _RENDERED.get("protectedTopics", "").split(","), (
        f"{COMPONENT} does not protect {TOPIC!r}, so its publishing scope is inert and the inbox could still publish provenance"
    )


def test_the_inbox_cannot_publish_the_provenance_bus() -> None:
    """The point of the change."""
    published = _RENDERED.get("publishingScopes", "")

    assert TOPIC not in published, f"notifications was granted publish on the provenance bus: {published!r}"
    assert "notifications=dlq.notifications" in published, (
        "notifications holds no publish grant at all — its sidecar parks dead letters on `dlq.notifications` via "
        "`deadLetterTopic`, and whether that publish is scope-checked is undocumented, so withholding it is the same "
        f"guess that broke delivery once: {published!r}"
    )


@pytest.mark.parametrize("topic", _SUBSCRIBES)
def test_every_topic_the_inbox_subscribes_to_is_listed(topic: str) -> None:
    """THE INVARIANT THAT BROKE. `subscriptionScopes` is a complete allowlist, not an addition to the
    protected set, so a topic missing here is refused even though nothing protects it."""
    subscribed = _RENDERED.get("subscriptionScopes", "")

    assert topic in subscribed, (
        f"{topic!r} is missing from {COMPONENT}'s subscriptionScopes ({subscribed!r}) — naming the app there makes that "
        "list its COMPLETE allowlist, so this subscription is refused at runtime with nothing in the render to show it"
    )


def test_no_sibling_component_carries_these_keys() -> None:
    """The blast radius, bounded at the mechanism that actually did harm. Eight components carried these
    keys once and three apps lost subscriptions; only this one may carry them until each app's full
    topic list has been measured the same way."""
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

    offenders = []
    for doc in yaml.load_all(out, Loader=FAST_LOADER):
        if not (isinstance(doc, dict) and doc.get("kind") == "Component" and doc["spec"]["type"] == "pubsub.jetstream"):
            continue
        name = doc["metadata"]["name"]
        keys = {m["name"] for m in doc["spec"].get("metadata", [])}
        if name != COMPONENT and keys & {"protectedTopics", "publishingScopes", "subscriptionScopes"}:
            offenders.append(name)

    assert not offenders, (
        f"these components gained scope keys without their full topic lists being measured: {offenders} — "
        "`subscriptionScopes` is a complete allowlist, so a partial one silently refuses the topics it omits"
    )
