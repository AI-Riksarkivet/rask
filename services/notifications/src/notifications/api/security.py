"""Who is asking, and what they may see — the inbox door's authn/authz wiring.

**The subject is the token's `sub` and nothing else.** There is deliberately no header fallback in
`service_kit.governed.deps`, and this door adds none: an inbox is addressed by
`encode_subject(subject)`, so a fallback would not merely weaken authentication — it would let a
caller name somebody else's actor. Nothing in this service accepts a subject as a parameter.

With auth off (every knob defaults off) the subject is `anon` and the visibility filter is
pass-through, so a dev stack behaves exactly as it did before this module existed — one anonymous
inbox, which is the honest local-development shape rather than a special case in the code.

This module must not grow `from __future__ import annotations`. `make_auth_deps` annotates its inner
dependencies with the settings dep it is HANDED, and deferred annotations turn that into a string
FastAPI resolves against `deps.py`'s globals — where the name does not exist. The failure is not an
error: the parameter is silently demoted to a query param and every gated route answers 422 instead
of authorizing. It shipped that way once. `tests/unit/test_auth_deps_resolve.py` builds a bare app
with no overrides precisely to catch it.
"""

from typing import Annotated

from fastapi import Depends, Request

from notifications.api.visibility import Visibility
from notifications.dependencies import NotificationsSettingsDep
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(NotificationsSettingsDep)

#: The verified subject — the actor id this request's inbox is derived from.
CurrentSubject = Annotated[str, Depends(_deps.current_subject)]

#: A single-object `check`, for the one door that asks one: the watch gate (`project#member` on
#: `project:<id>`). Distinct from `VisibilityDep` below, which is a `batch_check` over a page — asking
#: "which of these may I see" one object at a time is the N-round-trip mistake `governed()` avoids.
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]


def get_visibility(request: Request, settings: NotificationsSettingsDep) -> Visibility:
    """The per-request view onto OpenFGA, built from the client the lifespan put on `app.state`.

    Deliberately NOT `make_auth_deps().get_checker`: that resolves a single-object `check`, and every
    authorization question this door asks is "which of these may I see" over a page of rows — one
    `batch_check` round-trip, never N checks. The three-outcome rule is unchanged and lives in
    `Visibility` itself.
    """
    return Visibility(client=getattr(request.app.state, "fga", None), enabled=settings.fga_enabled)


VisibilityDep = Annotated[Visibility, Depends(get_visibility)]
