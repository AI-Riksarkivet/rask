<script lang="ts">
	/**
	 * AtlasMap — the EVōC embedding map as a reusable child of the search page.
	 *
	 * Renders a custom WebGPU point scatter (<GpuScatter>) over Float32Array x/y
	 * for the CURRENT projection space. The renderer draws instanced quads shaded
	 * into round, premultiplied sprites — modern WebGPU only, no legacy fallback.
	 *
	 * The shared cross-filter store fuses into a per-point RGBA buffer (the
	 * shader reads true per-point alpha, so "dim" is just a low alpha — no
	 * reserved colour slot, no 256-category cap). Bidirectional:
	 *
	 *   • search/filter → `crossFilter.filteredIds` dims non-matching points;
	 *   • lasso/cluster on the map → `crossFilter.selectedIds` fills the table
	 *     (via the page) and can seed a search.
	 *
	 * Everything is Lance-native: points ← GET /api/atlas/points, selection detail
	 * ← POST /api/atlas/chunks, single playback ← GET /api/atlas/chunk.
	 */
	import GpuScatter from './gpu-scatter.svelte';
	import {
		getAtlasPoints,
		getAtlasChunk,
		getAtlasChunks,
		getAtlasStatus,
		type AtlasPoints,
		type AtlasSpace,
		type Hit,
	} from '@rask/media-api';
	import { crossFilter, buildKeyIndex, hitKey, type ColorBy } from './cross-filter.svelte';
	import { gpuUnsupportedReason } from './gpu-support';
	import { activeView } from '@rask/media-api/descriptor';
	import { buildGrid, nearestIndex, type SpatialGrid } from './atlas-grid';
	import { hexToRgb, hueRgb, buildHuePalette, type Rgb } from './atlas-colors';
	import { indicesInPolygon, type Pt } from './atlas-geometry';
	import AtlasTooltip from './AtlasTooltip.svelte';
	import AtlasLegend from './AtlasLegend.svelte';
	import {
		visibleIds,
		buildClusterLegend,
		clusterStats as buildClusterStats,
		buildCategoryLegend,
		channelLabel,
		categoryTitle as deriveCategoryTitle,
		type CategoryChannel,
		type ClusterRanking,
	} from './atlas-legend';
	// The canvas is this component's own WebGPU renderer; only the overlay CONTROLS
	// use the shared design system, so the map chrome matches the rest of the estate.
	import { Button } from '@rask/ui';
	import { Select } from '@rask/ui/select';
	import type { SelectOption } from '@rask/ui/select';
	import { Loader2, Lasso, X, Hand, Settings2 } from '@lucide/svelte';
	import { useColorMode } from '@rask/ui/color-mode';

	let {
		active = $bindable(null),
		onSeedSearch,
		onSelectionHits,
	}: {
		active?: Hit | null;
		/** Promote the current map selection to a search (by stable `_rowid`). */
		onSeedSearch?: (rowids: number[]) => void;
		/** Surface the lasso/box selection's hits to the page (drives HitTable). */
		onSelectionHits?: (hits: Hit[], total: number) => void;
	} = $props();

	// The dataset descriptor — every space/channel/identity read routes through
	// it, so the map names no corpus column. Loaded by the root layout before any
	// route renders, so it's resolved here.
	const view = activeView();

	// The WebGPU scatter reads TRUE per-point alpha, so the old 256-slot /
	// categoryColors palette (and its grey-padding monochrome bug) is gone: we
	// build a per-point RGBA buffer directly. We still bound the number of
	// DISTINCT cluster/language hues only to keep the legend sane.
	const MAX_DISTINCT = 512;

	let error = $state<string | null>(null);
	let loading = $state(true);
	let pts = $state.raw<AtlasPoints | null>(null);
	let x = $state.raw<Float32Array | null>(null); // decoded f32 coords (CPU math)
	let y = $state.raw<Float32Array | null>(null);
	let xBits = $state.raw<Uint16Array | null>(null); // raw f16 bits (GPU vertex data)
	let yBits = $state.raw<Uint16Array | null>(null);
	// Data extent computed from the decoded f32 x/y (same source the grid uses) so
	// GpuScatter's camera fit matches the rendered f16 positions.
	let fitBounds = $state.raw<{ minX: number; minY: number; maxX: number; maxY: number }>({
		minX: 0,
		minY: 0,
		maxX: 1,
		maxY: 1,
	});
	let grid = $state.raw<SpatialGrid | null>(null); // uniform bucket index for hover
	// Which declared spaces are built (x column has non-null rows), keyed by
	// declared space name — gates each space tab. The first/current space stays
	// selectable even before /status resolves.
	let builtSpaces = $state.raw<Record<string, boolean>>({});

	let pointSize = $state(0); // 0 = auto
	let filterAlpha = $state(8); // 0..120 — opacity of EXCLUDED points (search-filtered OR lasso-not-selected)
	// Derived straight from the store — the selection is also cleared OUTSIDE this
	// component (routes/+page, the workflow Atlas modal), so a manual copy desyncs.
	const selectionCount = $derived(crossFilter.selectedIds.size);
	let tableLoading = $state(false);
	let mode = $state<'lasso' | 'pan'>('lasso');

	// hover popover
	let hoverIndex = $state<number | null>(null);
	let hoverX = $state(0);
	let hoverY = $state(0);
	let mapEl = $state<HTMLDivElement | null>(null);

	// scatter size
	let mapW = $state(0);
	let mapH = $state(0);

	// theme follows the app's theme button (toggles `.dark` on <html>)
	const theme = useColorMode();
	const isDark = $derived(theme.isDark);

	// Spatial-grid hover picking (`buildGrid`/`nearestIndex`) lives in ./atlas-grid;
	// lasso geometry in ./atlas-geometry; colour math in ./atlas-colors — all pure.

	/** Data extent of the decoded f32 coords (drives GpuScatter's camera fit). */
	function boundsOf(
		xs: Float32Array,
		ys: Float32Array,
	): { minX: number; minY: number; maxX: number; maxY: number } {
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		const n = Math.min(xs.length, ys.length);
		for (let i = 0; i < n; i++) {
			const xi = xs[i]!;
			const yi = ys[i]!;
			if (xi < minX) minX = xi;
			if (xi > maxX) maxX = xi;
			if (yi < minY) minY = yi;
			if (yi > maxY) maxY = yi;
		}
		if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
		return { minX, minY, maxX, maxY };
	}

	// ── load points for a space + (re)build the shared key→index map ──────────
	async function load(space: AtlasSpace): Promise<void> {
		loading = true;
		error = null;
		try {
			const p = await getAtlasPoints(space);
			pts = p;
			// x/y arrive already decoded to owned Float32Arrays (api.ts) — consume the
			// views directly, no `Float32Array.from(...)` copy. The raw f16 bits feed
			// the GPU vertex buffer.
			const xs = p.x;
			const ys = p.y;
			x = xs;
			y = ys;
			xBits = p.xBits;
			yBits = p.yBits;
			grid = buildGrid(xs, ys);
			// Compute the data extent from the SAME decoded f32 so GpuScatter's fit
			// lines up with the f16 GPU positions (both derive from these bits).
			fitBounds = boundsOf(xs, ys);
			crossFilter.resetForSpace(buildKeyIndex(p));
			onSelectionHits?.([], 0);
		} catch (e) {
			pts = null;
			x = null;
			y = null;
			xBits = null;
			yBits = null;
			grid = null;
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	// Keep the shown space valid for THIS dataset: if the persisted/default space
	// isn't one the descriptor declares, fall back to the first declared space.
	$effect(() => {
		const spaces = view.atlasSpaces;
		if (spaces.length > 0 && !spaces.some((s) => s.name === crossFilter.space)) {
			crossFilter.setSpace(spaces[0]!.name);
		}
	});

	// Whether this dataset declares any atlas spaces at all. When it doesn't
	// (e.g. a corpus with no projections), the whole view degrades to an empty
	// state and NEVER fetches /api/atlas/* — no spurious 404s.
	const hasAtlas = $derived(view.atlasSpaces.length > 0);

	// Probe which declared spaces are built (gates the space tabs), then load the
	// current one. /status reports every declared space's built-ness by name.
	$effect(() => {
		if (!hasAtlas) return;
		let cancelled = false;
		(async () => {
			try {
				const status = await getAtlasStatus(crossFilter.space);
				if (!cancelled) builtSpaces = status.spaces ?? {};
			} catch {
				/* leave builtSpaces as-is */
			}
		})();
		return () => {
			cancelled = true;
		};
	});

	// Can this browser draw the map at all? Probed once, and BEFORE any projection is fetched.
	// `undefined` = not probed yet, `null` = supported, a string = the reason it is not. The three states
	// are distinct on purpose: treating "not yet known" as "supported" would fetch on the first tick and
	// defeat the gate, and treating it as "unsupported" would flash an error on every load.
	//
	// The svelte MCP autofixer flags assigning state inside an `$effect` and suggests `$derived`. Judged and
	// kept, deliberately: this zone does enable `compilerOptions.experimental.async`, so
	// `$derived(await gpuUnsupportedReason())` would compile — but awaiting in a derived *suspends* the
	// subtree until it settles, which needs a `<svelte:boundary>` with a pending snippet and changes what
	// every load renders. This is a one-shot async probe with a cancellation guard, which is the documented
	// correct shape for `$effect`; and the three-state above is load-bearing, since "not yet known" is
	// precisely the state that must not fetch. Deviates from the suggestion, with a reason.
	let gpuReason = $state<string | null | undefined>(undefined);
	$effect(() => {
		let cancelled = false;
		void gpuUnsupportedReason().then((reason) => {
			if (!cancelled) gpuReason = reason;
		});
		return () => {
			cancelled = true;
		};
	});

	// Load whenever the chosen space changes (initial mount + Text/Visual toggle).
	$effect(() => {
		const space = crossFilter.space;
		if (!hasAtlas) return;
		// The projection is 6.6 MB and the renderer is WebGPU-only with no fallback, so on a browser that
		// cannot run it every one of those bytes buys a red error message. Measured before this gate
		// existed: 31.8 MiB across four toggles, all of it discarded. Wait for the probe rather than
		// racing it — `undefined` means we do not yet know, and guessing costs a download.
		if (gpuReason !== null) return;
		void load(space);
	});

	async function switchSpace(space: AtlasSpace): Promise<void> {
		if (space === crossFilter.space) return;
		if (spaceTabs.find((t) => t.value === space)?.disabled) return; // gated until built
		crossFilter.setSpace(space);
	}

	// Colour math (hslToRgb / hexToRgb / hueRgb / hueCss / buildHuePalette) is in
	// ./atlas-colors; the derived swatch/point colours below pass the live theme.
	const NOISE_RGB = $derived(hexToRgb(isDark ? '#52525b' : '#cccccc'));
	const OTHER_RGB = $derived(hexToRgb(isDark ? '#71717a' : '#a1a1aa'));
	const ACCENT_RGB = $derived(hueRgb(1, 4, isDark));
	const BG_COLOR = $derived(isDark ? '#0a0a0a' : '#ffffff');

	const NOISE_ALPHA = 70; // HDBSCAN noise: muted so coloured clusters dominate

	/**
	 * THEME-INDEPENDENT size ranking. `slotOf` maps clusterId → its dense rank
	 * (0..distinct-1, or -1 for the small-cluster tail beyond MAX_DISTINCT).
	 * `distinct` is how many distinct cluster hues we emit.
	 */
	const clusterRanking = $derived.by((): ClusterRanking | null => {
		const cl = pts?.cluster;
		if (!cl) return null;
		const counts = new Map<number, number>();
		for (const c of cl) if (c >= 0) counts.set(c, (counts.get(c) ?? 0) + 1);
		const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
		const distinct = Math.min(ranked.length, MAX_DISTINCT);
		const slotOf = new Map<number, number>();
		ranked.forEach(([id], rank) => slotOf.set(id, rank < MAX_DISTINCT ? rank : -1));
		return { slotOf, distinct };
	});

	/** The factorized (codes, labels) pair backing a categorical colour mode — a
	 *  declared atlas channel read by name from `points.channels`. `cluster`/`none`
	 *  are NOT channels (cluster carries its own noise/#id/hide semantics). */
	function channelFor(p: AtlasPoints | null, mode: ColorBy): CategoryChannel | null {
		if (!p || mode === 'cluster' || mode === 'none') return null;
		return p.channels[mode] ?? null;
	}

	// Membership state enum bytes (must match the WGSL fragment + the old alphaFor
	// priority): 0=NORMAL (full), 1=NOISE (noiseAlpha), 2=DIMMED (filterAlpha),
	// 3=HIDDEN (discard). DIMMED outranks NOISE — a filtered-out/unselected noise
	// point dims to filterAlpha, exactly as the old alphaFor did.
	const ST_NORMAL = 0;
	const ST_NOISE = 1;
	const ST_DIMMED = 2;
	const ST_HIDDEN = 3;

	/**
	 * Per-point base RGB hue (length 3*count) — the category colour ONLY, no
	 * alpha. The shader applies the state-derived alpha. Rebuilt only when the
	 * HUE changes: colorBy / theme (isDark) / space (pts) / clusterRanking — NOT
	 * on the filterAlpha slider, NOT on selection. (hidden points are handled by
	 * the state buffer, so `hidden` is intentionally not a dep here.)
	 */
	const baseRgb = $derived.by((): Uint8Array | null => {
		const p = pts;
		if (!p) return null;
		const n = p.count;
		const colorBy = crossFilter.colorBy;
		const buf = new Uint8Array(n * 3);

		const write = (i: number, col: Rgb): void => {
			const o = i * 3;
			buf[o] = col.r;
			buf[o + 1] = col.g;
			buf[o + 2] = col.b;
		};

		if (colorBy === 'cluster' && p.cluster) {
			const r = clusterRanking;
			if (!r) return null;
			const { slotOf, distinct } = r;
			const palette = buildHuePalette(distinct, isDark); // distinct hues once, not per point
			const cl = p.cluster;
			const noise = NOISE_RGB;
			const other = OTHER_RGB;
			for (let i = 0; i < n; i++) {
				const c = cl[i]!;
				if (c < 0) {
					write(i, noise);
				} else {
					const rank = slotOf.get(c) ?? -1;
					write(i, rank < 0 ? other : palette[rank]!);
				}
			}
			return buf;
		}

		const channel = channelFor(p, colorBy);
		if (channel) {
			const { codes, labels } = channel;
			const distinct = Math.min(MAX_DISTINCT, labels.length);
			const palette = buildHuePalette(distinct, isDark); // distinct hues once, not per point
			const other = OTHER_RGB;
			const noise = NOISE_RGB;
			for (let i = 0; i < n; i++) {
				const code = codes[i] ?? 0;
				const empty = (labels[code] ?? '') === ''; // unclustered/noise → muted grey
				write(i, empty ? noise : code < distinct ? palette[code]! : other);
			}
			return buf;
		}

		// colorBy === 'none' (or the channel's data is absent): one accent hue.
		const accent = ACCENT_RGB;
		for (let i = 0; i < n; i++) write(i, accent);
		return buf;
	});

	/**
	 * Per-point membership state (length count, 1 byte/point). Rebuilt only on
	 * filteredIds / selectedIds / hidden / colorBy / space change — NOT on the
	 * filterAlpha slider (a uniform) and NOT on theme. This is the buffer a
	 * selection/filter change re-uploads (8× smaller than the old RGBA).
	 */
	const pointState = $derived.by((): Uint8Array | null => {
		const p = pts;
		if (!p) return null;
		const n = p.count;
		const colorBy = crossFilter.colorBy;
		const buf = new Uint8Array(n);

		// Read both cross-filter Sets once (tracks them as deps + avoids 145k method
		// calls). A point EXCLUDED by either the search filter or the lasso/cluster
		// selection becomes DIMMED (the shader applies the slider's filterAlpha).
		const fIds = crossFilter.filteredIds;
		const sel = crossFilter.selectedIds;
		const selActive = sel.size > 0;
		const hidden = crossFilter.hidden;

		// Cluster mode: noise (c<0) shares one hide key (-1); clusters use their id.
		if (colorBy === 'cluster' && p.cluster) {
			const cl = p.cluster;
			for (let i = 0; i < n; i++) {
				const c = cl[i]!;
				if (hidden.has(c < 0 ? -1 : c)) {
					buf[i] = ST_HIDDEN;
					continue;
				}
				if ((fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i))) buf[i] = ST_DIMMED;
				else if (c < 0) buf[i] = ST_NOISE;
				else buf[i] = ST_NORMAL;
			}
			return buf;
		}

		const channel = channelFor(p, colorBy);
		if (channel) {
			const { codes, labels } = channel;
			for (let i = 0; i < n; i++) {
				const code = codes[i] ?? 0;
				if (hidden.has(code)) {
					buf[i] = ST_HIDDEN;
					continue;
				}
				if ((fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i))) buf[i] = ST_DIMMED;
				else if ((labels[code] ?? '') === '')
					buf[i] = ST_NOISE; // empty label → muted
				else buf[i] = ST_NORMAL;
			}
			return buf;
		}

		// colorBy === 'none': no noise/hidden semantics — only the dim membership.
		for (let i = 0; i < n; i++) {
			buf[i] =
				(fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i)) ? ST_DIMMED : ST_NORMAL;
		}
		return buf;
	});

	const autoPointSize = $derived(pointSize > 0 ? pointSize : 5);

	// Data-space coords of the active hit's point (the one in the player / table)
	// so <GpuScatter> can ring it — resolves via the shared key→index map.
	const activeMarkerXY = $derived.by((): [number, number] | null => {
		const a = active;
		const xs = x;
		const ys = y;
		if (!a || !xs || !ys) return null;
		const i = crossFilter.keyToIndex.get(hitKey(a));
		if (i === undefined) return null;
		const mx = xs[i];
		const my = ys[i];
		return mx !== undefined && my !== undefined ? [mx, my] : null;
	});

	// ── filter-aware legend counts ────────────────────────────────────────────
	// The legend math is PURE (./atlas-legend, unit-testable): these thin deriveds
	// resolve the reactive store reads (filtered/selected sets, colorBy, theme),
	// the channel + ranking, and hand them to the builders. The presentational
	// <AtlasLegend/> renders the rows — it computes nothing.
	const visible = $derived(
		visibleIds(crossFilter.filteredIds, crossFilter.hasSelection ? crossFilter.selectedIds : null),
	);
	const legendFiltered = $derived(visible !== null);
	const clusterRows = $derived(
		pts?.cluster && clusterRanking
			? buildClusterLegend(pts.cluster, clusterRanking, visible, isDark)
			: [],
	);
	const clusterStatsValue = $derived(buildClusterStats(pts?.cluster, visible));
	const categoryChannel = $derived(channelFor(pts, crossFilter.colorBy));
	const categoryRows = $derived(
		buildCategoryLegend(categoryChannel, visible, isDark, MAX_DISTINCT),
	);
	const categoryTotal = $derived(categoryChannel?.labels.length ?? 0);
	const categoryTitleValue = $derived(deriveCategoryTitle(crossFilter.colorBy));

	// Colour modes: `cluster` (always), then each declared atlas channel present
	// in the points payload (by name, in descriptor order), then `none`. A channel
	// only appears once its column is built into /points, so the menu tracks data.
	const colorOptions = $derived.by((): (SelectOption & { value: ColorBy })[] => {
		const opts: (SelectOption & { value: ColorBy })[] = [{ value: 'cluster', label: 'Cluster' }];
		const p = pts;
		if (p) {
			for (const name of view.atlasChannels(crossFilter.space)) {
				if (p.channels[name]) opts.push({ value: name, label: channelLabel(name) });
			}
		}
		opts.push({ value: 'none', label: 'None' });
		return opts;
	});
	const colorValues = $derived(new Set(colorOptions.map((o) => o.value as string)));

	// Toolbar: the size/opacity sliders live in a ⚙ popover to keep the bar clean.
	let showSettings = $state(false);

	// Space tabs come from the descriptor's declared atlas spaces (DRY the
	// segmented toggle). The first declared space stays selectable; the rest are
	// gated until /status reports their projection built.
	const spaceTabs: { value: AtlasSpace; label: string; disabled: boolean; title: string }[] =
		$derived(
			view.atlasSpaces.map((s, i) => {
				const built = builtSpaces[s.name] ?? false;
				const disabled = i > 0 && !built;
				const label = channelLabel(s.name);
				return {
					value: s.name,
					label,
					disabled,
					title: disabled
						? `Not built yet — run \`ratch feature atlas --space ${s.name}\``
						: `${label} embedding map`,
				};
			}),
		);

	// ── selection helpers (data-space) ────────────────────────────────────────
	/** The identity key path for point `i`, in descriptor key order: the doc key
	 *  (resolved through the `doc` dictionary) then the remaining key fields from
	 *  `points.keys`. Feeds getAtlasChunk (route arity = identity key fields). */
	function keyAt(i: number): (string | number)[] | null {
		const p = pts;
		if (!p) return null;
		const path: (string | number)[] = [];
		for (const field of view.keyFields) {
			if (field === view.docKeyField) {
				const dc = p.doc[i];
				const doc = dc !== undefined ? p.docs[dc] : undefined;
				if (doc === undefined) return null;
				path.push(doc);
			} else {
				const v = p.keys[field]?.[i];
				if (v === undefined) return null;
				path.push(v);
			}
		}
		return path;
	}

	// Pure geometry/grid helpers live in ./atlas-geometry + ./atlas-grid; these
	// thin wrappers bind them to the component's currently-loaded points/grid.
	function indicesInLasso(poly: Pt[]): number[] {
		return x && y ? indicesInPolygon(x, y, poly) : [];
	}
	function pickIndex(qx: number, qy: number): number | null {
		return grid && x && y ? nearestIndex(grid, x, y, qx, qy) : null;
	}

	// ── load the table for a set of indices (1000-cap; full set stays in store) ─
	// Resolved by stable `_rowid` (one per point from /points) — the backend takes
	// exactly these rows, so even a max-size selection lists in a few hundred ms.
	async function loadTableForIndices(idx: number[]): Promise<void> {
		if (idx.length === 0) {
			onSelectionHits?.([], 0);
			return;
		}
		const rowids = pts?.rowid;
		if (!rowids) {
			// No per-point rowid (e.g. a stale cached /points) — we can't fetch the
			// rows. Report an EMPTY selection (total 0, not idx.length) so the page
			// falls back to its search hits / empty-state rather than showing an empty
			// table under a misleading "N chunks" header. A cache-busted /points
			// (api.ts) restores rowids; this is just the belt-and-braces guard.
			console.warn('atlas: /points payload has no rowid — cannot list selection (stale cache?)');
			crossFilter.clearSelection(); // also undims the map so state stays consistent
			onSelectionHits?.([], 0);
			return;
		}
		const ids = idx
			.slice(0, 1000)
			.map((i) => rowids[i])
			.filter((r): r is number => r !== undefined);
		tableLoading = true;
		try {
			const hits = await getAtlasChunks(ids);
			onSelectionHits?.(hits, idx.length);
		} catch (e) {
			console.error('atlas: failed to load selection', e);
			// Report total 0 (matching the no-rowid guard above) — never a phantom
			// "N chunks" header over an empty table.
			onSelectionHits?.([], 0);
		} finally {
			tableLoading = false;
		}
	}

	// ── GpuScatter callbacks ──────────────────────────────────────────────────
	// hover → nearest index → tooltip (anchored at cursor)
	function onScatterHover(dataX: number, dataY: number): void {
		const i = pickIndex(dataX, dataY);
		hoverIndex = i;
		if (i != null) {
			hoverX = lastPointerX;
			hoverY = lastPointerY;
		}
	}
	function onScatterHoverEnd(): void {
		hoverIndex = null;
	}

	// click a point → load full hit into the shared player
	async function onScatterPick(dataX: number, dataY: number): Promise<void> {
		const i = pickIndex(dataX, dataY);
		if (i == null) return;
		const key = keyAt(i);
		if (!key) return;
		try {
			active = await getAtlasChunk(key);
		} catch {
			/* keep previous */
		}
	}

	// lasso → FULL index selection (drives dimming) + capped table fetch
	async function onScatterLasso(polyDataXY: number[]): Promise<void> {
		const poly: Pt[] = [];
		for (let i = 0; i + 1 < polyDataXY.length; i += 2) {
			poly.push({ x: polyDataXY[i]!, y: polyDataXY[i + 1]! });
		}
		if (poly.length < 3) return;
		const idx = indicesInLasso(poly);
		crossFilter.setSelectedIndices(idx); // untruncated — drives the dim recolour
		await loadTableForIndices(idx);
	}

	// legend click → select an entire cluster / language (a first-class lasso)
	async function pickCluster(id: number): Promise<void> {
		const cl = pts?.cluster;
		if (!cl) return;
		crossFilter.selectCluster(id, cl);
		await loadTableForIndices([...crossFilter.selectedIds]);
	}

	/** Select every point in a category (language / topic / doc_topic) → table +
	 *  seedable search. The atlas analog of the Tree page's "show results". */
	async function pickCategory(code: number): Promise<void> {
		const ch = channelFor(pts, crossFilter.colorBy);
		if (!ch) return;
		const idx: number[] = [];
		for (let i = 0; i < ch.codes.length; i++) if (ch.codes[i] === code) idx.push(i);
		crossFilter.setSelectedIndices(idx);
		await loadTableForIndices(idx);
	}

	function clearSelection(): void {
		crossFilter.clearSelection();
		onSelectionHits?.([], 0);
	}

	function seedSearch(): void {
		const rowids = pts?.rowid;
		if (!rowids) return;
		const ids = [...crossFilter.selectedIds]
			.map((i) => rowids[i])
			.filter((r): r is number => r !== undefined);
		if (ids.length) onSeedSearch?.(ids);
	}

	// ── hover popover wiring ──────────────────────────────────────────────────
	// Plain (non-reactive) cursor coords: read synchronously when a hover lands,
	// never rendered directly — so a pointermove no longer triggers reactivity.
	let lastPointerX = 0;
	let lastPointerY = 0;
	let rafId = 0;
	function flushHover(): void {
		rafId = 0;
		if (hoverIndex != null) {
			hoverX = lastPointerX;
			hoverY = lastPointerY;
		}
	}
	function trackPointer(e: PointerEvent): void {
		const rect = mapEl?.getBoundingClientRect();
		if (!rect) return;
		lastPointerX = e.clientX - rect.left;
		lastPointerY = e.clientY - rect.top;
		if (hoverIndex != null && rafId === 0) {
			rafId = requestAnimationFrame(flushHover);
		}
	}
	$effect(() => () => {
		if (rafId !== 0) cancelAnimationFrame(rafId);
	});

	const legendMode = $derived(crossFilter.colorBy);
</script>

{#if !hasAtlas}
	<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-sm">
		This dataset has no embedding map.
	</div>
{:else if error}
	<div class="text-destructive grid h-full place-items-center p-6 text-center text-sm">
		Failed to load the embedding map: {error}
	</div>
{:else}
	<div
		bind:this={mapEl}
		bind:clientWidth={mapW}
		bind:clientHeight={mapH}
		onpointermove={trackPointer}
		role="presentation"
		class="bg-background relative h-full min-h-0"
	>
		{#if gpuReason}
			<!-- The reason belongs HERE, not only in <GpuScatter>: now that an unsupported browser never
			     fetches, `pts` stays null and the scatter never mounts, so its own message would never be
			     reached and the panel would spin for ever. Same wording, one source (`gpu-support.ts`). -->
			<div
				class="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
			>
				<span class="text-destructive">{gpuReason}</span>
				<span>The embedding map needs WebGPU. Search, Tree and Graph work without it.</span>
			</div>
		{:else if loading || !x || !y || !xBits || !yBits || !pts || !baseRgb || !pointState}
			<div class="text-muted-foreground flex h-full items-center justify-center gap-2 text-sm">
				<Loader2 class="size-4 animate-spin" /> Loading embedding map…
			</div>
		{:else}
			{#if mapW > 0 && mapH > 0}
				<GpuScatter
					{xBits}
					{yBits}
					{fitBounds}
					{baseRgb}
					state={pointState}
					{filterAlpha}
					noiseAlpha={NOISE_ALPHA}
					pointSize={autoPointSize}
					width={mapW}
					height={mapH}
					background={BG_COLOR}
					onHover={onScatterHover}
					onHoverEnd={onScatterHoverEnd}
					onPick={onScatterPick}
					onLasso={onScatterLasso}
					{mode}
					markerXY={activeMarkerXY}
				/>
			{/if}

			<AtlasTooltip {pts} index={hoverIndex} x={hoverX} y={hoverY} />

			<!-- toolbar -->
			<div
				class="border-border bg-card/85 absolute top-3 left-3 flex flex-wrap items-center gap-1.5 rounded-lg border p-1 text-xs shadow-sm backdrop-blur"
			>
				<span class="text-muted-foreground px-1.5">{(pts.count ?? 0).toLocaleString()} pts</span>

				<!-- projection space (segmented) -->
				<div class="border-border flex items-center gap-0.5 rounded-lg border p-0.5">
					{#each spaceTabs as t (t.value)}
						<Button
							variant={crossFilter.space === t.value ? 'secondary' : 'ghost'}
							size="xs"
							class={crossFilter.space === t.value ? '' : 'text-muted-foreground'}
							aria-pressed={crossFilter.space === t.value}
							disabled={t.disabled}
							title={t.title}
							onclick={() => switchSpace(t.value)}
						>
							{t.label}
						</Button>
					{/each}
				</div>

				<!-- tool: lasso / pan (segmented, icon-only) -->
				<div class="border-border flex items-center gap-0.5 rounded-lg border p-0.5">
					<Button
						variant={mode === 'lasso' ? 'secondary' : 'ghost'}
						size="icon-xs"
						title="Lasso — drag to select a region"
						aria-label="Lasso select"
						aria-pressed={mode === 'lasso'}
						onclick={() => (mode = 'lasso')}
					>
						<Lasso />
					</Button>
					<Button
						variant={mode === 'pan' ? 'secondary' : 'ghost'}
						size="icon-xs"
						title="Pan — drag to move (or shift / middle-drag in any mode; scroll to zoom)"
						aria-label="Pan"
						aria-pressed={mode === 'pan'}
						onclick={() => (mode = 'pan')}
					>
						<Hand />
					</Button>
				</div>

				<!-- colour by: a function binding straight to the singleton — the design-system
             Select binds a plain string, so the setter narrows it to the store's
             ColorBy union at the boundary (no shadow $state to clobber the
             persisted value on remount). -->
				<Select
					bind:value={
	() => crossFilter.colorBy,
	(v) => {
		if (colorValues.has(v)) crossFilter.colorBy = v;
	}
}
					options={colorOptions}
					ariaLabel="Colour points by"
				/>

				<!-- display settings (point size, filtered opacity) -->
				<div class="relative">
					<Button
						variant={showSettings ? 'secondary' : 'ghost'}
						size="icon-sm"
						title="Display settings"
						aria-label="Display settings"
						aria-pressed={showSettings}
						onclick={() => (showSettings = !showSettings)}
					>
						<Settings2 />
					</Button>
					{#if showSettings}
						<button
							type="button"
							aria-label="Close settings"
							class="fixed inset-0 z-10 cursor-default"
							onclick={() => (showSettings = false)}
						></button>
						<div
							class="border-border bg-popover text-popover-foreground absolute top-full left-0 z-20 mt-1 flex w-56 flex-col gap-3 rounded-lg border p-3 text-xs shadow-md"
						>
							<label class="block">
								<span class="text-muted-foreground mb-1 flex items-center justify-between">
									Point size <span class="font-mono">{pointSize === 0 ? 'auto' : pointSize}</span>
								</span>
								<input
									type="range"
									min="0"
									max="20"
									step="0.5"
									bind:value={pointSize}
									class="accent-primary w-full"
								/>
							</label>
							<label class="block">
								<span class="text-muted-foreground mb-1 flex items-center justify-between">
									Dimmed opacity <span class="font-mono">{filterAlpha}</span>
								</span>
								<input
									type="range"
									min="0"
									max="120"
									step="1"
									bind:value={filterAlpha}
									class="accent-primary w-full"
								/>
								<span class="text-muted-foreground/70 mt-0.5 block text-[0.7rem]">
									How visible excluded points are — search-filtered or not in the lasso/cluster selection
									(left = hidden).
								</span>
							</label>
						</div>
					{/if}
				</div>
			</div>

			<!-- legend / distribution (clickable → select) -->
			<AtlasLegend
				{legendMode}
				{clusterRows}
				{categoryRows}
				clusterStats={clusterStatsValue}
				categoryTitle={categoryTitleValue}
				{categoryTotal}
				{legendFiltered}
				hidden={crossFilter.hidden}
				{isDark}
				onPickCluster={pickCluster}
				onPickCategory={pickCategory}
				onToggleHidden={(code) => crossFilter.toggleHidden(code)}
				onShowAll={() => crossFilter.showAll()}
			/>

			<!-- selection status + actions -->
			<div
				class="border-border bg-card/85 absolute bottom-3 left-3 flex items-center gap-2 rounded-lg border p-1 pl-2 text-xs shadow-sm backdrop-blur"
			>
				{#if tableLoading}
					<Loader2 class="text-muted-foreground size-3.5 animate-spin" />
				{/if}
				{#if selectionCount > 0}
					<span class="text-foreground">{selectionCount.toLocaleString()} selected</span>
					{#if selectionCount > 1000}
						<span class="text-muted-foreground/70">· table shows 1000</span>
					{/if}
					<Button
						variant="outline"
						size="xs"
						onclick={seedSearch}
						title="Open this map selection as the full results list (paging, rerank, table/grid views)"
					>
						Show as results
					</Button>
					<Button
						variant="ghost"
						size="icon-xs"
						onclick={clearSelection}
						title="Clear selection"
						aria-label="Clear selection"
					>
						<X />
					</Button>
				{:else}
					<Lasso class="text-muted-foreground size-3.5" />
					<span class="text-muted-foreground"
						>drag to lasso · click a legend to select · click a point to play · scroll to zoom</span
					>
				{/if}
			</div>
		{/if}
	</div>
{/if}
