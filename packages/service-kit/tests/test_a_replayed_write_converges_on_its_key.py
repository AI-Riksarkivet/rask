"""A retried write must converge on the caller's key instead of re-executing.

THE DEFECT THIS CLOSES (§ Q17-32). `dapr-resiliency.yaml` replays a write door's failure up to three
times, and the catalog's 91 write routes carry no idempotency key on any of them — so a 500 raised
AFTER the Lance write in `create_governed_table` (the schema read-back, `emit_create`, `emit_control`)
is replayed with nothing to converge it. With `mode=Create` the replays answer AlreadyExists, so a
fully successful create surfaces as a 409 nobody can tell from a name collision; with `mode=Overwrite`
each replay RE-EXECUTES the destructive path — drop and rewrite the dataset, strip and re-seed the
ACL, four `table_created` events for one logical create.

`python-infrastructure`'s rule offers exactly two outs — *"Always pair retry with an idempotency key
OR mark the operation non-retryable"* — and the estate has taken the second for the catalog already
(its app-id sits on `writeRetry`, so a bare 500 is no longer replayed). This is the first: the two
cover what the other misses, which is why the rule says do both.

THE KEY IS OPTIONAL, AND THAT IS A SPEC CONSTRAINT RATHER THAN A COMPROMISE. The Lance Namespace spec
defines no `Idempotency-Key`, and `rask-lance-catalog` records that a stock Lance client must work with
no rask SDK — so requiring it would break conformance on 54 routed operations. A caller that sends one
gets convergence; one that does not is exactly where it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.lakehouse.idempotency import InFlightError, KeyReusedError, Replay, claim, record_outcome


SCOPE = "acme"
KEY = "caller-supplied-key-1"
ENDPOINT = "POST /v1/table/{id}/create"


def _root(tmp_path: Path) -> str:
    return str(tmp_path)


class TestTheFirstAttemptOwnsTheKey:
    def test_a_fresh_key_returns_None_so_the_caller_proceeds(self, tmp_path: Path) -> None:
        assert claim(_root(tmp_path), {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0) is None

    def test_two_scopes_do_not_collide_on_one_key(self, tmp_path: Path) -> None:
        """A key is the CALLER's, so the same string from two tenants is two different operations."""
        assert claim(_root(tmp_path), {}, scope="acme", key=KEY, endpoint=ENDPOINT, now=1000.0) is None
        assert claim(_root(tmp_path), {}, scope="other", key=KEY, endpoint=ENDPOINT, now=1000.0) is None


class TestAReplayGetsTheFirstAttemptsAnswer:
    def test_the_stored_outcome_comes_back_instead_of_re_executing(self, tmp_path: Path) -> None:
        root = _root(tmp_path)
        assert claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0) is None
        record_outcome(root, {}, scope=SCOPE, key=KEY, status=200, body={"table": "acme$t", "version": 1})

        replay = claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1001.0)

        assert replay == Replay(status=200, body={"table": "acme$t", "version": 1})

    def test_a_FAILED_outcome_replays_too(self, tmp_path: Path) -> None:
        """The point is one execution, not one success. Replaying a 4xx the caller already earned is
        strictly better than re-running the door to earn it again."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0)
        record_outcome(root, {}, scope=SCOPE, key=KEY, status=409, body={"error": "exists"})

        assert claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1001.0) == Replay(status=409, body={"error": "exists"})


class TestTheKeyIsBoundToTHISOPERATION:
    def test_the_same_key_on_a_DIFFERENT_endpoint_is_refused_not_replayed(self, tmp_path: Path) -> None:
        """Replaying here would hand a `drop` caller a `create`'s response body — a wrong answer, not
        a slow one. The refusal names the mistake instead."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0)
        record_outcome(root, {}, scope=SCOPE, key=KEY, status=200, body={"ok": True})

        with pytest.raises(KeyReusedError):
            claim(root, {}, scope=SCOPE, key=KEY, endpoint="POST /v1/table/{id}/drop", now=1001.0)


class TestAnAttemptStillRunningIsNotAReplay:
    def test_a_concurrent_second_attempt_is_told_to_wait(self, tmp_path: Path) -> None:
        """The first attempt claimed the key and has not finished. Returning "no replay" would let it
        execute twice, and returning an empty replay would answer with a result nobody produced."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0)

        with pytest.raises(InFlightError):
            claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1001.0)

    def test_a_claim_ABANDONED_past_the_lease_is_reclaimable(self, tmp_path: Path) -> None:
        """A process that died between claiming and finishing must not wedge the key forever — that
        would turn a crash into a permanently unusable idempotency key for that caller."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0, lease_seconds=60)

        assert claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0 + 61, lease_seconds=60) is None

    def test_reclaiming_does_not_lose_the_outcome_the_first_attempt_then_writes(self, tmp_path: Path) -> None:
        """The abandoned attempt may not be dead, only slow. Whichever finishes writes the outcome, and
        a later replay must see one — never a record still marked in-flight."""
        root = _root(tmp_path)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1000.0, lease_seconds=60)
        claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1061.0, lease_seconds=60)
        record_outcome(root, {}, scope=SCOPE, key=KEY, status=200, body={"ok": True})

        assert claim(root, {}, scope=SCOPE, key=KEY, endpoint=ENDPOINT, now=1100.0) == Replay(status=200, body={"ok": True})


class TestTheKeyIsNeverAPathTraversal:
    @pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "..", "", "a" * 200])
    def test_a_key_that_is_not_a_plain_token_is_refused(self, tmp_path: Path, bad: str) -> None:
        """The key becomes part of an object key, so an unvalidated one writes outside its own prefix.
        The doors constrain it at the header too; this refuses it at the seam, because a second caller
        of this module must not have to remember."""
        with pytest.raises(ValueError, match="idempotency key"):
            claim(_root(tmp_path), {}, scope=SCOPE, key=bad, endpoint=ENDPOINT, now=1000.0)
