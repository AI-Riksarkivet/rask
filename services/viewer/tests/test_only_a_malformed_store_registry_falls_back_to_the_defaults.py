"""The viewer's store registry answers a malformed ``RASK_STORES`` value with its defaults, and nothing else.

A malformed env value is what the fallback is for. A fault in the registry's own code must propagate instead, or a
broken registry would be indistinguishable from an unset one and the browser would list the wrong stores.
"""

from __future__ import annotations

import pytest

from viewer.core import stores


_ONE_STORE = '[{"name": "a", "bucket": "a", "role": "bronze"}]'


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json at all", id="value-error"),
        pytest.param("5", id="type-error"),
    ],
)
def test_a_malformed_registry_falls_back_to_the_defaults(raw: str) -> None:
    assert stores.registered_stores(raw) == list(stores.DEFAULT_STORES)


def test_a_fault_in_the_registry_code_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(item: object) -> object:
        raise AttributeError("Store gained a field nobody constructs")

    monkeypatch.setattr(stores.Store, "model_validate", staticmethod(_boom))
    with pytest.raises(AttributeError):
        stores.registered_stores(_ONE_STORE)
