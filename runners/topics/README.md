# `topics` runner — the reference template for job-driven runners

A **corpus-global model runner**: ratch drives it as a *job*, never as a
pipeline stage — Toponymy fits the whole atlas map at once, so there is no
per-batch compute (`actor.py` raises exactly that explanation). It exists as its
own dir because Toponymy transitively needs **transformers < 5** while ratch's
model extra resolves **transformers 5.x** — the conflict is resolved by giving
this runner its **own env**, never by leaking deps into ratch.

## What lives here (the pattern every job-driven `runners/<name>/` follows)

| file | role |
|---|---|
| `pyproject.toml` | **this runner's env** — the conflicting deps live here and nowhere else |
| `worker.py` | the compute (`run()` + a CLI `main()` reading argv) — the Ray Job entrypoint |
| `actor.py` | the deliberate refusal: corpus-global ⇒ no `map_batches` stage |
| `deployment.py` | the **Ray Serve** `@serve.deployment` (merge-time online form) |
| `README.md` | this |

ratch reaches it through **`ratch/core/jobs.py`** (`run_runner`) — the same
seam as lance-ns `medallion ray_submit`:

- `RATCH_RAY_ENABLED=1`: submitted as a **Ray Job** — entrypoint
  `python -m runners.topics.worker --db <db>`, this pyproject's deps in the
  job's `runtime_env`, deterministic submission id (re-runs re-attach or
  resubmit, never race).
- default (no cluster): the worker runs **in-process** — which needs its deps,
  so the local convenience is the Make target instead:

```bash
# local sealed-env run (what `make topics` does — Make-level, no Python subprocess):
uv run --project runners/topics python -m runners.topics.worker --db transcripts_v2.lance
uv run ratch --db transcripts_v2.lance feature topic_tree   # the pure-compute follow-up

# at merge, as a Ray Serve deployment (needs the `serve` extra):
#   serve run runners/topics/deployment.py:app
```

## Why this shape

- **ratch stays model-free** — pure Ray Data/Jobs orchestration; it knows the
  runner's NAME, imports no model library, shells out to nothing.
- **The dep conflict dissolves structurally** — one env per runner.
- **It's the merge target, pre-built** — at merge this dir is one Ray Job
  (batch) + one Serve deployment (online), and `.docker/topics.dockerfile`
  builds its image (RA convention; pip runtime_env is the dev bridge only).
