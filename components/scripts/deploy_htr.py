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
  - Model is the **private** `trocr-large-handwritten-hist-swe-3-char`. Its v3
    1024x192 aspect ratio needs `interpolate_pos_encoding: True` or the
    positional encodings mismatch at generate() time (see the model card).
  - HF_TOKEN is NOT set here — it comes from the Ada worker pods' env, wired via
    the `huggingface` secret in ai-dev `kuberay-cluster/overlays/ai-dev/values.yaml`.
  - `health_check_timeout_s=1200` so the slow first model download doesn't trip
    the health check and kill the replica mid-pull.
"""

import ray
from ray import serve
from fastapi import FastAPI, Request, Response

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
        model: Riksarkivet/trocr-large-handwritten-hist-swe-3-char
      generation_settings:
        batch_size: 8
        # v3 uses a 1024x192 aspect ratio -> generate() needs this or the
        # positional encodings mismatch (see the model card).
        interpolate_pos_encoding: True
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
        "runtime_env": {"uv": [
            "opencv-python-headless",
            "git+https://github.com/AI-Riksarkivet/htrflow.git",
        ]},
    },
    health_check_period_s=30,
    health_check_timeout_s=1200,
    graceful_shutdown_timeout_s=60,
)
@serve.ingress(api)
class HTRFlow:
    def __init__(self):
        from htrflow.pipeline.pipeline import Pipeline
        from htrflow.serialization.serialization import get_serializer
        with open("/tmp/htrflow_pipeline.yaml", "w") as f:
            f.write(PIPELINE_YAML)
        self.pipeline = Pipeline.from_config("/tmp/htrflow_pipeline.yaml")
        self.serializer = get_serializer("alto")

    @api.post("/transcribe")
    async def transcribe(self, request: Request):
        import os, tempfile
        from htrflow.document import Document
        data = await request.body()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(data); path = tf.name
        try:
            doc = self.pipeline.run(Document(path))
            out = self.serializer.serialize(doc)
        finally:
            os.unlink(path)
        if isinstance(out, list):
            out = "\n".join(x[1] if isinstance(x, (tuple, list)) else str(x) for x in out)
        return Response(content=out or "", media_type="application/xml")


serve.run(HTRFlow.bind(), name="htr", route_prefix="/htr", blocking=False)
print("htr submitted on Ada (POST /htr/transcribe)")
