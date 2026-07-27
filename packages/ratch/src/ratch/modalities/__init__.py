"""Modality-specific stage implementations, gated by MIME at the registry.

``av/`` holds everything audio/video (ffmpeg, pyannote, ASR, voiceprints).
The pipeline core never imports from here directly.
"""
