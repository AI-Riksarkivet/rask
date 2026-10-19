"""Offline annotator tests never open a Dapr sidecar channel.

[[XC-071]]. Three source paths reach `typed_proxy` — `actor.py:350` and `lakehouse.py:415,420` — each
behind a function-local import that re-reads `annotator.projects.proxies` on every call. Two of them
sit under an `except Exception` that is deliberately non-fatal ("an annotator's submit must not fail
because the project actor is briefly unreachable"), so a test that reaches one does not FAIL on it: it
WAITS, the exception is swallowed, and the only symptom is time.

MEASURED 2026-09-23. With `DAPR_HTTP_PORT` pointed at a dead port — which is what CI has —
`test_task_history_is_bounded.py` runs past 300s and is killed. On a developer box it completes, because something answers
on 127.0.0.1:3500; on this host that was a `fake_sidecar.py` left running for 57 days. A suite whose
result depends on a stray local process is not measuring what it claims to, and `ms-test` had been red
on exactly that difference.

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
