"""`compose_base_store_params` fails CLOSED when a base's credential reference does not resolve ([[LH-067]]).

Silently using the estate credential for a base whose own secret is missing reads or writes the
caller's bytes under an identity nobody chose and reports success; that is the failure this whole axis
exists to prevent, so the resolver's raise is allowed through. That each referenced base is opened and
written under its own key is pinned end to end by
`test_a_multibase_read_uses_each_bases_own_credential.py`.
"""

from __future__ import annotations

import pytest

from catalog.services.base_credentials import compose_base_store_params


ESTATE = {"endpoint": "http://estate:9000", "aws_access_key_id": "estate", "aws_secret_access_key": "estate-secret"}
OTHER = "s3://other-store/data"


def test_a_reference_that_does_not_resolve_RAISES_rather_than_using_the_estate_key() -> None:
    def _missing(**_kw: str) -> dict[str, str] | None:
        raise RuntimeError("secret unavailable — failing closed")

    with pytest.raises(RuntimeError, match="failing closed"):
        compose_base_store_params(bases=[OTHER], storage_options=ESTATE, refs={OTHER: "gone"}, resolve=_missing, store="s", field="f")
