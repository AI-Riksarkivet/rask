"""The staging ledger must be writable with the SCOPED credential, not only the ambient one.

`rask-ingest` is the LAST pod in this estate holding the RustFS root pair — measured 2026-09-08 by
reading every running pod's own environment, after the `lanceWriter` gate removed it from the gateway,
notifications, compute and flows. The owner's standing rule is that a secret reaches a workload by
exactly three paths, of which STS is the one for storage, and that "a scoped static key is not a fix".

The DATA writes already obey it: `write_unit_fragments` takes `storage_options`, and
`runtime.write_options_for` supplies a 900 s credential scoped to the table's prefix, proven enforced
on RustFS. The staging LEDGER did not — every manifest write, read, list and delete went through
`_client()`, the ambient chain — so the root credential had to stay mounted for the ledger alone.

The ledger lives UNDER the dataset (`<dataset>.lance/_ingest_staging/...`, beside Lance's own
`_versions`), which is why a table-scoped vend covers it: the same prefix the fragments are written to.

`None` keeps the ambient chain, unchanged — that is `mode_b`, where no credential is on offer and the
ambient one is the deployment's design rather than its failure.
"""

from __future__ import annotations

from typing import Any

import pytest


#: A vended STS triple, in the spelling `vend_storage_options` actually returns (verified against the
#: live catalog 2026-09-08: aws_access_key_id / aws_secret_access_key / aws_session_token / region).
VENDED = {
    "aws_access_key_id": "EAM88YED5PBVM8PLIVEJ",
    "aws_secret_access_key": "scoped-secret",
    "aws_session_token": "scoped-session-token",
    "region": "us-east-1",
    "endpoint": "http://rustfs:9000",
}


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Any:
    from ingest import staging

    for name in ("_client", "_client_for"):
        fn = getattr(staging, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()
    yield


def test_a_vended_credential_is_what_signs_the_ledger_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline: given scoped options, staging must not fall back to the ambient chain."""
    from ingest import staging

    seen: dict[str, Any] = {}

    def _fake_s3_client(endpoint: str | None = None, **kwargs: Any) -> Any:
        seen.update(kwargs, endpoint=endpoint)
        return object()

    monkeypatch.setattr("storage.s3_client", _fake_s3_client)
    staging._client_for_options(VENDED)

    assert seen.get("access_key") == VENDED["aws_access_key_id"], f"the ledger client was not built from the vended key: {seen}"
    assert seen.get("secret_key") == VENDED["aws_secret_access_key"]
    assert seen.get("session_token") == VENDED["aws_session_token"], (
        "no session token reached the client — an STS triple without it is not a valid credential and every call 403s"
    )
