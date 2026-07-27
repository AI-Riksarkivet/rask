# Runbook: Gemma + Qwen LLM cluster

Local OpenAI-compatible LLM backend (for the OpenCode / RASK CODE coding agent)
running as **Ray Serve LLM** apps. This is a **separate Ray cluster** from the
HTR pipeline — Ray refuses to mix Ray versions in one cluster, and the LLM stack
needs a different venv than HTR:

| | HTR cluster | LLM cluster (this runbook) |
|---|---|---|
| venv | rask `.venv` (ray 2.55.1) | `~/qwen-serve/.venv-ray2` (nightly ray 3.0.0.dev0 + vllm 0.20.0) |
| GPUs | `0` | `1,2` (one model per GPU) |
| GCS port | `6379` | `6380` |
| dashboard | `:8265` | `:8266` |
| temp-dir | `/tmp/ray` | `/tmp/ray-gemma` |
| Serve proxy | `:8000` (in-process handles) | `:8002` (OpenAI HTTP) |

Models: `google/gemma-4-31B-it` on route `/` (→ `:8002/v1`) and
`Qwen/Qwen3.6-27B` on route `/qwen` (→ `:8002/qwen/v1`). Both tool-calling
verified. Deploy logic: `scripts/deploy_qwen_llm.py`.

> Neither cluster auto-starts on reboot (no systemd). Bring it up by hand.

## Start

Run from the rask repo root (`/home/morgan/rask`).

**1. Start the LLM Ray head (GPUs 1,2):**

```bash
CUDA_VISIBLE_DEVICES=1,2 ~/qwen-serve/.venv-ray2/bin/ray start --head \
  --port=6380 --num-gpus=2 --dashboard-host=0.0.0.0 --dashboard-port=8266 \
  --temp-dir=/tmp/ray-gemma
```

**2. Deploy Gemma (route `/`, sets the Serve HTTP proxy to :8002):**

```bash
QWEN_MODEL=google/gemma-4-31B-it QWEN_CTX=131072 SERVE_HTTP_PORT=8002 \
  TOOL_CALL_PARSER=gemma4 \
  RAY_ADDRESS=10.16.51.53:6380 \
  ~/qwen-serve/.venv-ray2/bin/python scripts/deploy_qwen_llm.py up
```

**3. Deploy Qwen3.6 (route `/qwen`; omit `SERVE_HTTP_PORT` — the proxy is
already on :8002 from step 2):**

```bash
QWEN_MODEL=Qwen/Qwen3.6-27B QWEN_ROUTE=/qwen QWEN_CTX=131072 QWEN_MAX_SEQS=256 \
  TOOL_CALL_PARSER=qwen3_coder REASONING_PARSER=qwen3 \
  RAY_ADDRESS=10.16.51.53:6380 \
  ~/qwen-serve/.venv-ray2/bin/python scripts/deploy_qwen_llm.py up
```

`TOOL_CALL_PARSER` is **required** for agentic clients (they send
`tool_choice="auto"`, which vLLM rejects without it): `gemma4` for Gemma,
`qwen3_coder` for Qwen3.6. Keep `QWEN_CTX` large (131072) — OpenCode otherwise
requests ~32K *output* and overflows a 32K window. A 31B model takes a few
minutes to load; the script polls the Serve controller up to 15 min for
`RUNNING` (don't trust `serve.run`'s early readiness return).

## Verify

```bash
curl -s http://127.0.0.1:8002/v1/models          # -> google/gemma-4-31B-it
curl -s http://127.0.0.1:8002/qwen/v1/models     # -> Qwen/Qwen3.6-27B
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # GPU 1 & 2 ~90 GB each
```

## Stop

`deploy_qwen_llm.py down` removes one Serve app:

```bash
QWEN_MODEL=google/gemma-4-31B-it RAY_ADDRESS=10.16.51.53:6380 \
  ~/qwen-serve/.venv-ray2/bin/python scripts/deploy_qwen_llm.py down
QWEN_MODEL=Qwen/Qwen3.6-27B RAY_ADDRESS=10.16.51.53:6380 \
  ~/qwen-serve/.venv-ray2/bin/python scripts/deploy_qwen_llm.py down
```

To take the **whole cluster** down (free GPUs 1 & 2):

> ⚠️ **Do not run `ray stop`** — it is host-wide and kills the HTR cluster
> (`:6379`) too. To stop only this cluster, kill its process subtree by PID.
> Find the LLM cluster's GCS + raylet (their cmdlines live under
> `~/qwen-serve/.venv-ray2/` / session `/tmp/ray-gemma`), SIGTERM the raylet
> (drops the Serve replicas + vLLM GPU workers) and the GCS, then SIGKILL any
> head helpers (dashboard, monitor, client server) that linger. Confirm with
> `nvidia-smi` (GPU 1 & 2 back to ~0) and that `:8002` refuses connections.

## Clients

OpenCode (`~/.config/opencode/opencode.json`) and Pi / RASK CODE
(`~/.pi/agent/models.json`) point two providers at this proxy:
`rask-gemma` → `:8002/v1`, `rask-qwen` → `:8002/qwen/v1` (dummy API key,
`context: 131072`). Switch model in-session via `/models` (OpenCode) or
`/model` / Ctrl+P (Pi).
