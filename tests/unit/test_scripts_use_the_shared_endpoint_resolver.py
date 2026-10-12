"""The S3 endpoint's env precedence is resolved in ONE place: `storage.configured_endpoint()`.

The canonical-first list is `RASK_S3_ENDPOINT_URL`, `S3_ENDPOINT_URL`, `HCP_ENDPOINT`, and
`storage.s3_client()` resolves it that way when handed no endpoint. A second copy in a script does
not fail loudly when it drifts — it fails by omitting an alias, and then reports "S3 is not
configured" for a deployment the very client it is about to build would have connected to. That is
the least debuggable shape a configuration bug has: a green-looking refusal.

So `scripts/` may READ the endpoint, but may not decide what the endpoint is.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"


def test_smoke_scripts_report_the_endpoint_the_client_will_actually_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """A smoke script's printed endpoint is the one `s3_client()` builds against, aliases included."""
    from storage import configured_endpoint

    monkeypatch.delenv("RASK_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")
    resolved = configured_endpoint()
    for stem in ("smoke_s3", "smoke_rustfs"):
        source = (_SCRIPTS / f"{stem}.py").read_text()
        assert "configured_endpoint" in source, f"{stem}.py does not use the shared resolver"
    assert resolved == "http://localhost:9000"
