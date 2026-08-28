"""ffmpeg WAV transcode for the sealed ``diarize`` runner — pure compute.

Vendored from ``ratch.modalities.av.wav`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, moves 10 and 11). It is a COPY rather than an import, and
that is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` for the cu128 torch
stack, while every platform package (``ratch``, ``service-kit``, ``ray-kit``) is
``>=3.13``. No platform package can be imported here at all, so a copy is the
only honest way to keep the transcode.

Model-free by definition (an external transcoder, no inference), so the origin
argued it belonged in ``modalities/av`` with the other ffmpeg helpers rather
than in any runner. The dissolution rules the other way: the transcode is the
stage-side preparation of the media, and that preparation belongs to whichever
runner feeds its model 16 kHz mono WAV. ``voiceprint`` needs the same bytes and
must take its OWN copy — it cannot import this one either, because a runner may
not import a sibling runner any more than it may import a platform package.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


#: Sample rate / channel count the speech models (pyannote, WeSpeaker) expect.
TARGET_SAMPLE_RATE: int = 16_000


def extract_wav_16k_mono(source: Path, dest: Path, *, timeout: float = 1800.0) -> None:
    """Transcode ``source`` to a 16 kHz mono WAV at ``dest`` via ffmpeg.

    Raises :class:`RuntimeError` with the ffmpeg stderr tail on failure.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        "1",
        "-f",
        "wav",
        str(dest),
    ]
    try:
        # LIST-form argv, never `shell=True` — the safe form S603 cannot distinguish from an
        # unsafe one. The exemption rides on the line rather than in a config row: the origin's
        # was `packages/ratch/src/ratch/modalities/av/*.py = ["S603"]` in the ROOT pyproject, and
        # that row dies with ratch. A sealed runner's fixes travel with its code.
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s on {source}") from e
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg wav extraction failed (rc={proc.returncode}) on {source}: " + " | ".join(tail))
