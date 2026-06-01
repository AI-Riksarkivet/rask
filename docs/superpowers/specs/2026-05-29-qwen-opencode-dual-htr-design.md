# Qwen3.6-27B OpenCode backend + dual HTR services — design

**Date:** 2026-05-29
**Status:** Draft — awaiting user review
**Scope:** Run **Qwen3.6-27B** as a local OpenAI-compatible LLM backend for **OpenCode** on this 3-GPU workstation, while simultaneously keeping **both** rask HTR Serve apps (`transcribe` + `htrflow`) deployable. Two concerns that collide only on the shared GPU budget. Out of scope: any change to HTR model quality, the ALTO output contract, OpenCode's own config schema, and (per decision D2 below) orchestrator routing between the two HTR engines.

## Motivation

The box has 3× RTX PRO 6000 Blackwell (96 GB each). Today the HTR Serve apps each reserve *whole* GPUs (`transcribe`: 3×0.99, `htrflow`: 3×1.0), so only one runs at a time and there is no room for anything else. The user wants a local Qwen3.6-27B serving OpenCode (coding **and** screenshot/vision, which this checkpoint does natively) **and** both HTR services available. Memory is not the constraint — a dense BF16 27B is ~55–56 GB, comfortably one card — the constraint is Ray's *logical* whole-GPU reservation. The fix is to carve the GPUs into disjoint pools and relax the HTR reservations.

## Decisions adopted (flag at review to change)

- **D1 — Qwen serving:** vLLM (OpenAI-compatible) in an **isolated environment**, pinned to **GPU 2**, `tensor-parallel-size 1`.
- **D2 — "2 HTR services" means deployed & available**, *not* the orchestrator driving both concurrently. The orchestrator keeps submitting one pipeline (selected by `RASK_HTR_PIPELINE`); the second app is simply warm and reachable by handle. Concurrent-drive routing is explicitly deferred (YAGNI until needed).
- **D3 — GPU split:** Qwen = 1 GPU (GPU 2); Ray = 2 GPUs (GPU 0,1) shared by both HTR apps.
- **D4 — Sequencing:** cut straight to the new 2-GPU + Qwen layout. The currently-stopped backlog drain resumes under the new split rather than being restarted under the old 3-GPU layout first (which would be throwaway).

## Target architecture

```
Workstation — 3× RTX PRO 6000 Blackwell (96 GB each)
│
├─ GPU 2 ─ vLLM serve Qwen3.6-27B ── OpenAI API :8000 ──► OpenCode
│           (isolated env; tp=1; capped context)
│
└─ GPU 0,1 ─ Ray head (CUDA_VISIBLE_DEVICES=0,1, --num-gpus=2)
              ├─ Serve app "transcribe"  (TrOCR)   num_replicas=2 × 0.5 GPU
              └─ Serve app "htrflow"      (HTRflow)  num_replicas=2 × 0.5 GPU
                    ▲
              orchestrator (in viewer) submits one pipeline via RASK_HTR_PIPELINE
```

Hard GPU isolation is enforced by `CUDA_VISIBLE_DEVICES`, not just Ray's logical accounting: Ray never sees GPU 2, vLLM never sees GPU 0/1. So a Ray scheduling bug cannot land HTR work on the Qwen card and vice-versa.

## Piece 1 — Qwen3.6-27B backend for OpenCode

Almost entirely *outside* the rask codebase; the only rask-adjacent artifact is a launch script + Makefile target for convenience.

**Environment isolation (the critical boundary).** vLLM pins specific `torch` / `transformers` versions; the rask venv already runs `transformers >= 5.6` for TrOCR/htrflow. They must not share a venv. Two options, decide at review:
- **1a (recommended):** a standalone uv project/venv outside the workspace (e.g. `~/qwen-serve/`) with only `vllm`. Simplest, no Docker.
- **1b:** the official vLLM Docker image (`vllm/vllm-openai`), `--gpus '"device=2"'`, `-p 8000:8000`. Fully isolated, heavier.

**Launch (tp=1, single-user context cap):**
```bash
CUDA_VISIBLE_DEVICES=2 vllm serve Qwen/Qwen3.6-27B \
  --port 8000 --tensor-parallel-size 1 \
  --max-model-len 131072 --reasoning-parser qwen3
```
`--max-model-len` capped (131072) because full 262K context inflates KV cache; 128K is the model card's recommended floor for thinking mode and is ample for OpenCode. vLLM **≥ 0.19.0** required.

**OpenCode wiring.** Point OpenCode's OpenAI-compatible provider at `http://localhost:8000/v1`, model id `Qwen/Qwen3.6-27B`. (Exact config-file path is OpenCode's, set by the user; the rask repo holds nothing here.)

**Open risk to verify first:** vLLM image/vision support for this brand-new hybrid arch (Gated DeltaNet) may lag — text/coding is solid, vision is the unknown. Verification step (below) gates this. Fallback: SGLang ≥ 0.5.10, same OpenAI API.

## Piece 2 — both HTR services live on 2 GPUs

**Ray head pinned to 2 GPUs.** Start with `CUDA_VISIBLE_DEVICES=0,1` and `ray start --head --num-gpus=2`. Adds a `ray-up` variant (or env knob) since the current Makefile target takes all GPUs.

**Relax Serve reservations so both fit in 2 GPUs.** Change the two deployments from whole-GPU to fractional:
- `transcribe_service.py`: `num_gpus 0.99 → 0.5`, `num_replicas 3 → 2`.
- `htrflow_service.py`: `num_gpus 1.0 → 0.5`, `num_replicas 3 → 2`.

Total claim = 4 × 0.5 = 2.0 GPU, exactly the pool. Memory check: TrOCR (~1 GB) + YOLO/htrflow (a few GB) per replica ≪ 96 GB, so 2 replicas/card is trivially safe. Make replica/fraction counts **env-driven** (e.g. `RASK_SERVE_REPLICAS`, `RASK_SERVE_GPU_FRAC`) so re-splitting needs no code edit — this also documents the coupling.

**Deploy both.** `deploy_serve.py up --app transcribe` **and** `up --app htrflow` (two calls; the script already supports each). Optional: a `make serve-up-both` convenience target.

**Pipeline GPU assumptions (must audit).** `runner/pipeline.py` hardcodes GPU sizing "for a 3-GPU node" (per CLAUDE.md) and the Serve actors use `SHARDS = 3`. With Ray now at 2 GPUs and replicas at 2, the fan-out shard count and any per-actor `num_gpus` in `pipeline.py` need to be reconciled (likely `SHARDS → 2`) or jobs will under-fan or fail to schedule. This file is the main rask code-change risk and must be read before implementation.

## Data flow

- **Qwen path:** OpenCode → HTTP `:8000/v1/chat/completions` → vLLM (GPU 2) → response. No rask involvement, no S3, no DB.
- **HTR path:** unchanged from today — orchestrator (viewer) → Ray Data job → `*ViaServe` actor → Serve handle (`transcribe` or `htrflow` on GPU 0/1) → ALTO XML → `images-batch-alto`. The only change is the GPU pool the Serve replicas live on.

## Error handling / failure modes

- **vLLM OOM on GPU 2:** lower `--max-model-len` or add `--gpu-memory-utilization`. 55 GB weights + 131K KV fits in 96 GB with margin, so unlikely.
- **Ray tries to use GPU 2:** prevented structurally by `CUDA_VISIBLE_DEVICES=0,1` on the head.
- **Both HTR apps oversubscribe:** fractional reservations sum to exactly 2.0; if a future replica bump exceeds the pool, Serve replicas hang in `PENDING` (visible in `serve status`) rather than corrupting work — same failure surface as today's deadlock, just diagnosable.
- **vLLM vision broken for this arch:** caught by the verification step before OpenCode is repointed; coding-only fallback or SGLang.

## Testing / verification

1. **GPU isolation:** `nvidia-smi` shows vLLM only on GPU 2; `ray status` reports 2.0 GPU total.
2. **Both HTR apps READY:** `deploy_serve.py status` shows `transcribe` and `htrflow` both `RUNNING` with their replicas `RUNNING` (not `PENDING`).
3. **HTR still transcribes:** submit/resume one chunk; confirm ALTO lands in `images-batch-alto` and GPU 0/1 show utilization.
4. **Qwen text/coding:** `curl :8000/v1/chat/completions` with a coding prompt returns sane output.
5. **Qwen vision (gating):** send an image part in the chat request; confirm it's accepted and described. If unsupported → fall back per D1 risk note.
6. **OpenCode end-to-end:** a real OpenCode session against the local endpoint for both a code task and a screenshot task.

## Implementation order

1. Audit `runner/pipeline.py` + Serve actor `SHARDS`/GPU assumptions (read-only; informs the rest).
2. Make Serve replica/GPU-fraction env-driven; default to 2×0.5 each.
3. Add 2-GPU Ray-up path (`CUDA_VISIBLE_DEVICES=0,1`, `--num-gpus=2`).
4. Stand up the isolated vLLM env + launch script + Makefile target.
5. Verify Qwen text, then vision (gate).
6. Bring up Ray (2 GPU) + both Serve apps; verify both READY + HTR transcribes.
7. Repoint OpenCode; end-to-end check.

## Open questions for review

- **D1:** standalone uv venv (1a) or Docker (1b) for vLLM?
- **Q1:** Is `~/qwen-serve/` an acceptable home for the isolated vLLM env, or do you want it elsewhere?
- **Q2:** Confirm capping context at 131072 is fine for your OpenCode usage (vs. full 262K).
- **Q3:** Confirm D2 (deployed-&-available, no concurrent-drive) — or do you actually want both HTR engines draining the backlog in parallel (larger change)?
