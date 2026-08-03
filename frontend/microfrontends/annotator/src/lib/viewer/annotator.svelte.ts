/**
 * AnnotatorController — the reactive facade the ra-anno layout binds to.
 *
 * The engine core (InteractionManager / ArrowDataPlugin / ImagePlugin) is
 * framework-agnostic and lives locked inside PixiCanvas's context, reachable
 * only from the `onready` ctx. ra-anno's original layout assumed a Svelte
 * binding layer (`$lib/stores/*.svelte.ts`) that was never ported. This class
 * IS that layer: a route-level, Svelte-5-runes source of truth that
 *   1. captures the engine handle (via `attach`),
 *   2. mirrors the engine's plain getters + callbacks into reactive state so
 *      dumb/controlled layout components (Toolbar, Sidebar, ZoomControls, …)
 *      stay reactive, and
 *   3. owns the layer grouping + the annotation table the sidebar reads.
 *
 * Modality-neutral: spatial (image/video-frame) viewers `attach` a PixiContext;
 * a temporal (audio) viewer can attach its own control surface to the same
 * controller later. The layout binds to the controller, never to the engine.
 */
import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { toast } from 'svelte-sonner';
import type { Table } from 'apache-arrow';
import type { CommitShape, GeometryUpdate, PixiContext, Tool } from '@rask/engine';
import { LayerStore, buildBatchTable } from '@rask/engine';
import type { LabelDelta, LabelOp, LabelOutcome, Selection } from '@rask/labeling/types';
import { canonicalShapeType, engineToolsFor } from '@rask/labeling/shape-types';
import { isChunkSelection } from '@rask/labeling/types';
import { PRODUCERS } from '@rask/labeling/producers';
import { rowSignature } from '@rask/labeling/history';
import {
	AnnotationsHttpError,
	type InsertRow,
	buildSavePayload,
	loadAnnotations,
	makeInsertRow,
	payloadIsEmpty,
	postSave,
} from '@rask/labeling/annotations-client';
import { type AnnoRow, effectiveField, projectRows, rawField } from './annotation-rows';
import { requestAssist } from './remote/assist.remote';
import { submitBatchJob } from './remote/jobs.remote';

// Re-exported so the layout components keep their existing import site.
export type { AnnoRow } from './annotation-rows';
export type { InsertRow } from '@rask/labeling/annotations-client';

export type Mode = 'view' | 'edit';

/** The unit behind an annotations URL — the assist command's arguments.
 *
 *  `assistUrlFor` used to do this string surgery to build a SECOND url (`/api/annotations/…` →
 *  `/api/assist/…`) for a browser fetch. The assist transport is a remote command now, so the same
 *  surgery yields its args and the server builds the upstream path itself. Returns null when the
 *  controller holds no annotations URL shape — assist has nothing to run against. */
function assistTargetFor(url: string): { key: string; dataset: string | null } | null {
	const marker = '/api/annotations/';
	const at = url.indexOf(marker);
	if (at < 0) return null;
	const [key = '', search = ''] = url.slice(at + marker.length).split('?');
	return key === '' ? null : { key, dataset: new URLSearchParams(search).get('dataset') };
}

export interface BrushOptions {
	radius: number;
	erasing: boolean;
	maskMode: 'instance' | 'semantic';
	output: 'mask' | 'polygon';
}

/** Fields the sidebar can edit inline. */
export type EditableField = 'label' | 'status' | 'group' | 'text';

/** One reversible field edit (relabel / status / text / group).
 *  `before`/`after` are the effective (overlay-aware) string values. */
interface FieldEdit {
	kind: 'field';
	index: number;
	field: EditableField;
	before: string;
	after: string;
}

/** A drawn shape, reversible by hiding the appended row and dropping it from the save payload. */
interface InsertEdit {
	kind: 'insert';
	/** Row index of the appended row — an insert APPENDS, so this never shifts. */
	index: number;
	/** Position in `_inserts`, so redo can put the same row back where it was. */
	at: number;
	row: InsertRow;
}

/** A delete, reversible by un-hiding the row and dropping its id from the save payload. */
interface DeleteEdit {
	kind: 'delete';
	index: number;
	id: string;
}

/** A geometry move/resize/vertex-drag. `before` is null when the row had no pending edit yet. */
interface GeometryEdit {
	kind: 'geometry';
	index: number;
	before: GeometryUpdate | null;
	after: GeometryUpdate;
}

/** The unit of undo/redo.
 *
 *  This was `FieldEdit` alone, so drawing a shape, deleting one and dragging a vertex were all
 *  UNDOABLE ONLY BY RELOADING — which loses the whole session. With several annotators doing
 *  hundreds of shapes a day that is work lost in week one, and it is why an annotator stops
 *  trusting the canvas. One stack, so Ctrl+Z means the same thing whatever the last action was. */
type UndoOp = FieldEdit | InsertEdit | DeleteEdit | GeometryEdit;

const STRING_FIELD_CANDIDATES = ['label', 'status', 'source', 'group', 'reviewer'];

/** #rrggbb ⇄ 0xRRGGBB — self-contained so the panel needs no engine color util. */
function numToHex(n: number): string {
	return '#' + (n & 0xffffff).toString(16).padStart(6, '0');
}
function hexToNum(hex: string): number {
	return parseInt(hex.replace(/^#/, ''), 16) & 0xffffff;
}

export class AnnotatorController {
	// ── engine handle + data ──
	ctx = $state<PixiContext | null>(null);
	table = $state<Table | null>(null);

	// ── mirrored engine state ──
	mode = $state<Mode>('edit');
	activeTool = $state<Tool>('select');
	selectedIndex = $state<number | null>(null);
	readonly selectedSet = new SvelteSet<number>();
	// true while multi-selecting (Shift/Ctrl-click) — keeps the sidebar on the list so
	// more can be picked, instead of opening the single-annotation detail.
	multiSelect = $state(false);
	zoomPercent = $state(1);
	// Playhead time (video): a shape drawn on a paused frame is pinned to this moment, so
	// spatial + temporal share the annotations table. Stays 0 for images (no time axis).
	timeCursor = $state(0);
	// When set (e.g. "sam-click"), the next drawn box/point is sent to that interactive
	// segmenter as its region prompt instead of being inserted as a manual shape — the
	// ra-atr SAM click-to-segment loop. Null = normal drawing.
	assistProducer = $state<string | null>(null);
	// True when the magnetic corner-snap tool can run — a still image is loaded
	// (video frames don't qualify). Gates their toolbar buttons.
	cvCapable = $state(false);
	// Which CV tools have finished their lazy init (wasm + edge/corner maps) — the
	// toolbar shows loading state until then; the E2E suite waits on it.
	readonly cvReady = new SvelteSet<string>();
	// Live magnetic snap state — true while the cursor is locked onto a detected corner
	// (surfaced on the toolbar; the E2E asserts snapping actually engages).
	magneticSnapped = $state(false);
	count = $state(0);
	saving = $state(false);
	saveError = $state<string | null>(null);
	brushOptions = $state<BrushOptions>({
		radius: 20,
		erasing: false,
		maskMode: 'instance',
		output: 'mask',
	});

	// ── layer grouping (mirrored from LayerStore) ──
	readonly layers = new LayerStore();
	groupByColumn = $state('label');
	readonly hiddenGroups = new SvelteSet<string>();
	readonly groupColors = new SvelteMap<string, number>();

	// Local field overlay for sidebar/list display, keyed `${index}:${field}` (the
	// canvas is updated separately via arrow.setFieldOverride). SvelteMap ⇒ edits
	// re-derive `rows` with no manual version counter.
	private readonly _overrides = new SvelteMap<string, string>();

	// Undo/redo of field edits (the review operations: relabel / accept / reject /
	// text). Geometry dirtiness is tracked separately (the engine owns geometry).
	private _undo = $state<UndoOp[]>([]);
	private _redo = $state<UndoOp[]>([]);
	private _geoDirty = $state(false);

	// Structural edits queued for the next Save: shapes drawn (onCommit) and ids
	// deleted. Flushed by save() then reconciled by _reload() — no client-side Arrow
	// surgery (which would corrupt the index-keyed overlay). The sidebar reflects
	// deletes immediately (filtered from `rows`); new shapes render after save+reload.
	private _inserts = $state<InsertRow[]>([]);
	private _deletes = $state<string[]>([]);
	// Geometry moves of EXISTING shapes, keyed by row index (the canvas already renders
	// them via the plugin's dirty overlay; this queues them for Save).
	private readonly _geoEdits = new SvelteMap<number, GeometryUpdate>();
	// Segment-TIME resizes of EXISTING audio/video annotations, keyed by row index (the
	// waveform region already reflects the drag; this queues {t_start,t_end} for Save).
	private readonly _temporalEdits = new SvelteMap<number, { t_start: number; t_end: number }>();

	private _detachViewport: (() => void) | null = null;
	// POST target for Save (same URL the annotations are GET from). Null ⇒ read-only.
	private _saveUrl = $state<string | null>(null);
	// The Lance version we loaded — echoed on Save for optimistic concurrency (409).
	private _version: number | null = null;

	constructor() {
		this.layers.on(() => this._pullLayers());
		this._pullLayers();
	}

	// ── derived views the layout reads ──

	/** String columns available to group by. */
	readonly groupColumns = $derived.by<string[]>(() => {
		const t = this.table;
		if (!t) return [];
		const names = new Set(t.schema.fields.map((f) => f.name));
		return STRING_FIELD_CANDIDATES.filter((c) => names.has(c));
	});

	/** Flat rows for the sidebar list, overlay-aware (pure projection in annotation-rows). */
	readonly rows = $derived.by<AnnoRow[]>(() => {
		const t = this.table;
		return t ? projectRows(t, this._overrides, this._deletes) : [];
	});

	/** Distinct groups for the current group-by column, with counts. */
	readonly groups = $derived.by<{ name: string; count: number }[]>(() => {
		const col = this.groupByColumn;
		const counts = new Map<string, number>();
		for (const r of this.rows) {
			const key = (r as unknown as Record<string, string>)[col] ?? '';
			counts.set(key, (counts.get(key) ?? 0) + 1);
		}
		return [...counts.entries()]
			.map(([name, count]) => ({ name, count }))
			.sort((a, b) => a.name.localeCompare(b.name));
	});

	/** The currently selected row (single selection), overlay-aware. */
	readonly selected = $derived.by<AnnoRow | null>(() => {
		const i = this.selectedIndex;
		if (i == null) return null;
		return this.rows.find((r) => r.index === i) ?? null;
	});

	/** The review order — predictions first, highest uncertainty first (the active-
	 *  learning queue). The ONE source both the sidebar list and accept-and-advance
	 *  read, so "what to review next" is a sort, not a recompute. */
	readonly reviewQueue = $derived.by<AnnoRow[]>(() =>
		this.rows.toSorted((a, b) => {
			const ap = a.status === 'prediction' ? 0 : 1;
			const bp = b.status === 'prediction' ? 0 : 1;
			if (ap !== bp) return ap - bp;
			return (b.uncertainty ?? -1) - (a.uncertainty ?? -1);
		}),
	);

	/** 1-based position of the selection in the review queue (0 if none) + the total. */
	readonly queuePos = $derived.by<{ at: number; of: number }>(() => {
		const of = this.reviewQueue.length;
		const i = this.selectedIndex;
		const idx = i == null ? -1 : this.reviewQueue.findIndex((r) => r.index === i);
		return { at: idx < 0 ? 0 : idx + 1, of };
	});

	/** The label-class palette — distinct non-empty labels present in the data, so
	 *  "quick label" needs no hardcoded class list (data-derived, like the groups). */
	// The quick-label palette — drawn-shape classes only. Chunk TAG rows (shape_type=
	// "tag", zero geometry) are annotations too but not a drawing class, so they'd just
	// pollute the palette; excluded here (they still show in the review list/table).
	readonly labelClasses = $derived.by<string[]>(() =>
		[
			...new Set(
				this.rows
					.filter((r) => r.shape !== 'tag')
					.map((r) => r.label)
					.filter(Boolean),
			),
		].sort((a, b) => a.localeCompare(b)),
	);

	/** The annotations endpoint for this unit (GET/POST/versions) — null when read-only.
	 *  The compare-versions panel reads history + historical snapshots from it. */
	get annotationsUrl(): string | null {
		return this._saveUrl;
	}
	/** The current rows as id→signature, for diffing against a historical version. */
	readonly currentSignatures = $derived.by<Map<string, string>>(() => {
		const m = new Map<string, string>();
		for (const r of this.rows) m.set(r.id, rowSignature(r));
		return m;
	});

	/** The shape types the OPEN TASK permits, canonical names — empty = unconstrained.
	 *
	 *  Enforcement lives server-side at submit and stays there; this is the same contract read
	 *  EARLY so the rail can decline to offer a tool the task will refuse. Discovering a violation
	 *  after drawing is enforcement that punishes; not offering the tool is enforcement that
	 *  teaches, and the 409 remains as the backstop for any caller that is not this canvas. */
	allowedShapeTypes = $state<string[]>([]);

	/** Engine tool names the rail may show, or null for "no restriction". */
	readonly allowedTools = $derived(engineToolsFor(this.allowedShapeTypes));

	readonly canDraw = $derived(this.mode === 'edit');
	/** Rows queued to be INSERTED on the next save — read-only, so a test can assert that an undo
	 *  actually removed a drawn shape from the payload rather than merely hiding it. */
	readonly pendingInserts = $derived(this._inserts.length);
	/** Rows carrying a pending geometry edit. */
	readonly pendingGeometryEdits = $derived(this._geoEdits.size);

	readonly canUndo = $derived(this._undo.length > 0);
	readonly canRedo = $derived(this._redo.length > 0);
	/** Unsaved-edits flag: pending field edits, a canvas geometry edit, or queued
	 *  structural inserts/deletes. */
	readonly dirty = $derived(
		this._undo.length > 0 ||
			this._geoDirty ||
			this._inserts.length > 0 ||
			this._deletes.length > 0 ||
			this._temporalEdits.size > 0,
	);
	readonly canSave = $derived(this.dirty && !this.saving && this._saveUrl !== null);

	// ── engine lifecycle ──

	/** Called by a spatial viewer once its engine + data are ready. `saveUrl` (the
	 *  annotations endpoint) enables the local-first Save; omit it for read-only. */
	attach(ctx: PixiContext, table: Table, saveUrl?: string, version?: number): void {
		this.ctx = ctx;
		this.table = table;
		this.count = table.numRows;
		this._saveUrl = saveUrl ?? null;
		this._version = version ?? null;

		const im = ctx.plugins.interaction;
		im.setEditMode(this.mode === 'edit');
		im.setTool(this.activeTool);
		im.setBrushOptions(this.brushOptions);
		this.cvCapable = im.cvCapable; // still image loaded ⇒ the magnetic CV tool is available
		this.cvReady.clear();
		im.onCvToolReady = (tool) => this.cvReady.add(tool);
		this.magneticSnapped = false;
		im.onMagneticSnap = (snapped) => (this.magneticSnapped = snapped);
		im.onSelect = (index) => {
			this.selectedIndex = index;
			this._mirrorSelection(im.getSelectedSet());
		};
		im.onDirtyChange = (hasDirty) => {
			if (hasDirty) this._geoDirty = true;
		};
		im.onChange = (index, geo) => {
			// drag-end geometry of an existing shape → queue for Save (canvas already updated)
			// `before` is what a redo/undo must restore: the PENDING edit if one exists, else null
			// meaning "the row's own geometry from the table".
			this._pushUndo({
				kind: 'geometry',
				index,
				before: this._geoEdits.get(index) ?? null,
				after: geo,
			});
			this._geoEdits.set(index, geo);
			this._geoDirty = true;
		};
		im.onCommit = (shape) => {
			// In SAM mode the drawn box/point is a region prompt (→ a mask prediction), not a
			// manual annotation — its bbox travels as the region (a point = a zero box = click).
			if (this.assistProducer) {
				const region = { x: shape.x, y: shape.y, width: shape.width, height: shape.height };
				void this.assist('', region, this.assistProducer);
			} else {
				this._appendInsert(this._buildInsert(shape));
			}
		};

		// chain (never overwrite) the image viewport hook PixiCanvas installed, so
		// zoomPercent tracks wheel-zoom + pan + our zoom buttons alike.
		const img = ctx.plugins.image;
		const prev = img.onViewportChange;
		img.onViewportChange = (bounds) => {
			prev?.(bounds);
			this.zoomPercent = img.zoomPercent;
		};
		this._detachViewport = () => {
			img.onViewportChange = prev;
		};
		this.zoomPercent = img.zoomPercent;
		this._syncLayerConfig();
	}

	/** The TEMPORAL seam — audio/video viewers share the controller's data + review +
	 *  Save path WITHOUT a PixiContext (they own their own waveform / `<video>` surface).
	 *  Sets only the row-based state; segment create/resize flow through the same
	 *  inserts/edits overlay as spatial shapes, keyed by row id. */
	attachData(table: Table, saveUrl?: string, version?: number): void {
		this.ctx = null;
		this.table = table;
		this.count = table.numRows;
		this._saveUrl = saveUrl ?? null;
		this._version = version ?? null;
	}

	/** Undo `attach()` — every hook it installed, not just the viewport chain.
	 *
	 *  `attach` hands the engine SEVEN closures that capture `this`: six InteractionManager
	 *  callbacks and the chained `image.onViewportChange`. Clearing only the viewport left the
	 *  other six live, so an engine event after teardown still wrote this controller's `$state`
	 *  and re-evaluated its deriveds from a destroyed effect — which is precisely what Svelte
	 *  reports as `derived_inert`. Release them all, and release them BEFORE `ctx` is nulled
	 *  (they are reached through it). */
	detach(): void {
		this._detachViewport?.();
		this._detachViewport = null;
		const im = this.ctx?.plugins.interaction;
		if (im) {
			im.onCvToolReady = undefined;
			im.onMagneticSnap = undefined;
			im.onSelect = undefined;
			im.onDirtyChange = undefined;
			im.onChange = undefined;
			im.onCommit = undefined;
		}
		this.ctx = null;
		this.table = null;
	}

	// ── toolbar / mode ──
	setTool(tool: Tool): void {
		this.activeTool = tool;
		this.ctx?.plugins.interaction.setTool(tool);
	}
	toggleMode(): void {
		this.mode = this.mode === 'edit' ? 'view' : 'edit';
		this.ctx?.plugins.interaction.setEditMode(this.mode === 'edit');
		if (this.mode === 'view') this.setTool('select');
	}
	setBrushOptions(patch: Partial<BrushOptions>): void {
		this.brushOptions = { ...this.brushOptions, ...patch };
		this.ctx?.plugins.interaction.setBrushOptions(this.brushOptions);
	}

	// ── selection ──
	select(index: number | null): void {
		this.multiSelect = false;
		this.selectedIndex = index;
		const im = this.ctx?.plugins.interaction;
		im?.select(index);
		this._mirrorSelection(im?.getSelectedSet() ?? new Set());
	}
	/** Toggle an annotation in the multi-selection (Shift/Ctrl-click) — the `picked`
	 *  target for bulk ops ("apply to selection", Label Studio). */
	toggleSelect(index: number): void {
		this.multiSelect = true;
		const im = this.ctx?.plugins.interaction;
		im?.select(index, true);
		this._mirrorSelection(im?.getSelectedSet() ?? new Set());
		this.selectedIndex = im?.getSelectedIndex() ?? index;
	}
	clearSelection(): void {
		this.select(null);
	}
	/** Set a status on the WHOLE multi-selection — a bulk verdict through the picked seam. */
	bulkStatus(status: string): void {
		const picked = [...this.selectedSet];
		if (picked.length === 0) return;
		this.apply({
			target: { level: 'picked', indices: picked },
			producer: 'human',
			op: 'verdict',
			execution: 'interactive',
			payload: { fields: { status } },
		});
	}

	deleteSelected(): void {
		const i = this.selectedIndex;
		if (i == null) return;
		const t = this.table;
		const id = t ? rawField(t, 'id', i) : null;
		if (id) {
			this._deletes = [...this._deletes, id];
			this._pushUndo({ kind: 'delete', index: i, id: String(id) });
		}
		// The CANVAS half. This used to be `interaction.handleKeyDown('Delete')`, which never did
		// anything: `activeTool` is null in select mode — the only mode you can have a selection in —
		// and no tool implements 'Delete' regardless. So the row vanished from the sidebar and stayed
		// on the canvas until Save reloaded the table: one delete, two answers.
		this.ctx?.plugins.arrow.setDeleted(i);
		this.ctx?.plugins.arrow.sync();
		this._geoDirty = true;
		this.select(null);
	}

	// ── temporal (audio / video segments) ──

	/** Create a new time-range annotation — a waveform region the reviewer dragged, or a
	 *  shape pinned to a video moment. Flows through the SAME inserts overlay + Save path
	 *  as a drawn box (spatial fields zeroed); returns the new row id so the caller can
	 *  key its region to it. */
	addTemporalSegment(seg: { t_start: number; t_end: number; label?: string }): string {
		const row = makeInsertRow({
			shape_type: 'segment',
			t_start: seg.t_start,
			t_end: seg.t_end,
			label: seg.label ?? '',
		});
		this._appendInsert(row);
		return row.id;
	}

	/** Queue a segment-time resize of an EXISTING row (a region drag-end), keyed by row
	 *  index — merged into the Save delta as {t_start,t_end}, exactly like a geometry move. */
	updateSegmentTime(index: number, t_start: number, t_end: number): void {
		this._temporalEdits.set(index, { t_start, t_end });
		this._geoDirty = true;
	}

	/** Move the playhead (video scrub/play) so newly-drawn shapes pin to this moment. */
	setTimeCursor(seconds: number): void {
		this.timeCursor = seconds;
	}

	/** Forward a key to the engine's active tool — polygon/brush Enter-commit,
	 *  Escape-cancel, Backspace vertex-undo (the shell routes these while drawing). */
	forwardToolKey(key: string): void {
		this.ctx?.plugins.interaction.handleKeyDown(key);
	}

	/** Arm/disarm an interactive segmenter (SAM): while armed, the next drawn box/point
	 *  becomes its region prompt rather than a manual annotation. Null = normal drawing. */
	setAssistProducer(producer: string | null): void {
		this.assistProducer = producer;
	}

	/** Map exemplar row INDICES → stable annotation ids (the few-shot reference an INSID3
	 *  propagate deriver fetches masks/DINOv3 features by). */
	private _exemplarIds(indices: number[] | undefined): string[] {
		const t = this.table;
		if (!t || !indices?.length) return [];
		return indices.map((i) => rawField(t, 'id', i)).filter((id): id is string => !!id);
	}

	/** INSID3 few-shot propagate: take the picked annotations as EXEMPLARS and propagate
	 *  their masks over a chunk-level target selection — a batch deriver over the DINOv3
	 *  space ("1–few exemplars → apply to all from small data"). Flows through apply()/the
	 *  jobs seam like any batch op. */
	propagate(target: Selection, exemplarIndices?: number[]): LabelOutcome {
		const picked = exemplarIndices ?? [...this.selectedSet];
		return this.apply({
			target,
			producer: 'insid3',
			op: 'propagate',
			execution: 'batch',
			payload: { exemplars: picked },
		});
	}

	/** Queue a new row for Save AND render it optimistically — append AT THE END
	 *  (index = numRows, so the index-keyed overlay/selection stay valid), re-render the
	 *  WebGPU canvas, re-apply pending field patches (sync re-materializes caches).
	 *  Shared by manual draws (onCommit) and AI-assist predictions. */
	private _appendInsert(row: InsertRow): void {
		// Recorded BEFORE the append, so `index` is the row index the appended row will occupy.
		this._pushUndo({
			kind: 'insert',
			index: this.table?.numRows ?? this._inserts.length,
			at: this._inserts.length,
			row,
		});
		this._inserts = [...this._inserts, row];
		const t = this.table;
		if (t) {
			// Grow the row table so the review sidebar (and temporal lane) see the new row at
			// once; when a Pixi canvas is attached, feed it the grown table too (re-render +
			// re-apply pending field patches — sync re-materializes caches).
			const next = t.concat(buildBatchTable(t.schema, [row as unknown as Record<string, unknown>]));
			const arrow = this.ctx?.plugins.arrow;
			if (arrow) {
				// An insert APPENDS, so existing row indices are unchanged and the delete overlay stays
				// valid across this swap — unlike _reload(), which renumbers and must clear it.
				arrow.load(next);
				arrow.sync();
			}
			this.table = next;
			this.count = next.numRows;
			this._reapplyOverrides();
		} else {
			this.count = this._inserts.length;
		}
		this._geoDirty = true;
	}

	/** Interactive AI-assist (the ra-atr "draw/prompt → shapes" loop): run a producer
	 *  over this unit and drop the predicted shapes on the canvas as `status=prediction`,
	 *  `source=model:…` — reviewed/accepted like any prediction, persisted on Save. */
	async assist(
		prompt: string,
		region?: { x: number; y: number; width: number; height: number },
		producer = 'grounding-dino',
	): Promise<void> {
		const url = this._saveUrl;
		// GroundingDINO detects from a TEXT prompt; SAM segments from the REGION alone.
		const needsPrompt = producer === 'grounding-dino';
		if (!url || this.saving || (needsPrompt && !prompt.trim())) return;
		const target = assistTargetFor(url);
		if (!target) return;
		this.saving = true;
		this.saveError = null;
		try {
			const answer = await requestAssist({
				...target,
				producer,
				prompt,
				region: region ?? null,
			});
			if (!answer.ok) {
				// The server's own words: 401 (sign in), 403 (no permission), 5xx from the runner.
				this.saveError = answer.detail;
				return;
			}
			const result = answer.data;
			for (const s of result.shapes) {
				this._appendInsert(
					makeInsertRow({
						shape_type: canonicalShapeType(s.shape_type) ?? 'bbox',
						x: s.x,
						y: s.y,
						width: s.width,
						height: s.height,
						polygon: s.polygon ?? [],
						// Pin the prediction to the current video moment (0 for images).
						t_start: this.timeCursor,
						t_end: this.timeCursor,
						label: s.label || prompt,
						status: 'prediction',
						source: result.source ?? `model:${producer}`,
					}),
				);
			}
		} catch (e) {
			this.saveError = e instanceof Error ? e.message : String(e);
		} finally {
			this.saving = false;
		}
	}

	/** Map a committed engine shape → the queued insert row (backend NewAnnotation). */
	private _buildInsert(shape: CommitShape): InsertRow {
		return makeInsertRow({
			// ONE seam, and it maps into the SERVICE vocabulary. This used to say
			// `shape.type === 'rect' ? 'rectangle' : shape.type` — a third name accepted by neither
			// side, so five of seven tools produced a draft the service refused at submit.
			shape_type: canonicalShapeType(shape.type) ?? shape.type,
			x: shape.x,
			y: shape.y,
			width: shape.width,
			height: shape.height,
			rotation: shape.rotation ?? 0,
			polygon: shape.polygon ?? [],
			// Pin the shape to the current video moment (0 for images — no time axis).
			t_start: this.timeCursor,
			t_end: this.timeCursor,
			mask: shape.mask ?? '',
		});
	}
	convertToPolygon(): void {
		if (this.ctx?.plugins.interaction.convertToPolygon()) this._geoDirty = true;
	}

	// ── inline field edits (canvas + overlay) + undo/redo ──
	updateField(index: number, field: EditableField, value: string): void {
		const t = this.table;
		if (!t) return;
		const before = effectiveField(t, this._overrides, field, index) ?? '';
		if (before === value) return;
		this._pushUndo({ kind: 'field', index, field, before, after: value });
		this._setField(index, field, value);
	}
	setStatus(index: number, status: string): void {
		// Manual mode = ONE instance of the LabelOp abstraction (human · verdict ·
		// interactive · one). Routing through apply() proves the annotator isn't
		// coupled to the review flow — a model/batch producer slots into the same seam.
		this.apply({
			target: { level: 'one', index },
			producer: 'human',
			op: 'verdict',
			execution: 'interactive',
			payload: { fields: { status } },
		});
	}

	/** Set a label on the selection — one annotation, or the whole picked multi-select
	 *  (Label Studio's "apply to selection"). Flows through the same LabelOp seam. */
	applyLabel(label: string): void {
		const picked = [...this.selectedSet];
		const target: Selection =
			picked.length > 1
				? { level: 'picked', indices: picked }
				: this.selectedIndex != null
					? { level: 'one', index: this.selectedIndex }
					: { level: 'picked', indices: [] };
		this.apply({
			target,
			producer: 'human',
			op: 'set',
			execution: 'interactive',
			payload: { fields: { label } },
		});
		// The rows relabel in place (the inline feedback); the toast confirms the batch size.
		const n = picked.length > 1 ? picked.length : this.selectedIndex != null ? 1 : 0;
		if (n > 0) toast.success(`Labeled ${n} annotation${n === 1 ? '' : 's'} “${label}”`);
	}

	// ── review-queue navigation (accept-and-advance — the throughput loop) ──
	/** Move the selection by ±1 within the review queue (clamped). */
	selectQueueRelative(delta: number): void {
		const q = this.reviewQueue;
		if (!q.length) return;
		const pos = this.queuePos.at - 1; // queuePos is 1-based; -1 ⇒ nothing selected
		const target = pos < 0 ? 0 : Math.min(Math.max(pos + delta, 0), q.length - 1);
		const row = q[target];
		if (row) this.select(row.index);
	}
	next(): void {
		this.selectQueueRelative(1);
	}
	prev(): void {
		this.selectQueueRelative(-1);
	}
	/** Set the selection's status, then advance to the next item still needing review
	 *  (the next prediction in queue order), else the next row — the review loop. */
	acceptAndAdvance(status: string): void {
		const i = this.selectedIndex;
		if (i == null) return;
		const q = this.reviewQueue; // capture pre-change order (the row re-sorts after)
		const pos = q.findIndex((r) => r.index === i);
		this.setStatus(i, status);
		const rest = pos < 0 ? q : q.slice(pos + 1);
		const nextRow = rest.find((r) => r.status === 'prediction') ?? rest[0];
		this.select(nextRow ? nextRow.index : null);
	}

	// ── the write-plane seam: dispatch a LabelOp (all 3 modes flow through here) ──
	apply(op: LabelOp): LabelOutcome {
		const spec = PRODUCERS[op.producer];
		if (!spec) return { status: 'unsupported', reason: `unknown producer '${op.producer}'` };

		// Batch locus = a silver deriver over a chunk-level selection: enqueue via the jobs
		// seam (lance-ns runs it; the result surfaces async by media id + Lance version).
		if (op.execution === 'batch') {
			if (isChunkSelection(op.target)) {
				// Fire-and-forget submit; the toasts are the only submit-side feedback (results
				// surface async on the read plane by media id + Lance version). HONEST MOCK:
				// with no jobs runner deployed (MEDIA_JOBS_URL unset) the service answers with
				// a deterministic mock and NO job ever runs — the result's `backend` field is
				// that per-run signal, so a mock submit must never toast as plain success.
				void submitBatchJob({
					producer: op.producer,
					op: op.op,
					scope: op.target,
					prompt: op.payload.prompt,
					// PROPAGATE (INSID3): the few-shot reference exemplars are annotation INDICES in
					// the open unit — resolve to stable ids the deriver can fetch masks/features by.
					exemplars: this._exemplarIds(op.payload.exemplars),
				})
					.then((res) =>
						!res.ok
							? toast.error(`Job submit failed: ${res.detail}`)
							: res.data.backend === 'mock'
								? toast.warning(
										`Batch job mocked (${op.producer} · ${res.data.job_id}) — no jobs runner deployed, nothing will run`,
									)
								: toast.success(`Batch job queued (${op.producer} · ${res.data.job_id})`),
					)
					.catch((e: unknown) =>
						toast.error(`Job submit failed: ${e instanceof Error ? e.message : String(e)}`),
					);
			}
			return { status: 'queued', job: `${spec.source}:${op.op}`, note: 'batch deriver enqueued' };
		}

		// Interactive + human = the manual review path (real, local-first → Save).
		if (spec.kind === 'human' && (op.op === 'set' || op.op === 'verdict')) {
			const fields = op.payload.fields ?? {};
			const deltas: LabelDelta[] = [];
			for (const index of this._resolveInteractive(op.target)) {
				for (const [field, value] of Object.entries(fields)) {
					this.updateField(index, field as EditableField, value);
				}
				deltas.push({ index, fields });
			}
			return { status: 'applied', deltas };
		}

		// Interactive model/propagate (SAM click, INSID3-quick, DINO-on-a-page) — the
		// narrow interactive-assist exception; needs a predict/decode transport (follow-up).
		return {
			status: 'unsupported',
			reason: `interactive ${spec.kind} '${spec.name}' not wired yet`,
		};
	}

	/** Resolve an interactive (annotation-level) Selection to engine row indices.
	 *  Chunk-level selections are corpus-scale (batch), so client-side they fall back
	 *  to the current canvas selection. */
	private _resolveInteractive(sel: Selection): number[] {
		switch (sel.level) {
			case 'one':
				return [sel.index];
			case 'picked':
				return sel.indices;
			default:
				return [...this.selectedSet];
		}
	}

	/** Push an op and drop the redo branch — a new action invalidates any undone future. */
	private _pushUndo(op: UndoOp): void {
		this._undo = [...this._undo, op];
		this._redo = [];
	}

	/** Revert the last action — field edit, draw, delete or geometry drag alike. */
	undo(): void {
		const op = this._undo.at(-1);
		if (!op) return;
		this._undo = this._undo.slice(0, -1);
		this._redo = [...this._redo, op];
		this._apply(op, 'undo');
	}

	/** Re-apply the last undone action. */
	redo(): void {
		const op = this._redo.at(-1);
		if (!op) return;
		this._redo = this._redo.slice(0, -1);
		this._undo = [...this._undo, op];
		this._apply(op, 'redo');
	}

	/** The ONE reversal path, so undo and redo can never disagree about what an op means. */
	private _apply(op: UndoOp, direction: 'undo' | 'redo'): void {
		const arrow = this.ctx?.plugins.arrow;
		switch (op.kind) {
			case 'field':
				this._setField(op.index, op.field, direction === 'undo' ? op.before : op.after);
				break;
			case 'insert': {
				// A drawn row cannot be removed from an immutable Arrow table, so undo HIDES it (the
				// same overlay a delete uses) and drops it from the save payload — the row never
				// reaches the server either way, which is what "undone" has to mean.
				if (direction === 'undo') {
					this._inserts = this._inserts.filter((_, i) => i !== op.at);
					arrow?.setDeleted(op.index);
				} else {
					this._inserts = [...this._inserts.slice(0, op.at), op.row, ...this._inserts.slice(op.at)];
					arrow?.unsetDeleted(op.index);
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
			case 'delete': {
				if (direction === 'undo') {
					this._deletes = this._deletes.filter((id) => id !== op.id);
					arrow?.unsetDeleted(op.index);
				} else {
					this._deletes = [...this._deletes, op.id];
					arrow?.setDeleted(op.index);
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
			case 'geometry': {
				const geo = direction === 'undo' ? op.before : op.after;
				if (geo === null) {
					// No pending edit before this drag: drop the override entirely so the row falls back
					// to its own geometry from the table, rather than being pinned to a fabricated one.
					this._geoEdits.delete(op.index);
				} else {
					this._geoEdits.set(op.index, geo);
					arrow?.setOverride(op.index, {
						x: geo.x,
						y: geo.y,
						w: geo.w,
						h: geo.h,
						polygon: geo.polygon ?? [],
					});
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
		}
		this.select(op.index);
	}

	/** Apply a field value to BOTH the WebGPU canvas (arrow.setFieldOverride →
	 *  re-render) and the sidebar overlay. The single write path for edit/undo/redo. */
	private _setField(index: number, field: EditableField, value: string): void {
		this._overrides.set(`${index}:${field}`, value);
		this.ctx?.plugins.arrow.setFieldOverride(index, field, value);
		this.ctx?.plugins.arrow.sync();
	}

	/** Re-apply pending field edits to the canvas — a full re-materialize (after an
	 *  optimistic append) rebuilds column caches from this.table and wipes the in-place
	 *  setFieldOverride patches, so replay them once. */
	private _reapplyOverrides(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow || this._overrides.size === 0) return;
		for (const [key, value] of this._overrides) {
			const sep = key.indexOf(':');
			arrow.setFieldOverride(Number(key.slice(0, sep)), key.slice(sep + 1), value);
		}
		arrow.sync();
	}

	// ── persistence: local-first Save → Lance merge_insert (NOT sync-per-edit) ──

	/** Flush the accumulated field-edit overlay to Lance as ONE atomic version, then
	 *  reload so the display reflects the persisted (merge-reordered) rows. */
	async save(): Promise<void> {
		const t = this.table;
		const url = this._saveUrl;
		if (!t || !url || this.saving) return;

		const payload = buildSavePayload({
			overrides: this._overrides,
			geoEdits: this._geoEdits,
			temporalEdits: this._temporalEdits,
			inserts: this._inserts,
			deletes: this._deletes,
			version: this._version,
			resolveId: (index) => rawField(t, 'id', index),
		});
		if (payloadIsEmpty(payload)) return;

		this.saving = true;
		this.saveError = null;
		try {
			const { status } = await postSave(url, payload);
			if (status === 'conflict') {
				// The table advanced under us (another reviewer / a deriver). Reload to the
				// server state; the user's pending edits are dropped — they re-apply on fresh data.
				this.saveError = 'Annotations changed on the server — reloaded. Re-apply your edits.';
				toast.error(this.saveError);
			} else {
				toast.success('Annotations saved');
			}
			await this._reload();
			this._resetOverlays();
			await this._syncTaskDraft();
		} catch (e) {
			this.saveError = e instanceof Error ? e.message : String(e);
			toast.error(`Save failed: ${this.saveError}`);
		} finally {
			this.saving = false;
		}
	}

	/** S10 first half: a TASK-opened canvas snapshots the saved unit into the task's draft — the
	 *  document the publish reads — so canvas work travels into the published table. Guarded to the
	 *  single-unit task deep-link shape (a task carries ONE key; a multi-unit ad-hoc selection must
	 *  not snapshot someone else's unit into this task). A failure is LOUD: work that looks saved
	 *  but never publishes is the dishonesty this exists to prevent. */
	private async _syncTaskDraft(): Promise<void> {
		const { reviewSelection } = await import('$lib/labeling/review-selection.svelte');
		const taskId = reviewSelection.taskId;
		if (!taskId || reviewSelection.total !== 1 || !this.table) return;
		const { syncTaskDraft } = await import('$lib/projects/draft-sync');
		const detail = await syncTaskDraft(taskId, this.table);
		if (detail !== null) {
			toast.error(
				`Saved to the corpus, but the task draft did not update — the publish will not carry these shapes (${detail})`,
			);
		}
	}

	/** Drop every pending local edit (after a flush or a conflict reload). */
	private _resetOverlays(): void {
		this._undo = [];
		this._redo = [];
		this._inserts = [];
		this._deletes = [];
		this._geoEdits.clear();
		this._temporalEdits.clear();
		this._geoDirty = false;
	}

	/** Re-fetch the persisted annotations into the canvas + controller, clearing the
	 *  now-flushed overlay. merge_insert reorders rows, so selection is dropped. */
	private async _reload(): Promise<void> {
		const url = this._saveUrl;
		if (!url) return;
		// Pre-split fetch semantics, exactly: a non-2xx reload returns SILENTLY (save()
		// then clears the flushed overlays — the write already committed); a THROWN
		// network/parse error propagates to save()'s catch, which surfaces saveError and
		// skips _resetOverlays(), keeping the pending edits + version for retry.
		let loaded: { table: Table; version: number };
		try {
			loaded = await loadAnnotations(url);
		} catch (e) {
			if (e instanceof AnnotationsHttpError) return;
			throw e;
		}
		const { table, version } = loaded;
		this._version = version;
		this._overrides.clear();
		const ctx = this.ctx;
		if (ctx) {
			ctx.plugins.arrow.clearOverrides(); // drop stale geometry overrides — table is authoritative
			// The delete overlay is positional, so a reload renumbering rows would leave it hiding an
			// unrelated shape. The reloaded table has the deletions applied already.
			ctx.plugins.arrow.clearDeleted();
			ctx.plugins.arrow.load(table);
			ctx.plugins.arrow.sync();
		}
		this.table = table;
		this.count = table.numRows;
		this.select(null);
	}

	// ── zoom ──
	zoomIn(): void {
		this.ctx?.plugins.image.zoomIn();
	}
	zoomOut(): void {
		this.ctx?.plugins.image.zoomOut();
	}
	resetView(): void {
		this.ctx?.plugins.image.resetView();
	}

	// ── layers ──
	setGroupBy(column: string): void {
		this.layers.setGroupBy(column);
		this.groupByColumn = column;
	}
	toggleGroupVisible(group: string): void {
		this.layers.toggleVisibility(group);
		this.ctx?.plugins.arrow.setGroupVisible(group, !this.layers.isHidden(group));
	}
	setGroupColor(group: string, hex: string): void {
		this.layers.setColor(group, hexToNum(hex));
	}
	isHidden(group: string): boolean {
		return this.hiddenGroups.has(group);
	}
	groupColorHex(group: string): string {
		const n = this.groupColors.get(group);
		return n == null ? '#3b82f6' : numToHex(n);
	}

	// ── internals ──
	private _mirrorSelection(set: ReadonlySet<number>): void {
		this.selectedSet.clear();
		for (const i of set) this.selectedSet.add(i);
	}
	private _pullLayers(): void {
		this.groupByColumn = this.layers.groupByColumn;
		this.hiddenGroups.clear();
		for (const g of this.layers.hiddenGroups) this.hiddenGroups.add(g);
		this.groupColors.clear();
		for (const [k, v] of this.layers.groupColors) this.groupColors.set(k, v);
		this._syncLayerConfig();
	}
	private _syncLayerConfig(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow) return;
		arrow.setLayerConfig({
			hiddenGroups: this.layers.hiddenGroups,
			groupByColumn: this.layers.groupByColumn,
			groupColors: this.layers.groupColors,
		});
		arrow.sync();
	}
}
