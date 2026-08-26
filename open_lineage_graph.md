# open: lineage graph — Marquez parity backlog

Produced 2026-08-26 by an 8-agent audit (4 comparison dimensions, each finding adversarially
re-verified against **both** codebases by a second agent that re-ran the probes). **57 verified
differences, 30 confirmed matches.** Every item below carries file:line on both sides in the audit
transcript; this file keeps the decisions and the ordering.

Marquez read at `MarquezProject/marquez@main` via `gh api`. rask read at `main` (`bc4539bc`).

**Read the P0 section first: three of its four items are errors I shipped, and two are regressions
introduced by the ELK change itself (`08d82eea`).**

---

## P0 — wrong today, and mine

### 1. ELK stomps dragged node positions on every poll  *(significant)*

`LineageGraph.svelte:403` calls `elkLayout` unconditionally inside the build effect, and the
overwrite at `:407-410` is unconditional. The effect re-runs on every successful poll, because
`store.svelte.ts:66,72` reassigns fresh arrays each tick.

**This is a regression, verified against git.** `git show 08d82eea` shows the whole ELK block is new
in that commit. Before it, `prev.get(...)?.position ?? place.get(...)` was the only writer of
position, so a dragged node survived indefinitely. It now survives at most until the next poll.

Worse, the store's own comment at `store.svelte.ts:62-65` says its hard-failure guard exists so a
blip "never … destroys dragged node positions" — the ELK path destroys them on every *successful*
poll instead.

**Fix:** memoise the ELK input the way Marquez does — `useLayout.ts:153-156` compares the new ELK
input against a ref and returns early when equal, so a data-only change never re-lays out.

### 2. The depth comment and tooltip assert an equivalence that does not exist  *(significant)*

I changed depth to "graph hops, exactly as Marquez counts it" and wrote that claim into both a code
comment (`LineageGraph.svelte:321-324`) and a UI tooltip (`:573`). **Both are wrong.**

Marquez's depth is **job hops, server-side**: `LineageDao.java:69-99` is a recursive CTE seeding
`0 AS depth` from the rooted *job* and recursing while `depth < :depth`, stepping job→job on "shares
any dataset". Datasets are attached afterwards *regardless of depth*
(`LineageService.java:101-108`). The root is a job even when you ask for a dataset.

So Marquez `depth=2` ≈ rask hop 6, not hop 2. The two numbers are not the same unit, which is
exactly the outcome the comment claims to have avoided.

**Fix:** correct the comment and the tooltip. Decide separately whether to *match* the unit — that is
entangled with item 7 (depth fetches vs filters), so do not change behaviour to chase the label.

### 3. Three options documented as deliberate tuning are ELK defaults  *(cosmetic, but it is false documentation)*

`elk-layout.ts` carries `separateConnectedComponents: 'true'` (:94), `edgeRouting: 'ORTHOGONAL'`
(:87) and `nodePlacement.strategy: 'BRANDES_KOEPF'` (:90), each with a comment explaining it as a
choice. **All three restate ELK's defaults.** Leave-one-out: deleting :94 changes nothing
(1400×416 either way on a lineage-shaped probe).

The component-packing win over Marquez is real — but it comes from rask *not deviating*, while
Marquez forces separation off (`useLayout.ts:104` + `:117 hierarchyHandling: INCLUDE_CHILDREN`, which
are redundant with each other). My commit message for `08d82eea` claims the credit the wrong way
round.

The option sets differ **effectively on two axes only**: component separation, and layering
(`COFFMAN_GRAHAM`, effective only once separation is off) — plus rask's spacing and padding.

### 4. The docstring misdescribes Marquez's edge routing  *(cosmetic)*

`elk-layout.ts` calls Marquez's routing "a bundle of wires". Marquez routes orthogonally in ELK *and*
renders the orthogonal path as an SVG polyline (`Edge/ElbowEdge.tsx:43-50`). The comment asserts a
deliberate divergence that does not exist, in the file whose whole purpose is convergence.

---

## P1 — real functional gaps

| # | Gap | Detail |
|---|---|---|
| 5 | **Column graph has no layout engine** | `ColumnLineage.svelte:102` places by arithmetic (`x = 20 + layer*230, y = 24 + row*76`). Marquez shares one ELK across *both* graphs. Fix: point it at `elkLayout()`. |
| 6 | **"All" drops the upstream/downstream colouring** | Turning Full Graph on is the moment Marquez's highlight becomes meaningful, and the moment rask's disappears. |
| 7 | **Depth filters, it does not fetch** | Marquez's depth controls what is *fetched*; rask's filters an already-fetched, hard-capped window (union of a 60-dataset and a 200-event window). |
| 8 | **Search cannot leave the focused neighbourhood** | It matches only nodes currently drawn — deliberate, but it means you cannot jump *out* of a focus. |
| 9 | **No detail drawer** | Marquez's node click navigates *and* opens a drawer in one gesture; rask only re-roots, and detail is a separate page. |
| 10 | **ELK gets one constant box per node** | The `size` hook (`elk-layout.ts:56`) is dead — the sole call site passes no third argument, so every node is declared 200×64 while cards are content-height. Overlap needs a rendered height >98px (ELK reserves 64, sibling gap 34); chip-heavy cards approach it. Marquez feeds the real box, incl. `34 + fields.length * 10`. |
| 11 | **ELK's edge geometry is discarded** | We read only `child.x/y`; a lineage probe returns bendPoints `[2,2,0,0,2,2,2,0,2,0,0,0]`. Marquez draws them. Low visible impact (smoothstep also draws right angles) but it is information thrown away. |

---

## P2 — architectural

| # | Item |
|---|---|
| 12 | **Client-side estate composition vs server-side ego-graph.** rask composes a globally-capped feed in the browser; Marquez fetches a rooted subgraph. This is the scale ceiling, and the last structural divergence. |
| 13 | **rask's table graph is not bipartite** — it carries dataset→dataset fallback edges Marquez's model cannot express. Deliberate (the event window is bounded) but worth recording as a divergence. |
| 14 | **Job identity folds all runs of a job name into one node**, which then requires deleting edges to stay acyclic. Marquez takes per-job identity. This is the root of the cycle guard. |
| 15 | **ELK runs on the main thread**; Marquez uses a real Web Worker plus a progress indicator. |
| 16 | **elkjs version skew** between the two. |

---

## P3 — feature gaps (each small, none load-bearing)

Compact Nodes switch · per-node collapse chevron + `?collapsedNodes=` · manual Refresh ·
center-on-selected · column-graph depth control · column-graph hover highlighting of the connected
chain · column-graph minimap · rich hover cards (rask uses native `title`) · selected column as URL
state · node-identity-in-URL shape (path segments vs query params).

---

## Confirmed matches (do not "fix" these)

Both have a real, separate column-level graph. Reading direction left-to-right, upstream left. Zoom
in / out / fit on both. A side panel opens for the clicked thing on both. Depth carried in the URL
under the key `depth` on both. Name-search that re-roots on the picked node on both. Focused-node
marking on both. One canvas holding two node kinds, `type: JOB | DATASET`, on both. Node-id
convention `<type>:<name>` matching `generateNodeId`.

Two claimed matches were **refuted** by verification: Marquez's column graph *does* show a minimap
(`ZoomPanSvg.tsx:380-391`) and rask's does not; and the depth *default* matches at 2 while the
*unit* does not (item 2).
