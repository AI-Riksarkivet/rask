# Diarization runner

Speaker diarization over pyannote.audio with CUDA torch. Offline Ray Data actor: `actor.py` / `diarize.py` (speaker turns, `SpeakerTurn`). Worker-side env from this `pyproject.toml`.

Self-contained: `wav.py` (ffmpeg 16 kHz mono transcode), `dataset.py` (the Lance write seam and its create-time invariants), `schema.py` (`SPEAKER_TURNS_SCHEMA`), `audio.py` (`resolve_source`) and `context.py` (`RunnerContext`) are COPIES taken at the ratch dissolution (2026-08-28, `open_ray-kernel.md`), each carrying its provenance in its module docstring. The runner is sealed on `requires-python = ">=3.10,<3.13"` while every platform package is `>=3.13`, so nothing outside this directory is importable here — the copies are the seal working, not drift.
