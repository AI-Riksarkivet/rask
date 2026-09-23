"""A privileged identity's credential must not be readable by the other apps scoped to the same store.

[[XC-072]]. [[ZT-001]] made each ``service-token-<identity>`` INDEPENDENT of ``dapr.appToken``
(``lance.dedicatedServiceToken`` generates rather than derives), and that shipped. Independence is one
of the two properties a per-identity credential needs; the other is that its PEERS cannot read it, and
nothing in the chart provides it.

MEASURED LIVE 2026-09-23, from two pods in both directions. ``GET /v1.0/secrets/lance-secrets/lance``
against a pod's OWN sidecar needs no API token and returns the whole 21-field bundle. From
``rask-medallion-producer``: 8 ``service-token-*`` credentials, one of which is its own. From
``rask-lineage``: the same 8. So a signature keyed on this material (which is what [[LH-064]] wants)
would refuse an unauthenticated forger and refuse none of its peers.

WHY A ``Configuration`` ALONE IS NOT THE FIX, and why this gate asks about the SPLIT first: Dapr scopes
by secret NAME -- the ``{key}`` in ``GET /v1.0/secrets/{store}/{key}`` -- not by field within one. Every
token is a field of the single secret ``lance``. Measured from the pod: ``.../lance`` answers 200 with
21 fields while ``.../service-token-service-ingest`` answers HTTP 500, i.e. it is not an addressable
secret at all. So ``allowedSecrets: ["lance"]`` would grant the entire bundle.

THE PATTERN ALREADY EXISTS IN THIS FILE'S SUBJECT. ``chart/templates/openbao.yaml`` seeds
``secret/viewer-s3`` as its own secret, and says why in its own comment: "as their OWN secret rather
than two more keys on the `lance` bundle ... so the store registry can point a store at a credential
without every reader of the shared bundle gaining it". The service tokens never got that treatment.
"""

from __future__ import annotations

import re

import yaml
from test_invariants import _helm_template


#: The store whose bundle carries the privileged credentials.
STORE = "lance-secrets"

#: A credential written as a FIELD of another secret -- the shape this row exists to remove.
TOKEN_FIELD = re.compile(r"service-token-(\S+?)=")

#: A credential written as its OWN secret -- the shape that is scopeable.
TOKEN_SECRET = re.compile(r"secret/service-token-(\S+)")

#: The Configuration a scoped app-id must carry, by app-id.
CONFIG_FOR = "lance-config-{}".format


def _docs() -> list[dict]:
    # RENDERED AS THE CLUSTER DEPLOYS IT, not at bare defaults. `explorer.enabled` is off by default and
    # on in every real install, and it adds three app-ids to this store's scopes -- so a gate rendered
    # at defaults silently excused the explorer trio, which is the shape of the hole it is checking for.
    out = []
    for chunk in _helm_template("explorer.enabled=true").split("\n---\n"):
        try:
            doc = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("kind"):
            out.append(doc)
    return out


def _apps_scoped_to_the_store(docs: list[dict]) -> list[str]:
    """Every app-id the secret store admits. Derived from the rendered Component, never listed here."""
    for doc in docs:
        if doc.get("kind") == "Component" and doc.get("metadata", {}).get("name") == STORE:
            return list(doc.get("scopes") or [])
    return []


def _seeded_token_identities(docs: list[dict]) -> set[str]:
    """The identities the seed Job writes a dedicated credential for, read off the rendered command."""
    identities: set[str] = set()
    for doc in docs:
        if doc.get("kind") != "Job":
            continue
        pod = (doc.get("spec") or {}).get("template", {}).get("spec", {})
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            for part in (container.get("command") or []) + (container.get("args") or []):
                # BOTH SHAPES, so this vacuity guard keeps measuring across the split it guards. Keyed
                # on the field shape alone it went red the moment the fix landed -- which is the guard
                # doing its job, and the reason it now asks the question the fix does not change:
                # "does the seed write ANY dedicated credential at all".
                identities |= set(TOKEN_FIELD.findall(str(part))) | set(TOKEN_SECRET.findall(str(part)))
    return identities


def test_the_walk_sees_the_store_and_its_credentials() -> None:
    """Without this every assertion below passes by measuring nothing."""
    docs = _docs()
    apps = _apps_scoped_to_the_store(docs)
    assert len(apps) >= 5, f"only {len(apps)} app-ids scoped to {STORE} -- the Component or its scopes changed"
    assert _seeded_token_identities(docs), "the seed Job writes no service-token-* at all; this gate would pass vacuously"


def test_a_dedicated_credential_is_its_own_secret_rather_than_a_field_of_the_shared_bundle() -> None:
    """HALF ONE, and it must land first because scoping is inert without it.

    Dapr's ``allowedSecrets`` names SECRETS, not fields. While every credential is a field of
    ``secret/lance``, the only grant expressible is the whole bundle -- so this is not a stylistic
    preference, it is what makes half two able to say anything at all.

    The fix follows ``secret/viewer-s3`` in the same template: one ``bao kv put`` per identity.
    """
    docs = _docs()
    offenders: dict[str, list[str]] = {}
    for doc in docs:
        if doc.get("kind") != "Job":
            continue
        pod = (doc.get("spec") or {}).get("template", {}).get("spec", {})
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            joined = "\n".join(str(p) for p in (container.get("command") or []) + (container.get("args") or []))
            # SPLIT ON THE PUTS rather than regex-matching one, because the rendered command is a shell
            # script whose arguments continue across lines with a trailing backslash -- a single-line
            # pattern silently matched nothing and the gate passed while every token was a field of the
            # shared bundle. Each segment is one `kv put`: its first word is the PATH, the rest fields.
            for segment in joined.split("bao kv put ")[1:]:
                path, _, fields = segment.partition(" ")
                if path.strip() != "secret/lance":
                    continue
                found = TOKEN_FIELD.findall(fields.split("bao ")[0])
                if found:
                    offenders.setdefault(doc["metadata"]["name"], []).extend(found)
    offenders = {k: sorted(set(v)) for k, v in offenders.items()}

    assert not offenders, (
        f"these identities' credentials are FIELDS of the shared `secret/lance` bundle, so every app-id "
        f"scoped to {STORE} reads all of them and Dapr cannot scope them apart: {offenders}. "
        "Seed each as its own `secret/service-token-<identity>` (the `secret/viewer-s3` precedent in the "
        "same template) and point `dedicated_token_from_store` at it."
    )


def _readable_identities(docs: list[dict], seeded: set[str]) -> dict[str, set[str]]:
    """Per Configuration: which identity credentials it may still read, after its deny-list."""
    out: dict[str, set[str]] = {}
    for doc in docs:
        if doc.get("kind") != "Configuration":
            continue
        for entry in ((doc.get("spec") or {}).get("secrets") or {}).get("scopes") or []:
            if entry.get("storeName") != STORE:
                continue
            denied = {d.removeprefix("service-token-") for d in (entry.get("deniedSecrets") or [])}
            allowed = entry.get("allowedSecrets")
            readable = seeded - denied
            if entry.get("defaultAccess", "allow").lower() == "deny":
                readable &= {a.removeprefix("service-token-") for a in (allowed or [])}
            out[doc["metadata"]["name"]] = readable
    return out


def test_every_app_scoped_to_the_secret_store_carries_a_scope_for_it() -> None:
    """A missing scope entry is the same failure as a permissive one: Dapr's default is ALLOW.

    So an app-id with no entry reads every secret in the store, which is the state measured live on
    2026-09-23 and the reason this file exists. Absence counts as an offender rather than a skip.
    """
    docs = _docs()
    scoped = set(_readable_identities(docs, _seeded_token_identities(docs)))
    unscoped = sorted(app for app in _apps_scoped_to_the_store(docs) if CONFIG_FOR(app) not in scoped)

    assert not unscoped, (
        f"app-ids admitted to {STORE} with no secret scope at all: {unscoped}. Dapr defaults to ALLOW, "
        "so each of these reads every secret in the store including its peers' dedicated credentials. "
        "Give each one a Configuration carrying a `secrets.scopes` entry for this store."
    )


def test_only_the_verifier_doors_may_read_more_than_their_own_credential() -> None:
    """The invariant the split and the scope exist to produce: a PRODUCER reads one credential, its own.

    A DENY-LIST, NOT DENY-BY-DEFAULT, and the gate says so rather than quietly accepting the weaker of
    the two. Deny-by-default would be stronger, and it would also break the two readers that fetch a
    secret by a name known only at runtime -- `viewer/api/v1/endpoints/objects.py:102` and
    `ingest/objectstore.py:183` both resolve a per-STORE credential named by the store registry, so a
    store added after deploy names a secret no rendered allowlist can hold. This pins the hole that was
    MEASURED; tightening the default is a separate change that must enumerate those readers first.

    Deliberately a COUNT rather than a restatement of which identity belongs to which app. That mapping
    lives in `lance.identitiesForApp`; a test that repeated it would pass by agreeing with itself, going
    green on a helper that hands every app the same wrong identity so long as the test shared the error.

    THE TWO DOORS ARE EXEMPT BECAUSE OF HOW THE CREDENTIAL IS STORED, not because verification requires
    it. `service_principal` resolves the CLAIMED identity's token and compares it, so with the token in
    PLAINTEXT a door that cannot read it cannot admit that producer. Storing a hash for the doors --
    the house pattern for a service-to-service key, and the compare is already constant-time at
    `dapr_auth.py:507` -- lets a door verify without being able to forge, and this exemption goes away.
    """
    docs = _docs()
    seeded = _seeded_token_identities(docs)
    readable = _readable_identities(docs, seeded)
    doors = {CONFIG_FOR(app) for app in ("catalog", "lineage")}

    greedy = {name: sorted(ids) for name, ids in readable.items() if name not in doors and len(ids) > 1}
    blinded = {name: sorted(seeded - ids) for name, ids in readable.items() if name in doors and ids != seeded}

    assert not greedy, (
        f"these non-verifier app-ids may read more than one identity's credential: {greedy}. A producer "
        "presents exactly one identity, so it needs exactly one -- anything more reopens the hole this "
        "closes, where any producer read any other's token and a signature proved nothing."
    )
    assert not blinded, (
        f"these verifier doors cannot read credentials they must authenticate: {blinded}. `service_principal` "
        "resolves the CLAIMED identity's token to compare it, so denying one here refuses that producer "
        "at the door with a message about a missing credential rather than about a missing grant."
    )
