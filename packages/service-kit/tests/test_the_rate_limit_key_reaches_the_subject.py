"""`by_subject` must actually see a subject, or every caller shares one bucket.

THE DEFECT. `by_subject` reads `request.state.subject` and falls back to the client IP. Its docstring
explains why the subject is preferred — "keying by IP alone on an authenticated route is the corporate-
NAT defect" — and its unit test passes because it fabricates a request carrying `state.subject`.

NOTHING IN THE ESTATE EVER SET IT. `grep -rn "state.subject"` outside that read matches only tests, so
the subject branch was dead and every request fell to the IP branch. And these routes sit behind the
gateway, so the IP the service observes is the gateway pod, not the caller — one 30/min budget shared
by the entire estate. That is worse than no limiter: a single caller exhausts everyone's quota, which
is a denial-of-service the feature was added to prevent.

The fix belongs in `current_subject`, the dependency that already resolves the verified principal, so
every service on it gets a correct key rather than search alone. Publishing it on `request.state` is
also the only channel available: the limiter runs as a slowapi key function outside the dependency
graph and cannot ask for `CurrentSubject` itself.
"""

from __future__ import annotations

from types import SimpleNamespace

from service_kit.rate_limit import by_subject


class _Req:
    """Only what the key function reads."""

    def __init__(self, *, subject: str | None, host: str | None = "10.0.0.7") -> None:
        self.state = SimpleNamespace()
        if subject is not None:
            self.state.subject = subject
        self.client = SimpleNamespace(host=host) if host else None


def test_two_callers_behind_one_gateway_do_not_share_a_bucket() -> None:
    """THE HEADLINE: the same observed IP, two principals, two keys."""
    alice = by_subject(_Req(subject="alice", host="10.0.0.7"))
    bob = by_subject(_Req(subject="bob", host="10.0.0.7"))

    assert alice != bob, "two authenticated callers behind one gateway shared a rate-limit bucket"


def test_an_anonymous_caller_still_gets_metered() -> None:
    """The fallback is mandatory: presenting no credential must not buy an exemption."""
    key = by_subject(_Req(subject=None, host="10.0.0.7"))

    assert key.startswith("ip:"), "an anonymous caller must still be keyed, not un-metered"


def test_the_subject_dependency_publishes_what_the_limiter_reads() -> None:
    """The seam that was missing — proven against the REAL dependency, not a fabricated request.

    A test that builds its own `state.subject` proves the key function reads the attribute; it cannot
    prove anything ever writes it. That gap is the whole defect.
    """
    from service_kit.governed import deps

    source = (deps.__file__ or "").replace(".pyc", ".py")
    body = open(source).read()  # noqa: SIM115, PTH123 — reading our own module's source, not user input
    marker = body.split("def current_subject(", 1)[1].split("def ", 1)[0]

    assert "state.subject" in marker, (
        "`current_subject` resolves the verified principal and never publishes it to `request.state`, "
        "so `by_subject` can never see one and every caller falls to the shared IP bucket"
    )
