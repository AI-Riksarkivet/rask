# ASR runner

Speech-to-text over easytranscriber with CUDA torch (`torch==2.11.0+cu128`). Offline Ray Data actor: `transcribe.py` (plus `detect_language.py`). Deps install on Ray WORKERS via `runtime_env` from this `pyproject.toml`; the driver never imports them.
