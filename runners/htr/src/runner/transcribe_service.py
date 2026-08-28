"""Ray Serve deployment of the TrOCR transcription model.

Why this exists: Ray Data's streaming executor refuses to spread work across
multiple actors at the slowest stage of a multi-stage pipeline (it ranks
operators by smallest out-queue, deliberately keeping queues short). With a
3-actor TranscribeActor pool that means only 1 GPU was ever doing work at any
moment — the actors took turns instead of running concurrently.

Ray Serve dispatches by round-robin (or other configured policies) and is
oblivious to the data pipeline's backpressure. Fronting the GPU stage with a
Serve deployment moves the work-spreading concern out of Ray Data entirely.

Bonus: models stay loaded across job submissions. Each `runner` invocation
no longer pays the ~30 s per-actor TrOCR cold-start.

Deployment:
    make serve-up
    # then submit chunks normally — pipeline.py wires into this deployment.

torch / transformers are deliberately NOT imported at module scope: Ray Serve's
@deployment decorator pickles the class definition (and its module's globals),
and torch's `CudnnModule` isn't picklable. All torch imports happen inside
method bodies.
"""

import logging
import os

from PIL import Image
from ray import serve

from htr.models import MODEL_REVISION, TEXT_MODEL


logger = logging.getLogger(__name__)


# Mirror constants from htr.actors.transcription so behavior matches the
# old TranscribeActor exactly.
MAX_BATCH = 64
PREPROCESS_WORKERS = 4
DEFAULT_MODEL = TEXT_MODEL.repo


# Replica/GPU sizing is env-driven so transcribe + htrflow can co-reside on a
# sub-3-GPU pool. Defaults pack both apps onto a 2-GPU Ray head: 2 apps x 2
# replicas x 0.49 = 1.96 GPU, leaving ~0.04 for the htr pipeline's Layout/Line
# fractions (num_gpus=0.001 each). `max_ongoing_requests=2` lets each replica
# pipeline two batches (one preprocessing on CPU while the previous runs on GPU).
SERVE_REPLICAS = int(os.environ.get("RASK_SERVE_REPLICAS", "2"))
SERVE_GPU_FRAC = float(os.environ.get("RASK_SERVE_GPU_FRAC", "0.49"))


@serve.deployment(
    num_replicas=SERVE_REPLICAS,
    ray_actor_options={"num_gpus": SERVE_GPU_FRAC, "num_cpus": 1},
    max_ongoing_requests=2,
)
class TranscribeService:
    """Serve-managed TrOCR inference. Lives across job boundaries."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_batch: int = MAX_BATCH,
        dtype: str = "bf16",
        num_beams: int | None = None,
        use_tf32: bool = False,
    ) -> None:
        # All torch / transformers imports inside methods — see module docstring
        # for why (CudnnModule isn't picklable; @serve.deployment pickles the class).
        import torch
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        from transformers.models.trocr.modeling_trocr import TrOCRSinusoidalPositionalEmbedding

        dtypes = {"fp32": torch.float32, "bf16": torch.bfloat16, "fp16": torch.float16}

        self.model_name = model
        self.max_batch = max_batch
        self.dtype = dtypes[dtype]
        self.dtype_str = dtype

        if use_tf32:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = True

        self.processor = TrOCRProcessor.from_pretrained(self.model_name, use_fast=True, revision=MODEL_REVISION)
        self.model = VisionEncoderDecoderModel.from_pretrained(
            self.model_name,
            revision=MODEL_REVISION,
            dtype=self.dtype,
            device_map="cuda:0" if torch.cuda.is_available() else None,
            attn_implementation={"encoder": "sdpa", "decoder": "eager"},
        ).eval()

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        # Same meta-tensor workaround as TranscribeActor: TrOCRSinusoidalPositionalEmbedding
        # is a plain attribute (not a buffer/parameter), so transformers >= 5.6's lazy meta-init
        # leaves it stranded on meta. Materialize on the active device with the right dtype.
        for module in self.model.modules():
            if isinstance(module, TrOCRSinusoidalPositionalEmbedding):
                module.weights = module.get_embedding(
                    module.weights.size(0),
                    module.embedding_dim,
                    module.padding_idx,
                ).to(device, self.dtype)

        self.model.config.decoder_start_token_id = 0
        self.model.config.pad_token_id = 1
        self.model.config.eos_token_id = 2
        self.model.generation_config.max_length = None
        self.num_beams = num_beams if num_beams is not None else self.model.generation_config.num_beams

        logger.info(
            "TranscribeService loaded: %s (dtype=%s, beams=%d, max_batch=%d)",
            self.model_name,
            self.dtype_str,
            self.num_beams,
            self.max_batch,
        )

    async def transcribe(self, line_images: list[Image.Image]) -> list[tuple[str, float]]:
        """Run TrOCR on a flat list of line crops; return list of (text, confidence)."""
        from concurrent.futures import ThreadPoolExecutor

        import torch

        if len(line_images) == 0:
            return []

        chunks = [line_images[i : i + self.max_batch] for i in range(0, len(line_images), self.max_batch)]
        cuda_available = torch.cuda.is_available()
        device = "cuda:0" if cuda_available else "cpu"

        def preprocess(chunk: list[Image.Image]) -> torch.Tensor:
            return self.processor(images=chunk, return_tensors="pt").pixel_values

        all_results: list[tuple[str, float]] = []
        with ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS) as executor:
            pending_futures = [executor.submit(preprocess, c) for c in chunks]
            for i, _chunk in enumerate(chunks):
                pixel_values = pending_futures[i].result().to(device, self.dtype, non_blocking=True).contiguous(memory_format=torch.channels_last)
                with torch.no_grad():
                    encoder_outputs = self.model.get_encoder()(pixel_values)
                    outputs = self.model.generate(
                        encoder_outputs=encoder_outputs,
                        max_new_tokens=128,
                        use_cache=True,
                        output_scores=True,
                        return_dict_in_generate=True,
                        num_beams=self.num_beams,
                    )
                    if cuda_available:
                        torch.cuda.synchronize()
                texts = self.processor.batch_decode(outputs.sequences, skip_special_tokens=True)

                # Confidence: mean log-prob over the actually-emitted tokens, exp'd back.
                # `compute_transition_scores` follows beam_indices so we get the log-prob
                # from the beam that actually produced each token (not naive beam-0 gather).
                transition_scores = self.model.compute_transition_scores(
                    outputs.sequences,
                    outputs.scores,
                    beam_indices=getattr(outputs, "beam_indices", None),
                    normalize_logits=True,
                )
                token_ids = outputs.sequences[:, 1:]
                pad_mask = (token_ids != self.model.config.pad_token_id).float()
                seq_lens = pad_mask.sum(dim=1)
                masked_log_probs = torch.where(pad_mask.bool(), transition_scores, torch.zeros_like(transition_scores))
                confidences = (masked_log_probs.sum(dim=1) / seq_lens.clamp(min=1)).exp()
                confidences_cpu = confidences.cpu().tolist()
                seq_lens_cpu = seq_lens.cpu().tolist()
                for j, text in enumerate(texts):
                    conf = confidences_cpu[j] if seq_lens_cpu[j] > 0 else 0.0
                    all_results.append((text, conf))

        return all_results

    async def __call__(self, line_images: list[Image.Image]) -> list[tuple[str, float]]:
        """Default entry — Serve handles route requests here."""
        return await self.transcribe(line_images)


def build_app(
    *,
    model: str = DEFAULT_MODEL,
    max_batch: int = MAX_BATCH,
    dtype: str = "bf16",
    num_beams: int | None = None,
    use_tf32: bool = False,
) -> serve.Application:
    """Build the Serve application bound with the given constructor kwargs."""
    return TranscribeService.bind(
        model=model,
        max_batch=max_batch,
        dtype=dtype,
        num_beams=num_beams,
        use_tf32=use_tf32,
    )


# Default app name — must match scripts/deploy_serve.py:APP_NAME.
APP_NAME = "transcribe"


class TranscribeViaServe:
    """Stateless Ray Data map_batches step that delegates GPU work to the
    persistent TranscribeService Serve deployment.

    Replaces TranscribeActor in the HTR pipeline. The hot CPU work (decode
    JPEG, crop lines, length-bucket, reassemble) stays here; the GPU work
    (TrOCR encode + decode) is sent to the Serve replicas via a handle.

    Why class-based rather than a free function: map_batches with a class
    runs `__init__` once per actor in the pool, letting us cache the Serve
    handle. With a free function the handle would be looked up per call.
    """

    def __init__(self, app_name: str = APP_NAME, handle: object | None = None) -> None:
        # `handle=None` is the production path — Ray Data constructs this with no
        # handle and the app is looked up through Serve. A test passes a fake, the
        # same seam `HTRFlowViaServeBytes` already offers.
        self.app_name = app_name
        self._handle = handle if handle is not None else serve.get_app_handle(app_name)

    @staticmethod
    def _shard(items: list, num_shards: int) -> list[list]:
        """Round-robin split so length-bucketed crops stay roughly balanced
        across shards (a contiguous split would put all the long lines in one
        shard, blowing decode wall time for that replica)."""
        out: list[list] = [[] for _ in range(num_shards)]
        for i, x in enumerate(items):
            out[i % num_shards].append(x)
        return out

    def __call__(self, batch):
        # Lazy imports keep this module cheaply importable in the deploy script
        # where torch/htr aren't strictly needed yet.
        from io import BytesIO

        import numpy as np
        from PIL import Image

        from htr._columns import pack, unpack
        from htr.preprocessing import crop_region
        from htr.schemas import Line, TranscribedLine

        images = [Image.open(BytesIO(img_bytes)).convert("RGB") for img_bytes in batch["image_bytes"]]
        page_lines_per_row: list[list[Line]] = [unpack(b) for b in batch["lines"]]

        # Crop every line; build (row, line) -> Line + crop entries.
        entries: list[dict] = []
        for row_idx, (img, page_lines) in enumerate(zip(images, page_lines_per_row, strict=True)):
            for line_idx, line in enumerate(page_lines):
                w = max(1, int(line.w))
                h = max(1, int(line.h))
                crop = crop_region(img, line.abs_x, line.abs_y, w, h)
                if crop.width < 2 or crop.height < 2:
                    continue
                entries.append({"key": (row_idx, line_idx), "line": line, "crop": crop})

        # Length-bucket: sort by crop width before transcribing so each GPU batch
        # has similarly-long lines and short lines aren't padded out to the
        # longest's decode length.
        entries.sort(key=lambda e: e["crop"].width)

        # Fan out within this single map_batches call: split the flat crop
        # list into SHARDS sub-requests and fire them all to Serve simultaneously.
        # Each sub-request goes to a different replica via Serve's round-robin
        # router, so all 3 GPUs run concurrently — even though Ray Data's
        # streaming executor only ever has 1 TranscribeViaServe task in flight.
        # Without this, the executor's queue rule serialises the map_batches
        # step exactly like it serialised the old TranscribeActor pool.
        #
        # SHARD THE ENTRIES, not the crops, and flatten the entries back through the
        # SAME shards — the results come home in shard-concatenated order, not in the
        # sorted order `entries` is in. Zipping against `entries` attached each line's
        # text to a different line (`w60` got the width-120 crop's transcription) for
        # any batch of four or more crops, silently: `strict=True` guards length, not
        # order. `HTRFlowViaServe` rebuilds `flat_paths` from its shards for exactly
        # this reason, and now the two read alike.
        entry_shards = self._shard(entries, num_shards=3)
        responses = [self._handle.transcribe.remote([e["crop"] for e in shard]) for shard in entry_shards if shard]
        shard_results = [r.result() for r in responses]
        flat_entries = [e for shard in entry_shards for e in shard]
        flat_results = [item for shard in shard_results for item in shard]

        line_results: dict[tuple[int, int], tuple[Line, str, float]] = {
            e["key"]: (e["line"], text, conf) for e, (text, conf) in zip(flat_entries, flat_results, strict=True)
        }

        per_row: list[list[TranscribedLine]] = []
        for row_idx, page_lines in enumerate(page_lines_per_row):
            transcribed: list[TranscribedLine] = []
            for line_idx, _ in enumerate(page_lines):
                key = (row_idx, line_idx)
                if key in line_results:
                    line, text, conf = line_results[key]
                    transcribed.append(TranscribedLine(line=line, text=text, confidence=conf))
            per_row.append(transcribed)

        batch["transcribed"] = np.array([pack(ts) for ts in per_row], dtype=object)
        return batch
