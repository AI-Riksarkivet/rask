"""Transcription-stage commands: ``transcribe`` and ``detect-language``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ._app import app


@app.command("transcribe")
def cmd_transcribe(
    audio_dir: Annotated[Path, typer.Option("--audio-dir", exists=True, file_okay=False)],
    language: Annotated[str, typer.Option("--language", help="ISO-639-1 code (sv, en, …).")] = "sv",
    model: Annotated[str, typer.Option("--model")] = "KBLab/kb-whisper-large",
    emissions_model: Annotated[str | None, typer.Option("--emissions-model")] = None,
    vad: Annotated[str, typer.Option("--vad", help="pyannote or silero.")] = "pyannote",
    backend: Annotated[str, typer.Option("--backend", help="ct2 or hf.")] = "ct2",
    device: Annotated[str, typer.Option("--device")] = "cuda",
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("models"),
    output_root: Annotated[Path, typer.Option("--output-root")] = Path("output"),
    batch_size_features: Annotated[
        int,
        typer.Option(
            "--batch-size-features",
            help="Batch size for Whisper/wav2vec2 inference. 64 fits ~25 GB on a 96 GB GPU.",
        ),
    ] = 64,
    num_workers_features: Annotated[int, typer.Option("--num-workers-features")] = 8,
    num_workers_files: Annotated[int, typer.Option("--num-workers-files")] = 2,
    beam_size: Annotated[
        int,
        typer.Option(
            "--beam-size",
            help=(
                "Whisper beam size. 1 is ~3-5× faster than the default 5 "
                "with negligible quality loss on clean audio. Bump to 5 if "
                "you see obviously garbled transcripts."
            ),
        ),
    ] = 1,
    chunk_size: Annotated[
        int,
        typer.Option(
            "--chunk-size",
            help="Max VAD chunk length in seconds. Lower → finer-grained chunks (default 30).",
        ),
    ] = 30,
    alignment_strategy: Annotated[
        str,
        typer.Option(
            "--alignment-strategy",
            help="'chunk' uses VAD segments; 'speech' splits each speech into fixed chunk-size windows.",
        ),
    ] = "chunk",
) -> None:
    """Run easytranscriber on a directory of audio/video files → alignment JSONs."""
    # Lazy import — the `[transcribe]` extra is optional.
    from runners.asr.transcribe import run_transcribe

    if vad not in {"pyannote", "silero"}:
        raise typer.BadParameter("--vad must be 'pyannote' or 'silero'")
    if backend not in {"ct2", "hf"}:
        raise typer.BadParameter("--backend must be 'ct2' or 'hf'")
    if alignment_strategy not in {"chunk", "speech"}:
        raise typer.BadParameter("--alignment-strategy must be 'chunk' or 'speech'")

    run_transcribe(
        audio_dir=audio_dir,
        language=language,
        model=model,
        emissions_model=emissions_model,
        vad=vad,
        backend=backend,
        device=device,
        cache_dir=cache_dir,
        output_root=output_root,
        batch_size_features=batch_size_features,
        num_workers_features=num_workers_features,
        num_workers_files=num_workers_files,
        beam_size=beam_size,
        chunk_size=chunk_size,
        alignment_strategy=alignment_strategy,
    )


@app.command("detect-language")
def cmd_detect_language(
    audio_dir: Annotated[Path, typer.Option("--audio-dir", exists=True, file_okay=False)],
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help=(
                "Classifier model. Default facebook/mms-lid-256 (SOTA for "
                "language ID). Also supports multilingual Whisper like "
                "openai/whisper-large-v3. Never use language-fine-tuned "
                "models (e.g. KBLab/kb-whisper-large) — they over-predict."
            ),
        ),
    ] = "openai/whisper-large-v3",
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("models"),
    sample_seconds: Annotated[
        float,
        typer.Option("--sample-seconds", help="Audio clip length fed to Whisper per sample."),
    ] = 30.0,
    num_windows: Annotated[
        int,
        typer.Option(
            "--num-windows",
            help="Clips sampled per file, spread evenly across the whole recording (duration-aware).",
        ),
    ] = 8,
    device: Annotated[str, typer.Option("--device")] = "cuda",
    no_move: Annotated[
        bool,
        typer.Option("--no-move", help="Report detected languages without moving files."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show planned moves without executing them."),
    ] = False,
) -> None:
    """Detect language per file via Whisper and sort into <audio-dir>/<lang>/ subfolders."""
    from runners.asr.detect_language import detect_and_sort

    detect_and_sort(
        audio_dir=audio_dir,
        model=model,
        cache_dir=cache_dir,
        sample_seconds=sample_seconds,
        num_windows=num_windows,
        device=device,
        move=not no_move,
        dry_run=dry_run,
    )
