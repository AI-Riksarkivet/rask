# GPU + CUDA + Ray + PyTorch (rask)

Patterns specific to images that need a GPU at runtime. rask's runner is the only such image.
Base: `nvidia/cuda:12.4.0-runtime-ubuntu22.04`.

## CUDA variant matrix: `-base` / `-runtime` / `-devel`

The CUDA image family has three tiers. `-base` contains only the CUDA runtime libraries and nothing else — too
minimal for most workloads. `-runtime` adds cuDNN and the NCCL shared libraries that PyTorch and Ray link
against at load time. `-devel` stacks the full compiler toolchain, headers, and static libraries on top. The
rule is simple: **default to `-runtime`**. Switch to `-devel` only if a Python wheel needs to compile CUDA
C++ extensions at `pip install` / `uv sync` time (e.g., a custom Triton kernel or `flash-attn` from source).

PyTorch and Ray do not need `-devel`. Both ship their own CUDA shared libraries inside the wheel
(`torch/lib/libcuda*.so`, `torch/lib/libnccl.so`). They rely on the driver ABI exposed by the NVIDIA
container runtime and on the shared libs present in the `-runtime` image — nothing more. Using `-devel` in
production adds ~800 MB of compiler noise to the image for zero benefit and a wider attack surface.

## uv-managed Python on the CUDA Ubuntu base

The CUDA base is Ubuntu 22.04. Ubuntu's packaged CPython lags the version used in the rest of rask (3.13)
and the `deadsnakes/ppa` approach introduces apt churn and version skew risk. Instead, install uv and let it
manage a hermetic Python:

```dockerfile
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_DOWNLOADS=auto
COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:<DIGEST> /uv /usr/local/bin/uv
RUN uv python install 3.13
```

`UV_PYTHON_DOWNLOADS=auto` is the variable that gates network access for managed-Python downloads (valid values: `auto`/`true`, `manual`, `never`/`false`). `UV_PYTHON_PREFERENCE=only-managed` is the variable that controls system-vs-managed selection. These are distinct settings — `only-managed` is NOT a valid value for `UV_PYTHON_DOWNLOADS`.

The `COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:<DIGEST>` pattern keeps uv digest-pinned (per the principles.md "digest-pinned FROM" rule) and avoids needing pip in the CUDA base. The viewer template uses the same pattern — see `templates/runner.dockerfile`.

`UV_PYTHON_PREFERENCE=only-managed` ensures uv never falls back to the system Python. This costs roughly
+50 MB in the final image but guarantees exact version parity with the slim viewer. Mitigate the size cost
by putting `.venv/bin` first on `PATH` and never invoking `python3` directly — uv's shims handle dispatch
and the system Python stays invisible.

## System libs Ray + PyTorch actually need

The CUDA base is lean; three host-side packages cover everything Ray and PyTorch actually dlopen at runtime:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*
```

`libgomp1` provides the OpenMP runtime that PyTorch's CPU fallback paths and some BLAS kernels link against.
`ca-certificates` is needed for HTTPS connections to HuggingFace Hub (and any other TLS endpoint). `tini`
is the minimal init process: it forwards signals correctly and reaps zombie child processes spawned by Ray
workers. No `python3-dev`, no `build-essential`, no compilers belong in the final image.

## Model-weight strategy

The prescribed default for rask is **runtime-download**: models are fetched on first warm-up to
`$HF_HOME=/cache/hf` backed by a persistent local volume. Three options exist:

| Strategy | How it works | When to use | Trade-offs |
|---|---|---|---|
| **Runtime-download** (default) | `HF_HUB_DOWNLOAD` at startup writes to `$HF_HOME` on a persistent volume | Any deploy target with a durable local volume | First-start latency; needs network; needs volume |
| **Bake at build** | `RUN python -m runner.fetch_models` during `docker build` | Deploy target with no persistent volume (e.g., read-only rootfs, ephemeral nodes) | Very large image layers; re-download on every rebuild; not suitable for gated models without a build secret |
| **Sidecar init container** | K8s init container pulls weights to a shared emptyDir before the runner pod starts | Multi-replica K8s deploys needing fast rollouts | K8s-specific; adds manifest complexity; out of scope for rask today |

Bake-at-build is a valid fallback but creates layers that are expensive to rebuild and cache-bust on every
model version bump. Prefer runtime-download unless the deploy environment genuinely cannot provide a
persistent volume.

## `huggingface_hub>=0.24.7` pin

Versions of `huggingface_hub` below 0.24.7 contain a documented race condition in the file-locking
subsystem: when multiple processes try to download the same blob simultaneously, `.lock` files can be held
across a crash and never released, causing subsequent workers to hang indefinitely (HF #2543, HF #2038).

Multi-Ray-worker setups trip this constantly. A Ray Serve deployment with 3 replicas will attempt parallel
first-start downloads of the same checkpoint, which is exactly the concurrent-multi-process scenario the bug
affects. Pin in `projects/runner/pyproject.toml`:

```toml
[project]
dependencies = [
    "huggingface_hub>=0.24.7",
    ...
]
```

## Mount `$HF_HOME` on a local volume

The HuggingFace Hub download library uses advisory file locks to coordinate concurrent downloaders. These
locks rely on `fcntl` semantics that are undefined — and often broken — on networked filesystems such as
CIFS and NFS. Mounting `$HF_HOME` on a CIFS or NFS share causes the lock mechanism to deadlock silently:
workers stall waiting for a lock that was taken on another host or that was never released after a network
partition.

Always mount `$HF_HOME=/cache/hf` on a **local** volume: a `hostPath` or `emptyDir` in Kubernetes, or a
named Docker volume backed by the local disk. Never bind-mount a network share for model weight storage.

## `HF_HUB_ENABLE_HF_TRANSFER=1`

The `hf_transfer` Rust extension bypasses Python's HTTP stack and achieves sustained download speeds above
500 MB/s on capable links. Enable it unconditionally for the runner image:

```dockerfile
ENV HF_HUB_ENABLE_HF_TRANSFER=1
```

**Caveat:** `hf_transfer` suppresses chunk-level progress reporting. Downloads appear to stall in the final
seconds because the last TCP window is flushed without a progress event. Operators monitoring logs during a
first-start warm-up should wait at least 60 seconds after the last progress line before concluding the
download has hung. A healthy download with a large model (10+ GB) will produce a several-second silent tail
before the file appears on disk.

## HF telemetry off by default

Riksarkivet is the Swedish National Archives — a government authority subject to data minimisation
obligations. Sending telemetry to third-party endpoints from a production workload is not acceptable by
default. Bake the following ENV block into runner.dockerfile:

```dockerfile
ENV HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    DO_NOT_TRACK=1
```

`HF_HUB_DISABLE_TELEMETRY=1` stops the HuggingFace Hub client from phoning home. `HF_HUB_DISABLE_IMPLICIT_TOKEN=1`
prevents the library from silently reading `~/.cache/huggingface/token` when no explicit token is provided
(important for reproducibility in CI). `DO_NOT_TRACK=1` is a cross-ecosystem opt-out honoured by `gradio`,
`datasets`, and `diffusers` — if any of those are ever added to the runner, they will already be opted out.

`HF_HUB_OFFLINE=1` is **not** baked. The image must be capable of downloading weights on first warm-up in a
fresh deploy. Set `HF_HUB_OFFLINE=1` at runtime only when running in a deliberately air-gapped environment
where all weights are guaranteed to be pre-cached.

## `--mount=type=secret,id=hf_token` for gated weights

Gated model checkpoints require an HF access token. Never bake the token into a layer — it will be visible
in `docker history`. Use BuildKit's secret mount to inject it only for the duration of the `RUN` instruction:

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) \
    python -m runner.fetch_models
```

Build invocation:

```bash
docker buildx build \
  --secret id=hf_token,src=$HOME/.cache/huggingface/token \
  -f runner.dockerfile \
  -t rask/runner:latest .
```

The secret is never written to the image filesystem and does not appear in any layer digest or `docker inspect`
output. The token file at `$HOME/.cache/huggingface/token` is the standard location written by `huggingface-cli login`.

## Thread-storm ENV defaults

Without explicit limits, every Ray actor that imports NumPy, PyTorch, or any BLAS-linked library spawns
`cpu_count()` OpenMP and BLAS threads. On a 96-core node with 3 Ray Serve replicas, that is 288+ threads
contending for CPU time and cache lines — latency spikes and throughput collapse. Bake conservative defaults:

```dockerfile
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1
```

**Why these must be in dockerfile `ENV`, not Python code:** OpenBLAS reads `OPENBLAS_NUM_THREADS` during its
shared-library constructor, which runs at `dlopen` time — before any Python line executes. An
`os.environ["OPENBLAS_NUM_THREADS"] = "1"` assignment anywhere in Python code is too late; the thread pool
is already allocated. Only an environment variable present in the process environment at startup is reliable.

Ray can still override these per-actor when a specific pipeline stage benefits from more threads:

```python
@serve.deployment(ray_actor_options={"runtime_env": {"env_vars": {"OMP_NUM_THREADS": "4"}}})
class HeavyOCRActor:
    ...
```

## `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`

Long-running Ray Serve replicas accumulate CUDA memory fragmentation. After several hours of inference the
allocator fails to satisfy a request for a contiguous region even though enough total free memory exists —
producing an out-of-memory error that kills the replica. The `expandable_segments` allocator avoids this by
allowing the CUDA VM range to grow non-contiguously:

```dockerfile
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

**Caveat:** `expandable_segments` conflicts with NCCL's VMM-based collective memory allocator in multi-GPU
all-reduce operations (pytorch/pytorch#165419). Setting it on a multi-GPU NCCL workload can cause collective
communication hangs. This is safe for rask because Ray Serve replicas are single-GPU per `pipeline.py`
(3 replicas × 0.99 GPU on a 3-GPU node). Document this caveat for anyone considering a move to multi-GPU
NCCL tensor parallelism — they must remove this ENV variable before doing so.

## CUDA smoke test (optional)

A build-arg-gated smoke test can validate that the CUDA stack is correctly assembled without unconditionally
adding build time:

```dockerfile
ARG RUN_CUDA_SMOKE_TEST=0
RUN if [ "$RUN_CUDA_SMOKE_TEST" = "1" ]; then \
        python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'"; \
    fi
```

Enable during integration builds by passing `--build-arg RUN_CUDA_SMOKE_TEST=1`. The test requires a GPU
visible to the builder (i.e., `docker buildx build --gpus all`). Keep it disabled in CI pipelines that run
on CPU-only builders; defer the actual CUDA availability check to the runtime health probe in the Ray Serve
deployment instead.

## Runtime config that pairs with this image (pointer)

The dockerfile cannot set these; the deploy manifest must. Two Ray-specific requirements are frequently
missed in first deploys:

- **`--shm-size` ≥ 30 % of container RAM.** Ray's plasma object store defaults to `/dev/shm`. If shared
  memory is insufficient Ray silently falls back to `/tmp` (local disk), which degrades object-store
  throughput by an order of magnitude and generates confusing log noise. References: ray-project/ray #13619,
  ray-project/ray #14535.

- **`--ulimit nofile=65535`.** Ray Serve warns when the open-file limit is below 8192 and may fail to accept
  connections under load when it is at the Docker default of 1024. The plasma store, gRPC channels between
  workers, and the dashboard all hold persistent file descriptors. References: ray-project/ray #13045,
  ray-project/ray #16820.

Set both in `docker-compose.yml` under the `runner` service (`shm_size:` and `ulimits.nofile:`) or in the equivalent K8s pod spec — typically an `emptyDir` with `medium: Memory` mounted at `/dev/shm` for shared memory, plus pod-level ulimit configuration (the exact mechanism depends on cluster-level kubelet config and is outside the dockerfile's reach).
