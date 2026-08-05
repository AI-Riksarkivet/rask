"""The Serve seam of the governed HTR lane (#88 step 3): page bytes → ALTO, over plain HTTP.

The transcribe compute stays where the weights are warm — the Ray Serve ``/htrflow`` deployment.
What the medallion owns is GOVERNANCE, so this module is deliberately nothing but the boundary:
one POST, typed failures, no Ray import (the lane must run in the mover's slim image), and no
retries — the mover's pub/sub redelivery IS the retry loop, and a second retry layer here would
multiply attempts against a GPU endpoint that costs minutes per call.
"""

from __future__ import annotations

import httpx


class TranscribeError(RuntimeError):
    """The transcribe endpoint failed for one page — the mover fails the PAGE loudly.

    Carries enough to diagnose without re-driving the GPU: the status and the body's head. A
    half-transcribed page must never become a half-written gold row, so this always propagates.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"htrflow transcribe failed: HTTP {status} — {detail}")
        self.status = status


def transcribe_page(image_bytes: bytes, *, base_url: str, name: str = "page", timeout_seconds: float = 600.0) -> str:
    """One page's image bytes → its ALTO XML, via the deployed ``/htrflow`` ingress.

    ``base_url`` is ``MEDALLION_HTRFLOW_URL`` and must be set — an empty value raises here, at the
    seam, naming the env var, rather than letting httpx invent a connection error a reader has to
    decode. The body is the raw image (the ingress's contract: bytes in, ALTO out, no envelope);
    ``?name=`` becomes the ALTO's fileName, so the caller passes the page key and the document
    self-identifies.
    """
    if not base_url:
        raise TranscribeError(0, "MEDALLION_HTRFLOW_URL is not set — the HTR lane has no transcribe endpoint")
    with httpx.Client(base_url=base_url, timeout=timeout_seconds) as client:
        try:
            response = client.post("/", params={"name": name}, content=image_bytes)
        except httpx.HTTPError as exc:
            raise TranscribeError(0, f"transcribe endpoint unreachable: {exc}") from exc
    if response.status_code != 200:
        raise TranscribeError(response.status_code, response.text[:300])
    if not response.text.lstrip().startswith("<"):
        # A 200 that is not XML is a misrouted or half-configured endpoint (a proxy's HTML error
        # page, a JSON body) — parse_alto would refuse it anyway, but refusing HERE names the seam.
        raise TranscribeError(200, f"endpoint answered 200 but not XML: {response.text[:120]!r}")
    return response.text
