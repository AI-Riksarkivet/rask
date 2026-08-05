"""Test isolation for the flows service — scoped, and RESTORED.

A conftest that assigns `os.environ[...]` at module scope is doing it for the whole pytest SESSION, not
for its own suite: conftest files are imported during collection, so the value is in place before any
other suite's tests run. The ingest suite carries that lesson written down
(`test_sources_endpoint.py:44-50` — "these passed alone and errored in the full suite"), and the compute
suite's `setdefault` is only correct until something later overwrites it. So this does the same job
through a function-scoped `monkeypatch`, which unwinds after every test.

What is pinned, and why each is here at all — the tests construct `FlowsSettings` explicitly and mount
the routers themselves, so these are defence against the dev `.env` and the developer's shell rather
than inputs the suite depends on:

* `RASK_FLOWS_SERVE_URL` — an address that is never dialled. Every Serve call is intercepted by respx
  at the TRANSPORT, so a real origin here would be a lie about what is mocked; `.invalid` is the
  reserved TLD (RFC 2606), so an escaped call fails as a DNS error instead of reaching something.
* `DAPR_GRPC_PORT` removed — with it set, the lifespan imports `flows.workflow`, builds a gRPC worker
  for a sidecar that is not there, and routes every run through the durable lane. `test_workflow.py`
  drives that lane directly instead; these suites are about the inline one.

`RASK_API_PREFIX` is deliberately NOT set: nothing here imports `flows.app`, its only consumer. The
gateway suite pins it where it IS load-bearing — against the flows app's real openapi.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _flows_env() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RASK_FLOWS_SERVE_URL", "http://serve.invalid:8000")
        mp.delenv("DAPR_GRPC_PORT", raising=False)
        yield
