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

from tests.unit.test_invariants import _helm_template


#: The ONLY fleet service that still receives the root storage credential, and the reason it is a list
#: of one rather than a boolean: it is expected to become empty, not to gain members. `ingest` writes
#: table bytes through both a catalog-VENDED credential and the ambient `AWS_*` chain
#: (`objectstore.py`: "a registered store that declares no secret shares the deployment's
#: credentials"), so removing it before measuring which writes depend on the ambient pair would break
#: ingestion — which is the whole of why the two halves of § H8 are separate.
_MAY_HOLD_THE_ROOT_CREDENTIAL: Final = frozenset({"ingest"})


def _fleet_secret_holders() -> set[str]:
    """Every Deployment mounting the app secret, by its service suffix."""
    docs = [d for d in yaml.safe_load_all(_helm_template("dapr.enabled=true")) if d and d.get("kind") == "Deployment"]
    holders: set[str] = set()
    for doc in docs:
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for source in container.get("envFrom", []):
                ref = (source.get("secretRef") or {}).get("name", "")
                if ref.endswith("-app"):
                    holders.add(name.rsplit("-", 1)[-1])
    return holders


def test_the_gate_can_see_the_mount_at_all() -> None:
    """The precondition: if no Deployment mounts it, the demand below is vacuous and this file is
    decoration rather than a gate."""
    assert _fleet_secret_holders(), "no Deployment mounts the app secret — the extraction has drifted from the chart"


def test_only_a_service_that_uses_storage_holds_the_root_credential() -> None:
    """The headline: a service with no S3 client must not be handed the estate's widest storage key."""
    unexpected = sorted(_fleet_secret_holders() - _MAY_HOLD_THE_ROOT_CREDENTIAL)

    assert not unexpected, (
        f"these services receive the fleet secret carrying AWS_ACCESS_KEY_ID=<root>: {unexpected}. It is "
        "the widest storage credential in the estate (measured: 106 buckets) delivered by the mechanism "
        "the estate forbids (env). If one of them genuinely needs storage, give it an STS credential "
        "(`vending.build_session_policy`, scoped by bucket + prefix) rather than adding it here"
    )


def test_the_service_that_does_use_storage_still_gets_one() -> None:
    """The other direction, so the fix cannot pass by starving the estate: withholding a credential
    from a service that writes Lance would break ingestion rather than harden it."""
    assert "ingest" in _fleet_secret_holders(), (
        "ingest no longer receives a storage credential — it writes table bytes through the ambient "
        "AWS_* chain as well as vended ones, so this starves the ingest plane instead of scoping it"
    )
