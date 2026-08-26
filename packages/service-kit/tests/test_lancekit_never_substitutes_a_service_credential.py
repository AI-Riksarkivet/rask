"""`open_reader`/`open_writer` re-issued an anonymous request under the estate's service credential.

Both seams did `token=caller_token or settings.catalog_token`. `caller_token` is `RawBearerToken`,
which is `None` whenever the request carries no `Authorization` header — so an anonymous request did
not fail closed, it was forwarded to the catalog as MEDIA_CATALOG_TOKEN, the chart-provisioned
identity the values file tells operators to grant the writer rung on the annotations table.

THE FUNCTION THAT SUPPLIES THE INPUT NAMES THIS AS THE THING TO AVOID.
`service_kit.governed.deps.raw_bearer` exists for exactly this and says why: "A service reading Lance
through the REST catalog must forward the CALLER's bearer rather than a service credential: the
catalog's own `authorize` checks one relation on one `table:` object and injects no row predicate, so
a service token answers 200 for a caller with no grant at all. The two users diverge, not the rows."

The fallback's stated justification was "callers with no request context (the publish saga, which
outlives any request)". No such caller exists: all eight call sites of these functions are
request-scoped annotator routes (save.py:78/119, tags.py:110/114, commit.py:88, wire.py:93/75,
versions.py:75). The branch fired only for the case it must not serve.

Owner ruling 2026-08-26: never substitute, and delete the dead config. The service-credential PATTERN
is not wrong in general — the medallion's cascade movers use their own `MEDALLION_CATALOG_TOKEN` at
`transform.py:505/821/871` and genuinely have no caller. It is wrong on a request path.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable

import pytest

from service_kit.lancekit import reader as reader_mod
from service_kit.lancekit import writer as writer_mod


def _source(fn: Callable[..., object]) -> str:
    return inspect.getsource(fn)


@pytest.mark.parametrize(
    ("fn", "name"),
    [
        (reader_mod.open_reader, "open_reader"),
        (reader_mod.open_catalog_reader, "open_catalog_reader"),
        (writer_mod.open_writer, "open_writer"),
    ],
)
def test_no_seam_substitutes_the_service_credential_for_an_absent_caller(fn: Callable[..., object], name: str) -> None:
    """`caller_token or settings.catalog_token` is the confused deputy in one expression.

    Asserted on the SOURCE rather than by driving a request: these are plain factory functions with
    no app around them, and the property is about which token reaches the transport — which a
    behavioural test would have to reach through a live catalog to observe.
    """
    # The EXPRESSION, not the bare name. Asserting on `settings.catalog_token` matched the corrected
    # docstrings that now EXPLAIN the removal — the same false positive the POLL REASON gate
    # documents, where prose about a migration reads as the thing it migrated away from.
    src = re.sub(r'"""[\s\S]*?"""', " ", _source(fn))
    assert "or settings.catalog_token" not in src, (
        f"{name} still falls back to the estate's service credential when the request carries no "
        f"bearer, so an anonymous caller is re-issued as MEDIA_CATALOG_TOKEN and the catalog answers "
        f"200 for someone with no grant at all"
    )


def test_the_media_settings_no_longer_declare_a_credential_nothing_reads() -> None:
    """Dead config that LOOKS load-bearing is how this happened once already.

    With the fallback gone, `MEDIA_CATALOG_TOKEN` has no consumer anywhere in the tree. Leaving it
    declared and chart-provisioned leaves a credential wired to nothing, sitting next to the code
    that used to reach for it.
    """
    from service_kit.media.config import Settings

    assert "catalog_token" not in Settings.model_fields, (
        "media Settings still declares catalog_token; nothing reads it now, and a provisioned "
        "credential with no consumer is what the next accidental `or` will find"
    )


def test_the_MEDALLION_service_path_is_untouched() -> None:
    """The pattern is right where a caller genuinely does not exist — do not overcorrect.

    The cascade movers run with no human behind them and carry their own credential. This test
    exists so a later sweep for `catalog_token` does not delete the legitimate half too.
    """
    from medallion.core.config import MedallionSettings

    assert "catalog_token" in MedallionSettings.model_fields, (
        "MEDALLION_CATALOG_TOKEN was removed — the movers have no caller to forward and legitimately need a service credential"
    )
