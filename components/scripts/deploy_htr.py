"""Deploy the HTRflow pipeline as a standalone Ray Serve app on the dev-kuberay cluster.

This is the *cluster* deploy (distinct from `deploy_serve.py`, which targets a
local Ray). It's self-contained — the deployment is defined inline with a
FastAPI ingress, so the only thing the job needs to upload is this one file.
The htrflow pipeline YAML is written to disk by the replica at startup.

App `htr` → `POST /htr/transcribe` (raw image bytes in, ALTO XML out).

Submit it as a Ray job (the entrypoint then runs on the head node, where
`ray.init(address="auto")` resolves):

    ray job submit --address http://127.0.0.1:8265 \
        --working-dir components/scripts -- python deploy_htr.py

`--address` is the dashboard; reach the dev-kuberay one via a port-forward to
:8265 (or run from a head sidecar). IMPORTANT: the `ray job submit` *client*
version must match the cluster's ray (currently 3.0.0.dev0) — rask's pinned
2.55.1 will not do; submit from the `rayproject/ray-llm` image or a matching venv.

Key deployment decisions (learned the hard way — keep them):
  - Pinned to the **Ada** tier via the `gpu_ada` custom resource (1 whole GPU x
    4 replicas = the entire Ada tier). Gemma owns Blackwell; HTR owns Ada.
  - `runtime_env` installs deps with **uv** and lists `opencv-python-headless`
    BEFORE htrflow: uv resolves deterministically, so the GL-free cv2 wins over
    the full `opencv-python` htrflow pulls in (otherwise the replica dies on a
    missing libGL). torch is intentionally NOT pinned — the image's build is used.
  - Model is the **private** `trocr-base-handwritten-hist-swe-3` (subword). Its
    processor emits 192x1024 crops while the ViT grid is 384x384, so it needs
    `interpolate_pos_encoding: True` or generate() raises ValueError -> htrflow's
    unguarded worker thread dies -> the page hangs 600s. (Verified 2026-06-18.)
  - HF_TOKEN is NOT set here — it comes from the Ada worker pods' env, wired via
    the `huggingface` secret in ai-dev `kuberay-cluster/overlays/ai-dev/values.yaml`.
  - `health_check_timeout_s=1200` so the slow first model download doesn't trip
    the health check and kill the replica mid-pull.
"""

import ray
from fastapi import FastAPI, Request, Response
from ray import serve


ray.init(address="auto", ignore_reinit_error=True, log_to_driver=False)

PIPELINE_YAML = """
steps:
  - step: Segmentation
    settings:
      model: yolo
      model_settings:
        model: Riksarkivet/yolov9-regions-1
  - step: Segmentation
    settings:
      model: yolo
      model_settings:
        model: Riksarkivet/yolov9-lines-within-regions-1
  - step: TextRecognition
    settings:
      model: trocr
      model_settings:
        model: Riksarkivet/trocr-base-handwritten-hist-swe-3
      generation_settings:
        batch_size: 8
        # base-3's processor emits 192x1024 line crops but the ViT position grid
        # is 384x384; WITHOUT interpolate_pos_encoding generate() raises ValueError,
        # which kills htrflow's (unguarded) inference worker thread -> the page's
        # line futures never resolve -> the request hangs until the client's 600s
        # timeout. This was the root cause of "base-3 hangs". (Verified 2026-06-18.)
        interpolate_pos_encoding: True
        # base-3 is subword (vocab 50265) with an internally-consistent eos=2, so
        # it terminates cleanly (~11-21 tok/line, no junk tails); the cap is just a
        # safety net. ~8x faster than the old char model. (Verified 2026-06-18.)
        max_new_tokens: 128
        # base-3 ships generation_config use_cache=false -> O(n^2) decode; re-enable.
        use_cache: True
        eos_token_id: 2
  - step: OrderLines
"""

api = FastAPI()


@serve.deployment(
    name="HTRFlow",
    num_replicas=4,
    ray_actor_options={
        "num_gpus": 1,
        "resources": {"gpu_ada": 0.001},
        # uv (like rask) resolves deterministically so opencv-python-headless's
        # GL-free cv2 wins over the full opencv-python htrflow pulls in -> no libGL.
        "runtime_env": {
            "uv": [
                "opencv-python-headless",
                "git+https://github.com/AI-Riksarkivet/htrflow.git",
            ]
        },
    },
    health_check_period_s=30,
    health_check_timeout_s=1200,
    graceful_shutdown_timeout_s=60,
)
@serve.ingress(api)
class HTRFlow:
    def __init__(self):
        import tempfile

        from htrflow.pipeline.pipeline import Pipeline
        from htrflow.serialization.serialization import get_serializer

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(PIPELINE_YAML)
            config_path = f.name
        self.pipeline = Pipeline.from_config(config_path)
        self.serializer = get_serializer("alto")

    @api.post("/transcribe")
    async def transcribe(self, request: Request) -> Response:
        import asyncio
        import os
        import tempfile

        from htrflow.document import Document

        data = await request.body()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(data)
            path = tf.name

        # Offload the blocking htrflow pipeline (YOLO + TrOCR) to a worker thread so
        # the replica's asyncio event loop stays responsive to Serve's health probe.
        # Running it inline froze the loop -> "event loop unresponsive" -> Serve
        # killed every replica -> restart storm.
        def _run() -> str | list | None:
            try:
                doc = self.pipeline.run(Document(path))
                return self.serializer.serialize(doc)
            finally:
                os.unlink(path)

        out = await asyncio.to_thread(_run)
        if isinstance(out, list):
            out = "\n".join(x[1] if isinstance(x, (tuple, list)) else str(x) for x in out)
        return Response(content=out or "", media_type="application/xml")


serve.run(HTRFlow.bind(), name="htr", route_prefix="/htr", blocking=False)
print("htr submitted on Ada (POST /htr/transcribe)")
