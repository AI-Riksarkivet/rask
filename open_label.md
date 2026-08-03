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

**W1 — assist for real (IN FLIGHT).** `assist-runner` image dagger-building (first ever). Then:
import → `runners.assist` enabled → `MEDIA_ASSIST_URL` flips mock→real → live canvas drive (both
producers) → prove pluggability with a second backend behind the same contract + a contract test.
Acceptance: model shapes land as `origin:"model"` drafts in the live review loop, screenshot kept.

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
