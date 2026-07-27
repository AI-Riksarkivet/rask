"""ffmpeg WAV transcode — pure compute, shared by the speech runners.

Model-free by definition (an external transcoder, no inference), so it lives in
``modalities/av`` with the other ffmpeg helpers (frames, thumbnails) rather than
in any runner: diarize and voiceprint both feed their models 16 kHz mono WAV,
and the transcode is the stage-side preparation of the media, not the model.
(The viewer keeps its own copy in ``services/viewer/services/wespeaker.py`` on
purpose — the backend never imports the pipeline package.)
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
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s on {source}") from e
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg wav extraction failed (rc={proc.returncode}) on {source}: " + " | ".join(tail))
