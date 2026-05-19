"""TranscribeActor — TrOCR transcription with KV-cache and cross-row length bucketing.

Ray Data map_batches actor. Reads `image_bytes` + `lines`, writes `transcribed`.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image
from torch.cuda import nvtx
from torch.profiler import ProfilerActivity, profile, schedule
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
from transformers.models.trocr.modeling_trocr import TrOCRSinusoidalPositionalEmbedding

from htr._columns import pack, unpack
from htr.preprocessing import crop_region
from htr.schemas import Line, TranscribedLine


logger = logging.getLogger(__name__)

MAX_BATCH = 64  # length-bucketed; bigger amortizes kernel launches but raises peak GPU memory.
# 64 fits two actors per GPU on a 96 GB card with ~30 GB/actor headroom for decoder KV cache;
# 128 OOMs on text-dense volumes (A0038595).
PREPROCESS_WORKERS = 4  # number of concurrent CPU preprocessing threads feeding the GPU

_DTYPES = {"fp32": torch.float32, "bf16": torch.bfloat16, "fp16": torch.float16}


class _nvtx:
    """Context manager wrapping torch.cuda.nvtx.range_{push,pop} for nsys/Kineto.

    Cost when no profiler is attached is ~30 ns per push+pop — safe to leave on
    in production code paths.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "_nvtx":
        nvtx.range_push(self.name)
        return self

    def __exit__(self, *exc: object) -> None:
        nvtx.range_pop()


class TranscribeActor:
    """TrOCR transcription actor. Flattens lines across the input batch, length-buckets
    by crop width, transcribes in chunks, then re-assembles per-page output.

    Optimizations preserved from the original ra_batch.models.transcription.Transcriber:
      - bf16 / fp16 weights via HF dtype + device_map
      - cuDNN benchmark for fixed-shape (384x384) encoder
      - SDPA (Flash) attention on the encoder, eager on the decoder
      - channels-last memory format on pixel inputs
      - KV-cache decoding with `use_cache=True`
      - cross-row length bucketing — sort by crop width before batching
      - background thread preprocesses chunk N+1 while GPU runs chunk N
      - per-line confidence from generation log-probs
      - meta-tensor workaround for transformers >= 5.6 + TrOCRSinusoidalPositionalEmbedding
    """

    def __init__(
        self,
        model: str = "Riksarkivet/trocr-base-handwritten-hist-swe-2",
        max_batch: int = MAX_BATCH,
        use_tf32: bool = False,
        num_beams: int | None = None,
        dtype: str = "fp32",
        compile: bool = False,  # noqa: A002 — kwarg name is intentional and shadows builtin only locally
        profile_dir: str | Path | None = None,
        **_unused: Any,  # noqa: ANN401
    ):
        # Cheap config only — model loading is in _ensure_model() so weights land on the
        # actor's GPU when running under Ray Data.
        self.model_name = model
        self.max_batch = max_batch
        self.use_tf32 = use_tf32
        self.dtype_str = dtype
        self.dtype = _DTYPES[dtype]
        self._configured_num_beams = num_beams
        self._compile = compile
        self._loaded = False
        # When set, every __call__ runs inside a torch.profiler context and emits a
        # Kineto Chrome-trace JSON. Each actor writes its own file (PID in the name).
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._profile_call_idx = 0

    def _ensure_model(self) -> None:
        if self._loaded:
            return
        if self.use_tf32:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        # cuDNN benchmark picks fastest kernels for fixed shapes (encoder is 384x384).
        torch.backends.cudnn.benchmark = True

        self.processor = TrOCRProcessor.from_pretrained(self.model_name, use_fast=True)
        # device_map + dtype propagates through HF's lazy-init path so weights land
        # on cuda in the right precision; encoder gets HF native SDPA (Flash) for free.
        self.model = VisionEncoderDecoderModel.from_pretrained(
            self.model_name,
            dtype=self.dtype,
            device_map="cuda:0" if torch.cuda.is_available() else None,
            attn_implementation={"encoder": "sdpa", "decoder": "eager"},
        ).eval()
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        # Workaround: TrOCRSinusoidalPositionalEmbedding.weights is a plain attribute
        # (not a buffer/parameter), so transformers >= 5.6's lazy meta-init leaves it
        # stranded on meta. Materialize it on the active device with the right dtype.
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
        # Drop max_length so max_new_tokens is the sole generation cap (avoids
        # the transformers "both set" warning on every batch).
        self.model.generation_config.max_length = None
        self.num_beams = self._configured_num_beams if self._configured_num_beams is not None else self.model.generation_config.num_beams
        # Apply torch.compile last (after all module mutations). Profile shows
        # ~30% of wall is cudaStreamSynchronize from HF generate's per-token
        # `.item()`/`is_nonzero` calls; kernel fusion + reduced launch overhead
        # via torch.compile shrinks this. Use mode="default" + dynamic=True so
        # length-bucketed chunks of varying widths don't trigger recompilation.
        if self._compile and torch.cuda.is_available():
            try:
                self.model = torch.compile(self.model, mode="default", dynamic=True)
                logger.info("TranscribeActor: torch.compile applied (mode=default, dynamic=True)")
            except Exception as e:
                logger.warning("TranscribeActor: torch.compile failed (%s) — falling back to eager", e)
        logger.info(
            "TranscribeActor loaded: %s (dtype=%s, beams=%d, max_batch=%d, tf32=%s, compile=%s)",
            self.model_name,
            self.dtype_str,
            self.num_beams,
            self.max_batch,
            self.use_tf32,
            self._compile,
        )
        self._loaded = True

    def _transcribe(self, line_images: list[Image.Image]) -> list[tuple[str, float]]:
        """Transcribe line images in chunks, returning (text, confidence) tuples.

        Uses a thread pool to preprocess up to PREPROCESS_WORKERS chunks ahead of
        the GPU, overlapping the HF processor's CPU work with encoder/decoder GPU work.
        """
        if len(line_images) == 0:
            return []
        chunks = [line_images[i : i + self.max_batch] for i in range(0, len(line_images), self.max_batch)]
        cuda_available = torch.cuda.is_available()
        device = "cuda:0" if cuda_available else "cpu"

        def preprocess(chunk: list[Image.Image]) -> torch.Tensor:
            return self.processor(images=chunk, return_tensors="pt").pixel_values

        all_results: list[tuple[str, float]] = []
        with ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS) as executor:
            # Submit every chunk up front; the pool processes up to PREPROCESS_WORKERS in parallel.
            # The GPU loop consumes futures in order and so always has a preprocessed batch ready.
            pending_futures = [executor.submit(preprocess, c) for c in chunks]
            for i, chunk in enumerate(chunks):
                with _nvtx(f"chunk_{i}/{len(chunks)} ({len(chunk)} lines)"):
                    with _nvtx("h2d"):
                        pixel_values = pending_futures[i].result().to(device, self.dtype, non_blocking=True).contiguous(memory_format=torch.channels_last)
                    with torch.no_grad():
                        if cuda_available:
                            enc_start = torch.cuda.Event(enable_timing=True)
                            enc_end = torch.cuda.Event(enable_timing=True)
                            dec_start = torch.cuda.Event(enable_timing=True)
                            dec_end = torch.cuda.Event(enable_timing=True)

                            enc_start.record()
                            with _nvtx("encoder"):
                                encoder_outputs = self.model.get_encoder()(pixel_values)
                            enc_end.record()

                            dec_start.record()
                            with _nvtx("decoder.generate"):
                                outputs = self.model.generate(
                                    encoder_outputs=encoder_outputs,
                                    max_new_tokens=128,
                                    use_cache=True,
                                    output_scores=True,
                                    return_dict_in_generate=True,
                                    num_beams=self.num_beams,
                                )
                            dec_end.record()
                            torch.cuda.synchronize()
                            logger.debug(
                                "trocr chunk: %d lines, encoder=%.1fms decoder=%.1fms",
                                len(chunk),
                                enc_start.elapsed_time(enc_end),
                                dec_start.elapsed_time(dec_end),
                            )
                        else:
                            with _nvtx("encoder"):
                                encoder_outputs = self.model.get_encoder()(pixel_values)
                            with _nvtx("decoder.generate"):
                                outputs = self.model.generate(
                                    encoder_outputs=encoder_outputs,
                                    max_new_tokens=128,
                                    use_cache=True,
                                    output_scores=True,
                                    return_dict_in_generate=True,
                                    num_beams=self.num_beams,
                                )
                    with _nvtx("decode_tokens"):
                        texts = self.processor.batch_decode(outputs.sequences, skip_special_tokens=True)
                # `compute_transition_scores` handles beam search correctly: it follows
                # `outputs.beam_indices` to gather the log-prob of each token from the
                # beam that *actually produced it*, instead of naively gathering from
                # beam 0. Without this the WC values come out at ~1e-9 because beam-0
                # had nothing to do with the chosen sequence.
                # `normalize_logits=True` makes the scores log-probs so we can mean-then-exp.
                transition_scores = self.model.compute_transition_scores(
                    outputs.sequences,
                    outputs.scores,
                    beam_indices=getattr(outputs, "beam_indices", None),
                    normalize_logits=True,
                )
                token_ids = outputs.sequences[:, 1:]
                pad_mask = (token_ids != self.model.config.pad_token_id).float()
                seq_lens = pad_mask.sum(dim=1)
                # Replace -inf at padded positions with 0 so they don't poison the mean.
                masked_log_probs = torch.where(pad_mask.bool(), transition_scores, torch.zeros_like(transition_scores))
                confidences = (masked_log_probs.sum(dim=1) / seq_lens.clamp(min=1)).exp()
                confidences_cpu = confidences.cpu().tolist()
                seq_lens_cpu = seq_lens.cpu().tolist()
                for j, text in enumerate(texts):
                    conf = confidences_cpu[j] if seq_lens_cpu[j] > 0 else 0.0
                    all_results.append((text, conf))
        return all_results

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if self.profile_dir is not None:
            return self._call_profiled(batch)
        return self._call_inner(batch)

    def _call_profiled(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        idx = self._profile_call_idx
        self._profile_call_idx += 1
        trace_path = self.profile_dir / f"transcribe-pid{os.getpid()}-call{idx:03d}.json"
        activities = [ProfilerActivity.CPU]
        if torch.cuda.is_available():
            activities.append(ProfilerActivity.CUDA)
        # `wait=0, warmup=0, active=1, repeat=1` profiles exactly this one __call__ end-to-end.
        # profile_memory + record_shapes balloon the trace to multi-GB per actor;
        # default off for production tracing. Re-enable locally if you need them.
        with profile(
            activities=activities,
            schedule=schedule(wait=0, warmup=0, active=1, repeat=1),
            record_shapes=False,
            with_stack=False,
            profile_memory=False,
        ) as prof:
            with _nvtx(f"transcribe_call_{idx}"):
                out = self._call_inner(batch)
            prof.step()
        prof.export_chrome_trace(str(trace_path))
        logger.info("TranscribeActor: wrote Kineto trace to %s", trace_path)
        return out

    def _call_inner(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        self._ensure_model()
        # Flatten all valid line crops across every row into one pool, then
        # length-bucket (sort by crop width) before transcribing so each GPU batch has
        # similarly-long lines — avoids padding short lines out to the longest's decode length.
        with _nvtx("decode_jpeg"):
            images = [Image.open(BytesIO(img_bytes)).convert("RGB") for img_bytes in batch["image_bytes"]]

        # Unpack each row's `list[Line]` from the pickle column.
        page_lines_per_row: list[list[Line]] = [unpack(b) for b in batch["lines"]]

        with _nvtx("crop_lines"):
            entries: list[dict] = []
            for row_idx, (img, page_lines) in enumerate(zip(images, page_lines_per_row, strict=True)):
                for line_idx, line in enumerate(page_lines):
                    w = max(1, int(line.w))
                    h = max(1, int(line.h))
                    crop = crop_region(img, line.abs_x, line.abs_y, w, h)
                    if crop.width < 2 or crop.height < 2:
                        continue
                    entries.append({"key": (row_idx, line_idx), "line": line, "crop": crop})

        # Length-bucket within batch: sort by crop width before transcribing.
        entries.sort(key=lambda e: e["crop"].width)
        with _nvtx(f"transcribe ({len(entries)} lines)"):
            flat_results = self._transcribe([e["crop"] for e in entries])
        line_results: dict[tuple[int, int], tuple[Line, str, float]] = {
            e["key"]: (e["line"], text, conf) for e, (text, conf) in zip(entries, flat_results, strict=True)
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
