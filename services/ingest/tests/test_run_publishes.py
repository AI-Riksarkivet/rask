"""A run does not merely commit — it asks the catalog to PUBLISH what it committed (§ D2 D-R1).

The distinction these pin: a run that commits and stops has made rows READABLE, not READY. Nothing
downstream may act on them until the gate has passed them and the pointer has moved. So `finalize_run`
calls the catalog's publish operation and reports the outcome on the run record.

The other half is what a REFUSED gate means. It is not a failed run: the run fetched, wrote and
committed exactly what it was asked to, and it is the DATA the gate declined. Conflating the two
would either hide a rejection behind a green run or make a correct run look broken — and an operator
needs to tell "this ran and published nothing" from "this broke".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ingest.catalog import LocalCatalogSeam, ServiceCatalogSeam
from ingest.runtime import _publish


class _Spec:
    """The fields `_publish` reads. A real RunSpec drags the whole workflow module in for nothing.

    `namespace` derives from the SAME function the real spec's property does rather than being a
    hardcoded string. A stub that fakes a derived value drifts the moment the derivation changes —
    which is what happened here: the plane started addressing the catalog by namespace, the stub kept
    offering only `project`, and `_publish` swallowed the AttributeError into its
    catalog-cannot-publish branch. The run would have reported `published: False` with a plausible
    reason, in production, with nothing raising.

    That drift is now caught rather than described: `_publish` takes `runtime.PublishSpec`, so this
    class is structurally checked against the fields the function actually reads and dropping one
    fails the type gate instead of a production run.
    """

    project = "demo"
    dataset = "pages"
    run_id = "run-1"

    @property
    def namespace(self) -> str:
        from ingest.naming import bronze_namespace_for

        return bronze_namespace_for(self.project)


class _Catalog(ServiceCatalogSeam):
    """A publishing catalog — declared as the seam half it stands for, so it cannot drift from it.

    Only `publish` is exercised; the rest of the half is inherited. Subclassing the Protocol rather
    than duck-typing it means a signature this double gets wrong is a type error here instead of a
    TypeError in a deployed run, which is the whole reason the seam is typed.
    """

    def __init__(self, body: dict[str, Any] | None = None, raises: Exception | None = None) -> None:
        self._body = body or {}
        self._raises = raises
        self.calls: list[tuple[str, str, int]] = []

    def publish(self, namespace: str, dataset: str, version: int, *, key_column: str = "id", required_columns: Sequence[str] = ()) -> dict[str, object]:
        self.calls.append((namespace, dataset, version))
        if self._raises is not None:
            raise self._raises
        return self._body


def test_a_run_PUBLISHES_the_version_it_committed() -> None:
    """The call happens, and it names the version this run committed — not "latest".

    Between the commit and the publish another writer may have committed; publishing whatever is
    latest would advance the pointer past a version nobody gated, which is the one thing the gate
    exists to prevent.
    """
    catalog = _Catalog({"published": True, "from_version": 3, "to_version": 4})

    out = _publish(catalog, _Spec(), 4)

    # `demo-bronze`, not `demo`: publication addresses the catalog NAMESPACE, and a project is a level
    # above that. This asserted `demo` while the plane composed the same string everywhere — which is
    # how a table id nobody had granted anything on looked correct at every call site.
    assert catalog.calls == [("demo-bronze", "pages", 4)]
    assert out["published"] is True


def test_the_run_reports_the_RANGE_the_publication_covers() -> None:
    """D-R3: a consumer needs `{from, to}` to resolve an exact delta without a bookmark of its own,
    so the run record has to carry it out of the plane."""
    catalog = _Catalog({"published": True, "from_version": 3, "to_version": 4})

    out = _publish(catalog, _Spec(), 4)

    assert (out["from_version"], out["to_version"]) == (3, 4)


def test_a_REFUSED_gate_is_reported_not_raised() -> None:
    """The run completed; the DATA was declined. The reason travels so an operator can see WHICH
    assertion failed without re-running anything."""
    catalog = _Catalog({"published": False, "from_version": 3, "to_version": 4, "reason": "quality gate failed: not_null"})

    out = _publish(catalog, _Spec(), 4)

    assert out["published"] is False
    assert "not_null" in out["publish_reason"]


def test_an_UNREACHABLE_catalog_does_not_turn_a_landed_commit_into_a_failed_run() -> None:
    """The rows are committed and durable; a later publish can still gate them.

    Raising here would report a run that lost data when it lost none — and would strand fragments
    that are already in the table. Silence would be worse than either: a run that looks published
    and is not. So it is reported.
    """
    catalog = _Catalog(raises=RuntimeError("catalog unreachable for publish: connect refused"))

    out = _publish(catalog, _Spec(), 4)

    assert out["published"] is False
    assert "unreachable" in out["publish_error"]


def test_a_catalog_WITHOUT_publish_is_reported_rather_than_crashing() -> None:
    """The local/dev catalog seam has no publish operation.

    It must degrade to "not published" — which is TRUE — rather than raising an AttributeError that
    reads like a bug in the run. What it must never do is report success.
    """

    class _NoPublish(LocalCatalogSeam):
        pass

    out = _publish(_NoPublish(), _Spec(), 4)

    assert out["published"] is False
    assert "publish" in out["publish_error"]
