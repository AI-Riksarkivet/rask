"""Wire-protocol stub for the media query encoders — a TEST FIXTURE, never a deployment.

`scripts/verify_encoder_wiring.sh` runs this in-cluster to prove one thing and one thing
only: that the chart's `encoders.embedUrl` / `encoders.rerankUrl` values are load-bearing —
setting them turns `/api/search?mode=semantic` from a 503 into a 200 with rows, through the
ingress, with no code change anywhere. That is the whole claim the chart change makes.

**It does not do semantic search, and nothing here should ever be pointed at by a real
deployment.** The vectors below are a deterministic hash of the request body, not an
embedding: they land nowhere near the corpus's Qwen3-VL-Embedding-2B space, so the rows a
query returns are its nearest neighbours to an arbitrary point — real rows, real Lance
vector index, meaningless ranking. Proving the *ranking* is good needs the real encoder
(`Qwen/Qwen3-VL-Embedding-2B` on vLLM, GPU); this proves the *plumbing*.

Deterministic rather than random on purpose: the same query yields the same vector, so a
re-run of the gate returns the same rows and a flake is a real regression. Stdlib only —
it runs inside the already-deployed service image off a ConfigMap, so the gate needs no
image build and no `kind load`.

The three routes are exactly what the clients in `services/search/services/encoders/` and
the viewer's `/api/health` probe call:

* ``GET  /health``        — `viewer/api/v1/endpoints/system.py` pings this; 200 = `embed.ok`.
* ``POST /v1/embeddings`` — `encoders/embedding.py`, reply ``{"data": [{"embedding": [...]}]}``.
* ``POST /v1/rerank``     — `encoders/reranker.py`, reply ``{"results": [{"index", "relevance_score"}]}``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


#: Matches the corpus descriptor's vector bindings (2048-dim Qwen3-VL-Embedding-2B). The
#: real client validates every reply against the descriptor's `dim`, so a wrong value here
#: surfaces as a 503 — which is the check working, not the stub failing.
DEFAULT_DIM = 2048


def pseudo_vector(seed: bytes, dim: int) -> list[float]:
    """A deterministic unit-ish vector derived from ``seed`` — NOT an embedding.

    SHAKE-256 gives us `dim` × 4 bytes from any input length, unpacked as uint32 and
    mapped to [-1, 1). The search client L2-normalizes whatever we return, so the scale
    is irrelevant; only the dimension and the determinism matter.
    """
    raw = hashlib.shake_256(seed).digest(dim * 4)
    return [(v / 2147483647.5) - 1.0 for v in struct.unpack(f"<{dim}I", raw)]


def pseudo_score(seed: bytes) -> float:
    """A deterministic relevance score in [0, 1] — NOT a cross-encoder judgement."""
    return int.from_bytes(hashlib.shake_256(seed).digest(4), "little") / 4294967295.0


class StubHandler(BaseHTTPRequestHandler):
    """The three routes the media services call on an encoder server."""

    dim: int = DEFAULT_DIM

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/health"):
            self._send(200, {"status": "ok", "stub": True})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:
        body = self._read_body()
        path = self.path.rstrip("/")
        if path.endswith("/v1/embeddings"):
            # One vector per call: the client posts each text/image as its own request and
            # reads data[0], fanning out over a thread pool.
            self._send(200, {"data": [{"embedding": pseudo_vector(body, self.dim)}]})
        elif path.endswith("/v1/rerank"):
            docs = json.loads(body or b"{}").get("documents") or []
            self._send(
                200,
                {"results": [{"index": i, "relevance_score": pseudo_score(d.encode())} for i, d in enumerate(docs)]},
            )
        else:
            self._send(404, {"detail": "not found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the base class's param name
        """Log to stdout so `kubectl logs` shows which routes the services actually hit."""
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    args = parser.parse_args()
    StubHandler.dim = args.dim
    print(f"encoder stub (NOT a real encoder) on :{args.port}, dim={args.dim}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), StubHandler).serve_forever()


if __name__ == "__main__":
    main()
