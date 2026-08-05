"""The HTR lane's Serve seam (#88 step 3) — respx at the transport, per the testing canon.

What these pin: the wire contract (raw bytes out, ALTO text back, ``?name=`` carrying the page
key), and the failure posture — every non-ALTO outcome raises ``TranscribeError`` naming the seam,
because a half-transcribed page must never become a half-written gold row, and the mover's pub/sub
redelivery is the ONLY retry loop (a second one here would multiply minutes-long GPU calls).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from medallion.services.htr_transcribe import TranscribeError, transcribe_page


BASE = "http://serve.test/htrflow"
ALTO = '<?xml version="1.0"?><alto><Layout><Page WIDTH="1" HEIGHT="1"/></Layout></alto>'


@respx.mock
def test_the_wire_contract_bytes_out_alto_back() -> None:
    route = respx.post(f"{BASE}/").mock(return_value=httpx.Response(200, text=ALTO))

    result = transcribe_page(b"JPEGBYTES", base_url=BASE, name="A0060198/00012.jpg")

    assert result == ALTO
    request = route.calls.last.request
    assert request.content == b"JPEGBYTES"  # raw image, no envelope
    assert request.url.params["name"] == "A0060198/00012.jpg"  # the page self-identifies


@respx.mock
def test_a_serve_error_fails_the_page_loudly() -> None:
    respx.post(f"{BASE}/").mock(return_value=httpx.Response(500, text="replica died"))

    with pytest.raises(TranscribeError, match="HTTP 500.*replica died"):
        transcribe_page(b"x", base_url=BASE)


@respx.mock
def test_an_unreachable_endpoint_names_the_seam_not_a_socket() -> None:
    respx.post(f"{BASE}/").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TranscribeError, match="unreachable"):
        transcribe_page(b"x", base_url=BASE)


@respx.mock
def test_a_200_that_is_not_xml_is_refused_at_the_seam() -> None:
    """A proxy's HTML error page or a JSON body with status 200 is a MISROUTED endpoint —
    parse_alto would refuse it later, but refusing here names which boundary is wrong."""
    respx.post(f"{BASE}/").mock(return_value=httpx.Response(200, text='{"ok": true}'))

    with pytest.raises(TranscribeError, match="not XML"):
        transcribe_page(b"x", base_url=BASE)


def test_an_unset_url_raises_naming_the_env_var() -> None:
    """Fail at the seam with the fix in the message — not an httpx invented-connection error."""
    with pytest.raises(TranscribeError, match="MEDALLION_HTRFLOW_URL"):
        transcribe_page(b"x", base_url="")
