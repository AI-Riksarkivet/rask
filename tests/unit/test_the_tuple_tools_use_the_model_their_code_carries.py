"""The estate's tuple tools check and write against the model their own code carries ([[LH-201]]).

A tuple request that names no `authorization_model_id`, or names the store's newest model, is validated against
whichever image wrote last. LH-201 lets a legacy image's narrower body stay the newest for as long as that image
boots, and against it every `estate:rask` request fails on `type 'estate' not found`. So the e2e grant helpers find
their store and model by `write_model.carried_model`, the rule the model hook uses: the pinned store, else the newest
named `lance-catalog`, and in it the model whose body is the code's own `model.json`.

RUN, not read, against `openfga_stub`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lakehouse.governed.auth.write_model import carried_model
from tests.unit.openfga_stub import BY_NAME, CARRYING, PINNED, STORE, Recorded, openfga


_STORES = pytest.mark.parametrize(("stores", "pin"), [(BY_NAME, ""), (PINNED, STORE)], ids=["by-name", "pinned"])


@pytest.fixture
def token(tmp_path: Path) -> Path:
    """The catalog pod's projected `rask-openfga` token, the bearer the stub admits ([[XC-077]])."""
    path = tmp_path / "token"
    path.write_text("projected-sa-catalog-token\n")
    return path


@_STORES
def test_the_carried_model_is_the_checkouts_body_in_the_estates_store(stores: list[dict[str, str]], pin: str, token: Path) -> None:
    # The stub's history puts a legacy body newest, on page one, and this checkout's model on page two: a reader
    # that stops at the first page, or takes the newest, answers LEGACY. Pinned, a NEWER `lance-catalog` sits
    # beside the pinned store, so ignoring the pin answers the wrong store.
    with openfga(stores, Recorded(), token_file=token) as url:
        assert carried_model(url, token_file=str(token), pinned=pin) == (STORE, CARRYING)
