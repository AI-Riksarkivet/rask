"""Test isolation for the notifications service — scoped, and RESTORED.

A conftest that assigns `os.environ[...]` at module scope is doing it for the whole pytest SESSION, not
for its own suite: conftest files are imported during collection, so the value is in place before any
other suite runs. The flows suite carries that lesson written down; this does the same job through a
function-scoped `monkeypatch`, which unwinds after every test.

What is pinned, and why each is here at all:

* the three retention knobs — set to SMALL values so the boundary cases (the row cap biting, a pointer
  aging out) are reachable in a test rather than only after thirty days. They are inputs the suite
  depends on, not defence: the defaults are deliberately production-shaped.
* `LANCE_OIDC_ENABLED` / `LANCE_FGA_ENABLED` removed — a developer's `.env` enabling FGA without OIDC
  makes `GovernedAuthSettings` refuse to construct, and these suites are about the inbox, not auth.
* `DAPR_GRPC_PORT` removed — the estate-wide convention for "no sidecar here". Nothing in this suite
  talks to daprd, and a service that branches on it must not take the durable lane by accident.
* `APP_API_TOKEN` removed — MEASURED: with it set in the ambient environment, every subscription-route
  test 403s on `require_dapr_token`, because the delivery door then expects a header the test client
  does not send. A developer who exported it to drive a local sidecar would see four ingress failures
  with nothing in them naming the cause. The suites that are ABOUT the door set it themselves, inside
  their own function-scoped patch.

`get_notifications_settings` is cached (the InboxActor is built by the Dapr runtime and cannot be
handed a dependency), so the cache is cleared on BOTH sides of the yield: once so this test sees the
env above, once so the next suite does not inherit this one's settings object.
"""

from collections.abc import Iterator

import pytest

from notifications.config import get_notifications_settings


@pytest.fixture(autouse=True)
def _notifications_env() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RASK_NOTIFICATIONS_INBOX_TTL_SECONDS", "86400")
        mp.setenv("RASK_NOTIFICATIONS_COMPACTION_INTERVAL_SECONDS", "3600")
        mp.setenv("RASK_NOTIFICATIONS_INBOX_MAX_ROWS", "5")
        mp.delenv("LANCE_OIDC_ENABLED", raising=False)
        mp.delenv("LANCE_FGA_ENABLED", raising=False)
        mp.delenv("DAPR_GRPC_PORT", raising=False)
        mp.delenv("APP_API_TOKEN", raising=False)
        get_notifications_settings.cache_clear()
        yield
        get_notifications_settings.cache_clear()
