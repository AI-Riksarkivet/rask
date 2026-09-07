"""A service run with authentication OFF refuses to start unless someone said so out loud.

Q17-6 / §F2-2. `oidc_enabled` defaults to False and `authenticate()` returns `None` when it is off, so
every route opens. The chart flips it on (`auth.enabled: true`, pinned by `test_invariants`), which
makes the DEPLOYED estate closed and the CODE open — and the gap between those two is a service run
any other way: a `uv run` on a laptop, a compose stack, a test harness someone copies into a job, an
image started outside the chart. Lakekeeper OSS has the identical shape and only Lakekeeper *Plus*
refuses to boot without an authenticator; §F2-2 asks for Plus's behaviour.

WHY A BOOT REFUSAL AND NOT A FLIPPED DEFAULT. Flipping `oidc_enabled` to True would change what an
explicitly-configured deployment does and would red every suite that constructs settings without
naming auth — 49 files touch it. The ambiguity is the actual defect: "auth off because I meant it"
and "auth off because nothing set it" are indistinguishable today, and only the second is a
vulnerability. So the refusal fires on that ambiguity alone, and an operator resolves it by saying
which one they meant.

MODELLED ON `assert_app_token_configured`, deliberately: same module, same shape, same reasoning —
fail closed at startup rather than serve an unauthenticated door, and no-op once the feature is
properly configured. A second shape for the same class of problem is how one of them ends up wrong.
"""

from __future__ import annotations

import pytest

from service_kit.governed.settings import assert_authentication_configured


def test_a_service_with_auth_ON_starts() -> None:
    """The governed deployment path, unchanged. Nothing here should make a configured estate think."""
    assert_authentication_configured(oidc_enabled=True, insecure_allow_unauthenticated=False)


def test_a_service_that_DECLARED_itself_open_starts() -> None:
    """The dev/demo shape stays available — it just has to be chosen rather than defaulted into."""
    assert_authentication_configured(oidc_enabled=False, insecure_allow_unauthenticated=True)


def test_the_AMBIGUOUS_case_refuses_to_start() -> None:
    """The defect: auth off with nobody having said so. Indistinguishable from a misconfiguration,
    and it serves every route to anyone who can reach the port."""
    with pytest.raises(RuntimeError) as excinfo:
        assert_authentication_configured(oidc_enabled=False, insecure_allow_unauthenticated=False)

    message = str(excinfo.value)
    assert "RASK_INSECURE_ALLOW_UNAUTHENTICATED" in message, "the refusal must name the escape, or an operator cannot act on it"
    assert "RASK_OIDC_ENABLED" in message, "the refusal must name the fix, not only the escape"


def test_declaring_BOTH_is_not_a_way_to_be_half_open() -> None:
    """`oidc_enabled` wins: the escape is an acknowledgement that authentication is off, not a switch
    that turns it off. A deployment that sets both is authenticated, and this must not read as a
    conflict an operator has to resolve."""
    assert_authentication_configured(oidc_enabled=True, insecure_allow_unauthenticated=True)
