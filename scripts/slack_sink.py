"""A Slack incoming-webhook stand-in that COUNTS rather than merely accepting.

The duplicate is the thing under test — "does exactly ONE message leave the estate per
(event, subject, channel) when the broker redelivers?" — so the sink has to be able to say "this
arrived twice" without a human reading a log.

    GET    /deliveries   the ledger: every body seen, which arrived more than once, and the total
    POST   /             record a delivery (what the Dapr binding calls)
    DELETE /deliveries   reset between runs

Extracted from `.docker/docker-compose.notifications-channels.yml`, where it lived as an inline
`command:` heredoc, when that compose stack was replaced by `dagger call notifications-rig`. A
40-line HTTP server embedded in YAML is not reviewable, not lintable and not runnable on its own;
as a file it is all three, and the Dagger service just runs it.
"""

import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer


SEEN = Counter()


class H(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the base class names it `format`
        """Silence the per-request access log — the ledger is the output, not stderr."""

    def _send(self, code: int, body: dict[str, object]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
        SEEN[body.splitlines()[0] if body else "<empty>"] += 1
        self._send(200, {"ok": True})

    def do_GET(self) -> None:
        dupes = {k: v for k, v in SEEN.items() if v > 1}
        self._send(200, {"deliveries": dict(SEEN), "duplicates": dupes, "total": sum(SEEN.values())})

    def do_DELETE(self) -> None:
        SEEN.clear()
        self._send(200, {"cleared": True})


HTTPServer(("0.0.0.0", 9099), H).serve_forever()
