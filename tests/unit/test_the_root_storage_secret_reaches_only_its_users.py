"""The fleet secret carries the ROOT storage credential, so it goes only where it is used.

`open_lakehouse_diff_left.md` § H8.

MEASURED INSIDE THE RUNNING PODS 2026-09-08, because the Deployment's `env:` list does not answer this
and reading only that list produces the opposite conclusion — the credential arrives through
`envFrom`, which an `env:` survey cannot see:

    rask-ingest        ListBuckets -> 106 buckets (the whole estate)   AWS_ACCESS_KEY_ID=rustfsadmin
    rask-maintenance   ListBuckets -> AWS Error ACCESS_DENIED          (its scoped F2-1 identity)

That is the difference between the root credential and a scoped one, driven rather than inferred from
the key's name. The secret was mounted into `gateway`, `notifications`, `compute` and `flows` as well —
none of which constructs an S3 client at all (zero imports of `lance`, `pyarrow`, `boto3`, `storage` or
any `service_kit.lakehouse` storage module, no HF code, no HF/boto dependency). Blast radius no code
could use.

IT IS ALSO THE FORBIDDEN MECHANISM, which is why this gate is about the mount and not only the breadth.
Owner ruling 2026-09-08: *"Never secret through envs. Either from ESO, secret store dapr and STS for
zero trust"* — the rule `CLAUDE.md` already states as *"Secrets from the Dapr secret store only — never
env, never a fallback."* Withholding the secret here does not make the remaining consumer correct;
`ingest` still receives it, and § H8's remaining half is to replace that with an STS credential, which
`catalog.core.vending.build_session_policy` already implements scoped by bucket + prefix.

SO THIS GATE PINS THE DIRECTION, NOT THE DESTINATION: the set of services holding the root credential
may shrink and must never grow.
"""

from __future__ import annotations

from typing import Final

import yaml
from chart_yaml import FAST_LOADER

from tests.unit.test_invariants import _helm_template


#: EMPTY, and it is meant to stay that way. This was `{"ingest"}` while ingest was the last holder;
#: the mount moved off `lanceWriter` onto its own `ambientStorage` declaration, which nothing sets, so
#: the estate's widest storage credential now reaches no pod at all through env.
#:
#: A NAME ADDED HERE IS A DECISION, not a convenience: it hands that service AWS_ACCESS_KEY_ID=<root>
#: (measured: 106 buckets, the whole estate) through the mechanism the owner's rule forbids. The answer
#: for a service that genuinely needs storage is a scoped 900 s STS vend
#: (`vending.build_session_policy`, by bucket + prefix), or a registered store declaring a `secret` the
#: Dapr store holds — both already implemented.
_MAY_HOLD_THE_ROOT_CREDENTIAL: Final = frozenset()


def _fleet_secret_holders(*set_values: str) -> set[str]:
    """Every Deployment mounting the app secret, by its service suffix."""
    docs = [d for d in yaml.load_all(_helm_template("dapr.enabled=true", *set_values), Loader=FAST_LOADER) if d and d.get("kind") == "Deployment"]
    holders: set[str] = set()
    for doc in docs:
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for source in container.get("envFrom", []):
                ref = (source.get("secretRef") or {}).get("name", "")
                if ref.endswith("-app"):
                    holders.add(name.rsplit("-", 1)[-1])
            # BOTH MECHANISMS, because scanning one of them is how this gate missed a holder.
            # It was written about `envFrom` — the mount an `env:` survey cannot see — and the viewer
            # took the same credential the OTHER way, as an explicit `secretKeyRef` on
            # `AWS_SECRET_ACCESS_KEY`. Measured on the running pod 2026-09-09:
            # `AWS_ACCESS_KEY_ID=rustfsadmin` in a service this gate reported as clean. A gate that
            # knows one spelling of a mount certifies the other.
            for entry in container.get("env", []):
                ref = ((entry.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name", "")
                if ref.endswith("-app") and "SECRET" in entry.get("name", "").upper():
                    holders.add(name.rsplit("-", 1)[-1])
    return holders


def test_the_gate_can_see_the_mount_at_all() -> None:
    """The precondition, and it had to change with the posture.

    It used to assert that SOME Deployment mounts the secret — which was a fine precondition while one
    did, and becomes a false alarm the moment the estate reaches zero holders. The property that keeps
    this file a gate rather than decoration is that the extraction can SEE a mount when one exists, so
    it renders one deliberately: with `ambientStorage` on, ingest must appear. That also pins the flag
    as the switch — a rename would silently empty every assertion below.
    """
    holders = _fleet_secret_holders("services.ingest.ambientStorage=true")
    assert "ingest" in holders, f"the extraction cannot see an app-secret mount even when one is rendered: {sorted(holders)}"


def test_only_a_service_that_uses_storage_holds_the_root_credential() -> None:
    """The headline: a service with no S3 client must not be handed the estate's widest storage key.

    RENDERED WITH THE OPTIONAL PLANES ON, and that is the half this gate was missing. It rendered
    DEFAULT values, where `explorer.enabled` is off and the viewer's Deployment does not exist — so a
    service could hold the root credential and be certified clean by a gate that never rendered it.
    Measured 2026-09-09 on the running estate, which does enable it: `AWS_ACCESS_KEY_ID=rustfsadmin`
    in the viewer's own environment, on a pod that HAS a Dapr sidecar. A gate that only renders the
    default deployment is a gate about a deployment nobody runs.
    """
    unexpected = sorted(_fleet_secret_holders("explorer.enabled=true", "search.enabled=true") - _MAY_HOLD_THE_ROOT_CREDENTIAL)

    assert not unexpected, (
        f"these services receive the fleet secret carrying AWS_ACCESS_KEY_ID=<root>: {unexpected}. It is "
        "the widest storage credential in the estate (measured: 106 buckets) delivered by the mechanism "
        "the estate forbids (env). If one of them genuinely needs storage, give it an STS credential "
        "(`vending.build_session_policy`, scoped by bucket + prefix) rather than adding it here"
    )


def test_withholding_it_does_not_STARVE_the_ingest_plane() -> None:
    """The other direction, and the reason the holder set could reach zero at all.

    Removing a credential is only hardening if the work it paid for has somewhere else to go. Ingest's
    two writes do: fragments take a table-scoped 900 s vend and the staging ledger takes the SAME one,
    because the ledger lives under the dataset (`<dataset>.lance/_ingest_staging/`). Asserting both
    are wired keeps this from passing by simply starving the plane — which is what "withhold the
    secret" would otherwise mean.

    The SOURCE read is the half that genuinely loses the ambient chain, and losing it is the point: an
    unregistered source now fails closed rather than being read with the estate's widest credential.
    Its scoped answer is `objectstore._own_store_for`, which supplies a store's own credential from the
    Dapr secret store and stays inert until an operator registers a store declaring a `secret`.
    """
    from ingest import runtime

    assert callable(runtime.write_options_for), "the vended write path is gone — withholding the secret would starve ingestion"
    assert callable(runtime.ledger_options), "the staging ledger has no vended credential — it would fall back to the ambient chain"
