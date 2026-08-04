"""The real catalog, over HTTP — what replaces `LocalCatalog` in a deployment.

`LocalCatalog` writes datasets straight to the object store and records versions in a Python list.
That is fine for a test and wrong for a cluster: a commit nobody else can see is a commit the
cascade cannot ride, because the event that wakes a mover is the CATALOG's publication of a new
version. Registering locally means the run lands its data and nothing downstream ever learns of it.

**The two doors, and why they are two** (D6, §0 C10):

* `POST /v1/table/{id}/create` — CREATION IS SERVER-SIDE. The catalog's own rule: "CREATE and
  OVERWRITE stay server-side to centralize it and to owner-govern the destructive reset". It is also
  where the creation-time-only flags are applied, which is what A14 gates. We send an EMPTY Arrow
  stream, so "no byte transits the catalog" stays true rather than becoming false on every dataset's
  first run.
* `POST /v1/table/{id}/commit` — APPENDS ARE CLIENT-DIRECT. Workers write fragments straight to
  object storage; this door takes only the serialized `FragmentMetadata` plus the `read_version` they
  were built against, folds them into a metadata-only Lance commit under root credentials, and emits
  the INSERT lineage. No data byte moves through the catalog on the bulk path.

**The location is VENDED, never composed.** `describe` returns the table's object-store location, and
that is what workers write into. Composing `{warehouse}/{project}/{dataset}.lance` from env — which
is what the local path does — is exactly the "hardcoded dataset path" I2 exists to forbid: two
callers with different env compose different locations for the same logical table, and volume B
overwrites volume A.

**Fragments cross as DICTS here and as JSON STRINGS inside the plane.** `CommitFragmentsRequest`
declares `list[dict[str, Any]]`, while the workflow carries `json.dumps(f.to_json())` because a
fragment has to survive a Dapr activity boundary. The conversion is one `json.loads` and it belongs
here, at the wire, rather than changing what the plane transports.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence

    import pyarrow as pa

logger = logging.getLogger(__name__)

#: The catalog composes a table identifier from its parts with this separator (`bronze$pages`).
#: Matches the estate's `catalog_delimiter`; a mismatch addresses a different table rather than
#: failing, which is why it is read from env rather than assumed.
DELIMITER = os.getenv("RASK_CATALOG_DELIMITER", "$")

#: Generous, because a create can provision storage. Still bounded: an unbounded call here would
#: hang a workflow activity rather than letting Dapr retry it.
TIMEOUT_SECONDS = 30.0


def catalog_base_url() -> str:
    return os.getenv("RASK_CATALOG_URL", "http://rask-catalog:2333").rstrip("/")


def catalog_enabled() -> bool:
    """Whether to route through the catalog service at all.

    Explicit rather than inferred from a reachable URL: a catalog that is merely DOWN must fail the
    run loudly, not silently fall back to writing locally. A silent fallback is how an estate ends up
    with governed data that no catalog knows about.
    """
    return os.getenv("RASK_INGEST_USE_CATALOG", "").lower() in ("1", "true", "yes")


class CatalogError(RuntimeError):
    """The catalog refused, or could not be reached."""


class CatalogServiceClient:
    """Talks to the catalog service. Satisfies the same seam `LocalCatalog` does."""

    def __init__(self, schema: pa.Schema, base_url: str | None = None, token: str | None = None) -> None:
        self._schema = schema
        self._base = (base_url or catalog_base_url()).rstrip("/")
        self._token = token or os.getenv("RASK_CATALOG_TOKEN")
        self.registered: list[tuple[str, int, str]] = []

    # ── identity ──────────────────────────────────────────────────────────────────────

    def table_id(self, project: str, dataset: str) -> str:
        return f"{project}{DELIMITER}{dataset}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ── the two doors ─────────────────────────────────────────────────────────────────

    def ensure(self, project: str, dataset: str) -> str:
        """Create the namespace and the table if absent; return the location the catalog vends.

        THREE steps, not two. The design said "create the table, then commit fragments", and against
        a real catalog that fails at the first call with

            NamespaceNotFoundError — Child namespace reads require an existing __manifest dataset

        because a table lives IN a namespace and the namespace is itself a catalog object with its
        own manifest. It is not implicit in the table id: `demo$pages` names a namespace `demo` that
        has to exist first. Nothing about the table endpoints says so, and the failure only appears
        against a catalog whose namespace has never been created — which every dev catalog is, once.
        """
        located = self._describe(project, dataset)
        if located is not None:
            return located

        self._ensure_namespace(project)
        self._create_empty(project, dataset)
        located = self._describe(project, dataset)
        if located is None:
            raise CatalogError(f"catalog created {self.table_id(project, dataset)} but describes no location for it")
        return located

    def commit(self, project: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]:
        """Fold client-written fragments into ONE new version. Returns (version, row_count)."""
        import httpx

        payload = {
            # json.loads because the plane transports fragments as STRINGS (they cross a Dapr
            # activity boundary) while the catalog's schema declares dicts.
            "fragments": [json.loads(f) for f in fragments_json],
            "read_version": read_version,
        }
        url = f"{self._base}/v1/table/{self.table_id(project, dataset)}/commit"
        try:
            response = httpx.post(url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for commit: {exc}") from exc

        if response.status_code == 409:
            # Optimistic concurrency: another writer committed against the same read_version. The
            # activity's own retry re-reads and re-commits, which is the documented recovery — so
            # this must be raised, never swallowed into a success.
            raise CatalogError(f"commit conflict on {self.table_id(project, dataset)} at read_version {read_version} — re-read and retry")
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused the commit ({response.status_code}): {response.text[:300]}")

        body = response.json()
        version = int(body["version"])
        self.registered.append((self.table_id(project, dataset), version, run_id))
        return version, int(body.get("row_count", 0))

    def publish(self, project: str, dataset: str, version: int, *, key_column: str = "id") -> dict[str, object]:
        """Ask the catalog to gate `version` and, if it passes, advance the `published` tag.

        A commit makes bronze READABLE; this is what makes it READY (§ D2 D-R1). The plane does not
        move the tag itself and must not: publication is the catalog's operation so that every writer
        — this plane, a Ray job, a backfill — publishes identically, and so the FGA rung
        (`can_update_tag`) and the quality gate apply to all of them the same way.

        A REFUSED gate is a normal outcome, not an error: the response says `published: false` with
        the failed assertions, the pointer stays where it was, and the run reports what happened. A
        run whose data the gate rejected has still run correctly — it is the DATA that was refused.
        """
        import httpx

        url = f"{self._base}/v1/table/{self.table_id(project, dataset)}/publish"
        payload = {"version": version, "key_column": key_column}
        try:
            response = httpx.post(url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for publish: {exc}") from exc

        if response.status_code >= 400:
            raise CatalogError(f"catalog refused the publish ({response.status_code}): {response.text[:300]}")
        return dict(response.json())

    # ── internals ─────────────────────────────────────────────────────────────────────

    def _describe(self, project: str, dataset: str) -> str | None:
        import httpx

        url = f"{self._base}/v1/table/{self.table_id(project, dataset)}/describe"
        try:
            response = httpx.post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for describe: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused describe ({response.status_code}): {response.text[:300]}")

        location = response.json().get("location")
        return str(location) if location else None

    def _ensure_namespace(self, project: str) -> None:
        """Ensure the project's namespace exists. Probes FIRST, creates only if it does not.

        The probe is not an optimisation. Where namespaces are warehouse-scoped, a create against an
        ALREADY-BOUND namespace is refused by `require_warehouse_scoped` before the catalog ever
        reaches its already-exists check — so an unconditional create fails on a correctly
        provisioned tenant, which is exactly what happened here: the lane provisioned
        project > warehouse > namespace successfully and every run still died at this call.

        `exists` answers the question actually being asked, and it is the only form that behaves the
        same whether or not warehouses are enabled.
        """
        import httpx

        probe = f"{self._base}/v1/namespace/{project}/exists"
        try:
            found = httpx.post(probe, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for namespace probe: {exc}") from exc
        if found.status_code < 400:
            return

        url = f"{self._base}/v1/namespace/{project}/create"
        try:
            response = httpx.post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for namespace create: {exc}") from exc

        if response.status_code == 409:
            return  # another writer got there first
        if response.status_code >= 400:
            # A namespace that must belong to a WAREHOUSE is not something this plane may create.
            #
            # With `warehouses.enabled` (the chart default) the catalog enforces
            # `project > warehouse > namespace > table`, and a bare top-level create is refused. The
            # tempting fix — have ingest provision the chain — is wrong: `POST /v1/projects` is
            # estate-admin gated and writes the creator's `project#admin` tuple, so an ingest run
            # doing it would be the DATA plane provisioning tenancy for itself. Ingest is a writer.
            #
            # So it refuses, and names the three doors an admin uses. A caller seeing this has a
            # setup gap, not a bug, and the message is the fix.
            if "must belong to a warehouse" in response.text:
                raise CatalogError(
                    f"namespace {project!r} is not provisioned: this deployment scopes namespaces to warehouses "
                    f"(project > warehouse > namespace > table), and ingest does not provision tenancy. "
                    f"An admin creates it with POST /v1/projects, POST /v1/warehouses, "
                    f"POST /v1/warehouses/{{id}}/namespaces."
                )
            raise CatalogError(f"catalog refused namespace {project!r} ({response.status_code}): {response.text[:300]}")

    def _create_empty(self, project: str, dataset: str) -> None:
        """Step 1 of the creation two-step — zero rows, so no data byte transits the catalog."""
        import httpx
        import pyarrow as pa

        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, self._schema) as writer:
            writer.write_table(self._schema.empty_table())
        body = sink.getvalue().to_pybytes()

        url = f"{self._base}/v1/table/{self.table_id(project, dataset)}/create"
        try:
            response = httpx.post(
                url,
                content=body,
                headers=self._headers({"Content-Type": "application/vnd.apache.arrow.stream"}),
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for create: {exc}") from exc

        # 409 is a RACE, not a failure: two chunks of the same run, or two runs against one dataset,
        # can both find it absent and both try. The loser re-describes and proceeds.
        if response.status_code == 409:
            logger.info("catalog table %s already existed — another writer created it first", self.table_id(project, dataset))
            return
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused create ({response.status_code}): {response.text[:300]}")

    def describe_version(self, project: str, dataset: str) -> int:
        """The table's current version — the `read_version` a client-direct commit is built against."""
        import httpx

        url = f"{self._base}/v1/table/{self.table_id(project, dataset)}/describe"
        try:
            response = httpx.post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for version: {exc}") from exc
        return int(response.json().get("version") or 1)


def build_catalog(schema: pa.Schema) -> Any:  # noqa: ANN401 — LocalCatalog | CatalogServiceClient
    """The one place that decides which catalog the plane is talking to."""
    if catalog_enabled():
        return CatalogServiceClient(schema)
    from ingest.catalog import LocalCatalog

    return LocalCatalog(schema)
