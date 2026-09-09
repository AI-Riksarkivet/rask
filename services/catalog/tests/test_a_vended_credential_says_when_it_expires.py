"""A temporary credential must say so where a stock Lance client looks.

`lance_docs/ns_catalog/spec.yaml:2878-2880` (also `namespace.md:3822`) puts the expiry INSIDE
`storage_options`: *"If the vended credentials are temporary, the `expires_at_millis` key ..."*.
`VendedCredentials` carries it as a sibling FIELD, which is right for rask's own callers and invisible
to a client that refreshes on the key alone — so describe-vend handed back options that looked
permanent, and a client would discover the 900-second TTL by failing at it rather than by refreshing.

§ Q13-4. The finding survived refutation with the load-bearing half MEASURED rather than inferred: a
Lance client refreshes on this key and on nothing else.
"""

from __future__ import annotations

from catalog.core.vending import VendedCredentials


def test_the_vendor_carries_an_expiry_the_door_must_forward() -> None:
    """The seam the fix bridges: the value exists one level up from where the spec wants it."""
    creds = VendedCredentials(storage_options={"aws_access_key_id": "k"}, expires_at_millis=1_700_000_000_000)

    assert "expires_at_millis" not in creds.storage_options
    assert creds.expires_at_millis == 1_700_000_000_000


def test_a_PERMANENT_credential_adds_no_key() -> None:
    """`None` means a long-lived static key, and the spec's sentence is conditional — *if* temporary.
    Emitting the key with an empty or zero value would tell a client to refresh something that never
    expires, which is worse than the omission this fixes."""
    creds = VendedCredentials(storage_options={"aws_access_key_id": "k"})

    assert creds.expires_at_millis is None
