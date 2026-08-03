# open_label — modalities, task templates, and real AI-assist

*The driving doc for the labeling-platform wave (2026-08-03). Lives at root per convention;
delete when the waves land (fold the record into OPEN-WORK §B3). Owner questions that forced it:
is audio/video/text labeling real? DIY the doccano/Label-Studio feature set or adopt? Where do
DocQA / reading order fit? Is AI-assist actually pluggable?*

## Build-vs-buy verdict: DIY the tool surfaces, do not adopt doccano/LS

The expensive 80% of doccano/LS is the **workflow machine** — queues, leases, review, consensus,
RBAC, audit. We already built it, and ours is stronger where this estate cares: two-door FGA
publish, replica consensus with server-enforced independence, adjudication with attribution,
byte-identical replay, lineage pins. Neither reference has any of that. Adopting LS means either
LS becomes the annotation system of record (the governed plane loses its guarantees) or a
permanent two-way sync bridge (a worse project than the tools). What they have and we lack:
**tool surfaces** for more modalities and the **labeling-config template system**. Both are
buildable on our loop. We do NOT rebuild: LS export formats (rides the P7c exporter), their
ML-backend zoo (our assist contract covers it).

## Honest modality audit (2026-08-03)

| Modality / task | Data model | Tool surface | Send path | Loop-tested |
|---|---|---|---|---|
| Image bbox/polygon/mask | ✔ | ✔ Pixi canvas | ✔ | **✔ live** (consensus+adjudication+publish+pin) |
| Text chunk tagging | ✔ | ✔ (media plane's original job) | ✔ | via annotations plane only |
| Text spans (NER) | ✖ no char offsets | ✖ | ✖ | ✖ |
| Audio spans | ✔ t_start/t_end | ✔ AudioViewer + waveform lane | ✖ nothing sends audio | ✖ |
| Video | frame=image | ✔ VideoViewer (ImagePlugin) | ✖ | ✖ |
| DocQA / reading order | partial (group, attributes) | ✖ no relation/order editor | n/a | ✖ |
| AI-assist | ✔ contract (producer/prompt/region → shapes; drafts origin:"model") | **mock only** | n/a | ✖ runner never deployed |

## The waves

**W1 — assist for real: CORE LANDED 2026-08-03.** The `assist-runner` image dagger-built (first
ever, 3.9 GB), pushed to the dev registry, DEPLOYED in-cluster (`rask-assist` rolled out ready,
weights loaded) and driven for real: GroundingDINO returned a genuine "text" box (conf 0.7476) and
SAM a real polygon mask (conf 0.637) over the actual corpus page — via the canvas, the mock chip
gone, the prediction landing in the Review queue like any human work (screenshot kept). The
in-cluster runner cannot fetch frames until the cluster corpus mount is seeded (`ASSIST_FRAME_BASE`
→ viewer), so inference was driven through the local corpus against the same image. **W1 tail LANDED
2026-08-03 — pluggability PROVEN, not claimed:** (1) the producer REGISTRY
(`MEDIA_ASSIST_BACKENDS` name→URL, longest-prefix routing, `assist_url` fallback, mock when bare —
`backend_for` + `test_assist_registry.py`); (2) **INSID3 wired as the SECOND backend**
(`runners/insid3/` — same `/v1/assist` contract, reference-propagation mode: the drawn region
becomes the reference and the frozen DINOv3 finds everything similar) and driven END TO END: the
canvas bar now offers **Detect · Segment · insid3** (registry producers surface via `/api/config
assistProducers`), a drawn region returned a real 15-point polygon as `model:insid3`, landing as a
`prediction` in the review queue (screenshot kept) — the annotator's routing path unchanged;
(3) the previously-missing hermetic Playwright spec for assist→review (contract POST asserted,
prediction chip, Accept drains to accepted — required a full-schema empty Arrow fixture). Still
open in W1: the batch mode (`jobsUrl`), the in-cluster corpus seed (frames for `rask-assist`),
an insid3 image build (the runner runs from a checkout; weights are Meta-gated per its README).

**INSID3 evaluated as a candidate backend (2026-08-03, owner-requested —
github.com/visinf/insid3, CVPR'26 oral: training-free in-context segmentation on one frozen
DINOv3).** Tested for real on REAL Riksarkivet IIIF pages (A0060198, 18th-c. cursive), GPU
(RTX PRO 6000): **0.4–0.7 s/page with ViT-S @1024 — interactive-grade.** Verdict: it is a THIRD
assist MODE — reference-based propagation ("label one item, prelabel the rest") — which neither
grounding-dino (text prompt) nor SAM (geometry prompt) covers, and which LS/CVAT lack natively.
Quality on archival cursive: region-level concepts propagate well (a masked column found the
written leaf on the NEXT page with a clean boundary); line-level instances do NOT (one masked
line collapses to "the written area" — coarse, paper bleed). So: promising for text-REGION /
layout pre-labeling, not line segmentation. Untested upside: 5-shot, tau/merge tuning, ViT-L,
CRF. Integration facts: Apache-2.0 code; DINOv3 weights are Meta-gated — the HF safetensors were
converted back to the original checkpoint format (inverse of transformers' key mapping, k-bias
zero-filled, init-constant buffers merged, STRICT-load verified); torch needs cu128 on Blackwell
(cu126 wheels lack sm_120 — bit once). Wiring it as a producer needs a `reference` field
(item + mask) on the assist contract — fold into the W1 registry design.

**W2 — task templates v1 (the LS-config equivalent).** Declarative `template` on the labeling
task: `kind` (bbox-detection | segmentation | classification | text-span | transcription | doc-qa |
reading-order), `modality`, `tools`, `output` (required labels + typed attributes). Create-dialog
presets; canvas/queue constrain tools; submit validates the output block SERVER-side (same
never-trust-the-client posture as review_required); publish stamps the template into properties +
facet. v1 deliberately skips nested/conditional config (LS's XML rabbit hole).

**W3 — text spans (doccano-parity core).** New `char_start`/`char_end` on Shape (additive), a
span tool over the item's text panel, `text-span` + `classification` templates. The whole
review/consensus/publish machine needs zero new work — the DIY payoff.

**W4 — audio through the loop (then video frames).** A send path producing `media.kind: audio`
items (hits carry media_url), waveform span labeling, `transcription` template (span + text). One
audio item claimed→labeled→published live is the acceptance. Video v1 stays frame-based;
interpolation/tracking (CVAT's moat) out of scope until a use case names it.

**W5 — relations & reading order.** `relation` tool (arrow between shapes → group linkage +
typed attribute), `order` (integer attribute + next/prev flow). `doc-qa` = question span +
relation to answer span; `reading-order` = order over bboxes.

## Why this order

W1 validates an already-shipped claim (pluggable assist) + the LLM-labeller ask. W2 before
W3/W4/W5 so each later wave is *configuration plus one tool* rather than another hardcoded mode.
W3 before W4 because text needs no new send infrastructure. Frontend work follows the 2026-08-03
data-plane direction: new JSON surfaces are remote functions; only Arrow/binary planes stay BFF.
