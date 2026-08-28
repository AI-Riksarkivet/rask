"""ffmpeg WAV transcode — pure compute, the media preparation the speaker encoder is fed.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/modalities/av/wav.py``, taken WHOLE. A COPY because the runner is sealed
(``requires-python >=3.10,<3.13``; platform packages are ``>=3.13``).

Model-free by definition (an external transcoder, no inference). The origin kept it out of any one
runner precisely so the speech runners could share it; with ratch gone each sealed runner carries
its own copy instead, and the reason it sits beside the model rather than inside it is unchanged:
diarize and voiceprint both feed their models 16 kHz mono WAV, and the transcode is the stage-side
preparation of the media, not the model. (The read plane keeps its own copy in
``services/viewer/src/viewer/services/audio_prep.py`` on purpose — the backend never imports a
runner, and there is no inference on either side of that line.)
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
        # S603 is suppressed on the next line: LIST-form argv, never `shell=True` — the safe form
        # the rule cannot distinguish from an unsafe one. The origin carried the same exemption as a
        # per-file-ignore on `ratch/modalities/av/*.py`; here it is local, because the root config's
        # `runners/**` row does not list S603 and severing must not require an edit outside this
        # runner to keep `ruff check .` green.
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s on {source}") from e
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg wav extraction failed (rc={proc.returncode}) on {source}: " + " | ".join(tail))
