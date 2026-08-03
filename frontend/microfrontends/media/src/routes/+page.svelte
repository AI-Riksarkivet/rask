<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import {
		search,
		listDocuments,
		voiceSimilar,
		voiceSimilarUpload,
		ApiError,
		activeView,
		thumbnailUrl,
		type Hit,
		type SearchSpec,
		type Document,
		type VoiceAnchor,
		type VoiceHit,
		type VoiceQueryInfo,
		type VoiceSimilarResponse,
	} from '@rask/media-api';
	import { voiceSearch } from '$lib/voice-search.svelte';
	import { submitBatchJob } from '@rask/labeling/jobs';
	import { fmtTime, hitKey, queryTerms, makeHighlighter } from '$lib/utils';
	import SearchBar from '$lib/components/search-bar.svelte';
	import SavedViews from '$lib/components/saved-views.svelte';
	import ActiveFilters from '$lib/components/active-filters.svelte';
	import HitList from '$lib/components/hit-list.svelte';
	import HitCard from '$lib/components/hit-card.svelte';
	import HitTable, { TABLE_COLUMNS } from '$lib/components/hit-table.svelte';
	import SendToProjectDialog from '$lib/components/SendToProjectDialog.svelte';
	import { loadCols, persistCols, loadTablePrefs, persistTablePrefs } from '$lib/table-columns';
	import DocTile from '$lib/components/doc-tile.svelte';
	import PlayerPane from '$lib/components/player-pane.svelte';
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import AtlasMap from '$lib/atlas/AtlasMap.svelte';
	import { crossFilter } from '$lib/atlas/cross-filter.svelte';
	import { getAtlasChunks } from '@rask/media-api';
	import { Button, Badge } from '@rask/ui';
	import { Switch } from '@rask/ui/switch';
	import {
		LayoutGrid,
		List as ListIcon,
		Table as TableIcon,
		Map as MapIcon,
		Loader2,
		ChevronLeft,
		ChevronRight,
		SearchX,
		Minus,
		Plus,
		AudioLines,
		X,
	} from '@lucide/svelte';

	// The active dataset view — corpus column names (identity, time, display,
	// metadata) are read through it, so this page renders any dataset's schema.
	// Named `ds` because `view` is the layout-mode state below.
	const ds = activeView();
	// Declared time binding (the start/end field names), or null when the dataset
	// has no time dimension — used when stamping synthetic player rows.
	const timeBinding = ds.descriptor.declared.time;

	/** A minimal "hit" the player can open: the doc key plus an optional time
	 *  span. `extra` (a browsed Document) supplies the display fields — its title
	 *  and metadata ride along untouched so the card/player still show them. */
	function playerRow(docId: string, start: number, end: number, extra?: Hit): Hit {
		const row: Hit = { ...extra, _score: 0, alignments: [] };
		for (const k of ds.keyFields) {
			if (!(k in row)) row[k] = k === ds.docKeyField ? docId : 0;
		}
		row[ds.docKeyField] = docId;
		if (timeBinding) {
			row[timeBinding.start] = start;
			row[timeBinding.end] = end;
		}
		return row;
	}

	const PAGE_STEP = 30;
	// Default result count shown per search (the Settings "Results to return").
	let spec = $state<SearchSpec>({ q: '', n: 100, mode: 'fts' });

	// ── Search results (when there's a query or image) ──
	let hits = $state<Hit[]>([]);
	let active = $state<Hit | null>(null);
	const activeKey = $derived(active ? hitKey(active) : null);
	let loadingHits = $state(false);
	let loadingMore = $state(false);
	let error = $state<string | null>(null);
	/** True once a page returns fewer hits than requested → backend exhausted. */
	let allLoaded = $state(false);

	// ── Voice search ("Find this voice" — query-by-example) ──
	// Voice mode reuses `hits` for rendering (VoiceHit extends Hit, so the
	// cards/table/map all just work); these track the anchor + restore state.
	let voiceActive = $state(false);
	/** Resolved anchor info from the response (speaker label for the chip). */
	let voiceQuery = $state<VoiceQueryInfo | null>(null);
	/** Chip label for the anchor video (filename stem from the source hit). */
	let voiceLabel = $state<string | null>(null);
	/** Voice paging mirror of `spec.n` (voice ignores the text-search spec). */
	let voiceN = $state(PAGE_STEP);
	// Mirrors the backend's hard cap on `n` (services/viewer/services/voice_service.py _MAX_N):
	// requests are clamped server-side, so paging past it can never grow the set.
	const VOICE_MAX_N = 100;
	// Raw (pre-dedupe) hit count of the last voice response. Voice exhaustion is
	// inferred from growth between pages — NOT from `length < requested`: the
	// backend drops turns whose span overlaps no chunk (see similar_voices),
	// so short pages are routine even when more matches exist. Plain let —
	// only read inside handlers, never rendered.
	let voiceRawCount = 0;
	// Re-run target for the Settings toggle + load-more. Plain let — only read
	// inside handlers/effects, never rendered.
	let voiceAnchor: VoiceAnchor | null = null;
	// Uploaded-clip counterpart of `voiceAnchor` (load-more re-POSTs the same
	// File). Exactly one of the two is non-null while voice mode is active; the
	// Settings "include same video" toggle ignores uploads (a clip belongs to
	// no doc, so `exclude_same_doc` is meaningless). Plain let — never rendered.
	let voiceUpload: File | null = null;
	// Snapshot of the text-search results taken when voice mode starts; the
	// chip's ✕ restores it (mirrors the image-chip lifecycle: leave voice mode
	// → you're back exactly where you were). Plain let — never rendered.
	let prevResults: { hits: Hit[]; active: Hit | null; allLoaded: boolean } | null = null;

	// Query used for term-highlighting the rendered results: voice hits were
	// not matched on text, so highlighting the *previous* text query in them
	// would mislead — blank it while voice mode is active.
	const resultsQuery = $derived(voiceActive ? '' : spec.q);
	// Compile the query → highlighter ONCE for the grid view, then pass the
	// prebuilt fn to every tile (the grid renders HitCard directly, bypassing
	// hit-list). This avoids one RegExp compilation per card across large pages.
	const gridHighlight = $derived(makeHighlighter(queryTerms(resultsQuery)));

	/** Distinct document count for the current results. */
	const docCount = $derived(new Set(hits.map((h) => ds.docId(h))).size);

	// ── Document browse (when query is empty) ──
	const PER_PAGE = 24;
	let docs = $state<Document[]>([]);
	let docsTotal = $state(0);
	let docsPage = $state(1);
	let loadingDocs = $state(false);
	let docsError = $state<string | null>(null);

	// ── Layout ──
	let view = $state<'list' | 'grid' | 'table' | 'map'>('list');

	// ── Atlas map cross-filter glue ──
	// The map's lasso/box/cluster selection surfaces its hits here; the bottom
	// results table + player read this set while the Map view is active.
	let mapHits = $state<Hit[]>([]);
	let mapSelectionTotal = $state(0);
	// Descriptor-declared metadata field keys (the table columns map 1:1 to them).
	const META_KEYS = ds.metadataFields.map((m) => m.field);
	// Compact Map-view table: the UI columns + the primary label + time + body.
	const MAP_TABLE_COLS = [
		'play',
		'thumbnail',
		'score',
		...META_KEYS.slice(0, 1),
		...(ds.hasTime ? ['start'] : []),
		...(ds.bodyField ? [ds.bodyField] : []),
	];

	// The Map-view table shows the lasso/legend selection when there is one,
	// otherwise the active search hits (text or image search) — which are also
	// highlighted on the map. So searching populates the table too, not only map
	// selections; a lasso then narrows it to the picked region.
	const mapTableHits = $derived(mapSelectionTotal > 0 ? mapHits : hits);

	// DIRECTION A — search/filter → dim+highlight the map. When the map is shown,
	// project the current search hits onto point indices so matches light up and
	// the rest dims (a single GPU recolour). Cleared to whole-corpus when empty.
	// Depends on `keyToIndex` too, so a search that ran BEFORE the map loaded its
	// points re-projects once AtlasMap rebuilds the key→index map for the space.
	$effect(() => {
		if (view !== 'map') return;
		const ready = crossFilter.keyToIndex.size > 0; // touch for reactivity
		if (hits.length > 0 && ready) crossFilter.setFilteredFromHits(hits);
		else crossFilter.clearFilter();
	});

	function onMapSelectionHits(h: Hit[], total: number) {
		mapHits = h;
		mapSelectionTotal = total;
	}

	/** Read→annotate handoff: open the current map/search selection in the annotator
	 *  (deep-link the descriptor key-paths; the route steps through them). The
	 *  annotator is a SEPARATE zone, so this is a hard document navigation — a
	 *  soft goto() would target a route this app does not own (→ 404). */
	function annotateHits(reviewHits: Hit[]) {
		const dv = activeView();
		const keys = reviewHits.map((h) => dv.keyPath(h).join('/')).filter(Boolean);
		if (keys.length) location.assign(`/annotator?keys=${keys.join(',')}`);
	}

	// Read→PROJECT handoff (the funnel's send half): the selection becomes TASKS in an
	// annotation project — appended to one still taking items, or a new project created
	// around it. Distinct from `annotateHits` (ad-hoc canvas viewing) on purpose: a send
	// creates claimable, reviewable, publishable work with provenance.
	let sendOpen = $state(false);
	let sendKeys = $state<string[]>([]);
	let sendOrigin = $state<'search' | 'atlas'>('search');
	function sendHits(reviewHits: Hit[], origin: 'search' | 'atlas') {
		const dv = activeView();
		const keys = reviewHits.map((h) => dv.keyPath(h).join('/')).filter(Boolean);
		if (!keys.length) return;
		sendKeys = keys;
		sendOrigin = origin;
		sendOpen = true;
	}

	// Applying a saved view replaces `spec` wholesale, but SearchBar snapshots spec into
	// its own controls only at init — so bump this key to REMOUNT it, re-initializing the
	// query box / mode dials from the applied view (else the controls show the old query
	// and the next manual search would silently discard the view).
	// Bumped on every EXTERNAL spec replacement so the SearchBar re-adopts it
	// (its own submit echoes must not — see the bar's adoptSignal prop).
	let searchBarAdopt = $state(0);
	function applySavedView(next: SearchSpec) {
		void runSearch(next);
		searchBarAdopt += 1;
	}

	// Read→BATCH handoff (bulk/auto-labeling, the 3rd LabelOp mode): enqueue a producer
	// over this chunk-level selection as a lance-ns silver deriver. We only submit — the
	// deriver runs async and its predictions surface on re-read.
	let autoLabelMsg = $state<string | null>(null);
	async function autoLabelHits(reviewHits: Hit[]) {
		const dv = activeView();
		const keys = reviewHits.map((h) => dv.keyPath(h).join('/')).filter(Boolean);
		if (!keys.length) return;
		autoLabelMsg = 'submitting…';
		try {
			const job = await submitBatchJob({
				producer: 'grounding-dino',
				op: 'predict',
				scope: { level: 'chunks', keys },
			});
			autoLabelMsg = `${job.status} · ${job.job_id} (${keys.length} chunks)`;
		} catch (e) {
			autoLabelMsg = e instanceof Error ? e.message : 'submit failed';
		}
	}

	// DIRECTION B (seed) — promote a map selection to the search's result set by
	// stable _rowid (the points' addresses), not a synthesized giant WHERE.
	async function seedSearchFromSelection(rowids: number[]) {
		loadingHits = true;
		error = null;
		try {
			hits = await getAtlasChunks(rowids);
			active = null;
			allLoaded = true;
			view = 'list';
		} catch (e) {
			error = e instanceof Error ? e.message : 'failed to load selection';
		} finally {
			loadingHits = false;
		}
	}
	// Table-view preferences: which columns show, dragged column widths, and
	// the wrap-text toggle — one persisted record, with new default columns
	// merged in across app versions (see $lib/table-columns). Direct init (not
	// a one-shot $effect): this page is client-rendered, so localStorage is
	// available at init time and the restore can't clobber later user toggles.
	// v6 bundles widths + wrap with visibility; v5 was visibility-only
	// ({cols, known}); the v4 legacy key predates the merge format and
	// `speaker` is the only default column newer than that era.
	const TABLE_COL_KEYS = TABLE_COLUMNS().map((c) => c.key);
	// Default visible columns, derived from the descriptor: the fixed UI columns
	// (play/thumbnail/score/speaker) plus the declared metadata fields, time span,
	// and body/caption where the dataset declares them.
	const DEFAULT_TABLE_COLS = [
		'play',
		'thumbnail',
		'score',
		...META_KEYS,
		'speaker',
		...(ds.hasTime ? ['start', 'end', 'duration'] : []),
		...(ds.bodyField ? [ds.bodyField] : []),
		...(ds.captionField ? [ds.captionField] : []),
	];
	const TABLE_PREFS_KEY = 'lance-media-table-cols-v6';
	const initialTablePrefs = loadTablePrefs({
		storageKey: TABLE_PREFS_KEY,
		allKeys: TABLE_COL_KEYS,
		defaults: DEFAULT_TABLE_COLS,
		legacyMergedKey: 'lance-media-table-cols-v5',
		legacyPlainKey: 'lance-media-table-cols-v4',
		legacyAppend: ['speaker'],
	});
	let tableCols = $state<string[]>(initialTablePrefs.cols);
	/** Dragged column widths (px) by column key — absent key = auto-sized. */
	let tableWidths = $state<Record<string, number>>(initialTablePrefs.widths);
	/** Wrap long free-text columns (body/caption/title) instead of truncating. */
	let tableWrap = $state<boolean>(initialTablePrefs.wrap);

	function persistTable() {
		persistTablePrefs(
			TABLE_PREFS_KEY,
			{ cols: tableCols, widths: tableWidths, wrap: tableWrap },
			TABLE_COL_KEYS,
		);
	}
	/** A resize-handle drag ended (px) or was double-clicked (null = reset). */
	function setColWidth(key: string, width: number | null) {
		tableWidths =
			width === null
				? Object.fromEntries(Object.entries(tableWidths).filter(([k]) => k !== key))
				: { ...tableWidths, [key]: width };
		persistTable();
	}
	/** The column-chooser "Wrap text" switch — applies (and persists) instantly. */
	function setTableWrap(v: boolean) {
		tableWrap = v;
		persistTable();
	}

	// Columns shown in the browse-mode (pre-search) documents table. Documents
	// carry fewer fields than search hits, so this is its own small set.
	const DOC_COLUMNS: { key: string; label: string; get: (d: Document) => string }[] = [
		{ key: 'thumbnail', label: 'Thumb', get: () => '' },
		...ds.metadataFields.map(({ field, label }) => ({
			key: field,
			label,
			get: (d: Document) => {
				const v = d[field];
				return v == null ? '' : String(v);
			},
		})),
		...(ds.hasTime
			? [
					{
						key: 'duration',
						label: 'Dur',
						get: (d: Document) => {
							const dur = ds.duration(d);
							return dur != null ? fmtTime(dur) : '';
						},
					},
				]
			: []),
	];
	function toggleCol(key: string) {
		tableCols = tableCols.includes(key) ? tableCols.filter((k) => k !== key) : [...tableCols, key];
		persistTable();
	}

	// The browse-mode documents table keeps its OWN visible set (all doc columns
	// by default): it used to filter through `tableCols`, whose hit-table
	// defaults intersect DOC_COLUMNS on just 3 keys — so the first table a user
	// ever saw showed only Thumb/Name/Dur.
	const DOC_COLS_KEY = 'lance-media-doc-cols-v1';
	let docTableCols = $state<string[]>(
		loadCols(
			DOC_COLS_KEY,
			DOC_COLUMNS.map((c) => c.key),
		),
	);
	function toggleDocCol(key: string) {
		docTableCols = docTableCols.includes(key)
			? docTableCols.filter((k) => k !== key)
			: [...docTableCols, key];
		persistCols(DOC_COLS_KEY, docTableCols);
	}
	// Grid column count, persisted in localStorage so it sticks (direct init —
	// same reasoning as loadTableCols above).
	function loadGridCols(): number {
		const v = localStorage.getItem('lance-media-gridcols');
		return v ? Math.max(2, Math.min(6, Number(v) || 3)) : 3;
	}
	let gridCols = $state<number>(loadGridCols());
	function setGridCols(n: number) {
		gridCols = n;
		try {
			localStorage.setItem('lance-media-gridcols', String(n));
		} catch {}
	}

	/** Selected document in browse mode (so the grid can highlight it). */
	const activeDocId = $derived(active ? ds.docId(active) : null);

	const isBrowsing = $derived(!spec.q && !spec.image && !spec.topic && !voiceActive);
	const docsTotalPages = $derived(Math.max(1, Math.ceil(docsTotal / PER_PAGE)));

	// Supersession counter: FilterPopover auto-applies onchange, so searches can
	// overlap — only the LATEST call may write results. Plain let (never rendered).
	let searchSeq = 0;

	/** Voice hits are one-per-turn; two turns can share their max-overlap chunk,
	 *  which would collide on the keyed `{#each}` lists. Keep the best-ranked
	 *  (first) hit per chunk key. */
	function dedupeVoiceHits(vh: VoiceHit[]): VoiceHit[] {
		const seen = new Set<string>();
		return vh.filter((h) => {
			const k = hitKey(h);
			if (seen.has(k)) return false;
			seen.add(k);
			return true;
		});
	}

	/** Shared voice-mode entry (anchor + upload paths): snapshot the text
	 *  results once so the chip's ✕ can restore them, then reset the result
	 *  surfaces for the incoming hits. */
	function enterVoice(label: string | undefined): void {
		if (!voiceActive) prevResults = { hits, active, allLoaded };
		voiceActive = true;
		if (label !== undefined) voiceLabel = label;
		// Same reasoning as runSearch: fresh results supersede a stale map lasso.
		crossFilter.clearSelection();
		mapHits = [];
		mapSelectionTotal = 0;
		loadingHits = true;
		error = null;
	}

	/** Apply a (non-superseded) voice response: anchor info for the chip,
	 *  deduped hits, and the paging bookkeeping both voice paths share. */
	function applyVoiceResponse(res: VoiceSimilarResponse, requested: number): void {
		voiceQuery = res.query;
		hits = dedupeVoiceHits(res.hits);
		active = null;
		voiceRawCount = res.hits.length;
		// Only the server-side clamp guarantees exhaustion here; short pages are
		// routine (turns without an overlapping chunk are dropped server-side).
		allLoaded = requested >= VOICE_MAX_N;
	}

	/** Run (or re-run) a voice query. First entry into voice mode snapshots the
	 *  current text results so the chip's ✕ can restore them. Auto-applies.
	 *  `fallback` is a one-shot retry anchor for 404s on `t`-anchors (a text
	 *  hit's midpoint can fall in a mid-speech diarization gap). */
	async function runVoice(anchor: VoiceAnchor, label?: string, fallback?: VoiceAnchor) {
		const seq = ++searchSeq; // supersede any in-flight text/voice search
		enterVoice(label);
		voiceAnchor = anchor;
		voiceUpload = null;
		try {
			const requested = voiceN;
			const res = await voiceSimilar(anchor, {
				n: requested,
				excludeSameDoc: !voiceSearch.includeSameDoc,
			});
			if (seq !== searchSeq) return;
			applyVoiceResponse(res, requested);
		} catch (e) {
			if (seq !== searchSeq) return;
			if (e instanceof ApiError && e.status === 404 && 't' in anchor) {
				// ASR chunk spans and diarization turns disagree at boundaries, so a
				// `t`-anchor can land where no turn covers t exactly. Retry once at
				// the caller's fallback (the chunk start); the retry supersedes this
				// call's seq, so its own finally releases loadingHits.
				if (fallback) {
					void runVoice(fallback, label);
					return;
				}
				error =
					'No diarized speaker at that moment — try clicking a segment in the Speakers timeline.';
			} else {
				error = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : 'unknown error';
			}
			// First voice run failed → fall back to the results we were showing
			// (the header surfaces the error; the list keeps the old hits).
			if (!voiceQuery) leaveVoiceMode();
		} finally {
			if (seq === searchSeq) loadingHits = false;
		}
	}

	/** Run (or re-run) a voice query for an uploaded clip — the upload twin of
	 *  `runVoice` (same enter/snapshot/supersession lifecycle). No
	 *  `exclude_same_doc` (the clip belongs to no doc) and no 404 retry (there
	 *  is no anchor to miss); the response's `query` is all-null by design, so
	 *  the chip shows only `label`. `begin/endUpload` drive the search-bar's
	 *  attach-button spinner — the first upload after a backend restart
	 *  lazy-loads the encoder (~5 s). */
	async function runVoiceUpload(file: File, label: string) {
		const seq = ++searchSeq; // supersede any in-flight text/voice search
		enterVoice(label);
		voiceAnchor = null;
		voiceUpload = file;
		voiceSearch.beginUpload();
		try {
			const requested = voiceN;
			const res = await voiceSimilarUpload(file, { n: requested });
			if (seq !== searchSeq) return;
			applyVoiceResponse(res, requested);
		} catch (e) {
			if (seq !== searchSeq) return;
			error = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : 'unknown error';
			// First voice run failed → fall back to the results we were showing
			// (the header surfaces the error; the list keeps the old hits).
			if (!voiceQuery) leaveVoiceMode();
		} finally {
			voiceSearch.endUpload();
			if (seq === searchSeq) loadingHits = false;
		}
	}

	/** Exit voice mode and restore the snapshotted text-search results. */
	function leaveVoiceMode() {
		voiceActive = false;
		voiceQuery = null;
		voiceLabel = null;
		voiceAnchor = null;
		voiceUpload = null;
		voiceN = PAGE_STEP;
		voiceRawCount = 0;
		if (prevResults) {
			hits = prevResults.hits;
			active = prevResults.active;
			allLoaded = prevResults.allLoaded;
			prevResults = null;
		}
	}

	/** The voice chip's ✕ — back to the previous text-search results. */
	function dismissVoice() {
		searchSeq++; // supersede any in-flight voice fetch
		loadingHits = false;
		error = null;
		leaveVoiceMode();
	}

	// Consume voice-search requests from anywhere (hit-card buttons, the
	// diarization timeline via voiceSearch.request(), URL deep-links). The
	// store queues the request, so callers can fire before this page settles.
	$effect(() => {
		const req = voiceSearch.pending;
		if (!req) return;
		voiceSearch.pending = null;
		voiceN = PAGE_STEP; // a new anchor/clip restarts paging
		untrack(() => {
			if ('upload' in req) void runVoiceUpload(req.upload, req.label);
			else void runVoice(req.anchor, req.label, req.fallback);
		});
	});

	// Auto-apply the Settings "include same video" toggle to an active voice
	// query. `lastIncludeSame` keeps the effect from refiring on unrelated
	// dependency changes; `untrack` keeps runVoice's reads out of the deps.
	// Upload-anchored queries (voiceAnchor === null) are deliberately skipped —
	// the toggle can't change their results (a clip belongs to no doc).
	let lastIncludeSame = voiceSearch.includeSameDoc;
	$effect(() => {
		const inc = voiceSearch.includeSameDoc;
		if (inc === lastIncludeSame) return;
		lastIncludeSame = inc;
		untrack(() => {
			if (voiceActive && voiceAnchor) void runVoice(voiceAnchor);
		});
	});

	async function runSearch(next: SearchSpec) {
		const seq = ++searchSeq; // supersede any in-flight search/loadMore, incl. on clear
		// A new text search supersedes an active voice query AND its snapshot —
		// the user moved on, so there's nothing to "return" to anymore.
		prevResults = null;
		leaveVoiceMode();
		// Reset paging on a new search.
		spec = { ...next, n: next.n ?? PAGE_STEP };
		allLoaded = false;
		if (!spec.q && !spec.image && !spec.topic) {
			hits = [];
			loadingHits = false; // a superseded call's guarded finally won't reset it
			return;
		}
		// A new search supersedes any prior map selection so the Map-view table +
		// dimming reflect the fresh hits, not a stale lasso (mapTableHits is
		// `mapSelectionTotal > 0 ? mapHits : hits`). A later lasso re-narrows.
		crossFilter.clearSelection();
		mapHits = [];
		mapSelectionTotal = 0;
		loadingHits = true;
		error = null;
		try {
			const requested = spec.n ?? PAGE_STEP;
			const result = await search(spec);
			if (seq !== searchSeq) return; // a newer search superseded this one
			hits = result;
			active = null;
			if (result.length < requested) allLoaded = true;
		} catch (e) {
			if (seq !== searchSeq) return;
			hits = [];
			error = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : 'unknown error';
		} finally {
			if (seq === searchSeq) loadingHits = false;
		}
	}

	/** Load PAGE_STEP more hits by re-running the search with a larger limit. */
	async function loadMore() {
		if (loadingMore || allLoaded) return;
		loadingMore = true;
		const seq = ++searchSeq;
		try {
			if (voiceActive && (voiceAnchor || voiceUpload)) {
				// Voice mode pages on voiceN, not the text spec. Both voice flavours
				// page identically — only the fetch differs (anchor GET vs clip POST).
				const nextN = voiceN + PAGE_STEP;
				const res = voiceUpload
					? await voiceSimilarUpload(voiceUpload, { n: nextN })
					: voiceAnchor
						? await voiceSimilar(voiceAnchor, {
								n: nextN,
								excludeSameDoc: !voiceSearch.includeSameDoc,
							})
						: null;
				if (res && seq === searchSeq) {
					hits = dedupeVoiceHits(res.hits);
					voiceN = nextN;
					// Exhausted only when paging stopped yielding new raw hits, or the
					// request reached the backend clamp (no growth possible past it) —
					// `length < nextN` proves nothing in voice mode (see voiceRawCount).
					if (res.hits.length <= voiceRawCount || nextN >= VOICE_MAX_N) allLoaded = true;
					voiceRawCount = res.hits.length;
				}
				return;
			}
			const nextN = (spec.n ?? PAGE_STEP) + PAGE_STEP;
			const result = await search({ ...spec, n: nextN });
			if (seq === searchSeq) {
				hits = result;
				spec = { ...spec, n: nextN };
				if (result.length < nextN) allLoaded = true;
			}
		} catch {
			// Silent on load-more errors — keep the hits already shown.
		} finally {
			// Unguarded: runSearch never resets loadingMore, so a superseded
			// load-more must release the button itself.
			loadingMore = false;
		}
	}

	/** Load documents whenever in browse mode + page changes. */
	$effect(() => {
		if (!isBrowsing) return;
		let cancelled = false;
		loadingDocs = true;
		docsError = null;
		listDocuments(docsPage, PER_PAGE)
			.then((r) => {
				if (cancelled) return;
				docs = r.docs;
				docsTotal = r.total;
			})
			.catch((e) => {
				if (cancelled) return;
				docs = [];
				docsTotal = 0;
				docsError =
					e instanceof ApiError
						? e.detail
						: e instanceof Error
							? e.message
							: 'failed to load documents';
			})
			.finally(() => {
				if (!cancelled) loadingDocs = false;
			});
		return () => {
			cancelled = true;
		};
	});

	/**
	 * Global keyboard affordances:
	 *   "/"    focus the search box (unless already typing in a field)
	 *   Esc    return from a playing hit back to the results list
	 */
	function onWindowKeydown(e: KeyboardEvent) {
		const t = e.target as HTMLElement | null;
		const typing =
			!!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
		if (e.key === '/' && !typing) {
			e.preventDefault();
			const el = document.querySelector<HTMLInputElement>('input[type="search"]');
			el?.focus();
			el?.select();
		} else if (e.key === 'Escape' && active && !typing) {
			active = null;
		}
	}

	// Hand-off from the Tree page: `/?topic=<name>` opens Search already filtered
	// to that topic — a filter-only browse (no query text), backed by the
	// topic-only branch in `run_search`. Runs once on mount.
	onMount(() => {
		// One status probe gates every voice affordance (hit-card buttons, the
		// Settings toggle) — shared via the voiceSearch store.
		voiceSearch.probe();
		const topic = page.url.searchParams.get('topic');
		if (topic) runSearch({ q: '', topic, n: spec.n ?? 100, mode: spec.mode });
		// Voice deep-link (e.g. from the diarization timeline):
		//   /?voice_doc=<id>&(voice_turn=<int> | voice_speaker=<label> | voice_t=<seconds>)
		// Same one-shot URL-param pattern as `topic`/`doc` here. Runtime callers
		// on this page should use voiceSearch.request(...) instead.
		const voiceDoc = page.url.searchParams.get('voice_doc');
		if (voiceDoc) {
			const turnRaw = page.url.searchParams.get('voice_turn');
			const speaker = page.url.searchParams.get('voice_speaker');
			const tRaw = page.url.searchParams.get('voice_t');
			if (turnRaw !== null && Number.isInteger(Number(turnRaw))) {
				voiceSearch.request({ docId: voiceDoc, turnId: Number(turnRaw) });
			} else if (speaker) {
				voiceSearch.request({ docId: voiceDoc, speaker });
			} else if (tRaw !== null && Number.isFinite(Number(tRaw))) {
				voiceSearch.request({ docId: voiceDoc, t: Number(tRaw) });
			}
		}
		// Deep-link from the graph explorer: open a doc in the player at time `t`.
		// Player media is keyed on the doc id; a minimal synthetic hit seeks to t.
		const docId = page.url.searchParams.get('doc');
		if (docId) {
			const t = Number(page.url.searchParams.get('t'));
			const start = Number.isFinite(t) ? Math.max(0, t) : 0;
			active = playerRow(docId, start, start);
		}
	});

	/**
	 * When user clicks a doc tile, just open it in the player with a synthetic
	 * full-document "hit" pointing at start=0 (carrying the doc's own display
	 * fields). Earlier I auto-set a metadata filter — it then silently stuck
	 * across the next search and produced 0 hits. Now nothing is implicitly
	 * filtered.
	 */
	function openDoc(doc: Document) {
		active = playerRow(ds.docId(doc), 0, ds.duration(doc) ?? 0, doc);
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="grid h-full min-h-0 grid-rows-[auto_1fr]">
	<div class="border-border bg-card/40 border-b">
		<SearchBar bind:spec onsubmit={runSearch} adoptSignal={searchBarAdopt} />
		<div class="flex items-center justify-end px-6 pb-1">
			<SavedViews {spec} onapply={applySavedView} />
		</div>
		<ActiveFilters bind:spec onchange={runSearch} />
		{#if voiceActive}
			<!-- Voice query chip — mirrors the attached-image chip's lifecycle:
           dismissing returns to the previous text-search results. -->
			<div class="flex flex-wrap items-center gap-2 px-6 pb-3 text-xs">
				<Badge variant="secondary" class="gap-1.5 py-0.5 pr-0.5 pl-2">
					<AudioLines class="text-primary" />
					<span class="max-w-[24rem] truncate">
						Voice: {voiceLabel ?? voiceQuery?.doc_id ?? '…'}
						{#if voiceQuery?.speaker_label}· {voiceQuery.speaker_label}{/if}
					</span>
					<Button
						variant="ghost"
						size="icon-xs"
						class="size-4 rounded-full"
						title="Clear voice search — back to the previous results"
						aria-label="Clear voice search"
						onclick={dismissVoice}
					>
						<X />
					</Button>
				</Badge>
				{#if voiceQuery && voiceQuery.turn_start != null && voiceQuery.turn_end != null}
					<span class="text-muted-foreground">
						anchor turn {fmtTime(voiceQuery.turn_start)}–{fmtTime(voiceQuery.turn_end)}
					</span>
				{/if}
			</div>
		{/if}
	</div>

	<ResizableSplit minLeft={420} minRight={360} initial={0.6}>
		{#snippet left()}
			<!-- ── Left: results ── -->
			<div class="flex h-full min-h-0 flex-col">
				<!-- Results header: count + view-mode toggle -->
				<div class="border-border bg-card/30 flex items-center gap-3 border-b px-4 py-2 text-xs">
					<span class="text-muted-foreground">
						{#if isBrowsing}
							Browsing {docsTotal} document{docsTotal === 1 ? '' : 's'}
						{:else if loadingHits}
							Searching…
						{:else if error}
							<span class="text-destructive">Error: {error}</span>
						{:else if hits.length === 0}
							No hits.
						{:else}
							<strong class="text-foreground">{hits.length}</strong>
							{hits.length === 1 ? 'chunk' : 'chunks'}
							across
							<strong class="text-foreground">{docCount}</strong>
							{docCount === 1 ? 'document' : 'documents'}
							{#if allLoaded}<span class="text-muted-foreground/70">· all results shown</span>{/if}
						{/if}
					</span>

					<div class="ml-auto flex items-center gap-2">
						{#if !isBrowsing && hits.length > 0}
							<Button
								size="xs"
								title="Send these results into a labeling task as claimable items"
								onclick={() => sendHits(hits, 'search')}
							>
								Send to labeling task…
							</Button>
						{/if}
						{#if view === 'grid'}
							<div class="border-border mr-1 flex items-center gap-1 border-r pr-2">
								<span class="text-muted-foreground/70 mr-1">cols</span>
								<Button
									variant="ghost"
									size="icon-xs"
									disabled={gridCols <= 2}
									title="Fewer columns"
									aria-label="Fewer grid columns"
									onclick={() => setGridCols(Math.max(2, gridCols - 1))}
								>
									<Minus />
								</Button>
								<span class="w-4 text-center font-mono text-xs">{gridCols}</span>
								<Button
									variant="ghost"
									size="icon-xs"
									disabled={gridCols >= 6}
									title="More columns"
									aria-label="More grid columns"
									onclick={() => setGridCols(Math.min(6, gridCols + 1))}
								>
									<Plus />
								</Button>
							</div>
						{/if}
						<!-- Layout picker as one segmented control (the estate's grouped-button
						     idiom) rather than four free-floating icon buttons. -->
						<div
							class="border-border bg-background flex items-center gap-0.5 rounded-lg border p-0.5"
						>
							<Button
								variant={view === 'list' ? 'secondary' : 'ghost'}
								size="icon-sm"
								title="List view"
								aria-label="List view"
								aria-pressed={view === 'list'}
								onclick={() => (view = 'list')}
							>
								<ListIcon />
							</Button>
							<Button
								variant={view === 'grid' ? 'secondary' : 'ghost'}
								size="icon-sm"
								title="Grid view"
								aria-label="Grid view"
								aria-pressed={view === 'grid'}
								onclick={() => (view = 'grid')}
							>
								<LayoutGrid />
							</Button>
							<Button
								variant={view === 'table' ? 'secondary' : 'ghost'}
								size="icon-sm"
								title="Table view — see column values per row"
								aria-label="Table view"
								aria-pressed={view === 'table'}
								onclick={() => (view = 'table')}
							>
								<TableIcon />
							</Button>
							<Button
								variant={view === 'map' ? 'secondary' : 'ghost'}
								size="icon-sm"
								title="Map view — the EVōC embedding atlas (cross-filters with search)"
								aria-label="Map view"
								aria-pressed={view === 'map'}
								onclick={() => (view = 'map')}
							>
								<MapIcon />
							</Button>
						</div>
					</div>
				</div>

				{#if view === 'map'}
					<!-- ── Map view: the embedding atlas + a draggable selection/results table ── -->
					<div class="min-h-0 flex-1">
						<ResizableSplit
							orientation="vertical"
							storageKey="lance-media-search-map-vsplit"
							minLeft={220}
							minRight={120}
							initial={0.66}
						>
							{#snippet left()}
								<AtlasMap
									bind:active
									onSeedSearch={seedSearchFromSelection}
									onSelectionHits={onMapSelectionHits}
								/>
							{/snippet}
							{#snippet right()}
								<div class="border-border bg-card/30 flex h-full min-h-0 flex-col border-t">
									<div class="border-border flex items-center gap-2 border-b px-3 py-1.5 text-xs">
										{#if mapSelectionTotal > 0}
											<span class="text-foreground font-medium">Map selection</span>
											<span class="text-muted-foreground">
												{mapSelectionTotal.toLocaleString()} chunks
												{#if mapSelectionTotal > mapHits.length}
													<span class="text-muted-foreground/70">· showing {mapHits.length}</span>
												{/if}
											</span>
											{#if autoLabelMsg}
												<span class="text-muted-foreground ml-auto font-mono text-xs" title="batch job">
													{autoLabelMsg}
												</span>
											{/if}
											<Button
												variant="outline"
												size="xs"
												class={autoLabelMsg ? '' : 'ml-auto'}
												title="Auto-label this selection (batch deriver over the scope)"
												onclick={() => autoLabelHits(mapHits)}
											>
												Auto-label {mapHits.length}
											</Button>
											<Button
												variant="outline"
												size="xs"
												title="Open this selection in the annotator"
												onclick={() => annotateHits(mapHits)}
											>
												Annotate {mapHits.length}
											</Button>
											<Button
												size="xs"
												title="Send this selection into a labeling task as claimable items"
												onclick={() => sendHits(mapHits, 'atlas')}
											>
												Send to labeling task…
											</Button>
										{:else if hits.length > 0}
											<span class="text-foreground font-medium">Search results</span>
											<span class="text-muted-foreground">
												{hits.length.toLocaleString()} hits · highlighted on the map
											</span>
											<Button
												size="xs"
												class="ml-auto"
												title="Send these results into a labeling task as claimable items"
												onclick={() => sendHits(hits, 'search')}
											>
												Send to labeling task…
											</Button>
										{:else}
											<span class="text-foreground font-medium">Selection</span>
											<span class="text-muted-foreground"
												>lasso a region, click a legend, or search to list chunks</span
											>
										{/if}
									</div>
									<div class="min-h-0 flex-1 overflow-auto">
										{#if mapTableHits.length}
											<HitTable
												hits={mapTableHits}
												{active}
												visible={MAP_TABLE_COLS}
												widths={tableWidths}
												wrap={tableWrap}
												onwidthchange={setColWidth}
												query={mapSelectionTotal > 0 ? '' : resultsQuery}
												onselect={(h) => (active = h)}
											/>
										{/if}
									</div>
								</div>
							{/snippet}
						</ResizableSplit>
					</div>
				{:else}
					<div class="relative min-h-0 flex-1 overflow-y-auto">
						{#if isBrowsing}
							<!-- ── Browse mode: documents ── -->
							{#if loadingDocs && docs.length === 0}
								<div class="text-muted-foreground flex h-full items-center justify-center text-sm">
									<Loader2 class="mr-2 size-4 animate-spin" /> Loading documents…
								</div>
							{:else if docsError}
								<div class="flex h-full items-center justify-center px-6 text-center text-sm">
									<span class="text-destructive">Error: {docsError}</span>
								</div>
							{:else if view === 'grid'}
								<div
									class="grid gap-4 p-4"
									style:grid-template-columns="repeat({gridCols}, minmax(0, 1fr))"
								>
									{#each docs as doc (ds.docId(doc))}
										<DocTile
											{doc}
											active={activeDocId === ds.docId(doc)}
											onclick={() => openDoc(doc)}
										/>
									{/each}
								</div>
							{:else if view === 'table'}
								{@const docCols = DOC_COLUMNS.filter((c) => docTableCols.includes(c.key))}
								<div
									class="border-border bg-card/30 flex flex-wrap items-center gap-1 border-b px-3 py-2 text-xs"
								>
									<span class="text-muted-foreground mr-1">Columns:</span>
									{#each DOC_COLUMNS as c (c.key)}
										{@const shown = docTableCols.includes(c.key)}
										<Button
											variant={shown ? 'secondary' : 'ghost'}
											size="xs"
											class={shown ? '' : 'text-muted-foreground'}
											aria-pressed={shown}
											onclick={() => toggleDocCol(c.key)}
										>
											{c.label}
										</Button>
									{/each}
								</div>
								<div class="overflow-x-auto">
									<table class="w-full border-collapse text-xs">
										<thead>
											<tr
												class="border-border bg-card text-muted-foreground sticky top-0 z-10 border-b text-left"
											>
												{#each docCols as c (c.key)}
													<th class="px-3 py-2 font-medium whitespace-nowrap">{c.label}</th>
												{/each}
											</tr>
										</thead>
										<tbody>
											{#each docs as doc (ds.docId(doc))}
												<tr
													class={'border-border/60 hover:bg-muted/60 cursor-pointer border-b ' +
	(activeDocId === ds.docId(doc)
		? 'bg-primary/15 font-medium [box-shadow:inset_3px_0_0_0_var(--color-primary)]'
		: '')}
													onclick={() => openDoc(doc)}
												>
													{#each docCols as c (c.key)}
														{#if c.key === 'thumbnail'}
															<td class="px-3 py-1.5 align-top">
																<img
																	src={thumbnailUrl(doc)}
																	loading="lazy"
																	alt=""
																	class="bg-muted h-9 w-16 rounded-md object-cover"
																	onerror={(e) =>
	((e.currentTarget as HTMLImageElement).style.visibility =
		'hidden')}
																/>
															</td>
														{:else}
															<td
																class="text-muted-foreground max-w-[28rem] truncate px-3 py-1.5 align-top whitespace-nowrap"
																title={c.get(doc)}
															>
																{c.get(doc)}
															</td>
														{/if}
													{/each}
												</tr>
											{/each}
										</tbody>
									</table>
								</div>
							{:else}
								<ul class="divide-border divide-y">
									{#each docs as doc (ds.docId(doc))}
										<li>
											<button
												type="button"
												onclick={() => openDoc(doc)}
												class="hover:bg-muted/60 flex w-full items-center gap-3 px-4 py-2 text-left"
											>
												<span class="flex-1 truncate text-sm">{ds.title(doc)}</span>
												<span class="text-muted-foreground font-mono text-xs">
													{ds.metadata(doc).find((m) => m.value !== ds.title(doc))?.value ?? ''}
												</span>
											</button>
										</li>
									{/each}
								</ul>
							{/if}

							{#if docsTotalPages > 1}
								<div
									class="border-border bg-card/80 sticky bottom-0 flex items-center justify-end gap-1 border-t px-4 py-2 text-xs backdrop-blur"
								>
									<span class="text-muted-foreground mr-2">page {docsPage} / {docsTotalPages}</span>
									<Button
										variant="outline"
										size="icon-sm"
										aria-label="Previous page"
										disabled={docsPage <= 1}
										onclick={() => (docsPage = Math.max(1, docsPage - 1))}
									>
										<ChevronLeft />
									</Button>
									<Button
										variant="outline"
										size="icon-sm"
										aria-label="Next page"
										disabled={docsPage >= docsTotalPages}
										onclick={() => (docsPage = Math.min(docsTotalPages, docsPage + 1))}
									>
										<ChevronRight />
									</Button>
								</div>
							{/if}
						{:else if loadingHits}
							<div class="text-muted-foreground flex h-full items-center justify-center text-sm">
								<Loader2 class="mr-2 size-4 animate-spin" /> Searching…
							</div>
						{:else if hits.length === 0}
							<div
								class="text-muted-foreground flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-sm"
							>
								<SearchX class="text-muted-foreground/60 size-6" />
								<div>No hits.</div>
								<div class="text-xs">
									{#if voiceActive}
										No similar voices found — try <strong>include same video</strong> in Settings.
									{:else}
										Try toggling <strong>Match by</strong> to <em>Semantic</em> or switching
										<strong>Style</strong>
										to <em>Fuzzy</em>.
									{/if}
								</div>
							</div>
						{:else if view === 'grid'}
							<div
								class="grid gap-3 p-3"
								style:grid-template-columns="repeat({gridCols}, minmax(0, 1fr))"
							>
								{#each hits as hit (hitKey(hit))}
									<HitCard
										{hit}
										query={resultsQuery}
										highlight={gridHighlight}
										active={activeKey === hitKey(hit)}
										layout="tile"
										onclick={() => (active = hit)}
									/>
								{/each}
							</div>
						{:else if view === 'table'}
							<!-- Column chooser: click a column to show/hide it in the table. -->
							<div
								class="border-border bg-card/30 flex flex-wrap items-center gap-1 border-b px-3 py-2 text-xs"
							>
								<span class="text-muted-foreground mr-1">Columns:</span>
								{#each TABLE_COLUMNS() as c (c.key)}
									{@const shown = tableCols.includes(c.key)}
									<Button
										variant={shown ? 'secondary' : 'ghost'}
										size="xs"
										class={shown ? '' : 'text-muted-foreground'}
										aria-pressed={shown}
										onclick={() => toggleCol(c.key)}
									>
										{c.label}
									</Button>
								{/each}
								<!-- Wrap toggle: truncate (off) vs wrap-and-grow (on) for the
                     text-like columns. Applies + persists instantly. -->
								<label
									class="text-muted-foreground ml-auto flex cursor-pointer items-center gap-1.5 select-none"
								>
									<span>Wrap text</span>
									<Switch
										bind:checked={() => tableWrap, setTableWrap}
										aria-label="Wrap text in the text columns"
									/>
								</label>
							</div>
							<HitTable
								{hits}
								{active}
								visible={tableCols}
								widths={tableWidths}
								wrap={tableWrap}
								onwidthchange={setColWidth}
								query={resultsQuery}
								onselect={(h) => (active = h)}
							/>
						{:else}
							<HitList {hits} query={resultsQuery} {active} onselect={(h) => (active = h)} />
						{/if}

						<!-- Pagination: only when we have hits and aren't already exhausted. -->
						{#if !isBrowsing && hits.length > 0 && !allLoaded}
							<div class="border-border bg-card/40 flex justify-center border-t px-4 py-3">
								<Button variant="outline" size="sm" disabled={loadingMore} onclick={loadMore}>
									{loadingMore ? 'Loading…' : `Show ${PAGE_STEP} more`}
								</Button>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		{/snippet}

		{#snippet right()}
			<!-- ── Right: player pane ── -->
			<div class="bg-muted/30 h-full min-h-0">
				<PlayerPane hit={active} query={resultsQuery} />
			</div>
		{/snippet}
	</ResizableSplit>
</div>

<SendToProjectDialog
	bind:open={sendOpen}
	keys={sendKeys}
	dataset={ds.id}
	datasetVersion={ds.descriptor.tables[ds.descriptor.declared.search?.row_table ?? '']?.version ??
	null}
	origin={sendOrigin}
/>
