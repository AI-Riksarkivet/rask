# Labeling modalities & task templates — the plan

*2026-08-03. Owner questions that forced this: is audio/video/text labeling real? Do we DIY the
doccano/Label-Studio feature set or adopt? Where do DocQA / reading order / NER fit? Is AI-assist
actually pluggable?*

## The build-vs-buy verdict first, because it shapes everything

**DIY the tool surfaces. Do not adopt doccano or Label Studio. The reasoning is an asymmetry:**

The expensive 80% of doccano/LS is not the drawing tools — it is the **workflow machine**: task
queues, leases, assignment, review states, consensus, RBAC, audit, export. **We already built that,
and ours is strictly stronger where it matters to this estate**: two-door FGA-gated publish,
replica-based consensus with server-enforced independence, adjudication with attribution,
byte-identical replay, and lineage pins — none of which either reference has. Adopting LS would
mean either (a) LS becomes the system of record for annotation state — the governed plane loses
exactly the guarantees it exists for — or (b) a permanent two-way sync bridge, which is a worse
project than the tools themselves. Doccano is text-only and brings the same integration cost for a
third of the feature set.

What LS has that we genuinely lack is (1) **tool surfaces** for more modalities and (2) the
**labeling-config template system** that turns "a new task type" into configuration instead of
code. Both are buildable on our loop; the plan below is exactly those two things, in dependency
order. What we do NOT rebuild: LS's export formats (rides the P7c exporter, already deferred),
their ML-backend zoo (our assist contract covers it), their annotation-free viewers.

## Where each modality honestly stands (audited 2026-08-03)

| Modality / task | Data model | Tool surface | Send path | Loop-tested |
|---|---|---|---|---|
| Image bbox/polygon/mask | ✔ `Shape` | ✔ Pixi canvas | ✔ search/atlas/keys | **✔ live** (consensus+adjudication+publish+pin) |
| Text chunk tagging | ✔ `tag`/`text` | ✔ (media plane's original job) | ✔ | ✔ via annotations plane, not the task loop |
| Text spans (NER-style) | ✖ no offsets | ✖ | ✖ | ✖ |
| Audio spans | ✔ `t_start/t_end` | **✔ exists** (`AudioViewer`, engine waveform lane) | ✖ nothing sends audio | ✖ |
| Video | partial (frame = image) | ✔ `VideoViewer` (reuses ImagePlugin) | ✖ | ✖ |
| DocQA / reading order / relations | partial (`group`, free `attributes`) | ✖ no relation/sequence editor | n/a | ✖ |
| AI-assist | ✔ contract (`producer`, region, prompt → shapes; drafts `origin:"model"`) | mock only | n/a | ✖ — runner never deployed |

## The waves

### W1 — Assist for real (in flight now)

The `runners/assist` image (GroundingDINO-tiny + SAM, CPU) is dagger-building as this document is
written. Slice: import → enable `runners.assist` → `MEDIA_ASSIST_URL` flips the annotator's mock →
drive a canvas assist request live (both producers) → then **prove the pluggability claim** by
registering a second backend (a trivial echo/LLM producer) behind the same contract and driving it.
Acceptance: a live screenshot of model-suggested shapes landing as `origin:"model"` drafts riding
the normal review loop, and a contract test a third-party backend could be built against.

### W2 — Task templates v1 (the LS-config equivalent; unlocks everything after it)

A `template` on the labeling task, declarative and small:

```
template:
  kind: bbox-detection | segmentation | classification | text-span | transcription |
        doc-qa | reading-order
  modality: image | text | audio | video     # what the item viewer mounts
  tools: [bbox, polygon, span, tag, text, relation, order]
  output:                                     # what a completed item must contain
    required_labels: [...]                    # from label_schema
    attributes: {name: enum|free|int, ...}    # typed, validated at submit
```

Create-dialog: pick a template (presets seed label_schema + tools). The canvas/queue constrain
tools to the template; `submit` validates the output block server-side (a task the template says
needs a `question`+`answer_span` refuses submission without them — the same
never-trust-the-client posture as `review_required`). Publish stamps the template into table
properties + the run facet, so a downstream consumer knows what SHAPE of labels it holds. This is
deliberately v1-narrow: no nested/conditional config (LS's XML rabbit hole) — presets + typed
attributes cover the named tasks.

### W3 — Text spans (the doccano-parity core)

`span` tool: character-offset selections over the item's text (offsets in `Shape.x`/`width` reuse
is a lie — new fields `char_start`/`char_end`, additive schema change). Viewer: the text panel the
review-selection UI already renders, plus range selection → labeled span chips. Templates
`text-span` (NER) and `classification` (doc-level tag) make doccano's two core modes
configurations. Consensus/review/adjudication/publish need ZERO new work — that is the DIY payoff.

### W4 — Audio through the loop (then video frames)

The corpus already has audio. Slice: a send path producing `media.kind: audio` items (search hits
carry the chunk's `media_url`), the existing waveform lane labels `t_start/t_end` spans,
`transcription` template pairs a span with `Shape.text`. Live drive = one audio item claimed,
span-labeled, published. Video v1 stays frame-based (VideoViewer + image tools per frame);
interpolation/tracking (CVAT's moat) is explicitly out of scope until a real use case names it.

### W5 — Relations & reading order (DocQA becomes a template)

The `relation` tool: an arrow between two shapes (stored as `Shape.group` linkage + a typed
attribute), and `order` as an integer attribute with a next/prev keyboard flow. `doc-qa` =
`text-span`(question) + `relation` to the answer span; `reading-order` = `order` over bboxes.
Rendering rides `@rask/flow` if an overlay graph is wanted, else plain canvas arrows.

## Sequencing rationale

W1 is running because it validates a claim already shipped (pluggable assist) and the LLM-labeller
path the owner asked for twice. W2 before W3/W4/W5 because every later wave becomes *configuration
plus one tool* once templates exist — building spans or audio first would hardcode a second
implicit template and deepen the hole. W3 before W4 because text needs no new send infrastructure.

Frontend work follows the 2026-08-03 data-plane direction: new JSON surfaces are remote functions;
only the Arrow/binary planes stay BFF.
