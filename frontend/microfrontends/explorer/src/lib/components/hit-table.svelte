<script lang="ts" module>
	import type { Hit } from '@rask/explorer-api';
	import { relevanceOf, isVoiceHit } from '@rask/explorer-api';
	import { activeView, type DatasetView } from '@rask/explorer-api/descriptor';
	import { fmtTime } from '$lib/utils';

	/** A table column. `render` gives the displayed string. Set `numeric` for
	 *  columns that sort/filter on a number — then `sortValue` supplies the raw
	 *  comparable value (e.g. seconds, not the `H:MM:SS` label), and the filter
	 *  row offers a "min ≥" input instead of a text-contains box. `thumbnail` is
	 *  data-less so it's neither sortable nor filterable. */
	export type TableColumn = {
		key: string;
		label: string;
		render: (h: Hit) => string;
		numeric?: boolean;
		/** Comparable value for sort/filter; defaults to `render` for string cols.
		 *  Returns null when the hit has no value (sorted/filtered as absent). */
		sortValue?: (h: Hit) => number | null;
	};

	const asStr = (v: unknown): string | null => (v === null || v === undefined ? null : String(v));
	const asNum = (v: unknown): number | null =>
		typeof v === 'number' && Number.isFinite(v) ? v : null;

	/** Title-case a raw identity field name for a header (drops a trailing `id`,
	 *  so `doc_id` → "Doc", `chunk_id` → "Chunk"). */
	function humanize(field: string): string {
		const parts = field.split(/[_\s]+/).filter((p) => p && p.toLowerCase() !== 'id');
		const base = parts.length ? parts.join(' ') : field;
		return base.charAt(0).toUpperCase() + base.slice(1);
	}

	/** Numeric per the discovered Arrow type — mirrors the backend's column-kind
	 *  test so a column sorts/filters the same way `/api/columns` classifies it. */
	function isNumericArrow(t: string | null): boolean {
		if (!t) return false;
		return (
			t.startsWith('int') ||
			t.startsWith('uint') ||
			t === 'float' ||
			t === 'double' ||
			t === 'halffloat'
		);
	}

	/** Discovered Arrow type of a field — the search row table first, then any. */
	function arrowTypeOf(view: DatasetView, field: string): string | null {
		const tables = view.descriptor.tables;
		const rowTable = view.descriptor.declared.search?.row_table;
		const ordered =
			rowTable && tables[rowTable]
				? [tables[rowTable], ...Object.values(tables)]
				: Object.values(tables);
		for (const t of ordered) {
			const col = t.columns.find((c) => c.name === field);
			if (col) return col.arrow_type;
		}
		return null;
	}

	/** A generic scalar column reading `field` off the row. Numeric sort/filter
	 *  is driven by the field's discovered Arrow type, so it behaves like the
	 *  underlying value without naming any corpus column here. */
	function valueColumn(view: DatasetView, field: string, label: string): TableColumn {
		const numeric = isNumericArrow(arrowTypeOf(view, field));
		const col: TableColumn = { key: field, label, numeric, render: (h) => asStr(h[field]) ?? '' };
		if (numeric) col.sortValue = (h) => asNum(h[field]);
		return col;
	}

	/** The table column set, rebuilt from the active dataset descriptor: envelope
	 *  columns (play / thumbnail / relevance / speaker) + identity key fields +
	 *  time (start/end) + duration + declared metadata + body + caption. Called
	 *  from render (never at import) so the active view is always loaded. `play`,
	 *  `thumbnail` and `text` are rendered specially in the markup (audio-preview
	 *  button / image / highlighted HTML); their `render` output is unused there. */
	export function TABLE_COLUMNS(): TableColumn[] {
		const view = activeView();
		const cols: TableColumn[] = [
			// Per-row audio preview (the shared one-element audioPreview store).
			{ key: 'play', label: 'Play', render: () => '' },
			{ key: 'thumbnail', label: 'Thumb', render: () => '' },
			{
				// Mode-agnostic relevance (higher = better); blank for unranked hits.
				key: 'score',
				label: 'Relevance',
				numeric: true,
				sortValue: (h) => relevanceOf(h),
				render: (h) => {
					const r = relevanceOf(h);
					return r != null ? r.toFixed(3) : '';
				},
			},
			{
				// Voice-search results only (blank for text hits): the matched diarized
				// turn — per-video speaker label plus the turn's time span.
				key: 'speaker',
				label: 'Speaker',
				render: (h) =>
					isVoiceHit(h)
						? `${h.speaker_label} · ${fmtTime(h.turn_start)}–${fmtTime(h.turn_end)}`
						: '',
			},
		];

		// Identity key fields (doc key + any per-row keys) as their own columns.
		for (const field of view.keyFields) cols.push(valueColumn(view, field, humanize(field)));

		// Time span + duration, when the dataset declares a time binding.
		if (view.hasTime) {
			cols.push({
				key: 'start',
				label: 'Start',
				numeric: true,
				sortValue: (h) => view.time(h)?.start ?? null,
				render: (h) => {
					const t = view.time(h);
					return t ? fmtTime(t.start) : '';
				},
			});
			cols.push({
				key: 'end',
				label: 'End',
				numeric: true,
				sortValue: (h) => view.time(h)?.end ?? null,
				render: (h) => {
					const t = view.time(h);
					return t ? fmtTime(t.end) : '';
				},
			});
			cols.push({
				key: 'duration',
				label: 'Dur',
				numeric: true,
				sortValue: (h) => view.duration(h),
				render: (h) => {
					const d = view.duration(h);
					return d != null ? fmtTime(d) : '';
				},
			});
		}

		// Declared metadata fields (name / ref / language / … — descriptor-driven).
		const keyFields = new Set(view.keyFields);
		for (const { field, label } of view.metadataFields) {
			if (keyFields.has(field)) continue; // already shown as an identity column
			cols.push(valueColumn(view, field, label));
		}

		// Body + caption (rendered specially in the markup, keyed by their role).
		if (view.bodyField) cols.push({ key: 'text', label: 'Text', render: (h) => view.body(h) });
		if (view.captionField)
			cols.push({ key: 'caption', label: 'Caption', render: (h) => view.caption(h) ?? '' });

		return cols;
	}

	/** A column's comparable value: the numeric `sortValue` for numeric columns,
	 *  else the lowercased `render` string. Used by both sort and filter so they
	 *  agree on what each cell "is". */
	function cellValue(col: TableColumn, h: Hit): number | string | null {
		if (col.numeric) return col.sortValue ? col.sortValue(h) : null;
		return col.render(h).toLowerCase();
	}
</script>

<script lang="ts">
	import { thumbnailUrl } from '@rask/explorer-api';
	import { queryTerms, makeHighlighter, hitKey } from '$lib/utils';
	import { audioPreview } from '$lib/audio-preview.svelte';
	import { ChevronDown, ChevronUp, Play, Pause } from '@lucide/svelte';

	let {
		hits,
		active,
		onselect,
		visible,
		query = '',
		widths = {},
		wrap = false,
		onwidthchange,
	}: {
		hits: Hit[];
		active: Hit | null;
		onselect?: (h: Hit) => void;
		visible: string[];
		query?: string;
		/** Dragged column widths (px) by column key — a dragged width wins over
		 *  the default (auto) sizing; absent key = auto. */
		widths?: Record<string, number>;
		/** Wrap the free-text columns (body / caption / title) instead of truncating. */
		wrap?: boolean;
		/** A resize-handle drag ended (px) or was double-clicked (null = reset
		 *  to auto) — the parent persists alongside the column-visibility prefs. */
		onwidthchange?: (key: string, width: number | null) => void;
	} = $props();

	// The active dataset view — media URLs, body/caption text, time + identity all
	// read through it (safe: this component only renders once the layout has the
	// descriptor). Columns are rebuilt from the same view.
	const view = activeView();
	const allColumns = $derived(TABLE_COLUMNS());
	const cols = $derived(allColumns.filter((c) => visible.includes(c.key)));
	const terms = $derived(queryTerms(query));
	const highlight = $derived(makeHighlighter(terms));
	const activeKey = $derived(active ? hitKey(active) : null);

	// Header row height (px) — the filter row sticks directly beneath it. Matches
	// the header `<th>` padding (py-2 = 8px ×2) plus the text-xs line box (~16px).
	const HEADER_ROW_HEIGHT_PX = 32;

	// ── Client-side sort + filter (this component only; `hits` is never mutated) ──
	// The play + thumbnail columns are data-less, so they're excluded from the
	// sort/filter UI (and from the filter row's inputs).
	const DATALESS_KEYS = ['play', 'thumbnail'];
	const SORTABLE = (c: TableColumn): boolean => !DATALESS_KEYS.includes(c.key);

	// Columns the "Wrap text" pref applies to (long free-text values): the body,
	// the caption, and the declared title field(s). Descriptor-driven, so it holds
	// for any dataset instead of a fixed corpus list.
	const wrapKeys = $derived.by((): Set<string> => {
		const keys = new Set<string>();
		if (view.bodyField) keys.add('text');
		if (view.captionField) keys.add('caption');
		for (const f of view.descriptor.declared.display.title) keys.add(f);
		return keys;
	});

	// ── Column resize (pointer-drag on the header-edge handles) ──
	// Drag geometry caches at pointerdown and width writes coalesce to one per
	// animation frame (pointermove outpaces frames on high-Hz mice, and every
	// write re-lays-out the whole table) — same pattern as resizable-split.
	const MIN_COL_PX = 60;
	/** Live width of the column being dragged (wins over `widths` so the column
	 *  tracks the pointer without a persist per move). */
	let drag = $state<{ key: string; width: number } | null>(null);
	let dragStartX = 0;
	let dragStartW = 0;
	let dragPendingX = 0;
	let dragRafId = 0;

	const clampWidth = (w: number): number => Math.max(MIN_COL_PX, Math.round(w));

	/** Effective width for a column: in-flight drag → persisted dragged width
	 *  → undefined (default auto sizing). Persisted values re-clamp here so a
	 *  stale/tampered stored width below the minimum can't render a sliver. */
	const colWidth = (key: string): number | undefined => {
		if (drag?.key === key) return drag.width;
		const w = widths[key];
		return w !== undefined ? clampWidth(w) : undefined;
	};

	function onHandleDown(e: PointerEvent & { currentTarget: HTMLElement }, key: string): void {
		const handle = e.currentTarget;
		const th = handle.closest('th');
		if (!th) return;
		e.preventDefault(); // no text selection mid-drag
		e.stopPropagation(); // a resize must never read as a header click-to-sort
		dragStartX = e.clientX;
		dragPendingX = e.clientX;
		dragStartW = th.getBoundingClientRect().width;
		drag = { key, width: clampWidth(dragStartW) };
		handle.setPointerCapture(e.pointerId);
	}

	function onHandleMove(e: PointerEvent): void {
		if (!drag) return;
		dragPendingX = e.clientX;
		if (dragRafId) return; // a frame is already scheduled — just retarget it
		dragRafId = requestAnimationFrame(() => {
			dragRafId = 0;
			if (!drag) return; // drag ended before the frame fired
			drag = { key: drag.key, width: clampWidth(dragStartW + dragPendingX - dragStartX) };
		});
	}

	function onHandleUp(e: PointerEvent & { currentTarget: HTMLElement }): void {
		if (!drag) return;
		if (dragRafId) {
			cancelAnimationFrame(dragRafId);
			dragRafId = 0;
		}
		const handle = e.currentTarget;
		if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
		// Settle at the last tracked pointer position (NOT e.clientX — pointercancel
		// routes here too and may carry zeroed coordinates), then hand off.
		onwidthchange?.(drag.key, clampWidth(dragStartW + dragPendingX - dragStartX));
		drag = null;
	}

	/** Double-click a handle → back to automatic sizing for that column. */
	function onHandleReset(e: MouseEvent, key: string): void {
		e.preventDefault();
		e.stopPropagation();
		onwidthchange?.(key, null);
	}

	type SortDir = 'asc' | 'desc';
	let sortKey = $state<string | null>(null);
	let sortDir = $state<SortDir>('desc');
	// Filter text per column key: a min-number string for numeric columns,
	// a contains-substring for the rest. Empty/whitespace = no filter.
	const filters = $state<Record<string, string>>({});

	function toggleSort(c: TableColumn): void {
		if (!SORTABLE(c)) return;
		if (sortKey === c.key) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sortKey = c.key;
			// Numeric columns (relevance, time) read most usefully high-first.
			sortDir = 'desc';
		}
	}

	/** Does hit `h` pass the active filter for column `c`? Numeric columns keep
	 *  rows whose value ≥ the typed minimum; string columns keep substring
	 *  matches (case-insensitive). A non-numeric entry in a numeric box is
	 *  ignored (treated as no filter). */
	function passesFilter(c: TableColumn, h: Hit): boolean {
		const raw = filters[c.key]?.trim();
		if (!raw) return true;
		const value = cellValue(c, h);
		if (c.numeric) {
			const min = Number(raw);
			if (Number.isNaN(min)) return true;
			return typeof value === 'number' && value >= min;
		}
		return typeof value === 'string' && value.includes(raw.toLowerCase());
	}

	// Filtered then sorted, derived from a COPY so the `hits` prop stays intact
	// (selection keys by hitKey, so it survives any reorder).
	const filteredHits = $derived(hits.filter((h) => cols.every((c) => passesFilter(c, h))));

	const displayedHits = $derived.by(() => {
		const key = sortKey;
		if (key === null) return filteredHits;
		const col = allColumns.find((c) => c.key === key);
		if (!col) return filteredHits;
		const dir = sortDir === 'asc' ? 1 : -1;
		// Sort a shallow copy — never the source array.
		return [...filteredHits].sort((a, b) => compareCells(col, a, b, dir));
	});

	/** Compare two hits on column `col`. `dir` (1 = asc, -1 = desc) applies only
	 *  to present values; nulls always sort last regardless of direction (the
	 *  sign must not flip them to the top on a descending sort). */
	function compareCells(col: TableColumn, a: Hit, b: Hit, dir: number): number {
		const va = cellValue(col, a);
		const vb = cellValue(col, b);
		if (va === null && vb === null) return 0;
		if (va === null) return 1;
		if (vb === null) return -1;
		const base =
			typeof va === 'number' && typeof vb === 'number'
				? va - vb
				: String(va).localeCompare(String(vb));
		return base * dir;
	}
</script>

<div class="overflow-x-auto">
	<table class="w-full border-collapse text-xs">
		<thead>
			<tr class="border-border bg-card text-muted-foreground sticky top-0 z-10 border-b text-left">
				{#each cols as c (c.key)}
					{@const w = colWidth(c.key)}
					<th
						class="relative overflow-hidden px-3 py-2 font-medium whitespace-nowrap"
						style={w !== undefined ? `width:${w}px;min-width:${w}px;max-width:${w}px` : undefined}
					>
						{#if SORTABLE(c)}
							<button
								type="button"
								class="hover:text-foreground flex items-center gap-1"
								onclick={() => toggleSort(c)}
								title="Sort by {c.label}"
							>
								<span>{c.label}</span>
								{#if sortKey === c.key}
									{#if sortDir === 'asc'}
										<ChevronUp class="text-primary size-3" />
									{:else}
										<ChevronDown class="text-primary size-3" />
									{/if}
								{/if}
							</button>
						{:else}
							<span>{c.label}</span>
						{/if}
						<!-- Resize handle on the column's right edge: drag sets the width,
                 double-click resets to auto. Not in the tab order (pointer-only
                 affordance — the dragged width persists with the column prefs). -->
						<button
							type="button"
							tabindex={-1}
							aria-label="Resize {c.label} column"
							title="Drag to set column width · double-click to reset"
							onpointerdown={(e) => onHandleDown(e, c.key)}
							onpointermove={onHandleMove}
							onpointerup={onHandleUp}
							onpointercancel={onHandleUp}
							ondblclick={(e) => onHandleReset(e, c.key)}
							class={'hover:bg-primary/50 absolute top-0 right-0 z-[1] h-full w-1.5 cursor-col-resize transition-colors ' +
	(drag?.key === c.key ? 'bg-primary/60' : 'bg-transparent')}
						></button>
					</th>
				{/each}
			</tr>
			<!-- Compact per-column filter row: numeric "min ≥" or text-contains. It
           sticks directly beneath the header row (offset = header height). -->
			<tr
				class="border-border bg-card/95 sticky z-10 border-b"
				style="top: {HEADER_ROW_HEIGHT_PX}px"
			>
				{#each cols as c (c.key)}
					<th class="px-2 py-1 font-normal">
						{#if SORTABLE(c)}
							<input
								type="text"
								inputmode={c.numeric ? 'decimal' : 'text'}
								bind:value={filters[c.key]}
								placeholder={c.numeric ? 'min ≥' : 'filter'}
								aria-label={c.numeric ? `Minimum ${c.label}` : `Filter ${c.label}`}
								class="border-input dark:bg-input/30 text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 w-full rounded-md border bg-transparent px-1.5 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
							/>
						{/if}
					</th>
				{/each}
			</tr>
		</thead>
		<tbody>
			{#each displayedHits as hit (hitKey(hit))}
				<tr
					class={'border-border/60 hover:bg-muted/60 cursor-pointer border-b ' +
	(activeKey === hitKey(hit)
		? 'bg-primary/15 font-medium [box-shadow:inset_3px_0_0_0_var(--color-primary)]'
		: '')}
					onclick={() => onselect?.(hit)}
				>
					{#each cols as c (c.key)}
						{@const w = colWidth(c.key)}
						{#if c.key === 'play'}
							{@const rowKey = hitKey(hit)}
							{@const playing = audioPreview.isPlaying(rowKey)}
							{@const failed = audioPreview.isFailed(view.docId(hit))}
							<!-- Voice hits preview the matched diarized TURN (the audio that
                   actually matched), not the max-overlap ASR chunk span. -->
							{@const clipStart = isVoiceHit(hit) ? hit.turn_start : (view.time(hit)?.start ?? 0)}
							{@const clipEnd = isVoiceHit(hit) ? hit.turn_end : (view.time(hit)?.end ?? 0)}
							<td class="px-2 py-1 align-top">
								<!-- One shared <audio> behind every button (audioPreview):
                     playing a row pauses any other, clicking again pauses. -->
								<button
									type="button"
									disabled={failed}
									title={failed
	? 'Audio unavailable — media failed to load'
	: playing
		? 'Pause'
		: `Play ${fmtTime(clipStart)}–${fmtTime(clipEnd)}`}
									aria-label={playing ? 'Pause clip' : 'Play clip'}
									aria-pressed={playing}
									onclick={(e) => {
	e.stopPropagation(); // don't also select the row
	audioPreview.toggle({
		key: rowKey,
		docId: view.docId(hit),
		start: clipStart,
		end: clipEnd,
	});
}}
									class={'enabled:hover:bg-muted enabled:hover:text-foreground inline-flex size-6 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-40 ' +
	(playing ? 'text-primary' : 'text-muted-foreground')}
								>
									{#if playing}
										<Pause class="size-3.5" />
									{:else}
										<Play class="size-3.5" />
									{/if}
								</button>
							</td>
						{:else if c.key === 'thumbnail'}
							<td class="px-3 py-1.5 align-top">
								<img
									src={thumbnailUrl(hit)}
									loading="lazy"
									alt=""
									class="bg-muted h-9 w-16 rounded-md object-cover"
									onerror={(e) =>
	((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')}
								/>
							</td>
						{:else if c.key === 'text'}
							{@const body = view.body(hit)}
							<td
								class="text-foreground max-w-[32rem] px-3 py-1.5 align-top [overflow-wrap:anywhere]"
								style={w !== undefined ? `max-width:${w}px` : undefined}
								title={body}
							>
								<!-- highlight() escapes then wraps matches — safe to inject -->
								<!-- eslint-disable-next-line svelte/no-at-html-tags -->
								<div class={wrap ? '' : 'line-clamp-2'}>{@html highlight(body)}</div>
							</td>
						{:else}
							{@const wraps = wrap && wrapKeys.has(c.key)}
							<td
								class={'text-muted-foreground max-w-[28rem] px-3 py-1.5 align-top ' +
	(wraps ? 'break-words whitespace-normal' : 'truncate whitespace-nowrap')}
								style={w !== undefined ? `max-width:${w}px` : undefined}
								title={c.render(hit)}
							>
								{c.render(hit)}
							</td>
						{/if}
					{/each}
				</tr>
			{/each}
		</tbody>
	</table>
</div>
