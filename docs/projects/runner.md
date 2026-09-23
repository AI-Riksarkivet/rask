# Runners

A workload is a **sealed runner**: one directory, `runners/<workload>`, holding its pipeline and its
model code as a single project with its own `pyproject.toml` (and its own `uv.lock` wherever it builds
an image). There are nine — `asr`, `assist`, `diarize`, `dummy`, `htr`, `insid3`, `kg`, `topics`,
`voiceprint` — spanning audio, image and text, and none of them is the platform.

The platform knows nothing about any one of them. A runner's stage graph, models, GPU packing and
output format are its own business, documented in **that runner's README**
(`runners/<workload>/README.md`), never here. A workload reaches the platform as configuration: bytes
in, a governed table out, lineage emitted.

## What the seal means

- **Not a workspace member.** `runners/*` is matched by no glob in the root
  `[tool.uv.workspace] members`, so a runner's model stack never enters the fleet's resolution. Where a
  runner uses `storage`, it is a **path** dependency.
- **Its own tests and lint.** A runner's tests are invisible to the root `pytest`; `make test` runs
  a runner's suite as its own leg, from inside that runner's directory. Ruff resolves the nearest
  `pyproject.toml`, so a runner carries its own ruff config.
- **Its own image.** One workload's Ray image is built from the parametrized
  `.docker/ray-runner.dockerfile` (`ARG RUNNER`): the platform environment from the root lock plus
  exactly one runner from its own lock.

    ```bash
    bash scripts/dagger-image.sh --runner <workload> --tag ray-<workload>:<tag>
    ```

    A new runner is a new `--runner=` value and edits no platform file. Only a runner that ships its
    own `uv.lock` can be built this way — `--locked` is what seals it. The head image
    (`.docker/ray-cluster.dockerfile`) installs no runner at all.
- **Its own Serve applications.** Model weights that should stay warm across jobs live in Ray Serve
  applications the runner owns. A deployment declares each one under `ray.serveApplications[]` in its
  chart values — `name`, `importPath`, `routePrefix`, `image`, and optional `params` (each rendered
  as `RASK_PARAM_<KEY>`, so a workload cannot collide with a platform variable). `image` renders into
  `runtime_env.image_uri`, so several workloads share one cluster without sharing one environment.
  Replica counts and GPU fractions (`RASK_SERVE_REPLICAS`, `RASK_SERVE_GPU_FRAC`) are the runner's
  settings, not the platform's.
- **The two environments do not merge.** A runner image's default interpreter is the workload's
  venv, so `import lance` fails there by design; the platform's Lance plane belongs to the head image,
  which runs the jobs that need it.

Pinned by `tests/unit/test_invariants.py::test_the_deployed_ray_image_PROVIDES_every_serve_application_it_declares`,
which refuses a declared application whose import path no image it can run provides.

## Running one

Each runner names its own entrypoint and flags in its README. `make serve-up` / `serve-down` /
`serve-status` deploy, remove and inspect the active runner's Serve applications on a local Ray head
(`make ray-up`).
