"""The base -> secret-reference map an operator configures, parsed in one place ([[LH-067]]).

An operator says WHICH secret a data base uses, and the setting holds a NAME rather than material,
which is what lets it live in configuration at all.

PARSED HERE RATHER THAN AT THE CALLER, for the reason the list beside it is: a second place that
splits this string is a second place that can disagree about the separator, about whitespace, or about
what an empty entry means.

A REFERENCE FOR A BASE OFF THE ALLOWLIST IS REFUSED HERE: no create can register that base, so the
reference is configuration that does nothing, and the map is estate-wide, so this is the one place
that holds both lists.
"""

from __future__ import annotations

import pytest

from catalog.core.config import Settings


#: The fields `Settings` requires regardless of what a case is about.
_REQUIRED = {"LANCE_ROOT": "s3://root", "LANCE_S3_ACCESS_KEY_ID": "k", "LANCE_S3_SECRET_ACCESS_KEY": "s"}


def _settings(value: str) -> Settings:
    return Settings.model_validate({**_REQUIRED, "LANCE_MULTIBASE_DATA_BASES": "s3://a/data,s3://b/data", "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": value})


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


def test_a_reference_for_a_base_OFF_the_allowlist_is_refused() -> None:
    """An operator typo must not pass silently as "no per-base credential configured"."""
    with pytest.raises(ValueError, match="not on LANCE_MULTIBASE_DATA_BASES"):
        _ = _settings("s3://typo/data=x").multibase_base_credential_ref_map
