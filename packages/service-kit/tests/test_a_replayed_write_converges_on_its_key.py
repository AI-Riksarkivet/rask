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
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.lakehouse.idempotency import InFlightError, Replay, claim, record_outcome, release


SCOPE = "acme"
KEY = "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e10"
ENDPOINT = "POST /v1/table/{id}/create"


def _root(tmp_path: Path) -> str:
    return str(tmp_path)


class TestTheFirstAttemptOwnsTheKey:
    def test_two_scopes_do_not_collide_on_one_key(self, tmp_path: Path) -> None:
        """A key is the CALLER's, so the same string from two tenants is two different operations."""
        assert claim(_root(tmp_path), {}, scope="acme", key=KEY, endpoint=ENDPOINT, now=1000.0) is None
        assert claim(_root(tmp_path), {}, scope="other", key=KEY, endpoint=ENDPOINT, now=1000.0) is None


class TestAnAttemptStillRunningIsNotAReplay:
    def test_reclaiming_does_not_lose_the_outcome_the_first_attempt_then_writes(self, tmp_path: Path) -> None:
        """The abandoned attempt may not be dead, only slow. Whichever finishes writes the outcome, and
        a later replay must see one — never a record still marked in-flight."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0, lease_seconds=60)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1061.0, lease_seconds=60)
        record_outcome(root, {}, scope=SCOPE, key=KEY, status=200, body={"ok": True})

        assert claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1100.0) == Replay(status=200, body={"ok": True})

    def test_a_release_by_an_attempt_whose_claim_was_taken_over_frees_nothing(self, tmp_path: Path) -> None:
        """[[LH-487]] An attempt releases only the claim it made: once another attempt has taken the key over, the
        first one's release leaves the key held, so a third attempt still waits."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0, lease_seconds=60)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1061.0, lease_seconds=60)

        release(root, {}, scope=SCOPE, key=KEY, claimed_at=1000.0)

        with pytest.raises(InFlightError):
            claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1062.0, lease_seconds=60)


class TestTheKeyIsNeverAPathTraversal:
    @pytest.mark.parametrize("bad", ["..", KEY.upper()])
    def test_a_key_that_is_not_a_canonical_uuid_is_refused(self, tmp_path: Path, bad: str) -> None:
        """The key becomes part of an object key, so an unvalidated one writes outside its own prefix. A canonical
        lower-case UUID is the one spelling that cannot. The doors constrain it at the header too; this refuses it
        at the seam, because a second caller of this module must not have to remember."""
        with pytest.raises(ValueError, match="idempotency key"):
            claim(_root(tmp_path), {}, scope=SCOPE, key=bad, endpoint=ENDPOINT, now=1000.0)
