"""The shared read plane loaded a named model in-process, for one modality.

`viewer/api/v1/endpoints/voice.py::ensure_voice_encoder` said it outright: "Loads a model
**in-process** (pyannote's WeSpeaker encoder, ~30 s of import + weights)". That is a workload living
in a shared seam, and `runners/voiceprint` — "Speaker-embedding runner (WeSpeaker via pyannote)" —
already seals the SAME model. Two copies of one encoder, one of them inside the service every
modality's reads go through.

The rule it breaks is the estate's most load-bearing one: the platform knows no workload. A data
type must never enter a shared seam, and the test for every one of those is "would this be right for
audio?" — here the answer names audio, so the work belongs in a sealed runner.

WHAT THIS IS NOT. The GET forms (`/status`, `/similar`, `/identity`) read anchors from Lance and need
no encoder at all; they are descriptor-driven and stay. Only the UPLOAD form embeds, and only it
moves — behind the voiceprint runner's Ray Serve deployment, which is where long-lived model weights
already live in this estate ("model weights stay warm behind Ray Serve deployments the runner owns").

The cost is stated rather than hidden: the upload path gains a network hop and a dependency whose
absence is a 503 on that one endpoint. What it buys is a read plane with no model in it, one copy of
WeSpeaker instead of two, and an encoder that can use the cluster's GPUs instead of being pinned to
the viewer's CPU to avoid contending with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


VIEWER = Path(__file__).resolve().parents[1] / "src" / "viewer"


class TestNoINFERENCERunsHere:
    """The line is INFERENCE, not audio. `packages/ratch/modalities/av/wav.py` draws it and explains
    why the viewer keeps its own ffmpeg copy ON PURPOSE: the transcode is "model-free by definition
    (an external transcoder, no inference)" and "the backend never imports the pipeline package". So
    ffmpeg helpers and numpy math stay; a model does not."""

    def test_no_module_imports_a_model_framework(self) -> None:
        offenders = [
            p.relative_to(VIEWER).as_posix()
            for p in VIEWER.rglob("*.py")
            if "__pycache__" not in p.parts and any(f"import {name}" in p.read_text() or f"from {name}" in p.read_text() for name in ("torch", "pyannote"))
        ]
        assert offenders == [], (
            f"{offenders} import a model framework inside the shared read plane — runners/voiceprint "
            f"already seals the same WeSpeaker encoder, so this is a second copy in the wrong process"
        )

    def test_no_encoder_class_is_defined_here(self) -> None:
        offenders = [p.relative_to(VIEWER).as_posix() for p in VIEWER.rglob("*.py") if "__pycache__" not in p.parts and "class VoiceEncoder" in p.read_text()]
        assert offenders == [], f"{offenders} still define the encoder class"

    def test_no_in_process_encoder_is_constructed(self) -> None:
        voice = (VIEWER / "api" / "v1" / "endpoints" / "voice.py").read_text()
        assert "ensure_voice_encoder" not in voice, "the lazy in-process encoder loader is still here; the upload path must call the runner"

    def test_the_model_free_helpers_are_KEPT(self) -> None:
        """The opposite failure: deleting the ffmpeg transcode and the numpy math would trade one
        violation for a regression, and ratch's note says that duplication is deliberate."""
        helpers = "\n".join(p.read_text() for p in VIEWER.rglob("*.py") if "__pycache__" not in p.parts)
        for kept in ("extract_wav_16k_mono", "load_wav_16k_mono", "l2_normalize"):
            assert kept in helpers, f"{kept} is model-free media prep and must stay in the viewer"


class TestTheUploadPathCallsTheRunner:
    def test_it_reaches_serve_over_http(self) -> None:
        voice = (VIEWER / "api" / "v1" / "endpoints" / "voice.py").read_text()
        service = (VIEWER / "services" / "voice_service.py").read_text()
        assert "serve" in (voice + service).lower(), "nothing routes the upload embed to Ray Serve"

    def test_the_endpoint_is_configured_not_hardcoded(self) -> None:
        """A hard-coded URL does not survive multi-env deploys — `fastapi` references/anti-patterns.md."""
        config = (VIEWER / "core" / "config.py").read_text()
        assert "VOICEPRINT" in config.upper() or "voiceprint" in config, "the runner's Serve endpoint is not a setting, so it cannot differ per deployment"


class TestTheLanceAnchoredFormsAreUntouched:
    """Only the upload form moves. The GET forms read anchors FROM Lance — no encoder runs at query
    time — so breaking them would trade one violation for a regression."""

    @pytest.mark.parametrize("route", ["/status", "/similar", "/identity"])
    def test_the_route_still_exists(self, route: str) -> None:
        voice = (VIEWER / "api" / "v1" / "endpoints" / "voice.py").read_text()
        assert f'"{route}"' in voice
