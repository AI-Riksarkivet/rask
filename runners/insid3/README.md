# insid3-runner — in-context segmentation as an assist producer

The SECOND backend behind the assist contract (`POST /v1/assist` — same wire as `runners/assist`),
proving the producer registry: register it as `MEDIA_ASSIST_BACKENDS={"insid3": "<url>/v1/assist"}`
and the annotator routes `producer: "insid3"` here with zero code changes.

Mode: **reference propagation** — the drawn region becomes a reference mask and INSID3
(visinf/insid3, CVPR'26, Apache-2.0; one frozen DINOv3, training-free) finds everything similar on
the frame. Evaluated on real Riksarkivet pages 2026-08-03: region-level propagation works
(~0.5 s/page, ViT-S @1024 on GPU); line-level instances do not — see `open_label.md`.

Setup (weights are Meta-gated; nothing here downloads them for you):
1. `git clone https://github.com/visinf/insid3 <code-dir>` and `uv sync` THIS project.
2. Accept the DINOv3 license on Hugging Face, then
   `hf download facebook/dinov3-vits16-pretrain-lvd1689m model.safetensors --local-dir …`
3. `uv run python convert_weights.py <model.safetensors> <code-dir>/pretrain/dinov3_vits16_pretrain_lvd1689m-08c60483.pth`
   then complete/verify with the strict load (`fix` step inside the converter's docstring).
4. `INSID3_CODE=<code-dir> ASSIST_FRAME_BASE=<viewer-url> uv run python server.py`

Blackwell GPUs need the cu128 torch wheels (pinned here) — cu126 has no sm_120 kernels.
