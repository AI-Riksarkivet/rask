"""The lance-media zone app skeleton — ``MEDIA_*`` settings, CORS-only middleware (Range-streaming
safe), app state and DI wrappers. Ported whole from ``common.core`` + ``common.{state,deps}``
(gate 3, R19); env-var convergence to ``RASK_*`` is a later gate, deliberately NOT done here.

The DomainError hierarchy + problem+json handlers moved OUT to :mod:`service_kit.exceptions` and
the probes to :mod:`service_kit.probes` — the "DomainError unification" gate this docstring used to
defer. Nothing media-specific was ever in them, and duplicate copies had already drifted.
"""
