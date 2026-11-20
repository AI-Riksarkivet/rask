"""The service app: the factory catalog, lineage and maintenance each boot from, and the layers only it installs.

``factory.build_service_app`` builds every lakehouse service's FastAPI app in one order; the other modules are
the layers a request passes through on the way in (``no_store``, ``panic_catcher``, ``deadline``, ``body_limit``) and
the gauges of the serving loop's thread pool (``thread_pool``). The services import this plane and nothing else in the
library does (`.importlinter`, ``the-service-app-is-imported-by-the-services-only``), so a layer here is installed only
by the factory.

This module exports nothing, so importing the package loads none of it: every name is imported from the submodule that
defines it.
"""
