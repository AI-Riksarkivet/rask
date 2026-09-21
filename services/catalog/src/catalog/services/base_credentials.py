"""Give each registered data base ITS credentials, rather than the estate's ([[LH-067]]).

`_write_blob` composes `{uri: dict(so)}` — the same options for every base — and `core/config.py`
turns that into an operator obligation in the allowlist's own comment: "every base here MUST share the
catalog's S3 endpoint + creds". That invariant is what this removes. A data base is an arbitrary
approved S3 URI, and requiring it to share the estate's credential is exactly the coupling multi-base
exists to avoid.

REFERENCES, NEVER MATERIAL. What an operator configures is base URI -> secret NAME. The material is
fetched through the Dapr secret-store door the catalog already uses for its own S3 secret, so nothing
secret is ever in configuration, in a record, or in an environment variable.

EVERY BASE GETS AN ENTRY, and one WITHOUT a reference gets the estate's own options. pylance would
fall back to the top-level `storage_options` for an omitted base, so omitting would behave the same
today — but it makes this code's correctness depend on an upstream fallback for no benefit, and it is
the write path's existing shape (pinned by `tests/unit/test_multibase.py`). Being explicit costs
nothing and keeps "which options does this base use" answerable from the map alone.

A CREDENTIAL IS NOT AN ADDRESS. A referenced base keeps the estate's endpoint unless something else
moves it; swapping the key must not silently relocate where the base is read and written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


def compose_base_store_params(
    *,
    bases: Iterable[str],
    storage_options: Mapping[str, str],
    refs: Mapping[str, str],
    resolve: Callable[..., str | None],
    store: str,
    field: str,
) -> dict[str, dict[str, str]]:
    """`base_store_params` for `lance.write_dataset` / `lance.dataset`, per base.

    Raises when a reference names a base the write does not register: configuration that does nothing
    is the shape this row is about, because the write then succeeds against the estate credential and
    looks correct. Raises when a reference does not RESOLVE, for the stronger version of the same
    reason — falling back to the estate key would put the caller's bytes in a store under an identity
    they did not choose, and report success.
    """
    registered = list(bases)
    unknown = sorted(set(refs) - set(registered))
    if unknown:
        raise ValueError(f"{unknown} is not a registered data base; a per-base credential for a base nothing writes to is configuration that does nothing")

    params: dict[str, dict[str, str]] = {}
    for uri in registered:
        ref = refs.get(uri, "")
        if not ref:
            params[uri] = dict(storage_options)
            continue
        secret = resolve(store=store, ref=ref, field=field)
        if secret is None:
            raise ValueError(f"per-base credential reference {ref!r} for {uri!r} resolved to nothing")
        # The credential REPLACES the estate's; the endpoint and the rest do not move, because a
        # credential is not an address and swapping the key must not silently relocate the base.
        params[uri] = {**dict(storage_options), "aws_secret_access_key": secret}
    return params
