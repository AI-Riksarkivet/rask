"""#4 — REAL crash injection: SIGKILL a producer between the Lance commit and the publish ack.

This is the test the outbox actually exists for, and it never existed. The prior e2e *simulated* a crash by
writing the staged object itself, then asserted the relay drained it — which proves the relay works but says
NOTHING about the half that can actually lose data: whether a producer that DIES mid-flight leaves a
recoverable event behind. A mocked exception is not a crash. A `with pytest.raises(TimeoutError)` unwinds
the stack, runs `finally` blocks, and flushes buffers; SIGKILL does none of that. The distinction is the
whole point of the feature.

What this drives, for real:
  1. a child process stages a full RunEvent to the object-store outbox — exactly as
     `publish_lineage_with_outbox` does, in the window AFTER the Lance commit,
  2. the OS kills it with SIGKILL (-9) before it can publish or drop — no cleanup, no finally, no flush,
  3. the staged object must SURVIVE on object storage (the durable copy is all that is left),
  4. the relay tick must drain it into lineage's durable `/events` feed,
  5. the outbox must end empty (the recovered object is removed, not left to re-ingest forever).

THE STAGED EVENT IS SIGNED, because an estate that enforces signing ([[LH-064]]) refuses and retires an event no listed signer's
key verifies, and an unsigned one is exactly that. The harness signs as the catalog, the one identity that may stamp a person as
the author, with the catalog's seed: `LANCE_E2E_SIGNING_SEED` when the stack script exports it, else read the way an operator
reads it, from the dev store's own `seed` container. The seed is never printed or logged. An estate that enforces nothing ignores
the signature, so signing is harmless there; where no seed can be read the event is staged unsigned, and an enforcing estate then
refuses it, which the drain assertion below names.

Skipped unless the live stack env is set. Run via `make e2e-ci` / the CI `e2e-stack` job.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import UTC, datetime

import pytest
import requests
from outbox_probe import bearer, owned_output_table, table_uuids

from catalog.core.lineage_emit import INSERT, InputRef, build_write_event
from lineage_kit.signing import SigningKey, attach_signature
from service_kit.governed.signing_key import signing_key_secret
from service_kit.lakehouse import outbox


LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
BINDING = os.environ.get("LANCE_E2E_RELAY_BINDING", "lineage-relay-cron")
OUTBOX_URI = os.environ.get("LANCE_E2E_OUTBOX_URI", "s3://lance-catalog/_lineage_outbox")
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900")
S3_ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
S3_SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")

#: The job namespace the catalog stamps on its own events (`LANCE_LINEAGE_JOB_NAMESPACE`, default `lance-catalog`).
#: The staged event is the one the catalog's insert announces, so it names the job the way the catalog does.
CATALOG_JOB_NAMESPACE = os.environ.get("LANCE_E2E_CATALOG_JOB_NAMESPACE", "lance-catalog")

#: WHO THE HARNESS SIGNS AS: the catalog, the one chart-listed identity that may stamp a person as the author. The release names the
#: OpenBao Deployment whose `seed` container holds the store's CLI token, for the case where the stack script exports no seed.
RELEASE = os.environ.get("LANCE_E2E_RELEASE", "rask")
#: The catalog's service-account subject in the release's namespace (`lance.saSubject`).
SIGNING_IDENTITY = os.environ.get(
    "LANCE_E2E_SIGNING_IDENTITY", f"kubernetes~system:serviceaccount:{os.environ.get('LANCE_E2E_NAMESPACE', 'default')}:{RELEASE}-sa-catalog"
)

#: The output this probe's staged event names. NAMESPACE AND TABLE SEPARATELY because the event needs
#: both spellings: `namespace` is the OpenLineage domain label and `table_id` carries the CATALOG ID,
#: which is `<namespace>$<table>`.
PROBE_NAMESPACE = "bronze"
PROBE_TABLE = "e2e_crash_ds"

pytestmark = pytest.mark.e2e


def _feed_rows(lineage: str, run_id: str) -> list[dict]:
    """The `/events` rows for ``run_id``, read through lineage's governed feed as the probe table's owner.

    RECOVERY, NOT DRAINAGE: a non-zero `outbox_drained` only says the relay counted the event, and the feed
    row is what a follower of `/events` acts on. Scoped to the probe's table, newest first, so a fresh run is
    on the first page.
    """
    token = bearer()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(f"{lineage}/events", params={"table": f"{PROBE_NAMESPACE}${PROBE_TABLE}", "limit": 500}, headers=headers, timeout=30)
    assert resp.status_code == 200, f"GET /events answered {resp.status_code}: {resp.text[:300]}"
    return [row for row in resp.json()["events"] if (row.get("event", {}).get("run") or {}).get("runId") == run_id]


def _signing_key() -> SigningKey | None:
    """The catalog's signing key, or None when this harness cannot read one.

    A SEED IS A SECRET, so nothing here prints it and a failed read says only that it failed. It is read as an operator reads it:
    from the environment when the stack script exported it, else from the dev store through the pod that holds the store's own
    token. An estate whose store this harness cannot reach holds no key for it, and the event is then staged unsigned.
    """
    seed = os.environ.get("LANCE_E2E_SIGNING_SEED", "").strip()
    kubectl = shutil.which("kubectl")
    if not seed and kubectl is not None:
        read_seed = [
            kubectl,
            "exec",
            f"deploy/{RELEASE}-openbao",
            "-c",
            "seed",
            "--",
            "bao",
            "kv",
            "get",
            "-field=seed",
            f"secret/{signing_key_secret(SIGNING_IDENTITY)}",
        ]
        try:
            seed = subprocess.run(read_seed, capture_output=True, text=True, check=True, timeout=60).stdout.strip()  # noqa: S603
        except (OSError, subprocess.SubprocessError):
            return None
    return SigningKey.from_seed(seed) if seed else None


def _so() -> dict[str, str]:
    return {
        "endpoint": S3,
        "access_key_id": S3_ACCESS_KEY,
        "secret_access_key": S3_SECRET_KEY,
        "region": "us-east-1",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


@pytest.fixture(scope="module")
def lineage() -> str:
    if not LINEAGE or not DAPR_TOKEN:
        pytest.skip("set LANCE_E2E_LINEAGE_URL + LANCE_E2E_DAPR_TOKEN (deployed stack, outbox enabled)")
    try:
        requests.get(f"{LINEAGE}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("lineage service not reachable")
    return LINEAGE


def _kill_producer_mid_flight(event_json: str, run_id: str) -> None:
    """Spawn a real producer, let it stage, then SIGKILL it before it can publish or drop.

    The child stages the event and then blocks forever. We kill it with -9 from the parent: the process dies
    with the staged object on disk and no publish — byte-for-byte the state a producer that crashed between its commit and its announcement leaves behind.
    """
    child = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.path.join(os.getcwd(), "services")!r})
        from service_kit.lakehouse import outbox
        outbox.stage_event({OUTBOX_URI!r}, {_so()!r}, {run_id!r}, {event_json!r})
        print("STAGED", flush=True)          # the commit→publish window is now OPEN
        time.sleep(300)                       # ...and we die inside it
    """)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        # wait for the child to confirm it staged (do not race the kill against the write)
        deadline = time.time() + 30
        assert proc.stdout is not None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "STAGED":
                break
            if proc.poll() is not None:
                raise AssertionError(f"producer died early: {proc.stderr.read() if proc.stderr else ''}")
        else:
            raise AssertionError("producer never staged the event")

        # SIGKILL — not SIGTERM. No handlers, no finally, no buffer flush. The real thing.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL, f"expected SIGKILL, got returncode {proc.returncode}"
    finally:
        if proc.poll() is None:  # never leak the child if an assertion blew up above
            proc.kill()
            proc.wait(timeout=5)


def _staged_keys_for(run_id: str) -> list[str]:
    """Every outbox object belonging to ONE run.

    `list_events` yields the object KEY, and since `_object_key` widened that key is
    ``<run_id>@<eventType>`` — one run stages a COMPLETE and a FAIL as separate objects precisely so
    the second cannot truncate the first. Asserting `run_id in dict(...)` therefore compares an id to
    a key and is false however well durability works: it reported a LOSS while the event sat in the
    outbox, which is the loudest false alarm this suite could raise. The bare form is still matched
    because `_object_key` falls back to it for a payload with no readable eventType.
    """
    return [key for key, _ in outbox.list_events(OUTBOX_URI, _so()) if key == run_id or key.startswith(f"{run_id}@")]


def _drive_the_relay(lineage: str, run_id: str, *, budget_seconds: float = 420.0, gap: float = 10.0) -> bool:
    """Wait for the relay to recover this run, nudging it along the way.

    THE CLAIM UNDER TEST IS THE OUTCOME, NOT THE CALLER. "A SIGKILLed producer loses nothing" is a
    statement about the event reaching the feed; whether this POST or the cron's own tick did the
    work is not a property the estate has or needs. `lineage-relay-cron` fires on every replica's
    sidecar and every step of a tick is idempotent, so another tick may drain this run first: this call
    then reports `outbox_drained: 0` and the empty outbox is what says the run left.

    A TIMEOUT IS NOT A FAILURE EITHER. A tick that drains a backlog can outlive the HTTP read timeout and
    keeps running server-side after the client gives up, so the outbox is what to watch.
    """
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        try:
            resp = requests.post(f"{lineage}/{BINDING}", headers={"dapr-api-token": DAPR_TOKEN}, timeout=gap)
            if resp.status_code == 200 and resp.json().get("outbox_drained", 0) >= 1:
                return True
        except requests.exceptions.ReadTimeout:
            pass  # the tick is still running server-side; the object is what to watch, not this socket
        if not _staged_keys_for(run_id):
            return True  # another tick drained it; §4b decides whether it landed where it had to
        time.sleep(gap)
    return False


def test_sigkilled_producer_loses_nothing(lineage: str) -> None:
    # A FRESH run id per invocation. A run id that repeated across runs would already be in the feed after
    # the first run, and "the relay recovered it into the feed" would then pass VACUOUSLY even if the relay
    # did nothing at all. The pre-flight assertion below is what catches that; keep both.
    run_id = str(uuid.uuid4())
    # THE AUTHOR MUST BE A SUBJECT AND THE OUTPUT MUST EXIST, or the relay can never drain what this
    # leg stages — `enforce_bus_authz` resolves the event's `author.sub` and asks whether it may write
    # the table the event names. A role LITERAL matches no subject and a table the catalog never
    # created carries no tuples, so an event with either is refused by every identity, for ever.
    #
    # Measured on the deployed estate 2026-09-20 before this fixture existed: five objects dated
    # 2026-09-14, one per run of this leg, all `author="e2e"` / `output_name="e2e_crash_ds"`, reported
    # as `refused` on every sweep and never drained. A leg that cannot pass is bad; a leg that leaves
    # permanent residue behind each time it cannot pass is worse.
    author = owned_output_table(PROBE_NAMESPACE, PROBE_TABLE)
    # The event the catalog's own insert announces, from the catalog's own builder, so the staged object
    # is the shape the relay drains in production rather than a hand-made look-alike.
    event = build_write_event(
        table_id=f"{PROBE_NAMESPACE}${PROBE_TABLE}",
        namespace=PROBE_NAMESPACE,
        # `"e2e"` only where authz is off — there is nothing to resolve against and demanding a token
        # would skip this leg on the open deployments it was written for.
        author=author or "e2e",
        version=1,
        operation=INSERT,
        run_id=run_id,
        event_time=datetime.now(UTC).isoformat(),
        job_namespace=CATALOG_JOB_NAMESPACE,
        # THE NAMESPACE IS THE EXTERNAL MARKER, and `"external"` is not one. `is_external_source`
        # exempts an input from `can_get_metadata` in exactly two forms — a namespace carrying a URI
        # scheme (`s3://…`), or one of the bare identifiers in `EXTERNAL_SOURCE_NAMESPACES`
        # (`{"file", "lance"}`). A namespace that is neither reads as a CATALOG namespace, so a
        # notional source outside the estate is authorized like a governed table and refused on an
        # object that can never carry a tuple. Measured live 2026-09-20:
        # `can_get_metadata required on inputs: e2e_crash_src`.
        inputs=[InputRef(namespace="s3://e2e-crash-src", name="e2e_crash_src", version=None)],
        # The table's uuids, as every event the catalog signs names its table by them: lineage refuses an output no
        # uuid names ([[LH-166]]), so without them the relay drains this event into a refusal and the feed never holds it.
        uuids=table_uuids(PROBE_NAMESPACE, PROBE_TABLE),
    )
    key = _signing_key()
    if key is not None:
        event = attach_signature(event, key=key, identity=SIGNING_IDENTITY, on_behalf_of=author or "e2e")
    event_json = json.dumps(event)

    # 1-2. a REAL producer stages, then the OS kills it dead inside the commit→publish window.
    _kill_producer_mid_flight(event_json, run_id)

    # 3. The durable copy is all that survived the kill. If this fails, #4 does not work at all.
    assert _staged_keys_for(run_id), "the SIGKILLed producer left NO staged event — the commit→publish loss window is still open"

    # Nothing may have reached the feed yet — the producer died BEFORE publishing. If the run is already
    # there, the test is not proving recovery (something else delivered it) and every assert below is vacuous.
    assert not _feed_rows(lineage, run_id), "the run reached the feed without the relay — test is vacuous"

    # 4. The relay recovers it.
    drained = _drive_the_relay(lineage, run_id)
    assert drained, (
        "the relay never drained this run within its budget, or an estate that enforces signing refused an event this harness "
        f"{'signed' if key is not None else 'could not sign (no seed for ' + SIGNING_IDENTITY + ': set LANCE_E2E_SIGNING_SEED)'}"
    )

    # 4b. ...into the feed. `outbox_drained` only proves the relay COUNTED it; the feed row is the recovery.
    assert _feed_rows(lineage, run_id), f"SIGKILLed producer's run {run_id} never reached the /events feed — its provenance was LOST"

    # 5. ...and cleans up, so the object cannot be re-ingested forever.
    assert not _staged_keys_for(run_id), "the relay ingested the event and left it staged — it will be re-ingested every tick"
