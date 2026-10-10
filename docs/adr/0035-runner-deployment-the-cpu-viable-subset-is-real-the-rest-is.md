# 0035. Runner deployment — the CPU-viable subset is real, the rest is an honest GPU list (2026-07-24)

**Decision.** Of the folded `runners/` tree (the lance-audio model homes), exactly one runner deploys on
this GPU-less estate: **`runners/assist`** — a new ONLINE FastAPI model server (its own sealed env +
committed `uv.lock` + its own image, `.docker/assist-runner.dockerfile`) serving the annotator's
`MEDIA_ASSIST_URL` contract with **real CPU inference**: GroundingDINO-tiny (open-vocabulary text-prompted
detection, ~2.5 s/frame) + SAM-ViT-base (box/point segmentation → simplified polygon, ~1.8 s/frame).
Weights are baked into the image at build (HF cache layout, `HF_HUB_OFFLINE=1` at runtime); frames are
fetched from the viewer service only (relative `image_url` joined to `ASSIST_FRAME_BASE` — absolute URLs
rejected, no SSRF surface). The chart gains a `runners.enabled` flag (default **false**) rendering the
assist Deployment/Service (`component: assist`, appProbes, its own `resources.assist` tier — the default
request-pod tier would OOM a warm two-model torch process) and, on the annotator only, `MEDIA_ASSIST_URL`
→ the assist Service. Because the assist wire payload carries no `producer` field, the server routes by
what the user gave: prompt ⇒ detection (region narrows to a crop), region-only ⇒ segmentation (click ⇒
point prompt). `MEDIA_JOBS_URL` renders only when `runners.jobsUrl` is explicitly set — no batch deriver
exists yet, so the annotator keeps its honest submit/poll mock rather than a fake queue.

**The GPU-needed list (not deployable honestly on this box).**

- `asr` (whisper-large/wav2vec2, torch **cu128** pins) — CUDA env by construction; corpus is already
  transcribed, so a degraded CPU deployment would also be pointless.
- `diarize` (pyannote community-1, cu128) and `voiceprint` (WeSpeaker via pyannote, cu128) — same CUDA
  envs; offline Ray Data actors, not online services.
- `topics` (Toponymy) — CPU-tolerant clustering but requires live LLM endpoints (namer + embedder) that
  do not exist on this estate; also corpus-global batch (its own actor.py refuses per-batch use).
- `kg` (LightRAG) — needs an OpenAI-compatible LLM; batch pipeline, not a service.

**Rationale.** The assist seam is the one runner-shaped gap a 64-core GPU-less box can serve for real —
interactive single-frame inference where seconds-per-frame is acceptable — and it converts the annotator's
in-repo mock into live model predictions with zero annotator code change (the mock/remote seam was built
for exactly this drop-in). Everything else in `runners/` either hard-pins CUDA wheels or depends on LLM
serving we don't run; deploying those as CPU stand-ins would be the speculative-feature anti-pattern
(claiming a capability the estate cannot exercise). The subset boundary is therefore *honest by
construction*: real half deployed and live-proven, GPU half recorded here as the merge-time backlog.
