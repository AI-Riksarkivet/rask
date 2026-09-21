"""The base -> secret-reference map an operator configures, parsed in one place ([[LH-067]]).

The allowlist beside it carries an operator OBLIGATION in its own comment — "every base here MUST
share the catalog's S3 endpoint + creds" — because the read path could not carry per-base options.
That is now retired at both doors, so an operator needs a way to say WHICH secret a base uses. This is
that setting, and it holds a NAME rather than material, which is what lets it live in configuration at
all.

PARSED HERE RATHER THAN AT THE CALLER, for the reason the list beside it is: a second place that
splits this string is a second place that can disagree about the separator, about whitespace, or about
what an empty entry means.

A REFERENCE FOR AN UNLISTED BASE IS REFUSED AT COMPOSITION, not here — `compose_base_store_params`
knows which bases a given write actually registers, and this setting does not. Keeping the check where
the knowledge is avoids a guard that can only be approximately right.
"""

from __future__ import annotations

import pytest

from catalog.core.config import Settings


#: The fields `Settings` requires regardless of what a case is about.
_REQUIRED = {"LANCE_ROOT": "s3://root", "LANCE_S3_ACCESS_KEY_ID": "k", "LANCE_S3_SECRET_ACCESS_KEY": "s"}


def _settings(value: str) -> Settings:
    return Settings.model_validate({**_REQUIRED, "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": value})


def test_the_default_is_EMPTY_so_every_estate_is_unchanged() -> None:
    assert Settings.model_validate(dict(_REQUIRED)).multibase_base_credential_ref_map == {}


def test_a_single_pair_parses() -> None:
    assert _settings("s3://other/data=other-secret").multibase_base_credential_ref_map == {"s3://other/data": "other-secret"}


def test_several_pairs_parse_and_whitespace_is_tolerated() -> None:
    parsed = _settings(" s3://a/data = ref-a , s3://b/data=ref-b ").multibase_base_credential_ref_map

    assert parsed == {"s3://a/data": "ref-a", "s3://b/data": "ref-b"}


def test_an_entry_with_no_SEPARATOR_is_refused_rather_than_dropped() -> None:
    """A typo that silently vanishes leaves the base on the estate credential, looking correct.

    That is this row's failure shape — the write succeeds against the wrong identity — so a
    malformed entry has to be loud at boot rather than absent at composition.
    """
    with pytest.raises(ValueError, match="base=secret-ref"):
        _ = _settings("s3://other/data").multibase_base_credential_ref_map


def test_a_REPEATED_base_is_refused_rather_than_last_one_wins() -> None:
    """Two references for one base is a question the estate must not answer by ordering."""
    with pytest.raises(ValueError, match="more than once"):
        _ = _settings("s3://a/data=ref-a,s3://a/data=ref-b").multibase_base_credential_ref_map


def test_the_VALUE_is_a_reference_and_the_setting_never_holds_material() -> None:
    """Asserted on the setting's own name, because this is the line the secrets rule draws.

    A field called `..._credential_refs` holding a secret would be a lie a reader cannot detect; the
    estate's rule is that material reaches a workload only from the Dapr store, ESO, or STS.
    """
    names = set(Settings.model_fields)
    leaky = {n for n in names if "base" in n and ("secret" in n or "key" in n) and "ref" not in n}

    assert not leaky, f"a per-base setting looks like it holds material rather than a reference: {sorted(leaky)}"
