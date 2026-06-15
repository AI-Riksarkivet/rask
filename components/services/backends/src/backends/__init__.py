"""Local microservice compositions over the viewer.

Each submodule (`core_api`, `search_api`, `volumes_api`, `ray_api`,
`orchestrator`, `gateway`) exposes a module-level `app` so it can be run as its
own process, e.g. `uvicorn backends.search_api.app:app --port 8802`. See
`docs/superpowers/specs/2026-06-15-local-microservices-design.md`.
"""
