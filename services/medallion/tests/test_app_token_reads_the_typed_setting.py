"""MED-009: the shared service credential is read through the TYPED settings surface, everywhere.

`MedallionSettings.app_api_token` (alias ``APP_API_TOKEN``) exists precisely so the credential has one
read path — validated, defaulted, injectable in tests without `os.environ` games. Most call sites use
it (`transform.py`, `workflow.py`), but two kept reaching into the raw environment: the mover-ops
forward header and the produce door's expected-token read. A raw read bypasses the settings seam, so a
test that overrides settings changes what three sites see and not the fourth — the split this pin
prevents from returning.
"""

from __future__ import annotations

import re
from pathlib import Path

from medallion.api import mover_ops
from medallion.core.config import MedallionSettings


_SRC = Path(__file__).resolve().parents[1] / "src" / "medallion"

#: A raw environment read of the credential: os.environ["..."], os.environ.get("..."), os.getenv("...").
_RAW_READ = re.compile(r"os\.(?:environ(?:\.get)?|getenv)\s*[\(\[]\s*['\"]APP_API_TOKEN['\"]")


def test_no_module_reads_APP_API_TOKEN_from_the_raw_environment() -> None:
    offenders = [str(path.relative_to(_SRC)) for path in sorted(_SRC.rglob("*.py")) if _RAW_READ.search(path.read_text())]
    assert offenders == [], f"these modules bypass MedallionSettings.app_api_token with a raw env read: {offenders}"


def test_the_mover_forward_header_comes_from_settings() -> None:
    """The sender-side header must track the settings object it is handed, not the process env."""
    settings = MedallionSettings.model_validate({"app_api_token": "tok-typed"})
    assert mover_ops._app_token_header(settings) == {"dapr-api-token": "tok-typed"}
    # The open dev default (no token configured) sends no header, matching the mover's no-op check.
    assert mover_ops._app_token_header(MedallionSettings.model_validate({"app_api_token": ""})) == {}
