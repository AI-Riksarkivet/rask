"""The chart wires event signing the same way in every deploy mode and verifies it where its switches say.

[[LH-064]], [[XC-078]] (values.yaml `signing:`). Three things the chart decides, each of which fails silently if it is wrong:

- The platform's store (`openbao.enabled=false`, `openbao.externalAddr`), which nothing here seeds, mints no key, so every signer would stay not Ready
  and lineage could verify nothing. The render refuses it, naming every secret and the script that creates them, until the
  operator attests with `signing.provisioned`. Nor does it mint a NATS user ([[XC-078]], values.yaml `nats.auth`): each
  `nats-user-<user>` waits for `nats.auth.provisioned`.
- Lineage has no switch: wherever auth is on it carries the signer set and the delegator set the chart derives, under the
  names the doors read them by and with no mode, and with auth off it carries neither. A render with auth on and no signer at all is refused rather than rendered as a lineage that
  requires nothing.
- `signing.doors` is the bus doors' mode (off, observe or enforce). Wherever the store and auth are on, every pod whose app hosts a
  door carries that mode and the two sets lineage verifies with. The pods whose doors take
  control events also carry the role map their per-action policy resolves signers from. A door verifying against other sets would
  refuse what reaches it honestly, or act on what lineage refuses. A mode that verifies is refused at render with no signer at all,
  since such a pod refuses to boot, and with a deployed control-event service no identity holds, since its doors would refuse every
  event that service signs.
"""

import json
import subprocess
from typing import Final

import pytest

from lakehouse.governed.signing_key import signing_key_secret, signing_public_secret
from tests.chart import chart_render
from tests.chart.chart_render import DEFAULT_ARGS


#: The platform's store makes a real deployment, which carries no published dev credential and names the host its routes
#: answer: the bundled dev Dex is off and a hostname is named, so the refusal under test is this one.
_PLATFORM_STORE = (
    "--set",
    "route.hostnames[0]=lance.example.com",
    "--set",
    "openbao.enabled=false",
    "--set-string",
    f"openbao.externalAddr={chart_render.SECRET_STORE_URL}",
    "--set",
    "dex.enabled=false",
    "--set",
    "auth.oidc.idpId=corp",
    "--set",
    "auth.oidc.connectors.ldap=corp",
    "--set",
    "objectStore.enabled=false",
    "--set-string",
    "objectStore.externalEndpoint=https://s3.example.com",
    "--set",
    "postgres.enabled=false",
    "--set-string",
    "postgres.externalHost=pg.example.com",
    "--set-string",
    r"openfga.podAnnotations.rask\.dev/openfga-datastore=pg.example.com:5432/openfga",
)


def _signers(docs: tuple[dict, ...]) -> list[str]:
    """The identities the Deployments sign as, from the env the chart renders on them."""
    return sorted(
        {
            env["RASK_SIGNING_IDENTITY"]
            for _, _, container in chart_render.containers(docs)
            if "RASK_SIGNING_IDENTITY" in (env := chart_render.env_of(container))
        }
    )


#: The apps that host a signature door, and whether one of their doors takes control events: maintenance's worker, whose
#: write-event lane runs while MAINTENANCE_ARRIVAL_ENABLED is true, and the catalog, which follows the control events it
#: publishes while LANCE_CONTROL_EMIT_ENABLED is true ([[CTL-32]]).
_DOOR_APPS = {"maintenance.worker": False, "catalog.main:app": True}
#: The env switch a door's lane waits on, for the apps whose door is conditional.
_DOOR_SWITCH = {"maintenance.worker": "MAINTENANCE_ARRIVAL_ENABLED", "catalog.main:app": "LANCE_CONTROL_EMIT_ENABLED"}
_DOOR_ENV = ("RASK_SIGNATURE_DOORS", "RASK_EVENT_SIGNERS", "RASK_EVENT_DELEGATORS", "RASK_CONTROL_SIGNER_ROLES")
#: The door hosts each case names, so a values change that stops rendering one fails the case rather than shrinking it.
_CONTROL_HOSTS = frozenset({"Deployment/rask-catalog"})
_ALL_HOSTS = _CONTROL_HOSTS | {"Deployment/rask-maintenance-worker"}
#: Lineage verifies every event it records, so it carries the two sets and neither a mode nor a role map.
_LINEAGE = "Deployment/rask-lineage"


def _app(container: dict) -> str:
    """What the container runs: the `module:attribute` its uvicorn serves, or the module `python -m` starts."""
    if target := chart_render.uvicorn_target(container):
        return target
    args = [str(part) for part in container.get("args") or []]
    return args[args.index("-m") + 1] if "-m" in args[:-1] else ""


def _door_hosts(docs: tuple[dict, ...]) -> dict[str, str]:
    """`kind/name` -> the app it runs, for every workload whose app hosts a door."""
    return {
        workload: _app(container)
        for workload, _, container in chart_render.containers(docs)
        if _app(container) in _DOOR_APPS
        and (_app(container) not in _DOOR_SWITCH or chart_render.env_of(container).get(_DOOR_SWITCH[_app(container)]) == "true")
    }


def _door_env(docs: tuple[dict, ...]) -> dict[str, dict[str, object]]:
    """`kind/name` -> the doors' settings it carries, the JSON ones parsed, for every workload that carries any."""
    carried: dict[str, dict[str, object]] = {}
    for workload, _, container in chart_render.containers(docs):
        env = chart_render.env_of(container)
        if door := {name: env[name] if name == "RASK_SIGNATURE_DOORS" else json.loads(env[name]) for name in _DOOR_ENV if name in env}:
            carried[workload] = door
    return carried


def _identities_running(docs: tuple[dict, ...], app: str) -> list[str]:
    """The identities the pods running `app` sign as."""
    return sorted(
        {
            env["RASK_SIGNING_IDENTITY"]
            for _, _, container in chart_render.containers(docs)
            if _app(container) == app and "RASK_SIGNING_IDENTITY" in (env := chart_render.env_of(container))
        }
    )


#: What a deployer attests of the platform's services, one refusal each: the signing keys, the bus users, and the object
#: store's change events.
_ATTESTED: Final = frozendict({"signing": "signing.provisioned=true", "nats": "nats.auth.provisioned=true", "events": "objectStore.events.provisioned=true"})


def _refusal_without(attestation: str) -> str:
    """The render's refusal when every attestation but ``attestation`` is given, so only its own guard can fire."""
    others = [arg for name, value in _ATTESTED.items() if name != attestation for arg in ("--set", value)]
    with pytest.raises(subprocess.CalledProcessError) as refused:
        chart_render.render(*DEFAULT_ARGS, *_PLATFORM_STORE, *others)
    return refused.value.stderr


def test_a_store_nothing_seeds_is_refused_until_the_operator_attests_its_signing_keys_bus_users_and_store_events() -> None:
    default = chart_render.render(*DEFAULT_ARGS)
    identities, users = _signers(default), chart_render.nats_users(default)
    assert identities and users, "the default render has no signer or no bus user, so the refusals below would name nothing"

    message = _refusal_without("signing")
    # The names each signer reads its pair under (`signing_key_secret`), so the chart and the signer agree on them.
    missing = [name for identity in identities for name in (signing_public_secret(identity), signing_key_secret(identity)) if name not in message]
    assert not missing, f"the refusal does not name {missing}, so the operator cannot tell which secrets to create"

    # The users a store nothing seeds must already hold; the bus's own config is the bus's, not rask's to attest.
    message = _refusal_without("nats")
    names = {f"nats-user-{user}" for user in users}
    assert not [name for name in names if name not in message], f"the refusal does not name {sorted(n for n in names if n not in message)}"
    assert "nats-route" not in message and "nats-server" not in message, "the refusal asks the operator to attest the bus's own config"

    # The platform's store is configured by its operator: the refusal names the subject and the events it must deliver.
    message = _refusal_without("events")
    assert ("store.events" in message, "s3:ObjectCreated:*" in message, "objectStore.events.provisioned" in message) == (True, True, True), message

    docs = chart_render.render(*DEFAULT_ARGS, *_PLATFORM_STORE, *(arg for value in _ATTESTED.values() for arg in ("--set", value)))
    assert _signers(docs) == identities, "an attested store must still render every signer's identity"
    assert not [name for _, name, _ in chart_render.containers(docs) if name == "mint"], "only the dev store has a mint step to run"
    assert not chart_render.nats_users(docs), "the dev seed issues users for a bus or a store it does not provision"


@pytest.mark.parametrize(
    ("overlay", "enforced", "doors", "door_hosts"),
    [
        pytest.param((), True, "enforce", _ALL_HOSTS, id="lineage-on-and-the-doors-enforcing-by-default-with-the-store-and-auth"),
        # maintenance off is the e2e-stack lane's overlay: a control-event service that is not deployed holds an empty role, which
        # blocks no mode because nothing it would sign can arrive.
        pytest.param(
            ("--set", "signing.doors=enforce", "--set", "maintenance.enabled=false"), True, "enforce", _CONTROL_HOSTS, id="the-mode-asked-with-maintenance-off"
        ),
        pytest.param(("--set", "auth.enabled=false"), False, None, _ALL_HOSTS, id="not-without-auth"),
    ],
)
def test_lineage_and_every_bus_door_verify_against_exactly_the_rendered_signers_wherever_the_store_and_auth_are_on(
    overlay: tuple[str, ...],
    enforced: bool,  # noqa: FBT001
    doors: str | None,
    door_hosts: frozenset[str],
) -> None:
    docs = chart_render.render(*DEFAULT_ARGS, *overlay)

    hosts = _door_hosts(docs)
    assert hosts.keys() == door_hosts, f"the pods whose app hosts a door are not the ones this case names: {sorted(hosts)}"
    # Each service that emits control events (lakehouse.events.control_events.ControlSigner), mapped to the identities its pods sign as.
    roles = {
        "catalog": _identities_running(docs, "catalog.main:app"),
        "maintenance": _identities_running(docs, "maintenance.worker"),
    }
    expected: dict[str, dict[str, object]] = {}
    if doors is not None:
        assert roles["catalog"], f"a role no rendered pod signs as makes the role map's comparison vacuous: {roles}"
        expected = {
            workload: {
                "RASK_SIGNATURE_DOORS": doors,
                "RASK_EVENT_SIGNERS": _signers(docs),
                "RASK_EVENT_DELEGATORS": roles["catalog"],
            }
            | ({"RASK_CONTROL_SIGNER_ROLES": roles} if _DOOR_APPS[app] else {})
            for workload, app in hosts.items()
        }
    if enforced:
        # Only the catalog, which authenticated the person, may sign for one.
        expected[_LINEAGE] = {"RASK_EVENT_SIGNERS": _signers(docs), "RASK_EVENT_DELEGATORS": roles["catalog"]}
    # One expected map for the whole render, so a door host or lineage missing a setting, a pod carrying one without verifying and a
    # set that differs from the one its door admits all fail on the same diff.
    assert _door_env(docs) == expected, "the verifier settings are not the sets lineage and the doors admit, in the asked mode, on exactly the pods that verify"
