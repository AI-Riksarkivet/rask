# Runners — every model's home

Each `runners/<name>/` is a **sealed, self-contained project**: its own `pyproject.toml` (and its own
`uv.lock` where it builds an image), its own README, its own compute. They are deliberately **not**
members of any uv workspace — each pins what its model needs (CUDA torch builds from the pytorch index,
a Ray minor, an OpenAI SDK) and those pins must never enter the services' resolution.

Two kinds:

- **Offline Ray Data runners** (`asr`, `diarize`, `kg`, `topics`, `voiceprint`): a model's actor plus the
  env Ray installs on WORKERS via `runtime_env` — the driver never imports the heavy deps.
- **Online servers** (`assist`): a standalone model service with its own image
  (`.docker/assist-runner.dockerfile`) built from its own lockfile.

The orchestrator contract: **ratch knows runner NAMES (`Stage.runner=`), nothing about the models.**
There is no `__init__.py` tree here on purpose — this directory is not a Python package, and importing
`runners.<x>` from outside is the coupling that rule forbids. (ratch's `cli/` still carries lazy
repo-relative imports from its lance-audio heritage; they are unwired today and get replaced by the
Ray-native name seam when the pipeline step lands — recorded in `docs/OPEN-WORK.md`.)
