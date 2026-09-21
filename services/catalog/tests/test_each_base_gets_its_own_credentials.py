"""Composing `base_store_params` so each base carries ITS credentials, not the estate's ([[LH-067]]).

`_write_blob` composes `{uri: dict(so)}` — the SAME options for every base — and `core/config.py`
turns that into an operator obligation in the allowlist's own comment: "every base here MUST share the
catalog's S3 endpoint + creds". That invariant is what this removes. A base is an arbitrary approved
S3 URI; requiring it to share the estate's credential is exactly the coupling multi-base exists to
avoid.

REFERENCES, NEVER MATERIAL, on the estate's standing rule. The map an operator configures is base URI
-> secret NAME; the material is fetched through `warehouse_credentials.resolve`, the same Dapr
secret-store door the catalog uses for its own S3 secret, and never appears in configuration.

EVERY BASE GETS AN ENTRY, and one without a reference gets the estate's own options — which is the
write path's existing shape, pinned by `tests/unit/test_multibase.py`. pylance would fall back to the
top-level options for an omitted base, so omitting would behave the same today; it would just make
this code's correctness rest on an upstream fallback for no benefit.

FAIL CLOSED, NOT FALL BACK, when a reference does not resolve. Silently using the estate credential
for a base whose own secret is missing writes the caller's bytes somewhere they did not intend and
reports success; that is the failure this whole axis exists to prevent, so the resolver's raise is
allowed through.
"""

from __future__ import annotations

import pytest

from catalog.services.base_credentials import compose_base_store_params


ESTATE = {"endpoint": "http://estate:9000", "aws_access_key_id": "estate", "aws_secret_access_key": "estate-secret"}
OTHER = "s3://other-store/data"
PLAIN = "s3://estate-bucket/data"


def _resolver(**_kw: str) -> str | None:
    return "the-other-stores-secret"


def test_a_base_with_no_reference_gets_the_ESTATE_options() -> None:
    """Additive: today's estate configures no references and must render exactly today's map."""
    params = compose_base_store_params(bases=[PLAIN], storage_options=ESTATE, refs={}, resolve=_resolver, store="s", field="f")

    assert params == {PLAIN: ESTATE}, f"an unreferenced base must keep the estate's options explicitly: {params}"


def test_a_base_WITH_a_reference_carries_its_own_credential() -> None:
    params = compose_base_store_params(bases=[OTHER, PLAIN], storage_options=ESTATE, refs={OTHER: "other-secret"}, resolve=_resolver, store="s", field="f")

    assert set(params) == {OTHER, PLAIN}, f"every registered base gets an entry: {sorted(params)}"
    assert params[OTHER]["aws_secret_access_key"] == "the-other-stores-secret"
    assert params[PLAIN]["aws_secret_access_key"] == ESTATE["aws_secret_access_key"], "the unreferenced base lost the estate credential"


def test_the_ESTATE_secret_never_leaks_into_a_referenced_base() -> None:
    """The failure that would make this look like it works: a merge that keeps the estate key."""
    params = compose_base_store_params(bases=[OTHER], storage_options=ESTATE, refs={OTHER: "other-secret"}, resolve=_resolver, store="s", field="f")

    assert params[OTHER]["aws_secret_access_key"] != ESTATE["aws_secret_access_key"], "the referenced base kept the estate credential"


def test_a_base_keeps_the_estate_ENDPOINT_unless_told_otherwise() -> None:
    """A credential is not an address. Swapping the key must not silently move the base's store."""
    params = compose_base_store_params(bases=[OTHER], storage_options=ESTATE, refs={OTHER: "other-secret"}, resolve=_resolver, store="s", field="f")

    assert params[OTHER]["endpoint"] == ESTATE["endpoint"]


def test_a_reference_that_does_not_resolve_RAISES_rather_than_using_the_estate_key() -> None:
    """Fail closed. Falling back would write the caller's bytes to a store with the wrong identity."""

    def _missing(**_kw: str) -> str | None:
        raise RuntimeError("secret unavailable — failing closed")

    with pytest.raises(RuntimeError, match="failing closed"):
        compose_base_store_params(bases=[OTHER], storage_options=ESTATE, refs={OTHER: "gone"}, resolve=_missing, store="s", field="f")


def test_a_reference_naming_a_base_that_is_not_REGISTERED_is_refused() -> None:
    """An operator typo must not pass silently as "no per-base credentials configured".

    A reference for a base the write never registers is configuration that does nothing, and the
    shape that hides it is the same one this row is about: the write succeeds against the estate
    credential and looks correct.
    """
    with pytest.raises(ValueError, match="not a registered data base"):
        compose_base_store_params(bases=[PLAIN], storage_options=ESTATE, refs={"s3://typo/data": "x"}, resolve=_resolver, store="s", field="f")


def test_EVERY_write_site_forwards_the_per_base_credentials() -> None:
    """A create path that skips the map writes its bases on the estate credential, silently.

    `create_table` reaches `_write_blob` at MORE THAN ONE site — the fresh create and the
    overwrite-an-existing branch — and wiring one of them is the failure this row is about wearing a
    different hat: the un-wired path succeeds against the wrong identity and looks correct. Measured
    2026-09-21: forwarding the first site alone left the second untouched and every test still green,
    because no case drives that branch with a configured reference.

    READ AS AN AST, not as a regex: an argument list wraps across lines and a text search either
    misses a call or matches the wrong one's neighbourhood. The self-check below is what stops this
    passing by finding no calls at all.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src/catalog/services/dataplane.py"
    tree = ast.parse(source.read_text())
    # EVERY HOP, not just the innermost. Measured 2026-09-21: an earlier version checked `_write_blob`
    # alone, which passed while the two `_write_blob_into` calls one layer up forwarded nothing — so the
    # refs reached `create_table`, stopped there, and every base silently took the estate credential.
    # Driving it live was the only thing that showed it: a create with a DELIBERATELY BOGUS secret
    # reference returned 200 where it had to fail closed.
    wired = ("_write_blob", "_write_blob_into")
    calls: list[tuple[str, ast.Call]] = [
        (node.func.id, node) for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in wired
    ]

    assert len(calls) >= 4, f"found {len(calls)} call(s) across {wired}; this gate is reading the wrong file or the shape changed"

    missing = [f"{name}:{call.lineno}" for name, call in calls if not any(kw.arg == "base_credential_refs" for kw in call.keywords)]
    assert not missing, f"these call sites write their bases on the estate credential: {missing}"
