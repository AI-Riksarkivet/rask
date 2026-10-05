"""The `openfga-model` hook presents its Job's projected token, and a refused one fails the hook ([[XC-077]]).

RUN: `write_model.main()`, the hook's whole entrypoint, against `openfga_stub`, which admits only the bearer its
token file holds. Admitted, the hook reads the store and finds this checkout's model already held. Refused, it
exits 1 at once rather than retrying for two minutes and exiting 0, which would record the hook as succeeded
with the model unwritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.governed.auth import write_model
from tests.unit.openfga_stub import BY_NAME, Recorded, openfga


@pytest.mark.parametrize(("presented", "outcome"), [pytest.param("admitted", (0, 0), id="admitted"), pytest.param("wrong", (1, 1), id="refused")])
def test_the_model_hook_presents_its_token_and_fails_when_refused(
    presented: str, outcome: tuple[int, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admitted = tmp_path / "admitted"
    admitted.write_text("the-jobs-projected-token\n")
    token = admitted if presented == "admitted" else tmp_path / "wrong"
    token.write_text(admitted.read_text() if presented == "admitted" else "another-token\n")
    recorded = Recorded()
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    with openfga(BY_NAME, recorded, token_file=admitted) as url:
        monkeypatch.setenv("FGA_API_URL", url)
        monkeypatch.setenv("RASK_FGA_TOKEN_FILE", str(token))
        monkeypatch.delenv("RASK_FGA_STORE_ID", raising=False)
        code = write_model.main()

    assert (code, recorded.refused) == outcome, capsys.readouterr()
