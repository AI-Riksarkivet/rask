"""OIDC authentication + the FGA checker seam for the annotator.

The three governed dependencies — verify the bearer, name the subject, check one relation — come from
the shared ``service_kit.governed.deps.make_auth_deps`` factory, which was extracted FROM this module.
Building them here against ``AnnotatorSettings`` (rather than re-copying the bodies) means the
annotator cannot drift from the other governed services into a different answer to "who is this
request", and a widening of the contract lands in one place.

**Why the annotator needs auth at all**: it writes per-subject state (projects, tasks, claims,
drafts). The shared factory fails closed twice — OIDC enabled but no verifier on ``app.state`` → 503,
FGA enabled but no client → 503 — because a configured-but-broken auth layer must never degrade to
open access.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from annotator.core.config import AnnotatorSettings, get_annotator_settings
from service_kit.governed.deps import ANONYMOUS_SUBJECT, FgaChecker, RawBearerToken, make_auth_deps
from service_kit.governed.oidc import IDToken


__all__ = [
    "ANONYMOUS_SUBJECT",
    "CheckerDep",
    "CurrentSubject",
    "CurrentToken",
    "FgaChecker",
    "FgaClientDep",
    "RawBearerToken",
    "SettingsDep",
    "authenticate",
    "current_subject",
    "get_checker",
    "get_fga_client",
]

SettingsDep = Annotated[AnnotatorSettings, Depends(get_annotator_settings)]

_deps = make_auth_deps(SettingsDep)

# Re-exported as module-level names so the routes' `Depends(...)` and the tests' `dependency_overrides`
# key on the same objects the factory built.
authenticate = _deps.authenticate
current_subject = _deps.current_subject
get_checker = _deps.get_checker
get_fga_client = _deps.get_fga_client

CurrentToken = Annotated[IDToken | None, Depends(authenticate)]
CurrentSubject = Annotated[str, Depends(current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(get_checker)]
FgaClientDep = Annotated[object | None, Depends(get_fga_client)]
