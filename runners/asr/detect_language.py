"""Language detection pre-step.

Samples a short clip from each audio/video file in a directory, runs a
language classifier, and (optionally) moves each file into
``<audio_dir>/<lang>/`` so the transcribe pipeline can be run per-language
with the correct wav2vec2 emissions model.

**Two backends supported** — chosen by the ``model`` arg:

- ``facebook/mms-lid-*`` (default: ``facebook/mms-lid-256``) — Meta's MMS LID
  classifier, a wav2vec2-SequenceClassification model purpose-built for
  language identification across 256 languages. State-of-the-art for this
  exact task.
- ``openai/whisper-*`` / other Whisper models — the built-in Whisper LID head
  via ctranslate2. Lower accuracy than MMS for sv-vs-en edge cases but
  useful as a fallback.

**Never** pass a language-fine-tuned Whisper (``KBLab/kb-whisper-large``) —
those over-predict their training language (every file comes back as `sv`).

Exposed as ``ratch detect-language …`` via :mod:`ratch.cli`.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ratch.errors import RatchError

from .transcribe import DEFAULT_EMISSIONS_MODEL


if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

    # An audio clip → (ISO language code, mean top-1 probability).
    LangProbe = Callable[[np.ndarray], tuple[str, float]]

logger = logging.getLogger(__name__)


def read_audio_segment(*args, **kwargs):
    """Lazy shim over easytranscriber's reader — the model stack is a ``[models]``
    extra, imported only when a segment is actually read, so the pure helpers
    below (windowing / scoring / sorting) import and test without it."""
    from easytranscriber.audio import read_audio_segment as _ras

    return _ras(*args, **kwargs)


# Files we actually want to sort. Everything else (images, docs, …) is ignored.
AUDIO_VIDEO_EXTS: frozenset[str] = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"})


# MMS-LID emits ISO 639-3 codes (``eng``, ``swe``, ``nor`` …). We route files
# into directories named with the 2-letter codes used by our transcribe
# pipeline (``en``, ``sv``, …), so map the common ones.
ISO_639_3_TO_1: dict[str, str] = {
    "swe": "sv",
    "eng": "en",
    "nor": "no",
    "dan": "da",
    "fin": "fi",
    "isl": "is",
    "deu": "de",
    "ger": "de",
    "fra": "fr",
    "spa": "es",
    "ita": "it",
    "nld": "nl",
    "pol": "pl",
    "por": "pt",
    "rus": "ru",
    "ara": "ar",
    "zho": "zh",
    "cmn": "zh",
    "jpn": "ja",
    "kor": "ko",
    "tur": "tr",
    "ukr": "uk",
    "ell": "el",
    "gre": "el",
    "ces": "cs",
    "cze": "cs",
    "hun": "hu",
    "ron": "ro",
    "rum": "ro",
    "est": "et",
    "lav": "lv",
    "lit": "lt",
    "heb": "he",
    "hin": "hi",
    "ind": "id",
    "tha": "th",
    "vie": "vi",
}


# How many clips to sample per file, spread evenly across the whole recording.
# More windows over the full duration beats a few near the start: a single
# offset can land on silence/music, and fixed offsets miss everything in a long
# file (and fall past the end of a short one).
DEFAULT_NUM_WINDOWS = 8


def _plan_sample_starts(
    duration_s: float,
    *,
    sample_seconds: float,
    num_windows: int,
    span: tuple[float, float] = (0.05, 0.95),
) -> list[float]:
    """Evenly-spaced clip start offsets (seconds) across the middle of a file.

    Spreads up to ``num_windows`` clips over ``span`` (default 5–95%) of the
    duration so detection sees the whole recording, not just the intro. Each
    ``sample_seconds`` clip is kept inside the file; a file shorter than one
    clip collapses to a single clip at 0.
    """
    last_start = duration_s - sample_seconds
    if duration_s <= 0 or num_windows < 1 or last_start <= 0:
        return [0.0]
    lo = max(0.0, span[0] * duration_s)
    hi = min(last_start, span[1] * duration_s)
    if num_windows == 1 or hi <= lo:
        return [round(min(lo, last_start), 3)]
    step = (hi - lo) / (num_windows - 1)
    return [round(lo + i * step, 3) for i in range(num_windows)]


def _probe_duration_s(path: Path, *, probe_window: float = 0.5, ceiling_s: float = 36000.0) -> float:
    """Approximate media duration (seconds) by seeking until a short read is empty.

    Uses ``read_audio_segment`` (ffmpeg fast-seek) for a handful of tiny reads —
    no full decode — so it stays cheap on multi-hour files. Returns the last
    offset that still had audio, a slight under-estimate that keeps sample
    windows inside the file. Returns 0 if the file has no readable audio.
    """

    def has_audio(start: float) -> bool:
        clip = read_audio_segment(audio_path=path, start_sec=start, duration_sec=probe_window, sample_rate=16000)
        return clip.size > 0

    if not has_audio(0.0):
        return 0.0
    lo, hi = 0.0, 60.0
    while hi < ceiling_s and has_audio(hi):
        lo, hi = hi, hi * 2
    for _ in range(8):  # binary-refine the EOF between lo (audio) and hi (silent)
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if has_audio(mid) else (lo, mid)
    return lo


def detect_and_sort(
    *,
    audio_dir: Path,
    model: str = "openai/whisper-large-v3",
    cache_dir: Path = Path("models"),
    sample_seconds: float = 30.0,
    num_windows: int = DEFAULT_NUM_WINDOWS,
    device: str = "cuda",
    move: bool = True,
    dry_run: bool = False,
) -> dict[str, tuple[str, float]]:
    """Detect the spoken language of each top-level file in ``audio_dir``.

    Args:
        audio_dir: Directory containing mixed-language audio/video files.
        model: Hugging Face model id. ``facebook/mms-lid-*`` (default) or a
            multilingual Whisper like ``openai/whisper-large-v3``.
        sample_seconds: Length of each audio clip fed to the classifier (30s
            default).
        num_windows: Number of clips sampled, evenly spread across ~5–95% of
            each file (duration-aware). Spreading across the whole recording
            avoids judging a long file by its intro and keeps short files from
            being skipped.
        move: If ``True`` (default), move each file into ``audio_dir/<lang>/``.
        dry_run: Print what would happen without moving files.

    Returns:
        ``{filename: (lang_code, probability)}``. ``lang_code`` is the 2-letter
        ISO 639-1 code whenever we can map it from MMS's 639-3 output;
        otherwise the raw code passes through.
    """
    if not audio_dir.is_dir():
        raise RatchError(f"Audio directory not found: {audio_dir}")

    files = sorted(f for f in audio_dir.iterdir() if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in AUDIO_VIDEO_EXTS)
    if not files:
        logger.warning("no audio/video files found directly in %s/ (subdirs ignored)", audio_dir)
        return {}

    if model.startswith("facebook/mms-lid"):
        probe = _mms_probe(model, cache_dir, device)
    else:
        probe = _whisper_probe(model, cache_dir, device)

    logger.info(
        "detecting language in %d file(s) with %s (%d × %.0fs windows per file)",
        len(files),
        model,
        num_windows,
        sample_seconds,
    )

    results: dict[str, tuple[str, float]] = {}
    for f in files:
        duration = _probe_duration_s(f)
        if duration <= 0.0:
            logger.warning("%s: no readable audio; skipping", f.name)
            continue

        votes: dict[str, float] = {}
        sampled = 0
        for start in _plan_sample_starts(duration, sample_seconds=sample_seconds, num_windows=num_windows):
            try:
                clip = read_audio_segment(audio_path=f, start_sec=start, duration_sec=sample_seconds, sample_rate=16000)
            except Exception:
                continue
            if clip.size == 0:
                continue
            lang_raw, prob = probe(clip)
            votes[lang_raw] = votes.get(lang_raw, 0.0) + prob
            sampled += 1
        if not votes:
            logger.warning("%s: no usable audio; skipping", f.name)
            continue

        lang_raw = max(votes, key=lambda k: votes[k])
        prob_avg = votes[lang_raw] / sampled
        lang = ISO_639_3_TO_1.get(lang_raw, lang_raw)
        results[f.name] = (lang, prob_avg)

        supported = "✓" if lang in DEFAULT_EMISSIONS_MODEL else "!"
        logger.info("%s %s: %s (p=%.3f)", supported, f.name, lang, prob_avg)

        if move and not dry_run:
            target_dir = audio_dir / lang
            target_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(target_dir / f.name))

    if move and not dry_run:
        logger.info("sorted %s file(s) into %s/<lang>/ subfolders", len(results), audio_dir)
        for lang in sorted({v[0] for v in results.values()}):
            if lang in DEFAULT_EMISSIONS_MODEL:
                logger.info(f"  next: ratch transcribe --audio-dir {audio_dir}/{lang} --language {lang} --output-root output/{lang}")
            else:
                logger.warning(f"  {lang}: no default wav2vec2 emissions model; pass --emissions-model yourself")
    elif dry_run:
        logger.info("dry run — no files moved")

    return results


# ──────────────────────── Backend probes ─────────────────────────────────────


def _mms_probe(model_id: str, cache_dir: Path, device: str) -> LangProbe:
    """Build an MMS-LID probe: audio clip → (ISO 639-3 code, probability)."""
    import torch
    from transformers import AutoFeatureExtractor, Wav2Vec2ForSequenceClassification

    logger.info("loading %s on %s", model_id, device)
    processor = AutoFeatureExtractor.from_pretrained(model_id, cache_dir=str(cache_dir))
    loaded = Wav2Vec2ForSequenceClassification.from_pretrained(model_id, cache_dir=str(cache_dir))
    # transformers' stubs mistype the from_pretrained/.to() chain (it infers a
    # bare `str`); the runtime call is correct for a HF model id.
    mdl = loaded.to(device).eval()  # ty: ignore[invalid-argument-type]
    id2label = mdl.config.id2label  # int → "eng"/"swe"/…

    def probe(audio: np.ndarray) -> tuple[str, float]:
        inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = mdl(**inputs).logits  # (1, 256)
        probs = torch.softmax(logits, dim=-1)[0]
        top = int(torch.argmax(probs).item())
        return id2label[top], float(probs[top].item())

    return probe


def _whisper_probe(model_id: str, cache_dir: Path, device: str) -> LangProbe:
    """Build a Whisper-LID probe: audio clip → (ISO 639-1 code, probability)."""
    import ctranslate2
    from easytranscriber.asr.ct2 import detect_language
    from easytranscriber.utils import hf_to_ct2_converter
    from transformers import WhisperProcessor

    logger.info("loading %s on %s", model_id, device)
    model_path = hf_to_ct2_converter(model_id, cache_dir=str(cache_dir))
    whisper = ctranslate2.models.Whisper(model_path.as_posix(), device=device)
    processor = WhisperProcessor.from_pretrained(model_id, cache_dir=str(cache_dir))

    def probe(audio: np.ndarray) -> tuple[str, float]:
        features = processor(audio, sampling_rate=16000, return_tensors="pt").input_features.numpy()
        features_ct2 = ctranslate2.StorageView.from_array(features)
        top = detect_language(whisper, features_ct2)[0]
        # top["language"] is ``<|sv|>`` — strip braces; it's already 639-1
        return top["language"].strip("<|>"), float(top["probability"])

    return probe
