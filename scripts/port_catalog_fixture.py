#!/usr/bin/env python
"""Serve the real catalog app for the Rust port's black-box tests, and print the line that points them at it.

The catalog is ``catalog.main:app`` under uvicorn, on real pylance over a ``dir`` namespace in a temporary directory,
with authentication and authorization off, which is how the Python integration tests serve it
(``tests/integration/lineage_app.py``). It creates the namespace ``lance_catalog$port``, which the Rust tests create
their tables in, prints ``RASK_PORT_CATALOG_URL=<url>``, and serves until it is stopped. Run::

    uv run scripts/port_catalog_fixture.py --port 8765
    cd rust && RASK_PORT_CATALOG_URL=http://127.0.0.1:8765 cargo test -p lakehouse-tables --test catalog
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
#: The namespace the Rust tests create their tables in.
NAMESPACE = "lance_catalog$port"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="the port to serve on; 0 takes a free one")
    port = parser.parse_args().port
    sys.path.insert(0, str(_ROOT / "services"))  # `uv run` doesn't apply the pytest pythonpath
    with tempfile.TemporaryDirectory(prefix="port-catalog-") as root:
        os.environ.update(
            {
                "LANCE_REST_IMPL": "dir",
                "LANCE_REST_ROOT": root,
                "LANCE_S3_ACCESS_KEY_ID": "test",
                "LANCE_S3_SECRET_ACCESS_KEY": "test",
                "RASK_OIDC_ENABLED": "false",
                "RASK_INSECURE_ALLOW_UNAUTHENTICATED": "true",
                "RASK_INSECURE_ALLOW_STATIC_STORE_KEY": "true",
                "LANCE_AUTO_MIGRATION": "false",
                "RASK_FGA_ENABLED": "false",
            }
        )
        import httpx
        import uvicorn

        from catalog.main import app

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 60
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise SystemExit("the catalog did not start")
            time.sleep(0.05)
        url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        created = httpx.post(f"{url}/v1/namespace/{NAMESPACE}/create", json={}, timeout=30)
        if created.status_code != 200:
            raise SystemExit(f"the namespace {NAMESPACE} was not created: {created.status_code} {created.text}")
        print(f"RASK_PORT_CATALOG_URL={url}", flush=True)
        try:
            thread.join()
        except KeyboardInterrupt:
            server.should_exit = True
            thread.join(timeout=30)


if __name__ == "__main__":
    main()
