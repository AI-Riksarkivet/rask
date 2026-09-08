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


def test_no_options_is_the_ambient_chain_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mode_b` and the auth-off profile must be untouched: no credential on offer means the ambient
    one, which is that deployment's design rather than its failure."""
    from ingest import staging

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("storage.s3_client", lambda endpoint=None, **kw: calls.append(dict(kw, endpoint=endpoint)) or object())
    staging._client_for_options(None)

    assert calls, "no client was built at all"
    assert not any(calls[0].get(k) for k in ("access_key", "secret_key", "session_token")), (
        f"an explicit credential was passed when none was vended: {calls[0]}"
    )


def test_every_ledger_door_can_be_handed_the_credential() -> None:
    """A DECLARATION WITHOUT ITS CLIENT HALF is this estate's most-repeated defect, and a partly-threaded
    credential is that shape: the writes that took it would be scoped, the ones that did not would keep
    the root key mounted for the whole pod, and the survey would show a control that is 80% real."""
    import inspect

    from ingest import staging

    ledger_doors = ("stage_fragments", "write_unit_manifest", "read_unit_slice", "discover_staged", "purge_staged")
    missing = [name for name in ledger_doors if "storage_options" not in inspect.signature(getattr(staging, name)).parameters]
    assert not missing, f"these ledger doors cannot be given the vended credential, so the pod still needs the root key: {missing}"


def test_every_ledger_CALL_SITE_hands_over_the_credential() -> None:
    """The half that decides whether any of this is real.

    A threaded parameter nobody passes is this estate's most-repeated defect — a control that surveys
    as present while the value never lands. Every ledger call outside `staging.py` is checked, because
    ONE site left on the ambient chain keeps the RustFS root pair mounted on the pod for that call
    alone, and the other four being scoped changes nothing about what the pod holds.

    Read from the SOURCE rather than driven, because these sites sit inside a durable workflow and a
    Ray-fed worker; standing those up to observe one keyword argument would test the harness.
    """
    import ast
    from pathlib import Path

    ledger_doors = {"stage_fragments", "write_unit_manifest", "read_unit_slice", "discover_staged", "purge_staged"}
    root = Path(__file__).resolve().parents[1] / "src" / "ingest"

    unscoped: list[str] = []
    seen = 0
    for module in sorted(root.glob("*.py")):
        if module.name == "staging.py":  # its internals are covered by the seam test above
            continue
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ledger_doors:
                continue
            seen += 1
            passes_options = len(node.args) > _REQUIRED_ARGS[node.func.id] or any(k.arg == "storage_options" for k in node.keywords)
            if not passes_options:
                unscoped.append(f"{module.name}:{node.lineno} {node.func.id}")

    assert seen >= len(ledger_doors), (
        f"only {seen} ledger call sites found across {root} — an empty result and a clean result are indistinguishable, so this pin asserts nothing"
    )
    assert not unscoped, f"these ledger calls still sign with the ambient credential, so the pod still needs the root key: {unscoped}"


#: How many positional arguments each door takes BEFORE `storage_options`, so a call carrying one more
#: is passing it positionally. Named rather than derived from the signature: the point is to notice when
#: a door's shape changes, not to follow it.
_REQUIRED_ARGS = {
    "stage_fragments": 4,
    "write_unit_manifest": 3,
    "read_unit_slice": 4,
    "discover_staged": 2,
    "purge_staged": 2,
}
