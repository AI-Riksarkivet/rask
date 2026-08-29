"""SK-20 — two shared swallow-and-continue paths that caught our own bugs along with the world's.

`registered_stores` wrapped `json.loads` + `Store.model_validate` in `except Exception` and answered
with the estate defaults. A malformed env var is exactly what that is for — but the same clause also
caught a fault in OUR code (a `Store` field renamed under this call site, an attribute error in a
validator) and served the defaults for it, so a broken registry was indistinguishable from an unset
one and the whole estate quietly listed the wrong stores.

`discover_tables` kept a THIRD verbatim copy of the not-found marker vocabulary that the registry and
the reader had already been de-duplicated onto, so a marker added for one object store's wording
would have been honoured on two of the three paths that classify the same condition.
"""

from __future__ import annotations

import pytest

from service_kit.lancekit import errors, introspect
from service_kit.schemas import storage


_ONE_STORE = '[{"name": "a", "bucket": "a", "role": "bronze"}]'


def test_a_malformed_env_value_still_falls_back_to_the_defaults() -> None:
    assert storage.registered_stores("not json at all") == list(storage.DEFAULT_STORES)
    assert storage.registered_stores('{"not": "an array"}') == list(storage.DEFAULT_STORES)
    assert storage.registered_stores('[{"name": "a"}]') == list(storage.DEFAULT_STORES)


def test_a_well_formed_value_is_still_honoured() -> None:
    stores = storage.registered_stores(_ONE_STORE)
    assert [s.name for s in stores] == ["a"]


def test_a_fault_in_our_own_code_is_no_longer_answered_with_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(item: object) -> object:
        raise AttributeError("Store gained a field nobody constructs")

    monkeypatch.setattr(storage.Store, "model_validate", staticmethod(_boom))
    with pytest.raises(AttributeError):
        storage.registered_stores(_ONE_STORE)


def test_discovery_classifies_absence_through_the_shared_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(errors, "_NOT_FOUND_MARKERS", ("not found", "does not exist", "no such table"))
    monkeypatch.setattr(introspect.store, "list_lance_stems", lambda *_a, **_k: ["t"])

    def _raise(_uri: object, _opts: object = None) -> object:
        raise RuntimeError("no such table: t")

    monkeypatch.setattr(introspect, "table_info", _raise)
    assert introspect.discover_tables("s3://b/db") == {}


def test_a_transient_read_is_still_not_laundered_into_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(introspect.store, "list_lance_stems", lambda *_a, **_k: ["t"])

    def _raise(_uri: object, _opts: object = None) -> object:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(introspect, "table_info", _raise)
    with pytest.raises(OSError, match="connection reset"):
        introspect.discover_tables("s3://b/db")
