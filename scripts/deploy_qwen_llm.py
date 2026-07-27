"""Deploy Qwen3.6-27B as a Ray Serve LLM app (OpenAI-compatible) on the cluster.

Unlike the HTR Serve apps (deploy_serve.py), this MUST be run from the
ray+vLLM venv (~/qwen-serve/.venv-ray) — that venv has `ray` matching the
cluster plus `vllm`, which the rask HTR venv deliberately lacks (vLLM's
torch/transformers pins clash with TrOCR/htrflow).

    ~/qwen-serve/.venv-ray/bin/python scripts/deploy_qwen_llm.py up
    ~/qwen-serve/.venv-ray/bin/python scripts/deploy_qwen_llm.py down

The Serve replica + vLLM engine run with `py_executable` pinned to this same
venv so vLLM (torch 2.11/cu130, Blackwell sm_120) is available on the worker
without polluting the rask venv. The engine reserves 1 GPU (tp=1); the HTR apps
share the other two.

Prereq: the `vllm.entrypoints.pooling.score` compat shim must exist in the venv
(Ray 2.55.1 imports the pre-rename path that vLLM 0.21 moved to `scoring`).
See the shim under .venv-ray/.../vllm/entrypoints/pooling/score/.

OpenAI endpoint after deploy: http://localhost:8000/v1 (Ray Serve HTTP proxy),
model id `Qwen/Qwen3.6-27B`.
"""

import argparse
import os
import sys
import time

import ray
from ray import serve
from ray.serve import Application


MODEL_ID = os.environ.get("QWEN_MODEL", "Qwen/Qwen3.6-27B")
# Serve app name shows in the Ray dashboard — derive it from the model so it
# reads e.g. "gemma-4-31B-it", not a stale hardcoded name. Override via LLM_APP_NAME.
APP_NAME = os.environ.get("LLM_APP_NAME") or MODEL_ID.split("/")[-1]
ROUTE = os.environ.get("QWEN_ROUTE", "/")
CTX = int(os.environ.get("QWEN_CTX", "131072"))
MAX_SEQS = int(os.environ.get("QWEN_MAX_SEQS", "256"))
# Set to give this cluster's Serve HTTP proxy a non-default port (needed when a
# second Ray cluster on the same host already holds :8000).
SERVE_HTTP_PORT = os.environ.get("SERVE_HTTP_PORT")
# Agentic clients (OpenCode) send tool_choice="auto" on every request; vLLM
# rejects that unless the server enables auto tool choice + a model-specific
# tool-call parser (gemma4 for Gemma, qwen3_coder for Qwen3.6). Set to turn on.
TOOL_CALL_PARSER = os.environ.get("TOOL_CALL_PARSER")
# Separates <think> blocks into a reasoning field (qwen3 for Qwen, gemma4 for Gemma).
REASONING_PARSER = os.environ.get("REASONING_PARSER")
# This interpreter (the .venv-ray python) is what replicas should run as.
VENV_PY = sys.executable


def _connect() -> None:
    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"), ignore_reinit_error=True, log_to_driver=False)


def build_app() -> Application:
    from ray.serve.llm import LLMConfig, build_openai_app

    # py_executable pins replica/engine processes to this vLLM venv; the env var
    # disables flashinfer's JIT sampler (no nvcc/CUDA toolkit on this box).
    runtime_env = {
        "py_executable": VENV_PY,
        "env_vars": {"VLLM_USE_FLASHINFER_SAMPLER": "0"},
    }
    engine_kwargs = {
        "tensor_parallel_size": 1,
        "max_model_len": CTX,
        "max_num_seqs": MAX_SEQS,
        "enforce_eager": True,
    }
    if TOOL_CALL_PARSER:
        engine_kwargs["enable_auto_tool_choice"] = True
        engine_kwargs["tool_call_parser"] = TOOL_CALL_PARSER
    if REASONING_PARSER:
        engine_kwargs["reasoning_parser"] = REASONING_PARSER
    # enforce_eager skips CUDA-graph capture (faster init, fine for one user).
    # On nightly ray 3.0.0.dev0 + vllm 0.20.0 the Ray-executor init works;
    # stable ray 2.55.1 + vllm 0.21 crashed the GPU worker ~80s into init.
    llm_config = LLMConfig(
        model_loading_config={"model_id": MODEL_ID},
        engine_kwargs=engine_kwargs,
        deployment_config={
            "num_replicas": 1,
            # A 52GB load + memory profiling far exceeds Serve's default health
            # check timeout. Without a generous timeout, Serve marks the
            # still-initializing replica unhealthy, tears it down, and the
            # retries collide on the single GPU -> DEPLOY_FAILED. See Ray docs
            # "Deployment Initialization" + issues on 32B model health checks.
            "health_check_period_s": 30,
            "health_check_timeout_s": 600,
            "graceful_shutdown_timeout_s": 60,
        },
        runtime_env=runtime_env,
    )
    # The ingress deployment (OpenAiIngress) imports ray.serve.llm too, so it
    # must also run on the vLLM venv — otherwise it dies with "vLLM not
    # installed" in the default (rask) cluster env.
    return build_openai_app(
        {
            "llm_configs": [llm_config],
            "ingress_deployment_config": {"ray_actor_options": {"runtime_env": runtime_env}},
        }
    )


def cmd_up() -> int:
    _connect()
    # Pin the Serve HTTP proxy port when set (so a second cluster doesn't fight
    # the first for :8000). Must run before serve.run starts the proxy.
    if SERVE_HTTP_PORT:
        serve.start(http_options={"host": "0.0.0.0", "port": int(SERVE_HTTP_PORT)})
    port = SERVE_HTTP_PORT or "8000"
    try:
        serve.run(build_app(), name=APP_NAME, route_prefix=ROUTE, blocking=False)
    except RuntimeError as exc:
        # serve.run's readiness wait can fire before a large model finishes
        # loading; the controller keeps initializing the replica regardless.
        # Don't treat this as failure — poll the controller for the real state.
        print(f"(serve.run readiness wait returned early: {exc}; polling controller...)")
    deadline = time.time() + 900
    while time.time() < deadline:
        app = serve.status().applications.get(APP_NAME)
        if app is None:
            # Controller hasn't registered the app yet — keep polling.
            time.sleep(5)
            continue
        status = str(app.status)
        if "RUNNING" in status:
            print(f"LLM app {APP_NAME!r} RUNNING at {ROUTE} — OpenAI API on the Serve proxy (:{port}/v1).")
            print(f"model_id={MODEL_ID}  max_model_len={CTX}  max_num_seqs={MAX_SEQS}")
            return 0
        if "DEPLOY_FAILED" in status or "UNHEALTHY" in status:
            print(f"Deploy FAILED ({status}): {(app.message or '')[:400]}")
            return 1
        time.sleep(5)
    print(f"Timed out (15 min) waiting for app {APP_NAME!r} to reach RUNNING.")
    return 1


def cmd_down() -> int:
    _connect()
    serve.delete(APP_NAME)
    print(f"Deleted app {APP_NAME!r}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["up", "down"])
    args = parser.parse_args()
    return cmd_up() if args.action == "up" else cmd_down()


if __name__ == "__main__":
    sys.exit(main())
