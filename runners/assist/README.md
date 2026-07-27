# Assist runner

The annotator’s interactive AI-assist model server — an ONLINE service, never imported by ratch. Serves the `MEDIA_ASSIST_URL` seam as its own image (`.docker/assist-runner.dockerfile`), built `--frozen` from this directory’s own `uv.lock` (CPU torch wheels from the pytorch index, `>=3.12,<3.14`). Config defaults live in `server.py` Settings and MUST stay in sync with the dockerfile’s env block.
