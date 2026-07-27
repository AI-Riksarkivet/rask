/**
 * The labeling abstraction — mode-AGNOSTIC, so the annotator is never coupled to a
 * single "review queue" flow. Four orthogonal axes over the provenance-rich
 * `annotations` schema (which stays mode-blind: it records `source` + `status` +
 * `confidence`, never "manual" vs "bulk"):
 *
 *   Selection (target)  ·  Producer (who)  ·  Op (what)  ·  Execution (where)
 *
 * The three annotation MODES are regions in (Producer × Execution):
 *   - Manual        = human × interactive × {one, picked}          (Label Studio)
 *   - AI-assisted   = model|propagate × interactive × {one,picked,all-from-small}
 *                     (X-AnyLabeling · INSID3 few-shot · SAM click-to-segment)
 *   - Bulk / judge  = model|judge|propagate × batch × {query, all}
 *                     (ActiveLabelingSystem loop · FiftyOne embedding-interaction)
 *
 * The two VIEWER RESOLUTIONS are just how a Selection is formed — browse
 * (page/gallery/table → one|picked) vs search/filter/embedding-interact (→ query).
 * Both feed the SAME LabelOp. See docs/ACTIVE_LABELING.md.
 */

/** The target set of a labeling op. Two GRANULARITIES, bridged by the descriptor
 *  identity `hitKey` (doc_id|speech_id|chunk_id) — the universal selection currency
 *  already used by the atlas cross-filter + the workflow scope, NOT a parallel one:
 *
 *   CHUNK-level (the READ plane's output — atlas lasso/box/cluster via
 *   `crossFilter.selectedIds`, search/filter via `scope.ts` → ScopeClause):
 *     - `chunks`  a set of `hitKey`s (the picked hits / lassoed points)
 *     - `scope`   a SQL WHERE (scope.ts `ScopeClause.clause`, cap-guarded)
 *     - `corpus`  everything
 *   Drives: chunk tags (the existing TaggerNode), "open these in the annotator", or
 *   a batch predict/propagate deriver over the scope.
 *
 *   ANNOTATION-level (WITHIN the open chunk — the annotator canvas/list, engine ROW
 *   INDEX; `id` is the stable annotation identity):
 *     - `one`     a single annotation
 *     - `picked`  a canvas/table multi-select
 *   Drives: manual set/verdict, interactive assist (SAM click / INSID3 on the page).
 */
export type ChunkSelection =
	| { level: 'chunks'; keys: string[] }
	| { level: 'scope'; where: string }
	| { level: 'corpus' };

export type AnnoSelection =
	| { level: 'one'; index: number }
	| { level: 'picked'; indices: number[] };

export type Selection = ChunkSelection | AnnoSelection;

/** Chunk-level selections are corpus-scale ⇒ batch; annotation-level run interactively. */
export const isChunkSelection = (s: Selection): s is ChunkSelection =>
	s.level === 'chunks' || s.level === 'scope' || s.level === 'corpus';

/** Who assigns the label — and the provenance stamp it writes to `annotations.source`. */
export type ProducerKind = 'human' | 'model' | 'propagate' | 'judge';

/** What happens to the target. */
export type Op = 'set' | 'verdict' | 'predict' | 'propagate' | 'judge';

/** Where the op runs. Interactive = local-first overlay → Save (merge_insert);
 *  batch = a silver deriver / lance-ray job (async, replace-protects-humans). */
export type Execution = 'interactive' | 'batch';

/** One labeling operation — the single verb spanning all three modes. */
export interface LabelOp {
	target: Selection;
	/** Registry key of the producer (see producers.ts). */
	producer: string;
	op: Op;
	execution: Execution;
	payload: {
		/** set / verdict — the field changes (e.g. {status:"accepted"}). */
		fields?: Record<string, string>;
		/** predict — open-vocab / VLM prompt (grounding-dino text, HTR none). */
		prompt?: string;
		/** propagate — annotation indices used as the few-shot reference (INSID3). */
		exemplars?: number[];
	};
}

/** The mode-blind result of applying an op — annotation field deltas keyed by row
 *  index (interactive) or id (batch merge). Status/source/confidence come from the
 *  producer, not the caller. */
export interface LabelDelta {
	index?: number;
	id?: string;
	fields: Record<string, string | number>;
}

/** Outcome of dispatching a LabelOp. Interactive ops apply immediately (deltas land
 *  in the overlay); batch ops are enqueued and surface asynchronously by media id +
 *  Lance version. */
export type LabelOutcome =
	| { status: 'applied'; deltas: LabelDelta[] }
	| { status: 'queued'; job: string; note: string }
	| { status: 'unsupported'; reason: string };
