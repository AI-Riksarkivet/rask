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
from functools import lru_cache
from typing import TYPE_CHECKING

from ingest.config import settings
from ingest.naming import delimiter


if TYPE_CHECKING:
    from collections.abc import Sequence

    import pyarrow as pa

    from ingest.catalog import CatalogSeam

logger = logging.getLogger(__name__)

#: Generous, because a create can provision storage. Still bounded: an unbounded call here would
#: hang a workflow activity rather than letting Dapr retry it.
TIMEOUT_SECONDS = 30.0


def catalog_base_url() -> str:
    return settings().catalog_url.rstrip("/")


def catalog_enabled() -> bool:
    """Whether to route through the catalog service at all.

    Explicit rather than inferred from a reachable URL: a catalog that is merely DOWN must fail the
    run loudly, not silently fall back to writing locally. A silent fallback is how an estate ends up
    with governed data that no catalog knows about.
    """
    return settings().use_catalog


@lru_cache(maxsize=1)
def catalog_token() -> str | None:
    """The catalog bearer, from the SECRET STORE — never from process env.

    This was `os.getenv("RASK_CATALOG_TOKEN")`, and the consequence was measured in-cluster rather
    than reasoned about: on a governed estate the variable is unset (the chart ships no plaintext env
    for a credential, which is the rule working), so every call went out with NO Authorization header
    and the catalog answered

        401 {"detail": "Missing bearer token"}

    on `describe`. That is `ensure_dataset` — the FIRST activity of every run — so no ingest could
    create its bronze dataset and every run ended FAILED with `units 0/0`. A door refusing an
    unauthenticated caller is correct; the caller having no way to be authenticated is the defect.

    FAIL-CLOSED, via the estate's one helper (`fetch_required_secrets`), which catalog / lineage /
    maintenance already use — so a missing secret raises here rather than booting a service that will
    401 on its first real call. Deliberately NO env fallback: a fallback makes the store optional, and
    an optional secret store is the "wired but never read" state an audit already found once.

    Returns None only when the catalog is not in use at all (`catalog_enabled()` false — the local/dev
    path, which has no catalog to authenticate to). Cached because a token fetch per HTTP call would
    put the secret store on the hot path of every unit.
    """
    if not catalog_enabled():
        return None
    # The SERVICE DOOR makes the bearer unnecessary, so do not demand a secret that is not needed.
    # This fail-closed fetch was written before the catalog had an identity door, and it turned a
    # missing-and-unneeded `catalog-token` into a failed run at the first activity. A service that
    # can authenticate as itself has nothing to look up.
    config = settings()
    if config.catalog_service_identity and config.catalog_app_token:
        return None
    from service_kit.governed.secrets import fetch_required_secrets

    bundle = fetch_required_secrets(config.secret_store, config.secret_key, require=config.catalog_token_field)
    return bundle[config.catalog_token_field]


class CatalogError(RuntimeError):
    """The catalog refused, or could not be reached."""


class CatalogServiceClient:
    """Talks to the catalog service — the `ServiceCatalogSeam` half of the seam (`ingest.catalog`).

    It shares `ensure` with `LocalCatalog` and nothing else: committing, publishing and reporting a
    version are operations only a real catalog has, and it registers no version locally because the
    run id rides the commit itself.
    """

    def __init__(self, schema: pa.Schema, base_url: str | None = None, token: str | None = None) -> None:
        self._schema = schema
        self._base = (base_url or catalog_base_url()).rstrip("/")
        self._token = token if token is not None else catalog_token()
        self.registered: list[tuple[str, int, str]] = []

    # ── identity ──────────────────────────────────────────────────────────────────────

    def vend_storage_options(self, namespace: str, dataset: str, *, tier: str = "write") -> dict[str, str] | None:
        """Ask the catalog for a SCOPED credential for this table, or ``None`` to use the ambient one.

        This is the client-direct flow's other half (#2): the worker writes fragments straight to
        object storage, so the credential signing those bytes should be scoped to one table prefix and
        short-lived rather than a long-lived key that reaches the whole bucket. Proven enforced on
        RustFS 2026-09-03 — a credential vended for one table read it and was refused on another with
        403 AccessDenied.

        ``None`` on ANY non-answer, and that is deliberate in three cases rather than one:

        * ``mode_b`` answers ``server_mediated`` with no credential. It is a supported posture, not a
          failure.
        * a vending error (the vendor down, misconfigured, unreachable) must not lose an ingest run.
          The ambient credential is what this writer used before vending existed, so falling back is
          strictly no worse — whereas raising would turn an optional hardening into a new single point
          of failure.
        * an unparseable body, for the same reason.

        The caller therefore never has to distinguish "not offered" from "not available": both mean
        write the way we always did.
        """
        import httpx

        from ingest.http import shared_client

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/credentials"
        try:
            response = shared_client().post(url, json={"tier": tier}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            # NARROW on purpose. A blanket `except Exception` here swallowed a `NameError` raised by
            # this very method and reported it as "vending unavailable" — the degradation path hid a
            # programming error as a configuration one, which is what a bare catch actually costs.
            # Transport failures degrade; anything else is a defect and must surface.
            logger.info("credential vending unreachable (%s) — writing with the ambient credential", exc)
            return None
        if response.status_code >= 400:
            logger.info("credential vending unavailable (%s) — writing with the ambient credential", response.status_code)
            return None
        try:
            options = (response.json().get("credentials") or {}).get("storage_options")
        except ValueError as exc:  # a non-JSON body from something in front of the catalog
            logger.info("credential vending answered unparseable content (%s) — writing with the ambient credential", exc)
            return None
        return options if isinstance(options, dict) and options else None

    def table_id(self, namespace: str, dataset: str) -> str:
        """`{namespace}${dataset}` — pure composition, and the argument is a NAMESPACE.

        It was named `project`, and that name was the whole defect. A project is a level of the
        hierarchy ABOVE the one a table lives in, so passing one produced `bind86$e2ewin` — the 403's
        object, which nobody had granted anything on because `namespace:bind86` does not exist. The
        callers were already right (`table_id("bronze", "pages")`); only the parameter lied.

        The project -> namespace resolution belongs at the boundary where a project is authoritative,
        which is `RunSpec.namespace`. Doing it here would mean two places qualify, and a caller that
        already holds a namespace would get it qualified twice (`bronze-bronze$pages`, measured).
        """
        return f"{namespace}{delimiter()}{dataset}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The credential this plane presents to the catalog.

        THE SERVICE IDENTITY IS PREFERRED, and that ordering is the fix. The catalog now runs the
        same identity door lineage does (`service_kit.governed.dapr_auth.service_principal`), so a
        service authenticates the way a service should — with the app token daprd already injects
        plus the subject it claims — and needs no bearer at all.

        The bearer path came first and was the wrong shape for the problem: the catalog verifies OIDC
        JWTs, a JWT EXPIRES, and a static string in a secret store cannot be one. Chasing it produced
        a fail-closed run on a `catalog-token` secret that never needed to exist. Kept below only for
        a caller that genuinely holds a user's bearer — forwarding a human's token is a real case, and
        it takes precedence over nothing here because a service call has no human to forward.

        Both halves, never one: the door requires the token AND the identity, and sending one is a
        request refused for a reason invisible from this side.
        """
        headers = dict(extra or {})
        config = settings()
        token, identity = config.catalog_app_token, config.catalog_service_identity
        if token and identity:
            headers["dapr-api-token"] = token
            headers["x-lance-service-identity"] = identity
        elif self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ── the two doors ─────────────────────────────────────────────────────────────────

    def ensure(self, namespace: str, dataset: str, external_base: str | None = None) -> str:
        """Create the namespace and the table if absent; return the location the catalog vends.

        ``external_base`` registers the root this dataset's blob descriptors may point at. It is
        stamped into the SCHEMA at creation — the same mechanism `lander.create_empty` uses — because
        the catalog's create door takes an Arrow schema and nothing else, and the base has to be
        recorded before the first fragment lands.

        The parameter existed on `LocalCatalog.ensure` and not here, so `runtime.py`'s call worked in
        every unit test and died in-cluster with `unexpected keyword argument \'external_base\'` at
        the activity that creates the table — measured on a real backfill 2026-08-26. A two-sided seam
        only holds if both sides accept the same call.

        THREE steps, not two. The design said "create the table, then commit fragments", and against
        a real catalog that fails at the first call with

            NamespaceNotFoundError — Child namespace reads require an existing __manifest dataset

        because a table lives IN a namespace and the namespace is itself a catalog object with its
        own manifest. It is not implicit in the table id: `demo$pages` names a namespace `demo` that
        has to exist first. Nothing about the table endpoints says so, and the failure only appears
        against a catalog whose namespace has never been created — which every dev catalog is, once.
        """
        located = self._describe(namespace, dataset)
        if located is not None:
            # LOAD-BEARING, not best-effort: workers now ALWAYS write the `etag` column
            # (identity material, owner ruling 2026-08-07), so a table created before the column
            # existed would refuse its next append with a schema mismatch AFTER every byte had
            # been fetched. Schema evolution is the format's own answer — an expression add of
            # `cast(NULL as string)` extends the schema and NULL-fills existing rows without a
            # rewrite (guide.md, Data Evolution) — and it runs at ensure, BEFORE any fan-out.
            self._ensure_etag_column(namespace, dataset)
            return located

        self._ensure_namespace(namespace)
        # The create's OWN response carries the location, so the happy path costs one call, not two —
        # and more importantly it does not re-ask a read door the question the read door cannot answer.
        created = self._create_empty(namespace, dataset, external_base)
        if created is not None:
            return created

        # Only a 409 reaches here: the table already existed. Re-describe, because the tuples that make
        # it describable were seeded by whoever created it.
        located = self._describe(namespace, dataset)
        if located is None:
            # It exists (409) and we still cannot see it (403). That is a real authorization gap on an
            # EXISTING table, not the absent-table case above, and it must not be reported as "created
            # but no location" — that message sent a reader looking for a catalog bug for an afternoon.
            raise CatalogError(
                f"{self.table_id(namespace, dataset)} already exists but this identity cannot describe it — "
                f"it needs can_get_metadata on table:{self.table_id(namespace, dataset)} (or writer on its namespace)"
            )
        return located

    def commit(self, namespace: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]:
        """Fold client-written fragments into ONE new version. Returns (version, row_count)."""
        from ingest.http import shared_client

        payload = {
            # json.loads because the plane transports fragments as STRINGS (they cross a Dapr
            # activity boundary) while the catalog's schema declares dicts.
            "fragments": [json.loads(f) for f in fragments_json],
            "read_version": read_version,
            # The run identity, ON THE WIRE at last. `CatalogClient.register_version`'s protocol has
            # promised "the run id in commit metadata is how a died-after-commit run is reconciled"
            # since the seam was written — and this client recorded the id in a LOCAL list
            # (`self.registered`) that died with the process, so the deployed path had no
            # reconciliation at all: a `finalize` retry after a successful commit re-appended every
            # row. The catalog now stamps it as a transaction property and answers a replayed commit
            # with the version this run already committed (idempotent replay).
            "run_id": run_id,
        }
        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/commit"
        try:
            response = shared_client().post(url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for commit: {exc}") from exc

        if response.status_code == 409:
            # Optimistic concurrency: another writer committed against the same read_version. The
            # activity's own retry re-reads and re-commits, which is the documented recovery — so
            # this must be raised, never swallowed into a success.
            raise CatalogError(f"commit conflict on {self.table_id(namespace, dataset)} at read_version {read_version} — re-read and retry")
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused the commit ({response.status_code}): {response.text[:300]}")

        body = response.json()
        version = int(body["version"])
        self.registered.append((self.table_id(namespace, dataset), version, run_id))
        return version, int(body.get("row_count", 0))

    def publish(
        self,
        namespace: str,
        dataset: str,
        version: int,
        *,
        key_column: str = "id",
        required_columns: Sequence[str] = (),
    ) -> dict[str, object]:
        """Ask the catalog to gate `version` and, if it passes, advance the `published` tag.

        A commit makes bronze READABLE; this is what makes it READY (§ D2 D-R1). The plane does not
        move the tag itself and must not: publication is the catalog's operation so that every writer
        — this plane, a Ray job, a backfill — publishes identically, and so the FGA rung
        (`can_update_tag`) and the quality gate apply to all of them the same way.

        A REFUSED gate is a normal outcome, not an error: the response says `published: false` with
        the failed assertions, the pointer stays where it was, and the run reports what happened. A
        run whose data the gate rejected has still run correctly — it is the DATA that was refused.
        """
        from ingest.http import shared_client

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/publish"
        # `required_columns` adds one `column_declared` assertion each — the breaking-change detector,
        # which refuses a version that dropped a column a consumer depends on. `PublishRequest` has
        # always accepted it and no caller sent any, so the door ran two assertions where the
        # medallion's local gate runs five on the same data.
        payload: dict[str, object] = {"version": version, "key_column": key_column, "required_columns": list(required_columns)}
        try:
            response = shared_client().post(url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for publish: {exc}") from exc

        if response.status_code >= 400:
            raise CatalogError(f"catalog refused the publish ({response.status_code}): {response.text[:300]}")
        return dict(response.json())

    # ── internals ─────────────────────────────────────────────────────────────────────

    def _describe(self, namespace: str, dataset: str) -> str | None:
        from ingest.http import shared_client

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/describe"
        try:
            response = shared_client().post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for describe: {exc}") from exc

        # 404 AND 403 both mean "no location for you here" — and conflating them is not laziness, it is
        # the only reading a caller is entitled to. A READ door cannot distinguish ABSENT from HIDDEN
        # without becoming an existence oracle for table names, so the catalog answers 403 for both.
        # Measured against the deployed catalog, service-ingest, 2026-08-06:
        #
        #     ABSENT  exists    -> 403 PermissionDeniedError      ABSENT  describe -> 403
        #     EXISTS  exists    -> 200                            EXISTS  describe -> 200 {location…}
        #
        # Treating the 403 as fatal is what made a new bronze table IMPOSSIBLE: `ensure` raised here
        # and `_create_empty` was never reached, on the service path as much as the UI one. Every run
        # that ever succeeded did so against a table someone had already created.
        #
        # Falling through is not a permission bypass. CREATE is the authoritative gate and it is
        # authorized on the PARENT — `can_create_table` on the namespace, the estate's create-on-parent
        # rule — so a caller who may not create is refused there, with the right object in the message.
        # Measured on the same run: `POST /v1/table/bind86-bronze$createprobe/create` -> 200.
        if response.status_code in (403, 404):
            return None
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused describe ({response.status_code}): {response.text[:300]}")

        location = response.json().get("location")
        return str(location) if location else None

    def _ensure_namespace(self, namespace: str) -> None:
        """Ensure the project's namespace exists. Probes FIRST, creates only if it does not.

        The probe is not an optimisation. Where namespaces are warehouse-scoped, a create against an
        ALREADY-BOUND namespace is refused by `require_warehouse_scoped` before the catalog ever
        reaches its already-exists check — so an unconditional create fails on a correctly
        provisioned tenant, which is exactly what happened here: the lane provisioned
        project > warehouse > namespace successfully and every run still died at this call.

        `exists` answers the question actually being asked, and it is the only form that behaves the
        same whether or not warehouses are enabled.
        """
        from ingest.http import shared_client

        probe = f"{self._base}/v1/namespace/{namespace}/exists"
        try:
            found = shared_client().post(probe, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for namespace probe: {exc}") from exc
        if found.status_code < 400:
            return

        url = f"{self._base}/v1/namespace/{namespace}/create"
        try:
            response = shared_client().post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
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
                    f"namespace {namespace!r} is not provisioned: this deployment scopes namespaces to warehouses "
                    f"(project > warehouse > namespace > table), and ingest does not provision tenancy. "
                    f"An admin creates it with POST /v1/projects, POST /v1/warehouses, "
                    f"POST /v1/warehouses/{{id}}/namespaces."
                )
            raise CatalogError(f"catalog refused namespace {namespace!r} ({response.status_code}): {response.text[:300]}")

    def _create_empty(self, namespace: str, dataset: str, external_base: str | None = None) -> str | None:
        """Step 1 of the creation two-step — zero rows, so no data byte transits the catalog.

        Returns the location the catalog vends, or None when the table already existed (409).

        RETURNING THE LOCATION IS THE POINT, not a convenience: this is the ONLY door in the sequence
        that can answer "does this table exist" without being an existence oracle, because a caller who
        may not create is refused by `can_create_table` on the NAMESPACE — a permission the parent
        genuinely carries. The read doors cannot: `describe` and `exists` both answer 403 for an absent
        table (measured 2026-08-06), so asking them first and believing the answer is what made a new
        bronze table impossible to create at all.
        """
        from ingest.http import shared_client
        from service_kit.lakehouse import blobs
        from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream

        # Stamp the approved external base onto the schema before the create, exactly as the local
        # path does — the catalog's door carries a schema and nothing else, so this is where the base
        # has to ride.
        schema = blobs.stamp_external_base(self._schema, external_base) if external_base else self._schema

        body = encode_arrow_stream(schema.empty_table())

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/create"
        try:
            response = shared_client().post(
                url,
                content=body,
                headers=self._headers({"Content-Type": ARROW_STREAM_MEDIA_TYPE}),
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for create: {exc}") from exc

        # 409 is a RACE, not a failure: two chunks of the same run, or two runs against one dataset,
        # can both find it absent and both try. The loser re-describes and proceeds.
        if response.status_code == 409:
            logger.info("catalog table %s already existed — another writer created it first", self.table_id(namespace, dataset))
            return None
        if response.status_code >= 400:
            raise CatalogError(f"catalog refused create ({response.status_code}): {response.text[:300]}")

        location = response.json().get("location")
        return str(location) if location else None

    def _ensure_etag_column(self, namespace: str, dataset: str) -> None:
        """Add the nullable `etag` column to a pre-existing table. Idempotent by refusal.

        `cast(NULL as string)` is the guide's own nullable-column idiom: the schema gains the
        field and existing rows are NULL-filled — one new version, no data rewrite. The catalog's
        add_columns door refuses a duplicate column name, and THAT refusal is the idempotence:
        second and later ensures are a cheap 4xx no-op. Any other failure RAISES — a table the
        column could not be added to would fail its append after the whole fetch, which is the
        expensive place to learn it.
        """
        from ingest.http import shared_client

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/add_columns"
        payload = {"new_columns": [{"name": "etag", "expression": "cast(NULL as string)"}]}
        try:
            response = shared_client().post(url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for schema evolution: {exc}") from exc
        if response.status_code < 400:
            logger.info("added etag column to %s (schema evolution at ensure)", self.table_id(namespace, dataset))
            return
        body = response.text.lower()
        if "already exists" in body or "duplicate" in body or "exists in schema" in body:
            return  # the column is there — the ordinary case after the first ensure
        raise CatalogError(f"catalog refused the etag column add ({response.status_code}): {response.text[:300]}")

    def describe_version(self, namespace: str, dataset: str) -> int:
        """The table's current version — the `read_version` a client-direct commit is built against."""
        from ingest.http import shared_client

        url = f"{self._base}/v1/table/{self.table_id(namespace, dataset)}/describe"
        try:
            response = shared_client().post(url, json={}, headers=self._headers(), timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except Exception as exc:
            raise CatalogError(f"catalog unreachable for version: {exc}") from exc
        return int(response.json().get("version") or 1)


def build_catalog(schema: pa.Schema) -> CatalogSeam:
    """The one place that decides which catalog the plane is talking to."""
    if catalog_enabled():
        return CatalogServiceClient(schema)
    from ingest.catalog import LocalCatalog

    return LocalCatalog(schema)
