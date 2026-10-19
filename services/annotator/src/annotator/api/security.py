"""OIDC authentication and the annotator's own checker.

Who the request is comes from the shared ``service_kit.governed.deps.make_auth_deps`` factory, so the annotator
cannot drift from the other governed services into a different answer to "who is this request".

What they may do is the annotator's to decide (``annotator.api.authorization``): its own project roles, and the
lakehouse's answers about the person on the person's own bearer. It holds no OpenFGA credential (ADR 0171).

**Why the annotator needs auth at all**: it writes per-subject state (projects, tasks, claims, drafts, roles). A
configured-but-broken layer fails closed: OIDC enabled with no verifier is a 503, and so is a checker with no catalog
to ask.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from annotator.api.authorization import ALLOW_ALL, AnnotatorChecker, CatalogAnswers, ProjectAccess
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.deps import ANONYMOUS_SUBJECT, FgaChecker, RawBearerToken, make_auth_deps
from service_kit.governed.person_identity import PersonPrincipal


__all__ = [
    "ANONYMOUS_SUBJECT",
    "Checker",
    "CheckerDep",
    "CurrentSubject",
    "CurrentToken",
    "RawBearerToken",
    "SettingsDep",
    "authenticate",
    "current_subject",
    "get_checker",
]

SettingsDep = Annotated[AnnotatorSettings, Depends(get_annotator_settings)]

_deps = make_auth_deps(SettingsDep)

# Re-exported as module-level names so the routes' `Depends(...)` and the tests' `dependency_overrides`
# key on the same objects the factory built.
authenticate = _deps.authenticate
current_subject = _deps.current_subject

CurrentToken = Annotated[PersonPrincipal | None, Depends(authenticate)]
CurrentSubject = Annotated[str, Depends(current_subject)]

#: The one question a route asks: may this caller do ``relation`` on ``obj``?
Checker = FgaChecker


async def _project_access(project_id: str, subject: str) -> ProjectAccess:
    """The project's own answer about ``subject``, from its actor. Lazy: the proxy opens a sidecar channel."""
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415 - deliberate
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415

    actor = typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)
    return ProjectAccess.model_validate(await actor.access({"subject": subject}))


def get_checker(request: Request, settings: SettingsDep, subject: CurrentSubject, bearer: RawBearerToken) -> Checker:
    """The annotator's checker for this request's caller: open with authentication off, the real decisions with it on."""
    if not settings.oidc_enabled:
        return ALLOW_ALL
    client = getattr(request.app.state, "catalog_http", None)
    if client is None or not settings.catalog_uri or not bearer:
        raise ServiceUnavailableError("authorization is enabled but the catalog it asks is unavailable")
    return AnnotatorChecker(subject=subject, catalog=CatalogAnswers(client, base_url=settings.catalog_uri, bearer=bearer), project_access=_project_access)


CheckerDep = Annotated[Checker, Depends(get_checker)]
