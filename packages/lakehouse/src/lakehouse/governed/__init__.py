"""The governed layer: the authentication, authorization and audit infrastructure every lakehouse service shares.

These modules (``secrets``, ``dapr_auth``, ``signing_key``, ``fga``, ``oidc``, ``audit``) are FastAPI-free (or
framework-light) infrastructure imported by the catalog, lineage and maintenance alike. They live in the library rather
than in a service so no service depends on another service's package: every service imports the shared pieces from
``lakehouse``, and ``lakehouse`` itself depends on none of the services.
"""
