"""The lance-media zone app skeleton — ``MEDIA_*`` settings, the HTTPException-based DomainError
hierarchy, problem+json handlers, CORS-only middleware (Range-streaming safe), probes, app state and
DI wrappers. Ported whole from ``common.core`` + ``common.{state,deps}`` (gate 3, R19); env-var
convergence to ``RASK_*`` and DomainError unification are later gates, deliberately NOT done here.
"""
