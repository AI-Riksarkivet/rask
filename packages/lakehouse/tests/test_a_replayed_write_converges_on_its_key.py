"""A retried write must converge on the caller's key instead of re-executing.

THE DEFECT THIS CLOSES (§ Q17-32). A retrying client replays a write door's failure, and the
catalog's 91 write routes carried no idempotency key on any of them — so a 500 raised
AFTER the Lance write in `create_governed_table` (the schema read-back, `emit_create`, `emit_control`)
is replayed with nothing to converge it. With `mode=Create` the replays answer AlreadyExists, so a
fully successful create surfaces as a 409 nobody can tell from a name collision; with `mode=Overwrite`
each replay RE-EXECUTES the destructive path — drop and rewrite the dataset, strip and re-seed the
ACL, four `table_created` events for one logical create.

`python-infrastructure`'s rule offers exactly two outs — *"Always pair retry with an idempotency key
OR mark the operation non-retryable"* — and no Dapr policy replays a catalog call (no rask service
invokes the catalog through Dapr, and `dapr-resiliency.yaml` targets lineage alone). This is the first
out, for every client whose own retry the estate cannot see.

THE KEY IS OPTIONAL, AND THAT IS A SPEC CONSTRAINT RATHER THAN A COMPROMISE. The Lance Namespace spec
defines no `Idempotency-Key`, and `rask-lance-catalog` records that a stock Lance client must work with
no rask SDK — so requiring it would break conformance on 54 routed operations. A caller that sends one
gets convergence; one that does not is exactly where it was.

Driven on a real local control root. A rival lands between an attempt's read of the key and its write by running
inside that read, so the interleaving happens on every run; a claim stamped a day ago is one whose read of the store's
clock answered a day ago, the one double here, since a lease is timed on that clock alone ([[LH-576]]).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lakehouse import records
from lakehouse.idempotency import PREFIX, InFlightError, KeyBinding, Replay, claim, reclaim, record_outcome, release
from lakehouse.storage_options import StorageOptions


SO = StorageOptions()
SCOPE = "acme"
KEY = "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e10"
CREATE = KeyBinding(endpoint="POST /v1/table/{id}/create", target="db$t")
LIFETIME = timedelta(days=1)


def _claimed_a_day_ago(root: str, monkeypatch: pytest.MonkeyPatch, *, attempt: str) -> None:
    """``attempt`` claimed the key a day ago on the store's clock, and never finished."""
    read = records.read_json_at
    monkeypatch.setattr(records, "read_json_at", lambda *args: read(*args).model_copy(update={"at": datetime.now(UTC) - LIFETIME}))
    claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt=attempt)
    monkeypatch.setattr(records, "read_json_at", read)


class TestTheFirstAttemptOwnsTheKey:
    def test_two_scopes_do_not_collide_on_one_key(self, tmp_path: Path) -> None:
        """A key is the CALLER's, so the same string from two tenants is two different operations."""
        assert claim(str(tmp_path), SO, scope="acme", key=KEY, binding=CREATE, attempt="a") is None
        assert claim(str(tmp_path), SO, scope="other", key=KEY, binding=CREATE, attempt="b") is None


class TestAnAttemptStillRunningIsNotAReplay:
    def test_two_takeovers_of_an_expired_claim_run_the_write_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two retries arriving after the lease both find the claim abandoned; the store lets one take the key over,
        and the other, whose write follows a read the first one's takeover outdated, finds the key held."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="crashed")
        read = records.read_json_at
        rival: list[object] = []

        def _read_then_take_over(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
            found = read(root_uri, storage_options, key)
            if not rival:
                rival.append("taking over")
                rival[0] = claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="rival")
            return found

        monkeypatch.setattr(records, "read_json_at", _read_then_take_over)

        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="late")
        assert rival == [None], "the rival's takeover must run, alone"

    def test_reclaiming_does_not_lose_the_outcome_the_first_attempt_then_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The abandoned attempt may not be dead, only slow. Whichever finishes writes the outcome, and
        a later replay must see one — never a record still marked in-flight."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="slow")
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="retry")
        record_outcome(root, SO, scope=SCOPE, key=KEY, status=200, body={"ok": True})

        assert claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="third") == Replay(status=200, body={"ok": True})

    def test_a_release_by_an_attempt_whose_claim_was_taken_over_frees_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """[[LH-487]] An attempt releases only the claim it made: once another attempt has taken the key over, the
        first one's release leaves the key held, so a third attempt still waits."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="first")
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="second")

        release(root, SO, scope=SCOPE, key=KEY, attempt="first")

        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="third")


class TestARecordOutlivesItsLifetimeByNoReclaim:
    def test_a_takeover_landing_inside_the_reclaim_keeps_its_record(self, tmp_path: Path) -> None:
        """A record no attempt wrote for its lifetime is reclaimed, but a retry that takes its key over between the
        reclaim's read and its delete writes the record again: the reclaim leaves it, so the attempt it admitted still
        holds the key and a third retry waits rather than running the write again."""
        root = str(tmp_path)
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="released")
        release(root, SO, scope=SCOPE, key=KEY, attempt="released")
        record = Path(root, PREFIX, SCOPE, f"{KEY}.json")
        a_day_ago = (datetime.now(UTC) - LIFETIME - timedelta(minutes=1)).timestamp()
        os.utime(record, (a_day_ago, a_day_ago))
        read = records.read_json_at
        taken: list[object] = []

        def _read_then_take_over(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
            found = read(root_uri, storage_options, key)
            if not taken:
                taken.append("taking over")
                taken[0] = claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="retry")
            return found

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(records, "read_json_at", _read_then_take_over)
            reclaimed = reclaim(root, SO, lifetime=LIFETIME, limit=10)

        assert (reclaimed.reclaimed, reclaimed.kept, taken) == (0, 1, [None])
        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, attempt="third")


class TestTheKeyIsNeverAPathTraversal:
    @pytest.mark.parametrize("bad", ["..", KEY.upper()])
    def test_a_key_that_is_not_a_canonical_uuid_is_refused(self, tmp_path: Path, bad: str) -> None:
        """The key becomes part of an object key, so an unvalidated one writes outside its own prefix. A canonical
        lower-case UUID is the one spelling that cannot. The doors constrain it at the header too; this refuses it
        at the seam, because a second caller of this module must not have to remember."""
        with pytest.raises(ValueError, match="idempotency key"):
            claim(str(tmp_path), SO, scope=SCOPE, key=bad, binding=CREATE, attempt="a")
