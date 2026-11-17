"""Of a stock client's create and another door acting on its name, the outcome is the one the declaration's owner is owed:
at most one create succeeds, it is the one whose rows the table holds, and a declaration is its declarer's (ADR 0227 §9,
owner 2026-11-17: "Declaration is its declarer's").

A stock client creates a table by declaring it (`DeclareTable`, whose claim is fenced for the declaring subject), writing
its rows with its own Lance, and finalizing version 1 through `version/create` (ADR 0157), which fences the claim for the
finalize. Each case drives one interleaving `specs/catalog/TableLifecycle.tla` names, through the real catalog app served
by uvicorn, with stock pylance as the client and two principals, alice the declarer and bob another:

- ``declared-then-catalog-create`` (DeclarationFence): bob's catalog create of a name alice declared answers 409 while
  her declaration's fence stands, and her version 1 lands. Without the fence his create writes into her declaration.
- ``catalog-create-then-declared``: the other order, bob's create held right before its commit; alice's declare finds the
  name taken (409) and his create lands.
- ``another-principals-version-1`` (DeclarerLifts): bob's stock client writes version 1 into alice's declaration and is
  refused 409; alice's own create, declaring again, is answered her declaration and lands.
- ``declaration-lapsed`` (StockFencesFirstVersion): alice's declaration lapses while she writes (``LEASE`` driven to zero,
  as `test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door.py` drives it), bob's catalog create fences it
  and is held right before its commit with a live fence; alice's late version/create is refused 409, and bob's lands.
  Without the fence version/create writes, her version 1 lands and his commit lands over it.
- ``declared-then-dropped``: alice drops her declared, unwritten table within the lease and it goes at once; her late
  version/create is refused.

Each runs on the local root, and on a real S3 store where `make lifecycle-rustfs-check` names one (``race_catalog``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Iterator
from datetime import timedelta
from typing import Any

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi import Header

from lakehouse import location_claims
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from tests.integration.conftest import RaceCatalog
from tests.integration.lineage_app import SUBJECT_HEADER


NAMESPACE = "lance_catalog$ns1"
TABLE = f"{NAMESPACE}$t"
ALICE, BOB = "dex~alice", "dex~bob"
#: How long one request waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 60


class _Gate:
    def __init__(self) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()

    def hold(self) -> None:
        self.arrived.set()
        if not self.release.wait(WAIT):
            raise TimeoutError("the held step was never released")


def _held_once(gate: _Gate, real: Callable[..., Any], *, after: bool = False) -> Callable[..., Any]:
    """``real``, its first call held at ``gate`` before it runs, or ``after`` it has run and before it answers."""
    pending = [True]

    def _call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the held function's own arguments
        first = bool(pending)
        pending.clear()
        if first and not after:
            gate.hold()
        answer = real(*args, **kwargs)
        if first and after:
            gate.hold()
        return answer

    return _call


class _Run:
    """One client's act on a thread of its own: whether it succeeded, once it has ended."""

    def __init__(self, act: Callable[[], bool]) -> None:
        self.succeeded: bool | None = None

        def _go() -> None:
            try:
                self.succeeded = act()
            except Exception:  # noqa: BLE001 — a refused act is an answer, the claim judges it
                self.succeeded = False

        self._thread = threading.Thread(target=_go)
        self._thread.start()

    def reaches(self, gate: _Gate) -> None:
        """Wait until the act is held at ``gate`` or has ended without reaching it."""
        for _ in range(WAIT * 20):
            if gate.arrived.wait(0.05) or not self._thread.is_alive():
                return

    def ended(self) -> bool | None:
        self._thread.join(WAIT)
        return self.succeeded


def _arrow(ids: list[int]) -> bytes:
    rows = pa.table({"id": pa.array(ids, pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def principals() -> Iterator[None]:
    """The catalog authenticates the caller its request names in ``SUBJECT_HEADER``, as a verified token would prove it."""
    from catalog.api.security import authenticate
    from catalog.main import app

    def caller(subject: str = Header(alias=SUBJECT_HEADER)) -> PersonPrincipal:
        principal = Principal.parse(subject)
        return PersonPrincipal(principal=principal, claims=IDToken(iss="https://idp.invalid/dex", sub=principal.subject, aud="rask", exp=4102444800, iat=0))

    app.dependency_overrides[authenticate] = caller
    yield
    app.dependency_overrides.pop(authenticate, None)


@pytest.fixture
def catalog(principals: None, race_catalog: RaceCatalog) -> Generator[RaceCatalog]:
    assert httpx.post(f"{race_catalog.url}/v1/namespace/{NAMESPACE}/create", json={}, headers={SUBJECT_HEADER: ALICE}, timeout=WAIT).status_code == 200
    yield race_catalog


def _stock(catalog: RaceCatalog, subject: str, ids: list[int], *, mode: str = "create") -> bool:
    """A stock client of ``subject`` writing ``ids`` to the table through the namespace: a create declares it first, an
    append writes version 1 into the declaration it finds."""
    lance.write_dataset(
        pa.table({"id": pa.array(ids, pa.int64())}),
        namespace_client=lance_namespace.connect("rest", {"uri": catalog.url, f"headers.{SUBJECT_HEADER}": subject}),
        table_id=[str(segment) for segment in TABLE.split("$")],
        mode=mode,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        storage_options=catalog.stock_options or None,
    )
    return True


def _post(catalog: RaceCatalog, subject: str, door: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401 — httpx's own arguments
    return httpx.post(f"{catalog.url}/v1/table/{TABLE}/{door}", headers={SUBJECT_HEADER: subject, **kwargs.pop("headers", {})}, timeout=WAIT, **kwargs)


def _catalog_create(catalog: RaceCatalog, subject: str, ids: list[int]) -> bool:
    created = _post(catalog, subject, "create", content=_arrow(ids), headers={"content-type": "application/vnd.apache.arrow.stream"})
    return created.status_code == 200


def _rows(catalog: RaceCatalog) -> int | None:
    counted = _post(catalog, ALICE, "count_rows", json={})
    return int(counted.text) if counted.status_code == 200 else None


CASES = ["declared-then-catalog-create", "catalog-create-then-declared", "another-principals-version-1", "declaration-lapsed", "declared-then-dropped"]


@pytest.mark.parametrize("case", CASES)
def test_of_a_stock_create_and_another_door_on_its_name_only_what_the_declaration_is_owed_lands(
    catalog: RaceCatalog, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from catalog.services import dataplane, table_claims

    declares, commits = _Gate(), _Gate()
    # alice's stock create writes 3 rows, bob's catalog create 2, bob's stock client 4: the table's rows name who landed.
    if case == "declared-then-catalog-create":
        # alice's declare has committed and is held before it answers; bob's create meets her declaration's fence.
        monkeypatch.setattr(dataplane, "declare_table", _held_once(declares, dataplane.declare_table, after=True))
        alice = _Run(lambda: _stock(catalog, ALICE, [1, 2, 3]))
        alice.reaches(declares)
        bob = _Run(lambda: _catalog_create(catalog, BOB, [7, 8]))
        bob.ended()
        declares.release.set()
        alice.ended()
        expected, landed = {"alice": True, "bob": False}, 3
    elif case == "catalog-create-then-declared":
        monkeypatch.setattr(table_claims, "renew_fence", _held_once(commits, table_claims.renew_fence))
        bob = _Run(lambda: _catalog_create(catalog, BOB, [7, 8]))
        bob.reaches(commits)
        alice = _Run(lambda: _stock(catalog, ALICE, [1, 2, 3]))
        alice.ended()
        commits.release.set()
        bob.ended()
        expected, landed = {"alice": False, "bob": True}, 2
    elif case == "another-principals-version-1":
        assert _post(catalog, ALICE, "declare", json={}).status_code == 200
        bob = _Run(lambda: _stock(catalog, BOB, [4, 5, 6, 7], mode="append"))
        bob.ended()
        alice = _Run(lambda: _stock(catalog, ALICE, [1, 2, 3]))
        alice.ended()
        expected, landed = {"alice": True, "bob": False}, 3
    elif case == "declaration-lapsed":
        monkeypatch.setattr(dataplane, "declare_table", _held_once(declares, dataplane.declare_table, after=True))
        monkeypatch.setattr(table_claims, "renew_fence", _held_once(commits, table_claims.renew_fence))
        alice = _Run(lambda: _stock(catalog, ALICE, [1, 2, 3]))
        alice.reaches(declares)
        # alice's declaration lapses: bob's create fences it, finds it empty, writes, and is held before its commit, its
        # own fence then live again.
        monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
        bob = _Run(lambda: _catalog_create(catalog, BOB, [7, 8]))
        bob.reaches(commits)
        monkeypatch.setattr(location_claims, "LEASE", timedelta(minutes=15))
        declares.release.set()
        alice.ended()
        commits.release.set()
        bob.ended()
        expected, landed = {"alice": False, "bob": True}, 2
    else:
        monkeypatch.setattr(dataplane, "declare_table", _held_once(declares, dataplane.declare_table, after=True))
        alice = _Run(lambda: _stock(catalog, ALICE, [1, 2, 3]))
        alice.reaches(declares)
        dropped = _post(catalog, ALICE, "drop", json={})
        declares.release.set()
        alice.ended()
        assert dropped.status_code == 200, f"alice's drop of her declared, unwritten table answered {dropped.status_code}: {dropped.text}"
        expected, landed = {"alice": False}, None
        bob = None

    answered = {"alice": alice.succeeded, **({"bob": bob.succeeded} if bob is not None else {})}
    held = _rows(catalog)
    assert (answered, held) == (expected, landed), f"answered {answered} and the table holds {held} rows; owed {expected} and {landed}"
