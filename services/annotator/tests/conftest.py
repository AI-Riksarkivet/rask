"""Offline annotator tests never open a Dapr sidecar channel.

[[XC-071]]. Three source paths reach `typed_proxy` — `actor.py:350` and `lakehouse.py:415,420` — each
behind a function-local import that re-reads `annotator.projects.proxies` on every call. Two of them
sit under an `except Exception` that is deliberately non-fatal ("an annotator's submit must not fail
because the project actor is briefly unreachable"), so a test that reaches one does not FAIL on it: it
WAITS, the exception is swallowed, and the only symptom is time.

MEASURED 2026-09-23. With `DAPR_HTTP_PORT` pointed at a dead port — which is what CI has —
`test_task_history_is_bounded.py` runs past 300s and is killed, and
`test_publish_token_after_credential_removal.py::test_the_idp_mint_is_still_the_configured_path`
times out too. On a developer box both pass, because something answers on 127.0.0.1:3500; on this host
that was a `fake_sidecar.py` left running for 57 days. A suite whose result depends on a stray local
process is not measuring what it claims to, and `ms-test` had been red on exactly that difference.

REFUSING RATHER THAN STUBBING A RETURN VALUE, because the two are different claims. A stub that
returns a mock says "the sidecar answered"; this says "an offline test must not ask". Every source
call site already tolerates the proxy being unreachable, so nothing under test depends on it —
`typed_proxy`'s own module docstring notes that unit-test fakes implement the wire contract without
dapr. A test that genuinely needs the proxy asks for it explicitly by overriding this fixture, which
is visible in the diff rather than inherited by accident.

AUTOUSE AND SUITE-WIDE, not per file: the offenders were in two files, the row that found it named
one, and a third would inherit the hole. Patched on the MODULE the function-local import resolves to —
patching the caller is looked straight past.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def refuse_sidecar_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    import annotator.projects.proxies as proxies

    def _refuse(actor_type: str, actor_id: str, interface: type) -> object:
        raise AssertionError(
            f"an offline test opened a sidecar channel to {actor_type}/{actor_id} — stub the collaborator, "
            "or override `refuse_sidecar_channels` in a test that genuinely needs the proxy"
        )

    monkeypatch.setattr(proxies, "typed_proxy", _refuse)


@pytest.fixture(autouse=True)
def refuse_sidecar_secret_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OTHER sidecar door, and the one that actually hung the second file.

    `lakehouse.publish_token` calls `fetch_required_secrets` before it ever reaches the IdP URL, and
    that goes to the Dapr secret store over the same 127.0.0.1:3500. Measured 2026-09-23:
    `test_the_idp_mint_is_still_the_configured_path` timed out there, not on `https://idp.example` as
    its own text suggests — the test asserts only that "any failure proves the branch was entered", so
    a connect timeout satisfied it and hid which door was being knocked on.

    Refusing makes that failure immediate and NAMED, which is what the test wanted: it proves the mint
    branch was entered and that the error does not mention the deleted `catalog_token` field.

    The two tests that genuinely exercise the transport (`test_publish_transport_parses_once`,
    `test_publish_transport_pools_and_retries`) already stub this themselves; a test-level
    `monkeypatch.setattr` runs after an autouse fixture, so theirs wins and nothing here has to know
    their names.
    """
    from service_kit.governed import secrets as sk_secrets

    def _refuse(store: str, key: str, **_k: object) -> dict[str, str]:
        raise AssertionError(f"an offline test read secret {key!r} from store {store!r} — stub `fetch_required_secrets`")

    monkeypatch.setattr(sk_secrets, "fetch_required_secrets", _refuse)
