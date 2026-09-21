"""A base written with its own credentials must be readable with them ([[LH-067]]).

THE ASYMMETRY IS RECORDED IN THE CODE THAT CAUSES IT. `dataplane._write_blob` composes
`base_store_params` for every registered data base, and its own comment states the consequence:
"the READ path (open_dataset) passes only the top-level storage_options, so a base needing DIFFERENT
creds/endpoint would write OK but be unreadable". That is the worst shape a storage bug takes — the
write succeeds, the operator is told nothing, and the data is unreachable later by a component that
looks correct.

`base_store_params` IS THE RIGHT MECHANISM AND NOT INTERCHANGEABLE WITH THE ALTERNATIVE. Verified
against the installed pylance 11.0.0 docstring rather than taken from this row's prose: it is
"Runtime-only object store parameters keyed by base path URI ... These are not persisted to the
manifest", and it "take[s] precedence over ``base_<id>.<key>`` entries in ``storage_options``". The
non-persistence is what makes it the only form a CREDENTIAL may take — the `base_<id>.<key>` spelling
puts the same material in the options dict with no such guarantee.

BOTH PATHS, because a branch read opens the dataset first. `open_dataset` reaches `lance.dataset`
twice — once for main, once before `checkout_version` — and forwarding on only one leaves branch
reads of a foreign-credentialled base failing exactly as before.

FALLBACK IS THE PYLANCE CONTRACT, not something re-implemented here: "when a base has no explicit
entry here, the top-level ``storage_options`` is used as a fallback". So passing `None` is
byte-identical to today, which is what makes this additive.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import LanceNamespace


class _Recorder:
    """Captures what reached `lance.dataset`, and answers enough to finish the call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _location: str, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self

    def checkout_version(self, _ref: Any) -> Any:
        return self


class _Resp:
    table_uri = "s3://acme/t"
    location = "s3://acme/t"


class _Ns:
    """The one method `open_dataset` calls. CAST rather than ignored at each call site: the estate
    forbids `# type: ignore`, and a double standing for a protocol it does not nominally implement is
    exactly what a cast is for."""

    def describe_table(self, _req: Any) -> _Resp:
        return _Resp()


def _ns() -> LanceNamespace:
    return cast(LanceNamespace, _Ns())


BASE_PARAMS = {"s3://other-store/data": {"aws_access_key_id": "k2", "aws_secret_access_key": "s2"}}


def test_the_MAIN_read_forwards_per_base_params(monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.core import namespace

    rec = _Recorder()
    monkeypatch.setattr(namespace.lance, "dataset", rec)

    namespace.open_dataset(_ns(), {"endpoint": "http://estate"}, ["acme-silver", "t"], base_store_params=BASE_PARAMS)

    assert rec.calls, "lance.dataset was never reached — this pin is checking nothing"
    assert rec.calls[0].get("base_store_params") == BASE_PARAMS, (
        f"a base with its own credentials is unreadable on main: {rec.calls[0].get('base_store_params')}"
    )


def test_the_BRANCH_read_forwards_them_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch opens the dataset before checking out, so forwarding on one leg is not enough."""
    from catalog.core import namespace

    rec = _Recorder()
    monkeypatch.setattr(namespace.lance, "dataset", rec)

    namespace.open_dataset(_ns(), {"endpoint": "http://estate"}, ["acme-silver", "t"], branch="dev", base_store_params=BASE_PARAMS)

    assert rec.calls, "lance.dataset was never reached — this pin is checking nothing"
    assert rec.calls[0].get("base_store_params") == BASE_PARAMS, "a branch read of a foreign-credentialled base still fails"


def test_passing_NOTHING_is_byte_identical_to_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additive: pylance falls back to the top-level options for any base with no entry."""
    from catalog.core import namespace

    rec = _Recorder()
    monkeypatch.setattr(namespace.lance, "dataset", rec)

    namespace.open_dataset(_ns(), {"endpoint": "http://estate"}, ["acme-silver", "t"])

    assert rec.calls[0].get("base_store_params") is None
    assert rec.calls[0]["storage_options"] == {"endpoint": "http://estate"}
