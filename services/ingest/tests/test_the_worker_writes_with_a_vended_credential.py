"""The worker's write must be signed by a SCOPED credential, and it must not expire mid-run.

`write_unit_fragments` can now take storage options (8cbab947) but the worker passed none, so every
client-direct write still signed with whatever ambient credential the pod holds — one key, whole
bucket, no expiry. The vending door hands out a credential scoped to one table prefix that expires in
900 seconds, and proven enforced on RustFS (a credential vended for one table is refused on another
with 403 AccessDenied).

TWO THINGS DECIDE THE SHAPE HERE, and they pull against each other:

* An ingest run is long and units are many — `ingest/queue.py` speaks of "millions of units" — so
  vending per unit would put a catalog round trip on every write.
* The credential EXPIRES. Vending once at the start and holding it means a run outliving the TTL
  starts failing halfway through, with a 403 that reads as a permission problem rather than an
  expiry.

So the credential is cached and refreshed before it expires, which is what `expires_at_millis` is
returned for. The provider is injected rather than constructed inside the worker (writing-python
`design-patterns` → Dependency injection: pass dependencies through constructors, contract as a
`Protocol`), so a test can drive expiry without a clock or a catalog.
"""

from __future__ import annotations

from typing import Any

from ingest.credentials import VendedCredentialCache


class _Vendor:
    """Counts vends so a test can prove caching, and can be told what to return."""

    def __init__(self, *answers: dict[str, str] | None) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self, *_a: Any, **_k: Any) -> dict[str, str] | None:
        self.calls += 1
        return self._answers[min(self.calls - 1, len(self._answers) - 1)]


def test_a_credential_is_vended_once_and_reused_until_it_nears_expiry() -> None:
    """A round trip per unit would put the catalog on the hot path of every write."""
    vendor = _Vendor({"access_key_id": "AK", "session_token": "T1"})
    cache = VendedCredentialCache(vendor, now=lambda: 1_000.0)

    first = cache.storage_options("ns", "ds", expires_at_millis=1_900_000)
    second = cache.storage_options("ns", "ds", expires_at_millis=1_900_000)

    assert first == second == {"access_key_id": "AK", "session_token": "T1"}
    assert vendor.calls == 1


def test_it_REFRESHES_before_the_credential_expires_rather_than_after() -> None:
    """Refreshing on failure would mean a 403 mid-run that reads as a permission problem, not an
    expiry — so the refresh has to happen while the credential is still valid."""
    vendor = _Vendor({"access_key_id": "AK", "session_token": "T1"}, {"access_key_id": "AK", "session_token": "T2"})
    clock = {"t": 1_000.0}
    cache = VendedCredentialCache(vendor, now=lambda: clock["t"])

    assert cache.storage_options("ns", "ds", expires_at_millis=1_900_000)["session_token"] == "T1"
    clock["t"] = 1_890.0  # inside the safety margin, still technically valid
    assert cache.storage_options("ns", "ds", expires_at_millis=1_900_000)["session_token"] == "T2"
    assert vendor.calls == 2


def test_each_table_gets_its_own_credential() -> None:
    """A credential scoped to one table prefix is useless for another — sharing one across tables
    would produce exactly the 403 the scoping is FOR."""
    vendor = _Vendor({"access_key_id": "A"}, {"access_key_id": "B"})
    cache = VendedCredentialCache(vendor, now=lambda: 0.0)

    a = cache.storage_options("ns", "one", expires_at_millis=9_000_000)
    b = cache.storage_options("ns", "two", expires_at_millis=9_000_000)
    assert a != b
    assert vendor.calls == 2


def test_a_vendor_that_offers_nothing_yields_none_and_is_not_retried_per_write() -> None:
    """`mode_b` vends nothing by design. Asking again on every single write would put a doomed round
    trip on the hot path of a deployment that has simply chosen server-mediated access."""
    vendor = _Vendor(None)
    cache = VendedCredentialCache(vendor, now=lambda: 0.0)

    assert cache.storage_options("ns", "ds", expires_at_millis=None) is None
    assert cache.storage_options("ns", "ds", expires_at_millis=None) is None
    assert vendor.calls == 1


def test_the_deadline_is_compared_on_the_STORE_S_clock_not_a_monotonic_one() -> None:
    """`expires_at_millis` is an epoch timestamp minted by the store, so the cache must compare it on
    the same scale.

    Written first against `time.monotonic`, which counts from an arbitrary per-process origin: on a
    freshly started pod `monotonic()` is near zero, so every credential looks valid for ~55 years, and
    on a long-lived one it exceeds any real epoch and every credential looks already expired. Neither
    failure is visible in a test that supplies both numbers on the same made-up scale, which is why
    this asserts the DEFAULT clock rather than an injected one.
    """
    import time as _time

    from ingest.credentials import VendedCredentialCache

    vendor = _Vendor({"access_key_id": "AK"})
    cache = VendedCredentialCache(vendor)  # no `now` — the production clock

    # A credential expiring 900s from now must be considered fresh; one that expired an hour ago must
    # not. Both are epoch-based, which only works if the cache's own clock is too.
    assert cache.storage_options("ns", "fresh", expires_at_millis=int((_time.time() + 900) * 1000)) is not None
    assert vendor.calls == 1
    cache.storage_options("ns", "stale", expires_at_millis=int((_time.time() - 3600) * 1000))
    cache.storage_options("ns", "stale", expires_at_millis=int((_time.time() - 3600) * 1000))
    assert vendor.calls == 3, "an expired credential must be re-vended, not held"
