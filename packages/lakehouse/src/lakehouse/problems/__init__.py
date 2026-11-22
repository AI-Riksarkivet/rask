"""How a lakehouse service answers an error: the RFC 9457 problem body and the spec's error mapping onto it.

``envelope`` is the body every refusal carries (RFC 9457 plus the spec's ``code`` and ``error``) and its media type;
``ns_errors`` maps the Lance Namespace error hierarchy onto it and installs the handlers every service app shares;
``spec_routes`` is the committed set of routes the spec owns, which decides a validation failure's status and the error
responses the published schema declares; ``outage`` reads whether a failure was the object store not answering, which
the spec answers 503. The ``lance_namespace`` error hierarchy is the one a door raises, rask-only doors included,
and every service app installs these handlers (``lakehouse.service_app.factory``), so one body shape, with the spec's
``code``, holds across the catalog, lineage and maintenance (`lance_docs/ns_catalog/spec.yaml`, ``ErrorResponse``).

This module exports nothing, so importing the package loads none of it: every name is imported from the submodule that
defines it.
"""
