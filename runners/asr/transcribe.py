"""Thin wrapper around ``easytranscriber.pipelines.pipeline`` that produces the
JSON files ``ratch ingest`` reads.

Exposed as ``ratch transcribe …`` via :mod:`ratch.cli`. ``easytranscriber``
(and ``easyaligner``) are core dependencies but heavy (torch + pyannote), so we
import them lazily — keeping ``ratch --help`` and FTS-only use light — and
surface a clear error if the environment is somehow missing them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ratch.errors import RatchError


logger = logging.getLogger(__name__)

# Suitable (language → wav2vec2 emissions model) defaults. See also:
# https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py
DEFAULT_EMISSIONS_MODEL: dict[str, str] = {
    "sv": "KBLab/wav2vec2-large-voxrex-swedish",
    "en": "facebook/wav2vec2-base-960h",
}

# easyaligner ships Punkt tokenizers keyed by language name, not ISO code.
PUNKT_LANG: dict[str, str] = {"sv": "swedish", "en": "english"}


def run_transcribe(
    *,
    audio_dir: Path,
    language: str = "sv",
    model: str = "KBLab/kb-whisper-large",
    emissions_model: str | None = None,
    vad: str = "pyannote",
    backend: str = "ct2",
    device: str = "cuda",
    cache_dir: Path = Path("models"),
    output_root: Path = Path("output"),
    batch_size_features: int = 64,
    num_workers_features: int = 8,
    num_workers_files: int = 2,
    beam_size: int = 1,
    chunk_size: int = 30,
    alignment_strategy: str = "chunk",
) -> Path:
    """Run the full VAD → Whisper → emissions → forced-alignment pipeline.

    Returns the directory the final alignment JSONs were written to
    (``output_root/alignments``).

    ``chunk_size`` is the max VAD chunk length in seconds (smaller speech
    segments merge up to it); lower it for finer-grained chunks. With
    ``alignment_strategy="speech"`` each speech is instead split into fixed
    ``chunk_size``-second windows. Both pass straight through to easytranscriber.
    """
    try:
        from easyaligner.text import load_tokenizer  # type: ignore[import-not-found]
        from easytranscriber.pipelines import pipeline  # type: ignore[import-not-found]
        from easytranscriber.text.normalization import (  # type: ignore[import-not-found]
            text_normalizer,
        )
    except ImportError as e:
        raise RatchError(
            f"Could not import easytranscriber/easyaligner (core dependencies).\nReinstall the project environment with:  uv sync\n(underlying error: {e})"
        ) from e

    if not audio_dir.is_dir():
        raise RatchError(f"Audio directory not found: {audio_dir}")

    emissions_model = emissions_model or DEFAULT_EMISSIONS_MODEL.get(language, "facebook/wav2vec2-base-960h")
    tokenizer = load_tokenizer(PUNKT_LANG[language]) if language in PUNKT_LANG else None

    audio_files = sorted(f.name for f in audio_dir.iterdir() if f.is_file() and not f.name.startswith("."))
    if not audio_files:
        raise RatchError(f"No audio files found in {audio_dir}")

    logger.info(f"transcribing {len(audio_files)} file(s) from {audio_dir} with {model} ({language}, {backend}/{device})")

    pipeline(
        vad_model=vad,
        emissions_model=emissions_model,
        transcription_model=model,
        audio_paths=audio_files,
        audio_dir=str(audio_dir),
        backend=backend,
        language=language,
        tokenizer=tokenizer,
        text_normalizer_fn=text_normalizer,
        cache_dir=str(cache_dir),
        device=device,
        chunk_size=chunk_size,
        alignment_strategy=alignment_strategy,
        # Defaults are tuned for 8-12 GB consumer GPUs; on a 96 GB PRO 6000 a
        # features batch of 64 (~25 GB) gives ~3-5x the Whisper+wav2vec2 throughput.
        num_workers_files=num_workers_files,
        batch_size_features=batch_size_features,
        num_workers_features=num_workers_features,
        # beam_size=1 is ~3-5x faster than the default 5, negligible quality loss
        # on clean audio (press conferences, lectures, interviews).
        beam_size=beam_size,
        output_vad_dir=str(output_root / "vad"),
        output_transcriptions_dir=str(output_root / "transcriptions"),
        output_emissions_dir=str(output_root / "emissions"),
        output_alignments_dir=str(output_root / "alignments"),
    )

    out_dir = output_root / "alignments"
    logger.info("done — alignment JSONs written to %s/", out_dir)
    logger.info("  next: ratch ingest %s/*.json", out_dir)
    return out_dir
