"""MED-009: the shared service credential is read through the TYPED settings surface, everywhere.

`MedallionSettings.app_api_token` (alias ``APP_API_TOKEN``) exists precisely so the credential has one
read path — validated, defaulted, injectable in tests without `os.environ` games. Most call sites use
it (`transform.py`, `workflow.py`), but two kept reaching into the raw environment: the stage runner-ops
forward header and the produce door's expected-token read. A raw read bypasses the settings seam, so a
test that overrides settings changes what three sites see and not the fourth — the split this pin
prevents from returning.
"""

from __future__ import annotations

from medallion.api import stage_runner_ops
from medallion.core.config import MedallionSettings


def test_the_stage_runner_forward_header_comes_from_settings() -> None:
    """The sender-side header must track the settings object it is handed, not the process env."""
    settings = MedallionSettings.model_validate({"app_api_token": "tok-typed"})
    assert stage_runner_ops._app_token_header(settings) == {"dapr-api-token": "tok-typed"}
    # No token configured = no header, rather than an empty one: the receiving door refuses either way,
    # and a blank `dapr-api-token` would read as a caller that tried and failed rather than one that
    # never had a credential to send.
    assert stage_runner_ops._app_token_header(MedallionSettings.model_validate({"app_api_token": ""})) == {}
