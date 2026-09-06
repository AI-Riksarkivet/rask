"""Governed FULL-UNION e2e — the shipped combination, driven live (§7's last coverage hole).

Every flag at once: **OIDC auth ON + OpenFGA ON (catalog, lineage reads, movers) + compute ON +
quality gate ON**, against the real kind stack (Dapr/NATS/AGE/RustFS/Dex/OpenFGA). The recurring bug
class here is the never-driven union — each feature green in isolation while the composition breaks —
so this suite asserts, live:

  1. the governed ALLOW path: one ``/produce`` cascades bronze→silver→gold (R23: the producer ingests
     straight into bronze) with the seeded service
     grants, correlated by the deterministic per-stage run ids; quality verdicts recorded; and the same
     stack really enforces (anon → 401, ungranted user → 403);
  2. FGA-deny → DROP: with the gold validator tuple revoked, the SAME drive stops at silver — gold's
     run never lands — and re-granting makes the next drive cascade again (the tuple is the only delta);
  3. quality-block: a bad batch (null ids) written to bronze is derived into silver, the gate records
     ``quality_passed=false`` + the failed ``not_null`` assertion in lineage, and gold is NOT triggered;
  4. the MEDIA lane under governance: ``/ingest-media`` lands blobs + derives thumbnail/embedding with
     the seeded ``service-media-to-silver`` grant, all read back through the GOVERNED lineage API.
  5. the TRAIN lane under governance (#115): ``/train`` drives a Ray training job that self-emits its
     OpenLineage lifecycle to the HTTP ingest — which under auth 401'd EVERY event (all training
     provenance silently lost) until the service-door credential. The run must land COMPLETE
     **attributed to ``service-trainer``** (a bare FGA subject, not a Dex sub), with the model node
     governed like the rest of the estate — the exact combination that was broken. (The pinned feature
     version rides the graph's READ edge but no API surfaces run inputs yet — see docs/GOAL-prove-it.md.)

Deploy the union + seed, then ``make e2e-governed-union`` (which port-forwards + seeds + fills env):

    helm upgrade --install lance-ns ./chart --set image.catalog.tag=dev --set image.web.tag=dev \\
      --set auth.enabled=true --set medallion.fgaEnabled=true \\
      --set medallion.compute=true --set medallion.quality=true --set openbao.enabled=false

Human lineage reads work because ``scripts/seed_medallion_fga.sh`` links every cascade dataset to its
stage namespace (table→namespace parent tuples) — the suite only grants its own reader on the warehouse.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
from promotion_review import approve_if_held
from topology import OUTSIDER


LANCERAY = os.environ.get("LANCE_E2E_LANCERAY_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
FGA = os.environ.get("LANCE_E2E_FGA", "")
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
# The bronze→silver mover, for the quality-block direct drive (movers have no k8s Service — the make
# target port-forwards the deployment) + its app token (the same guard its sidecar delivery carries).
MOVER_URL = os.environ.get("LANCE_E2E_MOVER_URL", "")
MOVER_TOKEN = os.environ.get("LANCE_E2E_MOVER_TOKEN", "")
S3_ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("LANCE_E2E_S3_BUCKET", "lance-catalog")
S3_ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "")
#: The governed catalog — the only thing that knows where a tier physically lives (rule I2). The
#: quality-block leg used to COMPOSE `s3://<bucket>/medallion/bronze`, which is the SINGLE-TENANT
#: path; on a project estate the mover reads the project's own warehouse root, so the leg corrupted a
#: dataset nothing in the cascade opens and then waited for a verdict on a batch nobody processed.
CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")

#: The warehouse alice is granted reader on. A TENANT drive cascades under its project's own zone
#: warehouse, not the estate root — `seed_medallion_fga.sh` links `warehouse:<zone-wh> -> namespace:
#: <project>-<tier>` — so a reader grant on the root reaches none of the tenant's stages.
#: DISCOVERED, NOT NAMED — the same rule `topology.OUTSIDER` follows and for the same reason. This
#: read `warehouse:lakehouse-wh` for ANY project estate; the runner discovers the project's real
#: warehouse (`acme-bucket` here) and exports it as `LANCE_E2E_WAREHOUSE`. Naming the wrong one is not
#: a near-miss: `_owner_tuples` revokes `owner` on it, and OpenFGA fails the whole delete BATCH when a
#: listed tuple does not exist — so test 2 revoked NOTHING, the mover kept `owner` on the warehouse it
#: really holds, owner outranked the writer rung, and the "denied" drive completed. The +12s negative
#: passed anyway because the Ray stage had not finished yet; only the post-redelivery re-check saw it.
#: `LANCE_E2E_FGA_WAREHOUSE` stays the explicit override for an estate whose FGA warehouse differs.
WAREHOUSE = (
    os.environ.get("LANCE_E2E_FGA_WAREHOUSE", "")
    or (f"warehouse:{os.environ['LANCE_E2E_WAREHOUSE']}" if os.environ.get("LANCE_E2E_WAREHOUSE") else "")
    or ("warehouse:lakehouse-wh" if os.environ.get("LANCE_E2E_PROJECT") else "warehouse:lance_catalog")
)
#: the silver→gold mover's validator rung, on the tier the drive actually targets (can_promote).
GOLD_VALIDATOR = {
    "user": "user:service-silver-to-gold",
    "relation": "validator",
    "object": "namespace:{}".format(f"{os.environ.get('LANCE_E2E_PROJECT')}-gold" if os.environ.get("LANCE_E2E_PROJECT") else "gold"),
}
#: the bronze→silver mover's writer rung — revoked in test 2's writer-gate sub-phase (can_create_table).
#: On a tenant drive this is the NAMESPACE rung, not the warehouse one: the mover also holds
#: `owner` on the warehouse from `seed_ownership`, so revoking a warehouse-level writer denies nothing.
SILVER_WRITER = (
    {
        "user": "user:service-bronze-to-silver",
        "relation": "writer",
        "object": "namespace:{}".format(f"{os.environ.get('LANCE_E2E_PROJECT')}-silver" if os.environ.get("LANCE_E2E_PROJECT") else "silver"),
    }
    if os.environ.get("LANCE_E2E_PROJECT")
    else {"user": "user:service-bronze-to-silver", "relation": "writer", "object": WAREHOUSE}
)


#: THE OWNER TUPLES `seed_ownership` WRITES, which a single-rung revoke cannot see past.
#:
#: A mover that CREATED a table is its owner, and owner outranks the rung the deny is aiming at.
#: Measured live 2026-08-25 with GOLD_VALIDATOR deleted:
#:     warehouse:lakehouse-wh        user:service-silver-to-gold  owner
#:     table:lakehouse-gold$catalog  user:service-silver-to-gold  owner
#:     check can_promote namespace:lakehouse-gold -> allowed = True
#: so the "denied" drive promoted anyway and the assertion passed only because it was looking for a run
#: id that could never exist (see OPERATIONS). Revoking these alongside the rung is what makes the deny
#: a deny — a strengthening of the assertion, not a relaxation of it.
def _owner_tuples(user: str, namespace: str, table: str) -> list[dict[str, str]]:
    """The two owner tuples `seed_ownership` actually writes — warehouse and table, NOT the namespace.

    Listing a tuple that does not exist is not harmless: OpenFGA fails the whole delete BATCH, so
    including a speculative `owner namespace:<ns>` revoked NOTHING and the "denied" drive sailed
    through with no deny logged anywhere. Measured 2026-08-25 against the live store:

        before revoke             can_create_table -> True
        after revoking the rung   can_create_table -> True    (owner still outranks it)
        after revoking owners too can_create_table -> False

    `namespace` is kept in the signature because the caller reads better naming the tier it is denying.
    """
    del namespace  # named by the caller for readability; seed_ownership writes no namespace owner
    return [
        {"user": user, "relation": "owner", "object": WAREHOUSE},
        {"user": user, "relation": "owner", "object": f"table:{table}"},
    ]


#: JetStream first-redelivery window: backOff[0] == ackWait, pinned to chart/templates/dapr-component.yaml
#: (backOff: 30s,…). Test 2 must observe a denied run stay absent PAST this, measured — not choreographed.
REDELIVERY_WINDOW = 30.0
#: Stage operations this drive can NAME BY RUN ID — bronze and silver only, and `aggregate_gold` is
#: deliberately absent.
#:
#: Under `medallion.cascadeViaPublish` the silver→gold hop is driven by the catalog's table_published
#: event, and `publication_trigger.py:138` mints that trigger's token from the publication `event_id`.
#: So gold's run id is seeded from a token this suite never sees and cannot compute — measured live as
#: a 32-hex id where the drive's own tokens are 12-hex. Polling for
#: `run_id_for("aggregate_gold-<produce token>")` therefore waits for an id that can never exist, and
#: reports it as "the cascade did not complete" about a cascade that completed.
#:
#: Gold is asserted the way the medallion suite asserts it instead: by its UPSTREAM CHAIN, which is
#: identity-free and true however the token was minted.
OPERATIONS = ("lance_ray_ingest", "embed_features")

pytestmark = [pytest.mark.e2e, pytest.mark.governed_union]


def _idem(prefix: str) -> str:
    """A fresh `Idempotency-Key`, REQUIRED by every medallion write door since 2026-08-27 (`2da0164c`).

    The header is declared with no default, so FastAPI answers 422 at header validation — before auth,
    before the lane, before anything a governance suite means to exercise. `f0b97870` fixed the identical
    omission in test_medallion_e2e and test_media_e2e; this suite was skipping then (the runner withheld
    `LANCE_E2E_LANCERAY_URL`), so it kept the miss and all five legs below died on a validation error
    rather than on the governance they assert.
    """
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# stack plumbing
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def stack() -> tuple[str, str]:
    if not (LANCERAY and LINEAGE and FGA):
        pytest.skip("set LANCE_E2E_LANCERAY_URL / LANCE_E2E_LINEAGE_URL / LANCE_E2E_FGA (see docstring)")
    for name, url in (("medallion-producer", LANCERAY), ("lineage", LINEAGE)):
        try:
            requests.get(f"{url.rstrip('/')}/livez", timeout=5).raise_for_status()
        except Exception:
            pytest.skip(f"{name} not reachable at {url}")
    try:
        requests.get(f"{DEX}/.well-known/openid-configuration", timeout=5).raise_for_status()
        requests.get(f"{FGA}/healthz", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("dex / openfga not reachable")
    # The whole point is the GOVERNED union — on an auth-off stack these assertions would prove nothing,
    # so detect it (an anonymous /runs read succeeding == lineage auth is off) and skip loudly.
    if requests.get(f"{LINEAGE.rstrip('/')}/runs", timeout=8).status_code != 401:
        pytest.skip("stack is not auth-on (anonymous /runs succeeded) — deploy the union per the docstring")
    return LANCERAY.rstrip("/"), LINEAGE.rstrip("/")


@pytest.fixture(scope="module")
def fga_store(stack: tuple[str, str]) -> tuple[str, str]:
    """The lance-catalog OpenFGA store + latest authorization model id (raw HTTP, like the services)."""
    _ = stack  # gate on the stack fixture's env + reachability (+ auth-on) skips BEFORE touching OpenFGA
    try:
        stores = requests.get(f"{FGA}/stores", timeout=10).json()["stores"]
    except Exception:
        # Unreachable/unset FGA must SKIP, not ERROR: an unguarded request raises out of a module-scoped
        # fixture, which pytest reports as an error for every test that uses it — indistinguishable in CI
        # from a real failure, on a suite that is supposed to be inert without a live stack.
        pytest.skip(f"openfga not reachable at {FGA or '<unset>'}")
    store = next((s["id"] for s in stores if s["name"] == "lance-catalog"), None)
    if store is None:
        # A bare next() raises StopIteration here — an unseeded stack is a skip, not a failure.
        pytest.skip("no 'lance-catalog' OpenFGA store — the stack is not seeded (scripts/seed_medallion_fga.sh)")
    models = requests.get(f"{FGA}/stores/{store}/authorization-models", timeout=10).json()["authorization_models"]
    if not models:
        pytest.skip("the lance-catalog OpenFGA store carries no authorization model")
    return store, models[0]["id"]


def _tuples(fga_store: tuple[str, str], *, writes: list[dict] | None = None, deletes: list[dict] | None = None) -> None:
    """Write/delete tuples via OpenFGA's Write RPC, idempotently across runs.

    Only the two IDEMPOTENCY 400s are tolerated (duplicate write / delete-of-absent — matched on the
    error message); any other 400 (malformed tuple, bad relation, wrong model id) fails HERE with the
    real error, not 90 seconds later as a misleading poll timeout (audit: a blanket 400-pass masked
    real seed errors)."""
    store, model = fga_store
    body: dict = {"authorization_model_id": model}
    if writes:
        body["writes"] = {"tuple_keys": writes}
    if deletes:
        body["deletes"] = {"tuple_keys": deletes}
    resp = requests.post(f"{FGA}/stores/{store}/write", json=body, timeout=10)
    if resp.status_code == 200:
        return
    message = resp.json().get("message", "") if resp.status_code == 400 else ""
    assert "already exists" in message or "did not exist" in message or "does not exist" in message, f"OpenFGA write failed ({resp.status_code}): {resp.text}"


def _token(username: str) -> str:
    data = {
        "grant_type": "password",
        "client_id": "lance-catalog",
        "username": username,
        "password": "password",
        "scope": "openid",
    }
    if DEX_SECRET:
        data["client_secret"] = DEX_SECRET
    body = requests.post(f"{DEX}/token", data=data, timeout=10).json()
    assert "id_token" in body, f"Dex token grant failed: {body}"
    return body["id_token"]


def _sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


@pytest.fixture(scope="module")
def alice(stack: tuple[str, str], fga_store: tuple[str, str]) -> Iterator[dict[str, str]]:
    """An authenticated READER over the medallion estate: warehouse reader + the seed script's
    table→namespace parent links give her can_get_metadata on every cascade dataset.

    Teardown deletes the grant — it's a durable BROAD read grant in the SHARED OpenFGA store, and
    leaving it behind would quietly widen alice's access for everything run after this suite."""
    token = _token("alice@example.com")
    grant = {"user": f"user:{_sub(token)}", "relation": "reader", "object": WAREHOUSE}
    _tuples(fga_store, writes=[grant])
    yield {"Authorization": f"Bearer {token}"}
    _tuples(fga_store, deletes=[grant])


def _run_id_for(operation: str, token: str, *, project: str | None = None) -> str:
    """The deterministic per-stage run id, seeded EXACTLY as the producer seeds it.

    Two shapes, and which one applies depends on whether the run carries a project
    (`medallion/schemas/events.py`):

        run_id_for("\x00".join((project, operation, token)))   # tenant
        run_id_for(f"{operation}-{token}")                      # single-tenant

    A NUL-bearing seed is unreachable from the `-`-joined one, so using the wrong shape does not
    mismatch by a little — it names an id that does not exist. Measured 2026-08-25: a tenant drive
    against the single-tenant seed reported every stage as `None`, which reads as a dead cascade.
    """
    from service_kit.openlineage import run_id_for

    seed_project = PROJECT if project is None else project
    if seed_project:
        return run_id_for("\x00".join((seed_project, operation, token)))
    return run_id_for(f"{operation}-{token}")


def _run_states(lineage: str, headers: dict[str, str]) -> dict[str, str]:
    resp = requests.get(f"{lineage}/runs?limit=1000", headers=headers, timeout=8)
    resp.raise_for_status()
    return {r["run_id"]: r.get("state") or "" for r in resp.json().get("runs", [])}


def _gold_runs(lineage: str, headers: dict[str, str]) -> set[str]:
    """Run ids that have produced the gold table — the only handle this suite has on the gold stage.

    Gold's own trigger token is the publication `event_id`, minted inside the catalog and never
    returned here, so no run id this suite computes can name it (see OPERATIONS). What IS observable is
    whether a NEW run produced gold, and that is what both halves of the deny/regrant argument actually
    need: a denied promotion adds none, a restored one adds one.
    """
    resp = requests.get(f"{lineage}/datasets/{_ds('gold$catalog')}/producers", headers=headers, timeout=8)
    if resp.status_code != 200:
        return set()
    produced = {p["run_id"] for p in resp.json().get("producers", [])}
    # COMPLETED runs only. With mover-side FGA on, a denied stage EMITS a FAIL run rather than
    # vanishing — auditable in lineage, the same rule the quality gate follows — so counting every run
    # that touched gold makes a correctly-denied promotion look like a leak. The property both halves
    # of the deny/regrant argument need is whether gold was SUCCESSFULLY produced.
    states = _run_states(lineage, headers)
    return {rid for rid in produced if states.get(rid) == "COMPLETE"}


def _quiesced_gold(lineage: str, headers: dict[str, str], *, still_for: float = 45.0, timeout: float = 150.0) -> set[str]:
    """The gold run set, once it has STOPPED changing — the only honest baseline for a deny window.

    Gold runs cannot be attributed to a drive: the silver→gold trigger's token is minted inside the
    catalog (see OPERATIONS), so "did THIS drive produce gold" is not answerable by name. What is
    answerable is "did any new gold run appear while the grant was revoked" — but only if the previous
    test's cascade has finished first. One Ray hop measures ~30 s, and the suite's tests run in
    sequence, so the PRIOR test's gold lands squarely inside this one's deny window and is counted as
    the denied drive's. Measured 2026-08-25: the deny assertion failed with exactly one extra run that
    the denied drive had not produced.

    `still_for` is 45 s because ONE Ray hop measures ~30 s: the previous sub-phase restores its grant
    and its cascade then runs on, so a window shorter than a hop still catches that drive's gold. This
    weakens nothing — the assertion still demands that NO new gold run appears while the grant is
    revoked; it only makes sure the baseline is taken when nothing is already in flight.
    """
    deadline = time.monotonic() + timeout
    seen = _gold_runs(lineage, headers)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(3)
        now = _gold_runs(lineage, headers)
        if now != seen:
            seen, stable_since = now, time.monotonic()
        elif time.monotonic() - stable_since >= still_for:
            return seen
    return seen


def _producer_for(lineage: str, headers: dict[str, str], dataset: str, run_id: str) -> dict | None:
    resp = requests.get(f"{lineage}/datasets/{dataset}/producers", headers=headers, timeout=8)
    if resp.status_code != 200:
        return None
    return next((p for p in resp.json().get("producers", []) if p["run_id"] == run_id), None)


#: The tenant this drive runs for. NOT optional on a publish-driven estate: with
#: `medallion.cascadeViaPublish` on, the cascade is driven by `publication_trigger`, which ALWAYS
#: carries a project because the mover cannot resolve its tiers without one. A PROJECTLESS produce
#: therefore publishes silver and GOLD NEVER FIRES — measured here 2026-08-25 as
#: {'lance_ray_ingest': 'COMPLETE', 'embed_features': 'COMPLETE', 'aggregate_gold': None}, which the
#: bare message reported as a broken cascade against a cascade working perfectly for a tenant. The
#: medallion suite names the same signature in `_projectless_diagnosis`.
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "")
#: Producing INTO a named project is a human-authorized act: `produce_auth` takes the service-token
#: branch as soon as a valid dapr-api-token is present and refuses the cross-project case THERE, before
#: any bearer is read — so the bearer must go INSTEAD of the token, not alongside it.
ADMIN_TOKEN = os.environ.get("LANCE_E2E_ADMIN_TOKEN", "")


def _ds(name: str) -> str:
    """Project-qualify a dataset id — `silver$features` -> `<project>-silver$features` for a tenant.

    `workflow.py::_qualified` does this at RUNTIME, so the graph records the qualified name and a suite
    that asks for the bare one gets nothing back and reports it as an absent cascade.
    """
    return f"{PROJECT}-{name}" if PROJECT else name


def _bearer(headers: dict[str, str]) -> str:
    """The raw token out of an ``Authorization: Bearer …`` header — the approver needs the token, not the header."""
    return headers.get("Authorization", "").removeprefix("Bearer ").strip()


def _poll(
    predicate: Callable[[], bool],
    *,
    timeout: float = 90.0,
    message: str | Callable[[], str],
    on_tick: Callable[[], object] | None = None,
) -> None:
    """Poll until ``predicate`` is true, else fail with ``message`` (a callable is evaluated AT failure —
    an f-string call-site message that reads live state would show the PRE-poll state, not the final one).
    A transient TRANSPORT error inside the predicate (one dropped port-forward packet) counts as
    not-ready rather than aborting the whole budget — but ONLY transport errors: an HTTP error status
    (401/403/500 via raise_for_status) is a real regression that must surface immediately, not burn 90s
    and get misreported as a timeout.

    ``on_tick`` runs once per iteration BEFORE the predicate, and exists for exactly one job: clearing
    the promotion hold this drive is waiting on. An estate running human-in-the-loop review holds every
    FIRST promotion (no predecessor row count, so the band reads it as a breach), so the cascade waits
    for a human and this poll burns its whole budget reporting "did not complete" — about a cascade that
    completed and was stopped by governance. See tests/e2e-py/promotion_review.py."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if on_tick is not None:
                on_tick()
            if predicate():
                return
        except (requests.ConnectionError, requests.Timeout):
            pass  # transient plumbing hiccup — keep polling until the budget is spent
        time.sleep(3)
    pytest.fail(message if isinstance(message, str) else message())


def _produce(lance_ray: str) -> str:
    headers: dict[str, str] = {"dapr-api-token": DAPR_TOKEN, "Idempotency-Key": _idem("gu-produce")}
    params: dict[str, str] = {}
    if PROJECT:
        # Bearer INSTEAD of the service token — see ADMIN_TOKEN.
        params["project"] = PROJECT
        if ADMIN_TOKEN:
            # SWAP THE CREDENTIAL, KEEP THE KEY. This replaced the whole dict, which silently dropped
            # `Idempotency-Key` — required with no default since `2da0164c` — so on a project estate
            # (which this one is: the runner discovers `acme`) every produce answered 422 at header
            # validation. Only the credential differs between the two shapes; nothing else about the
            # request does.
            del headers["dapr-api-token"]
            headers["Authorization"] = f"Bearer {ADMIN_TOKEN}"
    resp = requests.post(f"{lance_ray}/produce", headers=headers, params=params, timeout=30)
    assert resp.status_code == 202, resp.text
    return resp.json()["token"]


# --------------------------------------------------------------------------- #
# 1. governed ALLOW — the full union works, and the same stack really enforces
# --------------------------------------------------------------------------- #


def test_governed_allow_full_cascade_with_quality_verdicts(stack: tuple[str, str], alice: dict[str, str]) -> None:
    lance_ray, lineage = stack
    token = _produce(lance_ray)
    rids = {op: _run_id_for(op, token) for op in OPERATIONS}

    # Bronze and silver land COMPLETE under the seeded service grants — correlated to THIS drive by the
    # deterministic run ids (not by counts), so a stale graph can't false-pass. The diagnostic is a
    # CALLABLE so a timeout reports the final observed states, not the pre-poll snapshot (one fetch,
    # not one per operation — a per-op fetch would be N round-trips of N different snapshots).
    def _states_diag() -> str:
        states = _run_states(lineage, alice)
        return f"governed cascade did not complete for token {token}: { {op: states.get(rid) for op, rid in rids.items()} }"

    _poll(
        lambda: all(_run_states(lineage, alice).get(rid) == "COMPLETE" for rid in rids.values()),
        message=_states_diag,
        on_tick=lambda: approve_if_held(lance_ray, token, _bearer(alice)),
    )

    # GOLD IS ASSERTED BY ITS CHAIN, not by a run id — see OPERATIONS. Its trigger's token is the
    # publication `event_id`, minted inside the catalog and never returned to this drive, so no run id
    # this suite can compute will ever name it. The upstream chain is identity-free and says the same
    # thing the run id was there to say: the terminal tier was reached FROM this cascade's own tiers.
    gold = _ds("gold$catalog")
    chain = {_ds("bronze$events"), _ds("silver$features")}

    def _chain_diag() -> str:
        resp = requests.get(f"{lineage}/datasets/{gold}/upstream", headers=alice, timeout=8)
        seen = [r["name"] for r in resp.json().get("related", [])] if resp.status_code == 200 else []
        return f"gold never reached the full chain for token {token}: upstream={seen} (HTTP {resp.status_code})"

    def _gold_reached() -> bool:
        resp = requests.get(f"{lineage}/datasets/{gold}/upstream", headers=alice, timeout=8)
        return resp.status_code == 200 and chain <= {r["name"] for r in resp.json().get("related", [])}

    _poll(_gold_reached, message=_chain_diag, on_tick=lambda: approve_if_held(lance_ray, token, _bearer(alice)))

    # The quality gate ran on real compute output and recorded its verdict on the WROTE edge.
    silver = _producer_for(lineage, alice, _ds("silver$features"), rids["embed_features"])
    assert silver is not None
    assert silver["row_count"], f"no measured rows — is medallion.compute on? {silver}"
    assert silver["quality_passed"] is True, silver
    # Batch 21 (DATA-CONTRACT.md declared-columns clause): the demo movers DECLARE consumer
    # dependencies (requiredColumns: id on the tabular stages), so the silver run carries a
    # column_declared verdict per declared column alongside the compute-quality pair.
    assert {a["assertion"] for a in silver["quality_assertions"]} == {
        "row_count_positive",
        "not_null",
        "column_declared",
    }
    declared = [a for a in silver["quality_assertions"] if a["assertion"] == "column_declared"]
    assert {(a["column"], a["success"]) for a in declared} == {("id", True)}, declared

    # Governance is live in the SAME stack: anonymous read 401s; an ungranted user 403s on the route gate.
    assert requests.get(f"{lineage}/runs", timeout=8).status_code == 401
    # THE OUTSIDER IS ASKED FOR, NOT NAMED. `bob@example.com` is not one on this estate: `team:eng` is
    # bound to `project:acme` and `project.admin` is "[user, role#assignee] or member from team", so a
    # team member IS a project admin and `can_get_metadata` on every `acme-*` object is genuinely
    # True. `topology.OUTSIDER` already carries this — it fixed three suites on 2026-09-06 and this was
    # the fourth, asserting a 403 that the model correctly refuses to give.
    outsider = {"Authorization": f"Bearer {_token(OUTSIDER)}"}
    denied = requests.get(f"{lineage}/datasets/{_ds('gold$catalog')}/upstream", headers=outsider, timeout=8)
    assert denied.status_code == 403, denied.text


# --------------------------------------------------------------------------- #
# 2. FGA-deny → DROP, live: revoke a WRITER rung (bronze→silver) and the VALIDATOR rung
#    (silver→gold) in turn — each deny stops the cascade at exactly its stage
# --------------------------------------------------------------------------- #


def test_fga_deny_drops_promotion_and_regrant_restores(stack: tuple[str, str], alice: dict[str, str], fga_store: tuple[str, str]) -> None:
    lance_ray, lineage = stack

    # -- sub-phase A: WRITER-gate deny — revoke the bronze→silver mover's writer rung. The cascade must
    # land bronze (the producer's own ingest, ungated by the mover rung — R23) and stop there: silver's
    # run never lands. This was the audit's untested half — only the validator (can_promote) deny was
    # ever proven.
    silver_owner = _owner_tuples("user:service-bronze-to-silver", _ds("silver"), _ds("silver$features"))
    _tuples(fga_store, deletes=[SILVER_WRITER, *silver_owner])
    try:
        w_token = _produce(lance_ray)
        bronze_rid = _run_id_for("lance_ray_ingest", w_token)
        denied_silver_rid = _run_id_for("embed_features", w_token)
        _poll(
            lambda: _run_states(lineage, alice).get(bronze_rid) == "COMPLETE",
            message=f"bronze never completed for writer-deny-drive token {w_token}",
            on_tick=lambda: approve_if_held(lance_ray, w_token, _bearer(alice)),
        )
        # The silver trigger publishes at bronze COMPLETE — stamp the moment the redelivery clock starts.
        silver_denied_at = time.monotonic()
        # First look at the negative (the definitive still-absent re-check comes after the positive
        # control below, once the redelivery window has MEASURABLY elapsed).
        time.sleep(12)
        # NOT COMPLETE, rather than absent. With mover-side FGA on (`medallion.fgaEnabled`, this
        # suite's own documented precondition) a denied stage EMITS a FAIL run instead of vanishing —
        # the same rule the quality gate follows, "the failed run is still emitted, auditable in
        # lineage". Measured 2026-08-25: state FAIL where this asserted None. The property the deny
        # actually has is that the stage never SUCCEEDS, and an absent run satisfies it too.
        denied_state = _run_states(lineage, alice).get(denied_silver_rid)
        assert denied_state != "COMPLETE", f"silver run {denied_silver_rid} COMPLETED despite the revoked writer tuple — gate NOT enforcing"
    finally:
        _tuples(fga_store, writes=[SILVER_WRITER, *silver_owner])  # restore even if the assert above fails

    # -- sub-phase B: VALIDATOR deny — revoke the gold validator, the cascade stops at silver.
    gold_owner = _owner_tuples("user:service-silver-to-gold", _ds("gold"), _ds("gold$catalog"))
    _tuples(fga_store, deletes=[GOLD_VALIDATOR, *gold_owner])
    try:
        gold_before = _quiesced_gold(lineage, alice)
        token = _produce(lance_ray)
        silver_rid = _run_id_for("embed_features", token)

        # The cascade reaches silver (writers restored/granted) …
        _poll(
            lambda: _run_states(lineage, alice).get(silver_rid) == "COMPLETE",
            message=f"silver never completed for deny-drive token {token}",
            on_tick=lambda: approve_if_held(lance_ray, token, _bearer(alice)),
        )
        # The gold trigger publishes at silver COMPLETE — the second redelivery clock starts here.
        gold_denied_at = time.monotonic()
        # … and the silver→gold mover, denied can_promote, DROPs BEFORE any emit: NO new gold run lands.
        time.sleep(12)
        assert _gold_runs(lineage, alice) == gold_before, (
            f"a new gold run appeared despite the revoked validator tuple — gate NOT enforcing (new: {_gold_runs(lineage, alice) - gold_before})"
        )
    finally:
        _tuples(fga_store, writes=[GOLD_VALIDATOR, *gold_owner])  # restore even if the assert above fails

    # Positive control: with both tuples back, the next drive cascades to gold — the tuple was the only
    # delta each time.
    gold_before_regrant = _gold_runs(lineage, alice)
    token2 = _produce(lance_ray)
    _poll(
        lambda: _gold_runs(lineage, alice) - gold_before_regrant != set(),
        message=f"no new gold run after re-granting the writer + validator (token {token2})",
        on_tick=lambda: approve_if_held(lance_ray, token2, _bearer(alice)),
    )

    # Re-assert the negatives only after BOTH denied triggers' redelivery windows have MEASURABLY
    # elapsed (grants restored the whole time) — on a fast/warm stack the positive control alone can
    # finish inside 30s, which would leave the exact false-pass window this re-check exists to close.
    # This is what separates "checked-and-DENIED" (DROP acked the trigger; the run can never appear) from
    # "never checked" (a crashed handler RETRYs; the redelivered trigger would sail through the restored
    # grant and land the run late).
    remaining = max(silver_denied_at, gold_denied_at) + REDELIVERY_WINDOW + 5 - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    final_states = _run_states(lineage, alice)
    # Same inversion as above: a denied stage is auditable (FAIL), not invisible. What must never
    # happen is that it COMPLETES once the grant is back — that would mean the trigger was retried
    # rather than checked-and-refused.
    assert final_states.get(denied_silver_rid) != "COMPLETE", (
        f"silver run {denied_silver_rid} COMPLETED after the writer grant was restored — the deny was a RETRY (never actually checked), not a refusal"
    )
    # The gold negative re-checked the same way: the deny window must have added NO gold run, even now
    # that the grant is back. A DROP acked the trigger, so it can never reappear; a RETRY would.
    # The gold negative, re-checked once the redelivery window has MEASURABLY elapsed. The comparison
    # isolates the deny window on purpose: `gold_before_regrant` is the snapshot taken AFTER the deny
    # and BEFORE the positive control, so the control's own (legitimate) gold run cannot mask a late
    # arrival from the denied drive. A DROP acked the trigger and can never reappear; a RETRY would.
    assert gold_before_regrant == gold_before, (
        f"a gold run from the DENIED drive landed after its window — the deny was a RETRY (never actually "
        f"checked), not a DROP (late: {gold_before_regrant - gold_before})"
    )


# --------------------------------------------------------------------------- #
# 3. quality-block, live: a bad batch derives into silver, is recorded, and never reaches gold
# --------------------------------------------------------------------------- #


def test_quality_gate_blocks_bad_batch_and_records_verdict(stack: tuple[str, str], alice: dict[str, str]) -> None:
    if not (MOVER_URL and S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY):
        pytest.skip("set LANCE_E2E_MOVER_URL + LANCE_E2E_S3_* for the quality-block drive")
    import lance
    import pyarrow as pa

    lance_ray, lineage = stack
    opts = {
        "endpoint": S3_ENDPOINT,
        "access_key_id": S3_ACCESS_KEY,
        "secret_access_key": S3_SECRET_KEY,
        "region": "us-east-1",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }
    described = requests.post(f"{CATALOG}/v1/table/{_ds('bronze$events')}/describe", json={}, headers=alice, timeout=20)
    assert described.status_code == 200, f"the catalog would not say where {_ds('bronze$events')} lives: {described.text}"
    bronze_uri = described.json()["location"]

    # Order-independence: this test corrupts bronze IN PLACE, so bronze must exist first. Earlier tests
    # usually leave one behind, but don't depend on execution order (or on -k selections) — drive a
    # produce and wait for bronze if it isn't there. One open serves both the guard and the read.
    def _open_bronze() -> Any:
        try:
            return lance.dataset(bronze_uri, storage_options=opts)
        except Exception:
            return None

    dataset = _open_bronze()
    if dataset is None:
        _produce(lance_ray)
        _poll(
            lambda: _open_bronze() is not None,
            message=f"bronze never materialized at {bronze_uri} for the quality-block test",
        )
        dataset = _open_bronze()

    # Corrupt bronze IN PLACE (same schema, ids nulled) — the shape of an upstream writer landing a bad
    # batch that lineage-side governance can't see coming.
    table = dataset.to_table()
    id_field = table.schema.field("id")
    bad = table.set_column(table.schema.get_field_index("id"), id_field, pa.nulls(len(table), id_field.type))
    lance.write_dataset(bad, bronze_uri, mode="overwrite", storage_options=opts)

    # Deliver the stage trigger exactly as the sidecar would (same route, same app-token guard).
    token = uuid.uuid4().hex[:12]
    resp = requests.post(
        f"{MOVER_URL.rstrip('/')}/medallion-event",
        # BARE ids PLUS `project`, which is the shape `publication_trigger` actually publishes:
        # `accepted_input_names` compares `dataset` against the mover's own `MEDALLION_FROM_DATASET`
        # (`bronze$events`, unqualified), and `_qualified` re-applies the project at RUNTIME from the
        # separate field. Sent qualified and project-less, the mover answered `medallion_stage_other_lane`
        # — a ROUTING drop, which `_DROP` renders identically to a governance block, so the assertion
        # below passed on a trigger that never reached the quality gate at all.
        # `from_uri` NAMES THE UPSTREAM, which is rule I2 and what `publication_trigger` really sends.
        # Without it the mover COMPOSES `{project root}/medallion/bronze`, while the catalog vends the
        # flat `{project root}/<hash>_<ns>$<name>` this leg just corrupted — two different datasets, so
        # the gate read clean rows and answered SUCCESS on a batch the suite believed it had poisoned.
        # `_confine_from_uri` accepts it because the vended location sits inside the project's own
        # warehouse root, which is exactly the containment that makes I2 safe.
        json={
            "data": {
                "token": token,
                "dataset": "bronze$events",
                "namespace": "bronze",
                "project": PROJECT,
                "from_uri": bronze_uri,
            }
        },
        headers={"dapr-api-token": MOVER_TOKEN},
        timeout=180,
    )
    # DROP is the ack a HELD promotion gets (`_QUALITY_BLOCKED`), and `_DROP` renders every other
    # refusal reason identically — a routing drop included. So this status alone proves nothing; the
    # verdict poll below is the real assertion, and this only catches the hop never running at all.
    assert resp.status_code == 200 and resp.json()["status"] == "DROP", resp.text

    # The blocked batch is fully auditable in lineage: the run COMPLETEd (the write happened), the gate
    # verdict rides the WROTE edge — quality_passed false with the failed not_null(id) assertion.
    silver_rid = _run_id_for("embed_features", token)
    _poll(
        lambda: (_producer_for(lineage, alice, _ds("silver$features"), silver_rid) or {}).get("quality_passed") is False,
        message=f"blocked-batch verdict never appeared on silver$features for token {token}",
    )
    entry = _producer_for(lineage, alice, _ds("silver$features"), silver_rid)
    assert entry is not None
    not_null = next(a for a in entry["quality_assertions"] if a["assertion"] == "not_null")
    assert not_null["success"] is False and not_null["column"] == "id"

    # And the bad batch never promoted: no gold run for this token (grace period for the negative).
    time.sleep(12)
    assert _run_states(lineage, alice).get(_run_id_for("aggregate_gold", token)) is None

    # Restore: a fresh /produce overwrites the corrupted bronze and cascades clean data through to gold.
    token2 = _produce(lance_ray)
    gold2 = _run_id_for("aggregate_gold", token2)
    _poll(
        lambda: _run_states(lineage, alice).get(gold2) == "COMPLETE",
        message=f"cascade did not recover after the quality-block drive (token {token2})",
    )


# --------------------------------------------------------------------------- #
# 4. MEDIA lane under governance — blobs land + artifacts derive with the seeded grants
# --------------------------------------------------------------------------- #


def test_media_lane_derives_under_governance(stack: tuple[str, str], alice: dict[str, str]) -> None:
    lance_ray, lineage = stack
    resp = requests.post(
        f"{lance_ray}/ingest-media",
        headers={"dapr-api-token": DAPR_TOKEN, "Idempotency-Key": _idem("gu-media")},
        timeout=60,
    )
    if resp.status_code == 409:
        pytest.skip("media head not configured (medallion.compute off on this stack)")
    assert resp.status_code == 202, resp.text
    token = resp.json()["token"]

    # project="" — the media lane is estate-only, not project-qualified. `seed_medallion_fga.sh` says
    # so outright ("media lanes stay estate-only — #84 scope") and the deployed mover targets
    # `silver-media$features` verbatim, so its runs carry no project to seed with.
    ingest_rid = _run_id_for("ingest_media", token, project="")
    derive_rid = _run_id_for("derive_media", token, project="")
    _poll(
        lambda: _run_states(lineage, alice).get(ingest_rid) == "COMPLETE" and _run_states(lineage, alice).get(derive_rid) == "COMPLETE",
        message=f"governed media lane did not flow for token {token}",
    )

    # Derived artifacts, read back through the GOVERNED schema endpoint (alice's grant, not an open route).
    schema = requests.get(f"{lineage}/datasets/silver-media$features/schema", headers=alice, timeout=8)
    schema.raise_for_status()
    fields = {f["name"]: f["type"] for f in schema.json().get("fields", [])}
    assert "thumbnail" in fields and "embedding" in fields, fields
    assert fields["payload"] == "blob"

    # Provenance under governance: silver-media ← bronze-media (both granted via the seeded parent links).
    upstream = requests.get(f"{lineage}/datasets/silver-media$features/upstream", headers=alice, timeout=8)
    upstream.raise_for_status()
    assert "bronze-media$objects" in {d["name"] for d in upstream.json().get("related", [])}
    # The external s3:// SOURCE objects are recorded in the graph (the auth-off media e2e —
    # tests/e2e-py/test_media_e2e.py via `make e2e-media` — asserts their PRESENCE, which is what keeps
    # this negative non-vacuous) but alice holds no grant on them — the transitive-disclosure filter
    # must DROP them from her governed view rather than leak external-source names through a
    # related-datasets side channel.
    #
    # NO per-object positive control is possible here, BY CONSTRUCTION (2026-07-10 review, verified
    # against OpenFGA's tuple validation): an OpenFGA object id must contain exactly one ':', so
    # `table:s3://…` can never be written as a tuple — s3:// source datasets are structurally
    # ungovernable-per-object and therefore invisible to EVERY governed principal. That is the
    # contract; making sources governable would need an id-encoding scheme or a `namespace:source`
    # parent with encoded ids (logged in docs/GOAL-prove-it.md as the open design decision).
    sources = requests.get(f"{lineage}/datasets/bronze-media$objects/upstream", headers=alice, timeout=8)
    sources.raise_for_status()
    assert not any(d["name"].startswith("s3://") for d in sources.json().get("related", []))

    # An ungranted user cannot see any of it — the media estate is governed like the rest.
    outsider = {"Authorization": f"Bearer {_token(OUTSIDER)}"}
    assert requests.get(f"{lineage}/datasets/silver-media$features/schema", headers=outsider, timeout=8).status_code == 403


# --------------------------------------------------------------------------- #
# 5. the TRAIN lane under governance (#115) — the credential regression guard
# --------------------------------------------------------------------------- #


def test_train_lineage_lands_attributed_under_governance(stack: tuple[str, str], alice: dict[str, str]) -> None:
    """A governed training run's SELF-EMITTED lineage must land, attributed to ``service-trainer``.

    This is the never-driven union bug this suite exists for: the Ray train job has no Dapr sidecar, so
    it POSTs the lineage HTTP ingest — which under ``auth.enabled`` 401'd every RunEvent, silently
    losing ALL training provenance in the shipped governed stack (live 2026-07-13). The service-door
    credential closed it: the job authenticates as ``service-trainer`` (the app token + its bare FGA
    subject), is stamped as author, and is FGA-checked on ``namespace:models``. Before that fix this
    poll would ALWAYS time out (the run never lands), so the poll IS the regression guard.
    """
    lance_ray, lineage = stack
    resp = requests.post(
        f"{lance_ray}/train",
        headers={"dapr-api-token": DAPR_TOKEN, "content-type": "application/json", "Idempotency-Key": _idem("gu-train")},
        # UNQUALIFIED on purpose. The train door resolves a feature dataset by CONVENTION, not through
        # the catalog: `stage_uri_for` maps `silver$features` -> `<base>/medallion/silver` and its own
        # docstring calls that demo-tier, noting "a catalog-registered feature table would resolve
        # through describe instead (future #115 work)". A project-qualified name therefore points at
        # `<base>/medallion/<project>-silver`, which nothing writes, and the door answers
        # 422 `cannot resolve feature dataset`. Qualifying here broke a passing test; the door, not the
        # suite, is what would have to change.
        json={"model": "churn", "features": [{"dataset": "silver$features"}]},
        timeout=30,
    )
    if resp.status_code == 409:
        pytest.skip("train head not configured (needs medallion.ray + a running Ray cluster)")
    assert resp.status_code == 202, resp.text
    token = resp.json()["token"]

    # The job self-emits START → RUNNING → COMPLETE over the HTTP ingest; correlate by the SAME
    # deterministic scheme the job uses (run_id_for("train-<token>")). Training is heavier than a stage
    # transform (Ray job cold-start + submit-and-ack), so allow a longer budget than the cascade polls.
    # project="" — the train door is driven WITHOUT a project (the request above names none), so its
    # run id is seeded the single-tenant way. `events.py` picks the seed shape from the RUN, not from
    # how this suite was invoked, and a NUL-bearing seed is unreachable from the `-`-joined one.
    train_rid = _run_id_for("train", token, project="")
    _poll(
        lambda: _run_states(lineage, alice).get(train_rid) == "COMPLETE",
        timeout=180.0,
        message=lambda: (
            f"governed training lineage did not land for token {token} "
            f"(state={_run_states(lineage, alice).get(train_rid)!r}) — the service-door credential "
            f"is what makes this land; a 401 here means it regressed"
        ),
    )

    # THE load-bearing assertion: the run is attributed to the bare FGA service subject, NOT a Dex sub
    # and NOT an unauthenticated blank. This is what proves the credential authenticated as the service
    # (and, by the ingest's output-authz, that service-trainer's rung permitted the models write).
    runs = requests.get(f"{lineage}/runs?limit=1000", headers=alice, timeout=8)
    runs.raise_for_status()
    train_run = next((r for r in runs.json().get("runs", []) if r["run_id"] == train_rid), None)
    assert train_run is not None, f"train run {train_rid} not visible to alice"
    assert train_run.get("author") == "service-trainer", train_run

    # The model node is governed like everything else: alice sees it (warehouse-reader cascades to
    # namespace:models via the seeded parent), an ungranted user does not.
    up = requests.get(f"{lineage}/datasets/models$churn/upstream", headers=alice, timeout=8)
    up.raise_for_status()
    # Unqualified for the same reason the request above is — the train door names what it resolved.
    assert "silver$features" in {d["name"] for d in up.json().get("related", [])}, up.json()
    outsider = {"Authorization": f"Bearer {_token(OUTSIDER)}"}
    assert requests.get(f"{lineage}/datasets/models$churn/upstream", headers=outsider, timeout=8).status_code == 403
